# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Redis workload helpers for the snapshot live experiment."""

import re
import time

from ..constants import REDIS_WORKLOAD_PARAMS


def _setup_redis(vm, mem_size_mib):
    """Start redis-server and pre-populate it.

    Allocates half of guest RAM as Redis maxmemory (allkeys-lru), then
    pre-populates roughly 50 % of that budget with 512-byte string values.
    Returns redis_maxmem in MiB.
    """
    redis_maxmem = mem_size_mib // 2

    # Stop any system-started redis instance (it binds to 127.0.0.1 only per
    # /etc/redis/redis.conf).  Use systemctl stop so that systemd doesn't
    # auto-restart it; fall back to redis-cli shutdown for non-systemd setups.
    vm.ssh.check_output(
        "systemctl stop redis-server redis 2>/dev/null || "
        "redis-cli shutdown nosave 2>/dev/null || true; "
        "sleep 0.3"
    )
    vm.ssh.check_output(
        f"redis-server --daemonize yes "
        f"--maxmemory {redis_maxmem}mb "
        f"--maxmemory-policy allkeys-lru "
        f"--save '' --appendonly no "
        f"--bind 0.0.0.0 --protected-mode no"
    )

    # Wait until Redis is ready.
    vm.ssh.check_output(
        "for i in $(seq 1 30); do "
        "  redis-cli ping | grep -q PONG && break; "
        "  sleep 0.2; "
        "done",
        timeout=15,
    )

    # Pre-populate: target ~50 % of maxmemory using 512-byte values.
    prefill_ops = redis_maxmem * 1024  # each op stores ~1 KiB key+value overhead
    vm.ssh.check_output(
        f"redis-benchmark -t set -n {prefill_ops} -d 512 -r 1000000 -q",
        timeout=120,
    )

    return redis_maxmem


def _parse_redis_benchmark_output(output):
    """Parse redis-benchmark verbose output.

    Returns (ops_per_sec, avg_us, p50_us, p99_us, p999_us).  Missing
    values default to 0.0.  avg_us is extracted from the "latency summary"
    table printed by redis-benchmark >= 6.x; falls back to 0.0 on older
    versions that do not emit that section.
    """
    ops = 0.0
    avg_us = p50 = p99 = p999 = 0.0

    m = re.search(r"([\d.]+) requests per second", output)
    if m:
        ops = float(m.group(1))

    # Average latency from the "latency summary" block (redis-benchmark >= 6).
    # Format:
    #   latency summary (msec):
    #           avg       min       p50  ...
    #         0.083     0.032     0.079  ...
    m = re.search(
        r"latency summary \(msec\):\s+avg\s+\S.*?\n\s+([\d.]+)",
        output,
        re.DOTALL,
    )
    if m:
        avg_us = float(m.group(1)) * 1000

    # Histogram lines: "  50.00% <= 0.103 milliseconds"
    # redis-benchmark uses power-of-2 percentile buckets (50, 75, 87.5,
    # 93.75, 98.44, 99.22, 99.61, 99.90, ...) — there is never an exact
    # 99.00% or 99.90% line.  Use threshold matching instead: take the
    # first bucket at or above each target percentile.
    for line in output.splitlines():
        m = re.match(r"\s*([\d.]+)%\s*<=\s*([\d.]+)\s*milliseconds", line)
        if not m:
            continue
        pct = float(m.group(1))
        lat_ms = float(m.group(2))
        if abs(pct - 50.0) < 0.01:
            p50 = lat_ms * 1000
        elif 99.0 <= pct < 99.9 and p99 == 0.0:
            p99 = lat_ms * 1000
        elif pct >= 99.9 and p999 == 0.0:
            p999 = lat_ms * 1000

    return ops, avg_us, p50, p99, p999


def _measure_redis_baseline(vm, workload):
    """Run a 50 000-op redis-benchmark and return (ops, avg_us, p50, p99, p999)."""
    params = REDIS_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ops_arg = params["ops"]

    _, out, _ = vm.ssh.check_output(
        f"redis-benchmark -t {ops_arg} -n 50000 -c {clients}",
        timeout=60,
    )
    return _parse_redis_benchmark_output(out)


def _measure_post_snapshot_redis(vm, workload):
    """Run a post-snapshot redis-benchmark burst to measure recovery performance.

    Returns (ops, avg_us, p50, p99, p999).
    """
    return _measure_redis_baseline(vm, workload)


def _start_redis_background_workload(vm, workload):
    """Launch a continuous redis-benchmark loop in the background.

    Lets it settle for 2 s before returning.
    """
    params = REDIS_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ops_arg = params["ops"]

    vm.ssh.check_output(
        f"nohup sh -c '"
        f"while true; do "
        f"  redis-benchmark -t {ops_arg} -c {clients} -n 10000 -q 2>/dev/null; "
        f"done' </dev/null >/dev/null 2>&1 &"
    )
    time.sleep(2)


def _start_redis_during_burst(vm, workload, baseline_ops):
    """Launch a fixed-size redis-benchmark burst for the during-snapshot measurement.

    Targets ≈15 s of load.  Writes output to /tmp/redis_during.log and touches
    /tmp/redis_during.done when complete.
    """
    params = REDIS_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ops_arg = params["ops"]
    n = max(50_000, int(baseline_ops * 15))

    vm.ssh.check_output(
        f"nohup sh -c '"
        f"redis-benchmark -t {ops_arg} -c {clients} -n {n} "
        f"> /tmp/redis_during.log 2>&1; "
        f"touch /tmp/redis_during.done' "
        f"</dev/null >/dev/null 2>&1 &"
    )
