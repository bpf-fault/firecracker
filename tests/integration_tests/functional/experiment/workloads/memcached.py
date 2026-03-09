# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Memcached workload helpers for the snapshot live experiment."""

import re
import time

from ..constants import MEMCACHED_WORKLOAD_PARAMS


def _setup_memcached(vm, mem_size_mib):
    """Start memcached and pre-populate it.

    Uses half guest RAM, 2 threads, port 11211.  Pre-populates all keys.
    """
    mem_alloc = mem_size_mib // 2

    # Run memcached in the background via nohup rather than with -d, because
    # memcached on Ubuntu 24.04 may refuse to daemonize when already running as
    # root inside the guest.  Use setsid to fully detach from the SSH session.
    vm.ssh.check_output(
        f"setsid /usr/bin/memcached -m {mem_alloc} -t 2 -p 11211 -l 0.0.0.0 "
        f"</dev/null >/tmp/memcached.log 2>&1 &"
    )

    # Wait until the TCP port accepts connections (nc -z = port-scan mode).
    vm.ssh.check_output(
        "for i in $(seq 1 50); do "
        "  nc -z 127.0.0.1 11211 2>/dev/null && break; "
        "  sleep 0.2; "
        "done; "
        "nc -z 127.0.0.1 11211 || { echo 'memcached log:'; cat /tmp/memcached.log; exit 1; }",
        timeout=20,
    )

    # Pre-populate: load 500 000 keys with 512-byte values (SET only).
    # --key-maximum is required when using -n allkeys; without it
    # memtier_benchmark prints its help text and exits non-zero.
    vm.ssh.check_output(
        "memtier_benchmark -p 11211 --protocol=memcache_text "
        "--key-maximum=500000 --data-size=512 "
        "-c 10 -t 2 --ratio=1:0 -n allkeys --hide-histogram "
        "--key-pattern=P:P",
        timeout=120,
    )


def _parse_memtier_output(output):
    """Parse memtier_benchmark summary output.

    Finds the 'Totals' line and extracts ops/sec, average latency, and
    latency percentiles.
    Returns (ops, avg_us, p50_us, p99_us, p999_us).
    """
    ops = 0.0
    avg_us = p50 = p99 = p999 = 0.0

    for line in output.splitlines():
        if "Totals" not in line:
            continue
        parts = line.split()
        # Columns: Type  Ops/sec  Hits/sec  Miss/sec  Avg Lat  p50   p99   p99.9  ...
        # Indices:   0      1        2         3         4      5     6      7
        try:
            ops    = float(parts[1])
            # latency columns are in milliseconds; convert to microseconds
            avg_us = float(parts[4]) * 1000
            p50    = float(parts[5]) * 1000
            p99    = float(parts[6]) * 1000
            p999   = float(parts[7]) * 1000
        except (IndexError, ValueError):
            pass
        break

    return ops, avg_us, p50, p99, p999


def _measure_memcached_baseline(vm, workload):
    """Run a 10-second memtier benchmark and return (ops, avg_us, p50, p99, p999)."""
    params = MEMCACHED_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ratio = params["ratio"]

    _, out, _ = vm.ssh.check_output(
        f"memtier_benchmark -p 11211 --protocol=memcache_text "
        f"-c {clients} -t 2 --ratio={ratio} --test-time=10 --hide-histogram",
        timeout=30,
    )
    return _parse_memtier_output(out)


def _measure_post_snapshot_memcached(vm, workload):
    """Run a post-snapshot memtier burst to measure recovery performance.

    Returns (ops, avg_us, p50, p99, p999).
    """
    return _measure_memcached_baseline(vm, workload)


def _start_memcached_background_workload(vm, workload):
    """Launch a long-running memtier workload (600 s) in the background.

    Output is saved to /tmp/memtier_overall.log (without -q/--hide-histogram)
    so that the Totals line is parseable at the end of the test for overall
    run statistics.
    """
    params = MEMCACHED_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ratio = params["ratio"]

    vm.ssh.check_output(
        f"nohup memtier_benchmark -p 11211 --protocol=memcache_text "
        f"-c {clients} -t 2 --ratio={ratio} --test-time=600 "
        f"</dev/null >/tmp/memtier_overall.log 2>&1 &"
    )
    time.sleep(2)


def _start_memcached_during_burst(vm, workload):
    """Launch a 30-second memtier burst for the during-snapshot measurement.

    Writes output to /tmp/memtier_during.log and sentinel to
    /tmp/memtier_during.done.
    """
    params = MEMCACHED_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ratio = params["ratio"]

    vm.ssh.check_output(
        f"nohup sh -c '"
        f"memtier_benchmark -p 11211 --protocol=memcache_text "
        f"-c {clients} -t 2 --ratio={ratio} --test-time=30 --hide-histogram "
        f"> /tmp/memtier_during.log 2>&1; "
        f"touch /tmp/memtier_during.done' "
        f"</dev/null >/dev/null 2>&1 &"
    )
