#!/usr/bin/env python3
"""Run the Firecracker snapshot experiment and export the results.

Phases:
  1. Run pytest via devtool for each snapshot mode (unless --skip-run).
  2. Copy timeseries CSVs into <bench-dir>/results/timeseries/.
  3. Emit a JSON index to
     <bench-dir>/results/snapshot_benchmark_<workload>.json.

Usage:
    # Full run (~2.5 h for 3 modes × 3 mem sizes × 3 iterations)
    python3 run_snapshot_benchmark.py \\
        --workload redis_light \\
        --mem-sizes 2048 4096 8192 \\
        --iterations 3 \\
        --rootfs /srv/test_artifacts/ubuntu-24.04-app.ext4 \\
        --bench-dir /path/to/results/repo

    # Re-export only (use existing test_results/, skip pytest)
    python3 run_snapshot_benchmark.py \\
        --workload redis_light \\
        --skip-run
"""

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys

# ---------------------------------------------------------------------------
# Paths relative to this script's location (repo root / tests/...)
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT   = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", ".."))
_RESULTS_CSV = os.path.join(_REPO_ROOT, "test_results", "experiment_results.csv")
_TS_DIR      = os.path.join(_REPO_ROOT, "test_results", "timeseries")
_TEST_MODULE = "integration_tests/functional/test_snapshot_live_experiment.py"

_MODE_TO_TEST = {
    "live":     "test_live_snapshot_app_experiment",
    "live_bpf": "test_live_bpf_snapshot_app_experiment",
    "full":     "test_full_snapshot_app_experiment",
}

_DEFAULT_MEM_SIZES = [2048, 4096, 8192]
_DEFAULT_ROOTFS    = "/srv/test_artifacts/ubuntu-24.04-app.ext4"


# ---------------------------------------------------------------------------
# Phase 1 — run experiment
# ---------------------------------------------------------------------------

def _existing_configs(workload: str) -> set:
    """(mode, mem, iteration) combinations already in the CSV."""
    configs = set()
    if not os.path.exists(_RESULTS_CSV):
        return configs
    with open(_RESULTS_CSV, newline="") as f:
        for row in csv.DictReader(f):
            if row.get("workload") != workload:
                continue
            # A row whose timeseries file is gone is incomplete data:
            # treat it as missing so the configuration reruns.
            ts_rel = row.get("timeseries_file") or ""
            if ts_rel and not os.path.exists(
                    os.path.join(_REPO_ROOT, "test_results", ts_rel)):
                continue
            try:
                configs.add((row.get("snapshot_mode"),
                             int(row["mem_size_mib"]),
                             int(row.get("iteration") or 0)))
            except (KeyError, ValueError, TypeError):
                continue
    return configs


def run_experiment(workload: str, mem_sizes: list[int], iterations: int,
                   rootfs: str, reuse: bool = True):
    """Run devtool test for each snapshot mode, filtering by workload.

    With reuse (the default), configurations already present in the CSV
    are skipped: complete modes never launch the test container, and
    partial modes skip per-configuration inside pytest (EXPERIMENT_REUSE
    is forwarded into the container by devtool)."""
    mem_filter = " or ".join(str(m) for m in mem_sizes)
    env = {
        **os.environ,
        "EXPERIMENT_ROOTFS": rootfs,
        "APP_ITERATIONS":    str(iterations),
        "EXPERIMENT_REUSE":  "1" if reuse else "0",
    }
    existing = _existing_configs(workload) if reuse else set()

    for mode, test_fn in _MODE_TO_TEST.items():
        grid = [(mem, it) for mem in mem_sizes for it in range(iterations)]
        if all((mode, mem, it) in existing for mem, it in grid):
            for mem, it in grid:
                print(f"Skipping {workload} {mode} mem={mem} "
                      f"iteration={it} (already in results)", flush=True)
            continue
        # Pin the PCI fixture parametrization to one variant: the
        # experiment VMs are built fresh from EXPERIMENT_ROOTFS without a
        # pci argument, so PCI_ON and PCI_OFF run identical VMs and the
        # CSV export would keep only the later of the two anyway.
        k_filter = f"({test_fn}) and ({workload}) and ({mem_filter}) and PCI_OFF"
        cmd = [
            "./tools/devtool", "-y", "test", "--",
            "-k", k_filter,
            _TEST_MODULE,
            "-s", "--log-cli-level=INFO", "-m", "",
        ]
        print(f"\n{'='*70}")
        print(f"Running mode={mode}  workload={workload}  mem={mem_sizes}")
        print(f"  {' '.join(cmd)}")
        print(f"{'='*70}")
        subprocess.run(cmd, env=env, cwd=_REPO_ROOT, check=True)


# ---------------------------------------------------------------------------
# Phase 2+3 — read CSV, copy timeseries, emit JSON
# ---------------------------------------------------------------------------

def _flt(val, default=0.0):
    """Parse a CSV field to float, returning default on empty/None."""
    try:
        return float(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _int_or(val, default=0):
    try:
        return int(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _compute_window_stats(ts_path, t_start, t_end):
    """Compute throughput/latency stats for non-failed samples in [t_start, t_end].

    Reads the timeseries CSV produced by the experiment and aggregates samples
    whose ``t_rel_s`` falls within the requested window.  Latency values in the
    CSV are in milliseconds; the returned dict converts them to microseconds for
    consistency with the rest of the JSON.

    Returns all-zeros (sample_count=0) when the file is missing, the window
    contains no non-failed samples (e.g. full-snapshot VM pause), or the
    time range is invalid.

    Sanity invariant: ``max_avg_latency_us`` should be ≥ the freeze duration
    for any request that was in-flight during the pause.
    """
    empty = {
        "throughput_ops_s":   0.0,
        "avg_latency_us":     0.0,
        "p99_us":             0.0,
        "p999_us":            0.0,
        "max_avg_latency_us": 0.0,
        "max_p999_us":        0.0,
        "sample_count":       0,
    }
    if not ts_path or not os.path.exists(ts_path) or t_end <= t_start:
        return empty

    rows = []
    try:
        with open(ts_path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    t = float(row["t_rel_s"])
                    if t < t_start or t > t_end:
                        continue
                    if int(row.get("failed", "0") or 0):
                        continue
                    rows.append({
                        "thr":  float(row["throughput"]),
                        "avg":  float(row.get("avg_ms",  0) or 0),
                        "p99":  float(row.get("p99_ms",  0) or 0),
                        "p999": float(row.get("p999_ms", 0) or 0),
                    })
                except (KeyError, ValueError):
                    pass
    except OSError:
        return empty

    if not rows:
        return empty

    thr  = [r["thr"]  for r in rows]
    avg  = [r["avg"]  for r in rows]
    p99  = [r["p99"]  for r in rows]
    p999 = [r["p999"] for r in rows]
    n    = len(rows)

    return {
        "throughput_ops_s":   sum(thr)  / n,
        "avg_latency_us":     sum(avg)  / n * 1000,
        "p99_us":             sum(p99)  / n * 1000,
        "p999_us":            sum(p999) / n * 1000,
        "max_avg_latency_us": max(avg)      * 1000,
        "max_p999_us":        max(p999)     * 1000,
        "sample_count":       n,
    }


def export_results(workload: str, mem_sizes: list[int], bench_dir: str,
                   max_iteration: int = 2):
    """Read experiment_results.csv, copy CSVs, write JSON."""
    results_dir = os.path.join(bench_dir, "results")
    ts_dest_dir = os.path.join(results_dir, "timeseries")
    os.makedirs(ts_dest_dir, exist_ok=True)

    if not os.path.exists(_RESULTS_CSV):
        print(f"ERROR: {_RESULTS_CSV} not found — run the experiment first.", file=sys.stderr)
        sys.exit(1)

    with open(_RESULTS_CSV, newline="") as f:
        rows = list(csv.DictReader(f))

    # Deduplicate: keep only the most recent row per (mode, mem_size, iteration).
    # The CSV accumulates across runs; sort by timestamp descending and take the
    # first occurrence of each key.
    seen: dict[tuple, dict] = {}
    for row in sorted(rows, key=lambda r: r.get("timestamp", ""), reverse=True):
        if row.get("workload") != workload:
            continue
        try:
            mem = int(row["mem_size_mib"])
        except (ValueError, KeyError):
            continue
        if mem not in mem_sizes:
            continue
        mode      = row.get("snapshot_mode", "")
        iteration = _int_or(row.get("iteration", "0"))
        if iteration > max_iteration:
            continue
        key = (mode, mem, iteration)
        if key not in seen:
            seen[key] = row

    expected = {(mode, mem, it) for mode in _MODE_TO_TEST
                for mem in mem_sizes for it in range(max_iteration + 1)}
    missing = sorted(expected - set(seen.keys()))
    if missing:
        print(f"ERROR: {len(missing)} configuration(s) missing from "
              f"{_RESULTS_CSV} for workload {workload}:", file=sys.stderr)
        for mode, mem, it in missing:
            print(f"  {mode} mem={mem} iteration={it}", file=sys.stderr)
        sys.exit(1)

    records = []
    for row in seen.values():
        mode      = row.get("snapshot_mode", "")
        mem       = int(row["mem_size_mib"])
        iteration = _int_or(row.get("iteration", "0"))

        # ── timing ──────────────────────────────────────────────────────
        if mode == "full":
            total_ms   = _flt(row.get("full_total_ms"))
            downtime_ms = total_ms
            phases = {
                "create": total_ms * 1000,   # µs for consistency with live phases
            }
        else:
            total_ms    = _flt(row.get("total_us")) / 1000.0
            downtime_ms = _flt(row.get("freeze_us")) / 1000.0
            phases = {
                "phase1":         _flt(row.get("phase1_us")),
                "populate_pages": _flt(row.get("populate_pages_us")),
                "freeze":         _flt(row.get("freeze_us")),
                "stream":         _flt(row.get("stream_us")),
                "finalize":       _flt(row.get("finalize_us")),
            }

        # ── throughput / latency ─────────────────────────────────────────
        is_stream = workload == "stream"
        if is_stream:
            throughput = {
                "baseline_triad_mibs": _flt(row.get("stream_baseline_triad_mibs")),
                "during_triad_mibs":   _flt(row.get("stream_during_triad_mibs")),
                "post_triad_mibs":     _flt(row.get("stream_post_triad_mibs")),
                "baseline_copy_mibs":  _flt(row.get("stream_baseline_copy_mibs")),
                "during_copy_mibs":    _flt(row.get("stream_during_copy_mibs")),
            }
            latency_us = {}
            overall = {}
        else:
            throughput = {
                "baseline_ops_s": _flt(row.get("app_baseline_ops")),
                "during_ops_s":   _flt(row.get("app_during_ops")),
                "post_ops_s":     _flt(row.get("post_snap_ops")),
            }
            latency_us = {
                "baseline_avg":  _flt(row.get("app_baseline_avg_us")),
                "baseline_p99":  _flt(row.get("app_baseline_p99_us")),
                "during_avg":    _flt(row.get("app_during_avg_us")),
                "during_p99":    _flt(row.get("app_during_p99_us")),
                "during_p999":   _flt(row.get("app_during_p999_us")),
                "post_avg":      _flt(row.get("post_snap_avg_us")),
                "post_p99":      _flt(row.get("post_snap_p99_us")),
            }
            overall = {
                "ops_mean":       _flt(row.get("overall_ops_mean")),
                "ops_stddev":     _flt(row.get("overall_ops_stddev")),
                "avg_latency_us": _flt(row.get("overall_avg_latency_us_mean")),
                "p99_us_mean":    _flt(row.get("overall_p99_us_mean")),
                "p99_us_stddev":  _flt(row.get("overall_p99_us_stddev")),
            }

        # ── timeseries CSV ───────────────────────────────────────────────
        ts_rel  = row.get("timeseries_file", "")       # e.g. "timeseries/foo.csv"
        ts_src  = ""   # absolute path to source CSV (for window stats)
        ts_dest = ""
        if ts_rel:
            src = os.path.join(_REPO_ROOT, "test_results", ts_rel)
            if os.path.exists(src):
                ts_src = src
                basename = os.path.basename(src)
                dst = os.path.join(ts_dest_dir, basename)
                shutil.copy2(src, dst)
                # relative path within bench results dir
                ts_dest = f"timeseries/{basename}"
            else:
                print(f"ERROR: timeseries not found: {src}\n"
                      "The canonical data in test_results/ is incomplete; "
                      "rerun the experiment (reuse will redo only the "
                      "affected configurations).", file=sys.stderr)
                sys.exit(1)

        # ── freeze-window stats (phases 2-4, after prepare) ─────────────
        ts_freeze_start = _flt(row.get("ts_freeze_start_s"))
        ts_snap_end     = _flt(row.get("ts_snap_end_s"))
        freeze_window   = _compute_window_stats(ts_src, ts_freeze_start, ts_snap_end)

        record = {
            "config": {
                "name":        "firecracker_snapshot",
                "workload":    workload,
                "mem_size_mib": mem,
                "mode":        mode,
                "iteration":   iteration,
            },
            "results": {
                "total_snapshot_ms":  round(total_ms,    3),
                "downtime_ms":        round(downtime_ms, 3),
                "phase_breakdown_us": {k: round(v, 1) for k, v in phases.items()},
                "throughput":         {k: round(v, 3) for k, v in throughput.items()},
                "latency_us":         {k: round(v, 3) for k, v in latency_us.items()},
                "overall":            {k: round(v, 3) for k, v in overall.items()},
                "freeze_window":      {
                    k: (int(v) if k == "sample_count" else round(v, 3))
                    for k, v in freeze_window.items()
                },
                "timeseries_file":    ts_dest,
                "ts_snap_start_s":    _flt(row.get("ts_snap_start_s")),
                "ts_snap_end_s":      _flt(row.get("ts_snap_end_s")),
                "ts_freeze_start_s":  _flt(row.get("ts_freeze_start_s")),
                "ts_freeze_end_s":    _flt(row.get("ts_freeze_end_s")),
            },
        }
        records.append(record)

    records.sort(key=lambda r: (
        r["config"]["mode"],
        r["config"]["mem_size_mib"],
        r["config"]["iteration"],
    ))

    out_path = os.path.join(results_dir, f"snapshot_benchmark_{workload}.json")
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(records, f, indent=4)
    os.rename(tmp_path, out_path)

    print(f"\nExported {len(records)} records → {out_path}")
    print(f"Timeseries CSVs copied → {ts_dest_dir}")
    return out_path


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

# Per-test artifact directories created by pytest under test_results/.
# Each contains UUID-named VM directories with logs and binary copies (~40 MB
# each) that are only needed for post-failure debugging.
_ARTIFACT_DIRS = [
    *_MODE_TO_TEST.values(),           # test_{full,live,live_bpf}_snapshot_app_experiment
    "test_snapshot_experiment_quick",  # smoke-test artifacts
]


def cleanup_test_artifacts():
    """Remove per-test pytest artifact directories from test_results/.

    Keeps experiment_results.csv, test-report.json, and timeseries/.
    """
    test_results = os.path.join(_REPO_ROOT, "test_results")
    for name in _ARTIFACT_DIRS:
        path = os.path.join(test_results, name)
        if os.path.isdir(path):
            # Best-effort: never fail the run over debug artifacts
            shutil.rmtree(path, ignore_errors=True)
            print(f"Removed {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Run the Firecracker snapshot benchmark and export the results.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--workload",    default="redis_light",
                    help="Workload name to filter (e.g. redis_light, redis_heavy)")
    ap.add_argument("--mem-sizes",   type=int, nargs="+", default=_DEFAULT_MEM_SIZES,
                    metavar="MiB",   help="VM memory sizes to include")
    ap.add_argument("--iterations",  type=int, default=3,
                    help="Number of iterations per configuration")
    ap.add_argument("--rootfs",      default=_DEFAULT_ROOTFS,
                    help="Path to guest rootfs image")
    ap.add_argument("--bench-dir", default=_REPO_ROOT,
                    help="Directory to export into; results are written "
                         "to <bench-dir>/results/")
    ap.add_argument("--skip-run",      action="store_true",
                    help="Skip pytest; re-export from existing test_results/")
    ap.add_argument("--max-iteration", type=int, default=None,
                    help="Only export iterations 0..N (inclusive); "
                         "default: --iterations - 1")
    ap.add_argument("--keep-artifacts", action="store_true",
                    help="Keep per-test pytest artifact directories after a successful run")
    ap.add_argument("--no-reuse-results", action="store_true",
                    help="Rerun everything instead of skipping "
                         "configurations already present in the CSV")
    args = ap.parse_args()

    if not args.skip_run:
        run_experiment(args.workload, args.mem_sizes, args.iterations,
                       args.rootfs, reuse=not args.no_reuse_results)

    max_iteration = (args.max_iteration if args.max_iteration is not None
                     else args.iterations - 1)
    export_results(args.workload, args.mem_sizes, args.bench_dir,
                   max_iteration=max_iteration)

    if not args.skip_run and not args.keep_artifacts:
        cleanup_test_artifacts()


if __name__ == "__main__":
    main()
