# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Timeseries sampling (throughput timeline for plot 16).

Two backends are available, selected by ``TIMESERIES_BACKEND`` in constants.py:

* ``"tcp"``     — raw TCP RESP sampler (100 ms resolution, ``TIMESERIES_TIMEOUT_S``
                  timeout per sample).  Connection failures during VM freeze appear
                  as ``failed=1`` rows; slow responses during UFFD streaming appear
                  as real high-latency rows once the timeout is raised above 250 ms.

* ``"memtier"`` — runs ``memtier_benchmark`` from the host via ``nsenter`` for the
                  entire recording window, then parses the 1-second ``Time-Serie``
                  buckets from its JSON output.  No per-request timeout; zero-
                  throughput seconds during the snapshot pause are recorded as-is.
"""

import csv
import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time

from .constants import (
    MEMCACHED_WORKLOAD_PARAMS,
    REDIS_WORKLOAD_PARAMS,
    TIMESERIES_BACKEND,
    TIMESERIES_DIR,
    TIMESERIES_INTERVAL_S,
    TIMESERIES_SAMPLE_OPS,
    TIMESERIES_TIMEOUT_S,
)


CLONE_NEWNET = 0x40000000  # from <sched.h>

# Number of sequential ops per sample used for per-op latency measurement.
_TS_LAT_OPS = 50

# memtier test-time ceiling (seconds).  The process is SIGINT-stopped early;
# this value just needs to be larger than the longest possible recording window.
_MEMTIER_MAX_DURATION_SEC = 120


def _percentile(sorted_data, pct):
    """Linear-interpolation percentile on pre-sorted data."""
    n = len(sorted_data)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_data[0]
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_data[-1]
    frac = idx - lo
    return sorted_data[lo] + frac * (sorted_data[hi] - sorted_data[lo])


def _build_redis_cmd(op, value_size):
    """Build a pipelineable RESP command for a single SET or GET op.

    Uses a fixed key ``ts:k`` and a value of ``value_size`` zero bytes.
    Returns (cmd_bytes, expected_response_bytes).
    """
    key = b"ts:k"
    if op == "set":
        val = b"x" * value_size
        cmd = (
            b"*3\r\n$3\r\nSET\r\n"
            + b"$" + str(len(key)).encode() + b"\r\n" + key + b"\r\n"
            + b"$" + str(len(val)).encode() + b"\r\n" + val + b"\r\n"
        )
        resp = b"+OK\r\n"
    else:
        # GET — use a simple inline GET on the fixed key.
        cmd = (
            b"*2\r\n$3\r\nGET\r\n"
            + b"$" + str(len(key)).encode() + b"\r\n" + key + b"\r\n"
        )
        # Response is either a bulk string or a nil; we count response delimiters.
        resp = b"\r\n"  # used only to count responses via occurrence counting
    return cmd, resp


def _redis_workload_burst(host, n_ops, timeout, op, value_size, port=6379, netns_id=None):
    """Pipeline n_ops SET or GET commands and measure per-op latency.

    Returns (ops_per_sec, avg_ms, p50_ms, p99_ms, p999_ms, failed).
    ``failed`` is True when a connection error or timeout occurs.

    Two phases run on a single TCP connection:
    1. Sequential ops (_TS_LAT_OPS) for real per-op latency measurement.
    2. Pipelined burst (n_ops) for throughput measurement.

    Using the same operation type as the benchmark workload (SET or GET)
    ensures that write-protect faults triggered by SET operations appear as
    latency spikes in the timeseries, consistent with how the QEMU benchmark
    derives its per-second timeseries from actual workload traffic.

    When ``netns_id`` is set the socket is created inside the named network
    namespace (e.g. ``"netns-master-1"``).
    ``setns(CLONE_NEWNET)`` affects only the calling thread (Linux ≥ 3.8).
    """
    single_cmd, _ = _build_redis_cmd(op, value_size)
    burst_cmd = single_cmd * n_ops
    netns_fd = self_fd = None
    _fail = (0.0, timeout * 1000, timeout * 1000, timeout * 1000, timeout * 1000, True)
    try:
        if netns_id:
            netns_fd = os.open(f"/var/run/netns/{netns_id}", os.O_RDONLY)
            self_fd  = os.open("/proc/self/ns/net",           os.O_RDONLY)
        try:
            if netns_id:
                os.setns(netns_fd, CLONE_NEWNET)
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                # Phase 1: sequential ops for latency.
                latencies = []
                for _ in range(_TS_LAT_OPS):
                    t0 = time.monotonic()
                    s.sendall(single_cmd)
                    buf = b""
                    # Read until we get the first \r\n (end of status/bulk header).
                    while b"\r\n" not in buf:
                        chunk = s.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                    # For GET, drain the value bytes if present.
                    if op == "get" and buf.startswith(b"$") and not buf.startswith(b"$-1"):
                        header_end = buf.index(b"\r\n") + 2
                        val_len_str = buf[1:buf.index(b"\r\n")]
                        val_len = int(val_len_str) + 2  # +2 for trailing \r\n
                        total_needed = header_end + val_len
                        while len(buf) < total_needed:
                            chunk = s.recv(total_needed - len(buf))
                            if not chunk:
                                break
                            buf += chunk
                    latencies.append((time.monotonic() - t0) * 1000)
                # Phase 2: pipelined burst for throughput.
                t0 = time.monotonic()
                s.sendall(burst_cmd)
                # Drain responses by counting \r\n occurrences until we have
                # at least n_ops (each response ends with at least one \r\n).
                buf = b""
                crlf_count = 0
                while crlf_count < n_ops:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    crlf_count = buf.count(b"\r\n")
                elapsed = time.monotonic() - t0
        finally:
            if netns_id:
                os.setns(self_fd, CLONE_NEWNET)
                os.close(netns_fd)
                os.close(self_fd)
        if not latencies or elapsed <= 0:
            return _fail
        ops_per_sec = n_ops / elapsed
        sorted_lats = sorted(latencies)
        avg_ms = sum(latencies) / len(latencies)
        p50  = _percentile(sorted_lats, 50)
        p99  = _percentile(sorted_lats, 99)
        p999 = _percentile(sorted_lats, 99.9)
        return ops_per_sec, avg_ms, p50, p99, p999, False
    except Exception:  # noqa: BLE001
        return _fail


def _build_memcached_cmd(op, value_size):
    """Build a pipelineable memcached text-protocol command for a single SET or GET op.

    Uses a fixed key ``ts:k``.
    SET response sentinel: ``STORED\\r\\n``
    GET response sentinel: ``END\\r\\n``
    """
    key = b"ts:k"
    if op == "set":
        val = b"x" * value_size
        cmd = (
            b"set " + key + b" 0 0 " + str(len(val)).encode() + b"\r\n"
            + val + b"\r\n"
        )
    else:
        cmd = b"get " + key + b"\r\n"
    return cmd


def _memcached_workload_burst(host, n_ops, timeout, op, value_size, port=11211, netns_id=None):
    """Pipeline n_ops SET or GET commands to memcached and measure per-op latency.

    Identical structure to ``_redis_workload_burst``: sequential phase for latency,
    pipelined phase for throughput.  Uses the memcached text protocol.

    Returns (ops_per_sec, avg_ms, p50_ms, p99_ms, p999_ms, failed).
    ``failed`` is True when a connection error or timeout occurs.
    """
    cmd = _build_memcached_cmd(op, value_size)
    burst_cmd = cmd * n_ops
    sentinel = b"STORED\r\n" if op == "set" else b"END\r\n"
    netns_fd = self_fd = None
    _fail = (0.0, timeout * 1000, timeout * 1000, timeout * 1000, timeout * 1000, True)
    try:
        if netns_id:
            netns_fd = os.open(f"/var/run/netns/{netns_id}", os.O_RDONLY)
            self_fd  = os.open("/proc/self/ns/net",           os.O_RDONLY)
        try:
            if netns_id:
                os.setns(netns_fd, CLONE_NEWNET)
            with socket.create_connection((host, port), timeout=timeout) as s:
                s.settimeout(timeout)
                # Phase 1: sequential ops for latency.
                latencies = []
                for _ in range(_TS_LAT_OPS):
                    t0 = time.monotonic()
                    s.sendall(cmd)
                    buf = b""
                    while sentinel not in buf:
                        chunk = s.recv(256)
                        if not chunk:
                            break
                        buf += chunk
                    latencies.append((time.monotonic() - t0) * 1000)
                # Phase 2: pipelined burst for throughput.
                t0 = time.monotonic()
                s.sendall(burst_cmd)
                buf = b""
                count = 0
                while count < n_ops:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                    count = buf.count(sentinel)
                elapsed = time.monotonic() - t0
        finally:
            if netns_id:
                os.setns(self_fd, CLONE_NEWNET)
                os.close(netns_fd)
                os.close(self_fd)
        if not latencies or elapsed <= 0:
            return _fail
        ops_per_sec = n_ops / elapsed
        sorted_lats = sorted(latencies)
        avg_ms = sum(latencies) / len(latencies)
        p50  = _percentile(sorted_lats, 50)
        p99  = _percentile(sorted_lats, 99)
        p999 = _percentile(sorted_lats, 99.9)
        return ops_per_sec, avg_ms, p50, p99, p999, False
    except Exception as _exc:  # noqa: BLE001
        if not getattr(_memcached_workload_burst, "_logged", False):
            import sys as _sys
            import traceback as _tb
            print(
                f"[ts-memcached] burst failed: {_exc!r}\n{_tb.format_exc()}",
                flush=True, file=_sys.stderr,
            )
            _memcached_workload_burst._logged = True  # type: ignore[attr-defined]
        return _fail


# ---------------------------------------------------------------------------
# TCP backend
# ---------------------------------------------------------------------------

class _noop_lock:  # noqa: N801
    """Trivial no-op context manager used when a handle has no samples_lock."""
    def __enter__(self): return self
    def __exit__(self, *_): pass

def _start_timeseries_sampler(guest_ip, workload, netns_id=None, start_wall=None):
    """Start a daemon thread that samples workload throughput at TIMESERIES_INTERVAL_S cadence.

    Connects to the guest service over its TAP IP using raw TCP, so connection
    failures during VM pause/freeze appear as failed samples.  Slow responses
    (up to TIMESERIES_TIMEOUT_S) are recorded as real high-latency data points
    rather than being clipped to "failed".

    For Redis workloads: uses RESP on port 6379.
    For Memcached workloads: uses the text protocol on port 11211.

    SET operations are preferred so that write-protect faults triggered by the
    live snapshot appear as latency spikes rather than being masked.

    ``start_wall`` lets a second sampler share the same time origin as the
    first (pass ``handle["start_wall"]`` from the original sampler).  When
    omitted a new origin is established at call time.

    Returns a handle dict for use with _stop_timeseries_sampler / _write_timeseries_csv.
    Supports both Redis (RESP, port 6379) and Memcached (text protocol, port 11211).
    """
    # Choose burst function and parameters based on workload type.
    # Always prefer SET operations so that write-protect faults triggered by the
    # live snapshot appear as latency spikes rather than being masked.
    if workload in MEMCACHED_WORKLOAD_PARAMS:
        def _burst(host):
            return _memcached_workload_burst(
                host, TIMESERIES_SAMPLE_OPS, timeout=TIMESERIES_TIMEOUT_S,
                op="set", value_size=32, port=11211, netns_id=netns_id,
            )
    else:
        params = REDIS_WORKLOAD_PARAMS.get(workload, {})
        ops_str = params.get("ops", "get")
        op = "set" if "set" in ops_str else "get"
        value_size = params.get("value_size", 128)

        def _burst(host):
            return _redis_workload_burst(
                host, TIMESERIES_SAMPLE_OPS, timeout=TIMESERIES_TIMEOUT_S,
                op=op, value_size=value_size, netns_id=netns_id,
            )

    samples = []          # [(t_rel_s, ops_per_sec, avg_ms, p50_ms, p99_ms, p999_ms, failed)]
    samples_lock = threading.Lock()
    stop_event = threading.Event()
    start_wall = start_wall if start_wall is not None else time.monotonic()

    def _fire(t_rel):
        """Issue one burst and append the result stamped at issue time."""
        ops, avg_ms, p50, p99, p999, failed = _burst(guest_ip)
        entry = (
            round(t_rel, 3), ops,
            round(avg_ms, 3), round(p50, 3), round(p99, 3), round(p999, 3),
            failed,
        )
        with samples_lock:
            samples.append(entry)

    def _loop():
        while not stop_event.is_set():
            t_rel = time.monotonic() - start_wall
            threading.Thread(target=_fire, args=(t_rel,), daemon=True).start()
            stop_event.wait(TIMESERIES_INTERVAL_S)

    t = threading.Thread(target=_loop, daemon=True, name="ts_sampler")
    t.start()
    return {"thread": t, "stop": stop_event, "samples": samples,
            "samples_lock": samples_lock, "start_wall": start_wall}


def _stop_timeseries_sampler(handle):
    """Signal the sampler thread to stop and wait for in-flight samples to finish."""
    handle["stop"].set()
    handle["thread"].join(timeout=TIMESERIES_TIMEOUT_S + 2)
    # Give in-flight _fire threads up to TIMESERIES_TIMEOUT_S to complete and append.
    time.sleep(TIMESERIES_TIMEOUT_S + 0.2)


def _write_timeseries_csv(handle, workload, mem_size_mib, mode, iteration):
    """Write collected samples to a timeseries CSV. Returns the relative path string."""
    os.makedirs(TIMESERIES_DIR, exist_ok=True)
    name = f"{workload}_{mem_size_mib}mib_{mode}_iter{iteration:02d}.csv"
    path = os.path.join(TIMESERIES_DIR, name)
    with handle.get("samples_lock", _noop_lock()), open(path, "w", newline="") as f:
        sorted_samples = sorted(handle["samples"], key=lambda r: r[0])
        w = csv.writer(f)
        w.writerow(["t_ms", "t_rel_s", "throughput", "avg_ms", "p50_ms", "p99_ms", "p999_ms", "failed"])
        for t_rel, ops, avg_ms, p50_ms, p99_ms, p999_ms, failed in sorted_samples:
            w.writerow([
                round(t_rel * 1000, 1),
                t_rel,
                round(ops, 1),
                round(avg_ms, 3),
                round(p50_ms, 3),
                round(p99_ms, 3),
                round(p999_ms, 3),
                1 if failed else 0,
            ])
    return f"timeseries/{name}"


# ---------------------------------------------------------------------------
# memtier backend
# ---------------------------------------------------------------------------

def _memtier_ratio(ops_str):
    """Convert a REDIS_WORKLOAD_PARAMS ops string to a memtier ``--ratio`` value."""
    key = ops_str.lower()
    if key == "set":
        return "1:0"
    if key == "get":
        return "0:1"
    return "1:1"


def _start_timeseries_memtier(guest_ip, netns_id, workload, start_wall=None):
    """Start a host-side memtier_benchmark process for timeseries collection.

    Runs ``memtier_benchmark`` via ``nsenter`` with a generous ``--test-time``
    ceiling.  The caller stops it early with ``_stop_timeseries_memtier`` once
    the desired recording window has elapsed; memtier handles SIGINT gracefully
    and writes its full JSON output (including per-second Time-Serie buckets)
    before exiting.

    Returns a handle dict for use with
    ``_stop_timeseries_memtier`` / ``_write_timeseries_csv_from_memtier``.
    Only call for redis workloads.
    """
    params = REDIS_WORKLOAD_PARAMS.get(workload, {})
    tmp_dir = tempfile.mkdtemp(prefix="ts_memtier_")
    json_path = os.path.join(tmp_dir, "ts.json")
    cmd = [
        "nsenter", f"--net=/var/run/netns/{netns_id}",
        "memtier_benchmark",
        "--server", guest_ip,
        "--port", "6379",
        "--protocol", "redis",
        "--threads", "1",
        "--clients", str(params.get("clients", 1)),
        "--pipeline", str(params.get("pipeline", 1)),
        "--ratio", _memtier_ratio(params.get("ops", "get")),
        "--data-size", str(params.get("value_size", 128)),
        "--test-time", str(_MEMTIER_MAX_DURATION_SEC),
        "--json-out-file", json_path,
        "--hide-histogram",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    start_wall = start_wall if start_wall is not None else time.monotonic()
    return {
        "proc": proc,
        "json_path": json_path,
        "tmp_dir": tmp_dir,
        "start_wall": start_wall,
    }


def _stop_timeseries_memtier(handle):
    """Send SIGINT to the memtier process and wait for it to write its JSON output.

    memtier treats SIGINT as a graceful shutdown: it stops generating new
    requests and writes the accumulated JSON (including Time-Serie) before
    exiting.  Falls back to SIGKILL if it does not exit within 15 seconds.
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


def _write_timeseries_csv_from_memtier(handle, workload, mem_size_mib, mode, iteration):  # noqa: ARG001  workload unused (for API symmetry)
    """Parse the memtier JSON Time-Serie and write a timeseries CSV.

    Each 1-second bucket from memtier's ``Time-Serie`` field becomes one CSV
    row.  The format is identical to ``_write_timeseries_csv`` so that the
    plotting code works unchanged.

    Latency values in memtier's JSON are in milliseconds; they are written
    directly (the CSV columns use ``_ms`` suffix).

    The ``failed`` column is always ``0`` — memtier does not have a per-request
    timeout.  Zero-throughput seconds during the snapshot pause appear naturally
    as ``throughput=0`` rows.

    Returns the relative path string and cleans up the temporary directory.
    """
    json_path = handle["json_path"]
    tmp_dir = handle["tmp_dir"]
    try:
        with open(json_path) as f:
            data = json.load(f)
        totals = data["ALL STATS"]["Totals"]
        time_serie = totals.get("Time-Serie") or {}
        duration_sec = None
        runtime = data["ALL STATS"].get("Runtime", {})
        total_ms = runtime.get("Total duration")
        if total_ms:
            duration_sec = float(total_ms) / 1000.0

        buckets = []
        for key in sorted(time_serie.keys(), key=lambda k: int(k)):
            bucket = time_serie[key]
            bucket_start = float(int(key))
            if duration_sec is not None:
                bucket_end = min(duration_sec, bucket_start + 1.0)
            else:
                bucket_end = bucket_start + 1.0
            bucket_dur = max(0.0, bucket_end - bucket_start)
            if bucket_dur <= 0:
                continue
            count = float(bucket.get("Count") or 0)
            throughput_rps = count / bucket_dur
            avg_ms = float(bucket.get("Average Latency") or 0)
            p50_ms  = float(bucket.get("p50.00") or bucket.get("p50") or 0)
            p99_ms  = float(bucket.get("p99.00") or bucket.get("p99") or 0)
            p999_ms = float(bucket.get("p99.90") or bucket.get("p999") or 0)
            buckets.append((bucket_start, throughput_rps, avg_ms, p50_ms, p99_ms, p999_ms))
    except Exception:  # noqa: BLE001
        buckets = []
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    os.makedirs(TIMESERIES_DIR, exist_ok=True)
    name = f"{workload}_{mem_size_mib}mib_{mode}_iter{iteration:02d}.csv"
    path = os.path.join(TIMESERIES_DIR, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "t_rel_s", "throughput", "avg_ms", "p50_ms", "p99_ms", "p999_ms", "failed"])
        for t_rel_s, tput, avg_ms, p50_ms, p99_ms, p999_ms in buckets:
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
