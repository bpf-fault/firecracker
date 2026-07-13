#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the Firecracker snapshot benchmark and write JSON results.

This is a benchmark, not a test: it drives the experiment helpers
directly (one container for the whole sweep, no pytest) and maintains a
single results store: snapshot_benchmark_<workload>.json plus a
timeseries/ directory under --results-dir. Records use the
{config, results} schema and are checkpointed after every
configuration; configurations already present (with their timeseries
file intact) are skipped, so an interrupted sweep resumes where it
stopped and deleting results forces re-measurement.

Runs inside the devtool container: ./tools/devtool -y bench -- <args>
"""

import argparse
import csv
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

_TESTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_TESTS_DIR))
sys.path.insert(0, str(_TESTS_DIR / "integration_tests"))

# pylint: disable=wrong-import-position
from framework.artifacts import kernels
from framework.defs import DEFAULT_BINARY_DIR
from framework.microvm import MicroVMFactory

from functional.experiment import (
    _boot_app_experiment_vm,
    _check_workload_tools,
    _log_app_summary,
    _run_full_snapshot_app,
    _run_live_bpf_snapshot_app,
    _run_live_snapshot_app,
)
from functional.experiment.constants import TIMESERIES_DIR

_REPO_ROOT = _TESTS_DIR.parent
_DEFAULT_RESULTS_DIR = _REPO_ROOT / "test_results"
_DEFAULT_ROOTFS = "/srv/test_artifacts/ubuntu-24.04-app.ext4"

# The full-snapshot helper restores into the same VM and does not need a
# factory, unlike the live helpers, which boot a restore VM from it.
_MODE_RUNNERS = {
    "live": _run_live_snapshot_app,
    "live_bpf": _run_live_bpf_snapshot_app,
    "full": lambda vm, factory, mem, workload, iteration:
        _run_full_snapshot_app(vm, mem, workload, iteration),
}


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# Results store ({config, results} records, checkpointed)
# ---------------------------------------------------------------------------

def results_path(results_dir, workload):
    return os.path.join(results_dir, f"snapshot_benchmark_{workload}.json")


def load_records(results_dir, workload):
    path = results_path(results_dir, workload)
    if not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        log(f"warning: could not read {path}; starting fresh")
        return []


def checkpoint_records(results_dir, workload, records):
    records.sort(key=lambda r: (
        r["config"]["mode"], r["config"]["mem_size_mib"],
        r["config"]["iteration"]))
    path = results_path(results_dir, workload)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(records, f, indent=4)
    os.replace(tmp, path)


def config_done(records, results_dir, mode, mem, iteration):
    """A configuration counts as done only if its record exists and its
    timeseries file (when referenced) is still on disk — a half-deleted
    store re-measures rather than serving incomplete data."""
    for r in records:
        c = r["config"]
        if (c.get("mode"), c.get("mem_size_mib"), c.get("iteration")) != \
                (mode, mem, iteration):
            continue
        ts = r.get("results", {}).get("timeseries_file") or ""
        if ts and not os.path.isfile(os.path.join(results_dir, ts)):
            return False
        return True
    return False


# ---------------------------------------------------------------------------
# Row -> record conversion (CSV-row dicts from the experiment helpers)
# ---------------------------------------------------------------------------

def _flt(val, default=0.0):
    try:
        return float(val) if val not in (None, "") else default
    except (ValueError, TypeError):
        return default


def _compute_window_stats(ts_path, t_start, t_end):
    """Throughput/latency stats over non-failed samples in [t_start,
    t_end] (the freeze-to-snapshot-completion window). Latencies in the
    timeseries are ms; returned values are µs."""
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
    if not rows:
        return empty

    n = len(rows)
    avg = [r["avg"] for r in rows]
    p999 = [r["p999"] for r in rows]
    return {
        "throughput_ops_s":   sum(r["thr"] for r in rows) / n,
        "avg_latency_us":     sum(avg) / n * 1000,
        "p99_us":             sum(r["p99"] for r in rows) / n * 1000,
        "p999_us":            sum(p999) / n * 1000,
        "max_avg_latency_us": max(avg) * 1000,
        "max_p999_us":        max(p999) * 1000,
        "sample_count":       n,
    }


def row_to_record(row, workload, mode, mem, iteration, results_dir):
    """Build a results record from an experiment helper's row dict,
    moving its timeseries CSV into the results store."""
    if mode == "full":
        total_ms = _flt(row.get("full_total_ms"))
        downtime_ms = total_ms
        phases = {"create": total_ms * 1000}
    else:
        total_ms = _flt(row.get("total_us")) / 1000.0
        downtime_ms = _flt(row.get("freeze_us")) / 1000.0
        phases = {
            "phase1":         _flt(row.get("phase1_us")),
            "populate_pages": _flt(row.get("populate_pages_us")),
            "freeze":         _flt(row.get("freeze_us")),
            "stream":         _flt(row.get("stream_us")),
            "finalize":       _flt(row.get("finalize_us")),
        }

    if workload == "stream":
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

    # Move the timeseries CSV from the helpers' scratch location into
    # the results store; the record references it relatively.
    ts_rel = row.get("timeseries_file", "") or ""
    ts_dest = ""
    ts_path = ""
    if ts_rel:
        src = os.path.join(os.path.dirname(str(TIMESERIES_DIR)), ts_rel)
        if not os.path.isfile(src):
            raise RuntimeError(f"timeseries not found: {src}")
        dest_dir = os.path.join(results_dir, "timeseries")
        os.makedirs(dest_dir, exist_ok=True)
        ts_path = os.path.join(dest_dir, os.path.basename(src))
        # shutil.move, not os.replace: the results store may be a
        # separate mount from the scratch location.
        shutil.move(src, ts_path)
        ts_dest = f"timeseries/{os.path.basename(src)}"

    freeze_window = _compute_window_stats(
        ts_path, _flt(row.get("ts_freeze_start_s")),
        _flt(row.get("ts_snap_end_s")))

    return {
        "config": {
            "name":         "firecracker_snapshot",
            "workload":     workload,
            "mem_size_mib": mem,
            "mode":         mode,
            "iteration":    iteration,
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


# ---------------------------------------------------------------------------
# Benchmark loop
# ---------------------------------------------------------------------------

def pick_guest_kernel():
    for kernel in kernels("vmlinux-5.10*"):
        if "no-acpi" not in kernel.name:
            return kernel
    log("ERROR: no vmlinux-5.10 guest kernel artifact found")
    sys.exit(1)


def run_config(binary_dir, kernel, workload, mode, mem, iteration):
    """Boot a fresh VM, run one snapshot measurement, tear down."""
    factory = MicroVMFactory(binary_dir)
    try:
        shim = SimpleNamespace(kernel_file=kernel, rootfs_file=None)
        vm = _boot_app_experiment_vm(shim, factory, mem,
                                     bpf=(mode == "live_bpf"))
        _check_workload_tools(vm, workload)
        row = _MODE_RUNNERS[mode](vm, factory, mem, workload, iteration)
        _log_app_summary(row)
        return row
    finally:
        factory.kill()


def main():
    ap = argparse.ArgumentParser(
        description="Run the Firecracker snapshot benchmark.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--workloads", nargs="+",
                    default=["redis_heavy", "memcached_heavy"],
                    help="Workloads to run")
    ap.add_argument("--mem-sizes", type=int, nargs="+",
                    default=[4096, 8192], metavar="MiB",
                    help="VM memory sizes")
    ap.add_argument("--iterations", type=int, default=3,
                    help="Iterations per configuration")
    ap.add_argument("--results-dir", default=str(_DEFAULT_RESULTS_DIR),
                    help="Results store (JSON + timeseries/)")
    ap.add_argument("--rootfs", default=_DEFAULT_ROOTFS,
                    help="Guest rootfs with the application workloads")
    ap.add_argument("--no-reuse-results", action="store_true",
                    help="Rerun everything instead of skipping "
                         "configurations already present")
    args = ap.parse_args()

    os.environ["EXPERIMENT_ROOTFS"] = args.rootfs
    if not os.path.isfile(args.rootfs):
        log(f"ERROR: rootfs not found: {args.rootfs}")
        sys.exit(1)
    os.makedirs(args.results_dir, exist_ok=True)

    binary_dir = DEFAULT_BINARY_DIR
    kernel = pick_guest_kernel()
    log(f"Guest kernel: {kernel}")
    log(f"Results store: {args.results_dir}")

    for workload in args.workloads:
        records = [] if args.no_reuse_results \
            else load_records(args.results_dir, workload)
        for mode in _MODE_RUNNERS:
            for mem in args.mem_sizes:
                for iteration in range(args.iterations):
                    if config_done(records, args.results_dir, mode, mem,
                                   iteration):
                        log(f"Skipping {workload} {mode} mem={mem} "
                            f"iteration={iteration} (already in results)")
                        continue
                    log(f"Running config: {workload} {mode} mem={mem} "
                        f"iteration={iteration}")
                    row = run_config(binary_dir, kernel, workload, mode,
                                     mem, iteration)
                    records = [r for r in records
                               if (r["config"]["mode"],
                                   r["config"]["mem_size_mib"],
                                   r["config"]["iteration"])
                               != (mode, mem, iteration)]
                    records.append(row_to_record(row, workload, mode, mem,
                                                 iteration,
                                                 args.results_dir))
                    checkpoint_records(args.results_dir, workload, records)
        log(f"{workload}: {len(records)} records in "
            f"{results_path(args.results_dir, workload)}")


if __name__ == "__main__":
    main()
