# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Timeseries measurement via a single host-side memtier_benchmark process.

One ``memtier_benchmark`` process (run with ``--stats-interval=0.1``) spans the
entire experiment window — baseline, snapshot, and post-snapshot recovery.
After the process exits its JSON output is parsed and bucketed into the three
windows using recorded wall-clock offsets.

Public API:
  _start_memtier(guest_ip, netns_id, protocol, params, duration_sec) -> handle
  _stop_memtier(handle)
  _parse_memtier_windows(handle, snap_start_s, snap_end_s)
      -> (baseline_tuple, during_tuple, post_tuple)
  _write_timeseries_csv(handle, workload, mem_size_mib, mode, iteration) -> path
"""

import csv
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time

from .constants import TIMESERIES_DIR, TIMESERIES_INTERVAL_S


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ratio_str(params):
    """Convert workload params to a memtier --ratio value.

    Redis params use an ``ops`` key ("set", "get", "set,get").
    Memcached params use a ``ratio`` key already in "SET:GET" form.
    """
    if "ratio" in params:
        return params["ratio"]
    ops = params.get("ops", "get").lower()
    if ops == "set":
        return "1:0"
    if ops == "get":
        return "0:1"
    return "1:1"


def _memtier_cmd(guest_ip, netns_id, protocol, params, duration_sec, json_path):
    """Build a memtier_benchmark command list for host-side execution via nsenter."""
    return [
        "nsenter", f"--net=/var/run/netns/{netns_id}",
        "memtier_benchmark",
        "--server", guest_ip,
        "--port", "6379" if protocol == "redis" else "11211",
        "--protocol", protocol,
        "--threads", "1",
        "--clients", str(params["clients"]),
        "--pipeline", str(params.get("pipeline", 1)),
        "--ratio", _ratio_str(params),
        "--data-size", str(params.get("value_size", 128)),
        "--test-time", str(int(duration_sec)),
        "--stats-interval", str(TIMESERIES_INTERVAL_S),
        "--json-out-file", json_path,
        "--hide-histogram",
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _start_memtier(guest_ip, netns_id, protocol, params, duration_sec):
    """Start a host-side memtier_benchmark process for the full experiment window.

    Returns a handle dict for use with _stop_memtier, _parse_memtier_windows,
    and _write_timeseries_csv.
    """
    tmp_dir   = tempfile.mkdtemp(prefix="ts_memtier_")
    json_path = os.path.join(tmp_dir, "ts.json")
    cmd  = _memtier_cmd(guest_ip, netns_id, protocol, params, duration_sec, json_path)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {
        "proc":       proc,
        "json_path":  json_path,
        "tmp_dir":    tmp_dir,
        "start_wall": time.monotonic(),
    }


def _stop_memtier(handle):
    """Send SIGINT to the memtier process and wait for it to write its JSON output.

    memtier treats SIGINT as a graceful shutdown: it stops generating requests
    and writes the accumulated JSON (including Time-Serie) before exiting.
    Falls back to SIGKILL if it does not exit within 15 seconds.
    """
    proc = handle["proc"]
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except OSError:
        pass


def _parse_memtier_windows(handle, snap_start_s, snap_end_s):
    """Parse memtier JSON and derive per-window (baseline / during / post) metrics.

    Classifies each 0.1 s Time-Serie bucket by its timestamp relative to the
    recorded snapshot start and end offsets.  Per-window ops/s is computed from
    total counts; latency values are count-weighted means of per-bucket averages
    and simple means of per-bucket percentiles (approximate but consistent with
    the QEMU benchmark methodology).

    Returns (baseline, during, post) where each is:
        (ops_s, avg_us, p50_us, p95_us, p99_us, p999_us)

    p95_us is always 0.0 — not present in per-bucket Time-Serie data.

    During the full-snapshot pause all bucket counts will be 0, so during
    naturally returns all-zeros without special-casing.
    """
    _zero = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    json_path = handle["json_path"]
    try:
        with open(json_path) as f:
            data = json.load(f)
        time_serie = (
            data.get("ALL STATS", {})
                .get("Totals", {})
                .get("Time-Serie") or {}
        )
    except Exception:  # noqa: BLE001
        return _zero, _zero, _zero

    baseline_buckets = []
    during_buckets   = []
    post_buckets     = []

    for key in sorted(time_serie, key=lambda k: float(k)):
        t      = float(key)
        bucket = time_serie[key]
        count  = float(bucket.get("Count") or 0)
        avg_ms = float(bucket.get("Average Latency") or 0)
        p50_ms = float(bucket.get("p50.00") or 0)
        p99_ms = float(bucket.get("p99.00") or 0)
        p999_ms= float(bucket.get("p99.90") or 0)
        entry  = (t, count, avg_ms, p50_ms, p99_ms, p999_ms)

        if t <= snap_start_s:
            baseline_buckets.append(entry)
        elif t <= snap_end_s:
            during_buckets.append(entry)
        else:
            post_buckets.append(entry)

    def _aggregate(buckets):
        if not buckets:
            return _zero
        # Window duration: span from first bucket start to last bucket end.
        first_t = buckets[0][0] - TIMESERIES_INTERVAL_S
        last_t  = buckets[-1][0]
        duration = max(last_t - first_t, TIMESERIES_INTERVAL_S)

        total_count = sum(b[1] for b in buckets)
        ops_s = total_count / duration

        if total_count > 0:
            avg_us = sum(b[1] * b[2] for b in buckets) / total_count * 1000
        else:
            avg_us = 0.0

        n = len(buckets)
        p50_us  = sum(b[3] for b in buckets) / n * 1000
        p99_us  = sum(b[4] for b in buckets) / n * 1000
        p999_us = sum(b[5] for b in buckets) / n * 1000
        return ops_s, avg_us, p50_us, 0.0, p99_us, p999_us

    return _aggregate(baseline_buckets), _aggregate(during_buckets), _aggregate(post_buckets)


def _write_timeseries_csv(handle, workload, mem_size_mib, mode, iteration):
    """Write the memtier Time-Serie buckets to a timeseries CSV.

    Each 0.1 s bucket becomes one CSV row.  The format is identical to the
    previous TCP-sampler CSV so plotting code works unchanged.
    Cleans up the temporary directory after writing.

    Returns the relative path string (e.g. "timeseries/foo.csv").
    """
    json_path = handle["json_path"]
    tmp_dir   = handle["tmp_dir"]

    try:
        with open(json_path) as f:
            data = json.load(f)
        totals     = data.get("ALL STATS", {}).get("Totals", {})
        time_serie = totals.get("Time-Serie") or {}
        runtime    = data.get("ALL STATS", {}).get("Runtime", {})
        total_ms   = runtime.get("Total duration")
        duration_sec = float(total_ms) / 1000.0 if total_ms else None

        buckets = []
        for key in sorted(time_serie, key=lambda k: float(k)):
            bucket     = time_serie[key]
            t_rel_s    = float(key)
            if duration_sec is not None:
                bucket_end = min(duration_sec, t_rel_s)
                bucket_dur = max(0.0, min(TIMESERIES_INTERVAL_S,
                                          duration_sec - (t_rel_s - TIMESERIES_INTERVAL_S)))
            else:
                bucket_dur = TIMESERIES_INTERVAL_S
            if bucket_dur <= 0:
                continue
            count   = float(bucket.get("Count") or 0)
            tput    = count / bucket_dur
            avg_ms  = float(bucket.get("Average Latency") or 0)
            p50_ms  = float(bucket.get("p50.00") or 0)
            p99_ms  = float(bucket.get("p99.00") or 0)
            p999_ms = float(bucket.get("p99.90") or 0)
            buckets.append((t_rel_s, tput, avg_ms, p50_ms, p99_ms, p999_ms))
    except Exception:  # noqa: BLE001
        buckets = []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # Fill gaps (e.g. VM paused during full snapshot → memtier emits no bucket
    # for intervals with 0 completions).  Insert explicit zero rows so the CSV
    # is continuous and plotters don't interpolate across the pause window.
    filled = []
    for i, entry in enumerate(buckets):
        if i > 0:
            prev_t = buckets[i - 1][0]
            t_next = round(prev_t + TIMESERIES_INTERVAL_S, 3)
            while t_next < entry[0] - TIMESERIES_INTERVAL_S * 0.5:
                filled.append((t_next, 0.0, 0.0, 0.0, 0.0, 0.0))
                t_next = round(t_next + TIMESERIES_INTERVAL_S, 3)
        filled.append(entry)

    os.makedirs(TIMESERIES_DIR, exist_ok=True)
    name = f"{workload}_{mem_size_mib}mib_{mode}_iter{iteration:02d}.csv"
    path = os.path.join(TIMESERIES_DIR, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "t_rel_s", "throughput", "avg_ms",
                    "p50_ms", "p99_ms", "p999_ms", "failed"])
        for t_rel_s, tput, avg_ms, p50_ms, p99_ms, p999_ms in filled:
            w.writerow([
                round(t_rel_s * 1000, 1),
                round(t_rel_s, 3),
                round(tput, 1),
                round(avg_ms, 3),
                round(p50_ms, 3),
                round(p99_ms, 3),
                round(p999_ms, 3),
                0,
            ])
    return f"timeseries/{name}"
