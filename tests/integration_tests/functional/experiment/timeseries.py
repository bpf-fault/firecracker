# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Timeseries sampling (throughput timeline for plot 16).

The sampler runs on the host and connects to the guest Redis over its TAP IP
using raw TCP RESP pipelining.  This means connection failures (VM paused or
frozen) are directly observable as failed samples rather than SSH timeouts.
"""

import csv
import os
import socket
import threading
import time

from .constants import (
    TIMESERIES_DIR,
    TIMESERIES_INTERVAL_S,
    TIMESERIES_SAMPLE_OPS,
)


CLONE_NEWNET = 0x40000000  # from <sched.h>


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


def _redis_ping_burst(host, n_ops, timeout, port=6379, netns_id=None, n_lat_ops=50):
    """Pipeline n_ops PING commands and measure per-op latency on the same connection.

    Returns (ops_per_sec, avg_ms, p50_ms, p99_ms, p999_ms, failed).
    ``failed`` is True when a connection error or timeout occurs.

    Two phases run on a single TCP connection:
    1. Sequential pings (n_lat_ops × ~0.1 ms each) for real per-op latency.
    2. Pipelined burst (n_ops) for throughput measurement.

    When ``netns_id`` is set the socket is created inside the named network
    namespace (e.g. ``"netns-master-1"``).  The calling thread enters the
    namespace for socket creation only and is restored immediately after;
    the open socket remains valid in the original namespace because Linux
    pins sockets to the namespace in which they were created.
    ``setns(CLONE_NEWNET)`` affects only the calling thread (Linux ≥ 3.8).
    """
    cmd = b"*1\r\n$4\r\nPING\r\n" * n_ops
    expected_len = len(b"+PONG\r\n") * n_ops
    single_cmd = b"*1\r\n$4\r\nPING\r\n"
    single_expected_len = len(b"+PONG\r\n")
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
                # Phase 1: sequential pings for latency
                latencies = []
                for _ in range(n_lat_ops):
                    t0 = time.monotonic()
                    s.sendall(single_cmd)
                    buf = b""
                    while len(buf) < single_expected_len:
                        chunk = s.recv(single_expected_len)
                        if not chunk:
                            break
                        buf += chunk
                    latencies.append((time.monotonic() - t0) * 1000)
                # Phase 2: pipelined burst for throughput
                t0 = time.monotonic()
                s.sendall(cmd)
                buf = b""
                while len(buf) < expected_len:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                elapsed = time.monotonic() - t0
        finally:
            if netns_id:
                os.setns(self_fd, CLONE_NEWNET)
                os.close(netns_fd)
                os.close(self_fd)
        pongs = buf.count(b"+PONG")
        if pongs == 0 or not latencies:
            return _fail
        ops = pongs / elapsed if elapsed > 0 else 0.0
        sorted_lats = sorted(latencies)
        avg_ms = sum(latencies) / len(latencies)
        p50  = _percentile(sorted_lats, 50)
        p99  = _percentile(sorted_lats, 99)
        p999 = _percentile(sorted_lats, 99.9)
        return ops, avg_ms, p50, p99, p999, False
    except Exception:  # noqa: BLE001
        return _fail


def _start_timeseries_sampler(guest_ip, workload, netns_id=None, start_wall=None):  # noqa: ARG001 — workload reserved for future use
    """Start a daemon thread that samples Redis throughput at TIMESERIES_INTERVAL_S cadence.

    Connects to the guest Redis over its TAP IP using raw TCP RESP, so
    connection failures during VM pause/freeze appear as failed samples.

    ``start_wall`` lets a second sampler share the same time origin as the
    first (pass ``handle["start_wall"]`` from the original sampler).  When
    omitted a new origin is established at call time.

    Returns a handle dict for use with _stop_timeseries_sampler / _write_timeseries_csv.
    Only call for redis workloads.
    """
    samples = []          # [(t_rel_s, ops_per_sec, avg_ms, p50_ms, p99_ms, p999_ms, failed)]
    stop_event = threading.Event()
    start_wall = start_wall if start_wall is not None else time.monotonic()

    def _loop():
        while not stop_event.is_set():
            t_start = time.monotonic()
            t_rel = t_start - start_wall
            ops, avg_ms, p50, p99, p999, failed = _redis_ping_burst(
                guest_ip, TIMESERIES_SAMPLE_OPS, timeout=0.25, netns_id=netns_id
            )
            samples.append((
                round(t_rel, 3), ops,
                round(avg_ms, 3), round(p50, 3), round(p99, 3), round(p999, 3),
                failed,
            ))
            elapsed = time.monotonic() - t_start
            stop_event.wait(max(0.0, TIMESERIES_INTERVAL_S - elapsed))

    t = threading.Thread(target=_loop, daemon=True, name="ts_sampler")
    t.start()
    return {"thread": t, "stop": stop_event, "samples": samples, "start_wall": start_wall}


def _stop_timeseries_sampler(handle):
    """Signal the sampler thread to stop and wait for it to finish."""
    handle["stop"].set()
    handle["thread"].join(timeout=5)


def _write_timeseries_csv(handle, workload, mem_size_mib, mode, iteration):
    """Write collected samples to a timeseries CSV. Returns the relative path string."""
    os.makedirs(TIMESERIES_DIR, exist_ok=True)
    name = f"{workload}_{mem_size_mib}mib_{mode}_iter{iteration:02d}.csv"
    path = os.path.join(TIMESERIES_DIR, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_ms", "t_rel_s", "throughput", "avg_ms", "p50_ms", "p99_ms", "p999_ms", "failed"])
        for t_rel, ops, avg_ms, p50_ms, p99_ms, p999_ms, failed in handle["samples"]:
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
