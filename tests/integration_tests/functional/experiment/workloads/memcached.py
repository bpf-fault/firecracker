# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Memcached workload helpers for the snapshot live experiment."""

import os
import re
import socket
import time

from ..constants import MEMCACHED_WORKLOAD_PARAMS

_CLONE_NEWNET = 0x40000000  # from <sched.h>


def _assert_port_reachable(netns_id, host, port, timeout=2):
    """Verify that ``host:port`` is reachable from within ``netns_id``.

    Uses ``setns(CLONE_NEWNET)`` on the calling thread (Linux ≥ 3.8) so no
    external tools are required.  Raises ``AssertionError`` if the connection
    cannot be established within ``timeout`` seconds.
    """
    netns_fd = self_fd = None
    try:
        netns_fd = os.open(f"/var/run/netns/{netns_id}", os.O_RDONLY)
        self_fd  = os.open("/proc/self/ns/net",           os.O_RDONLY)
        try:
            os.setns(netns_fd, _CLONE_NEWNET)
            with socket.create_connection((host, port), timeout=timeout):
                pass  # connection succeeded
        finally:
            os.setns(self_fd, _CLONE_NEWNET)
    except Exception as exc:
        raise AssertionError(
            f"port {host}:{port} unreachable from netns {netns_id!r}: {exc!r}"
        ) from exc
    finally:
        if netns_fd is not None:
            os.close(netns_fd)
        if self_fd is not None:
            os.close(self_fd)


def _setup_memcached(vm, mem_size_mib):
    """Start memcached and pre-populate it.

    Uses half guest RAM, 2 threads, port 11211.  Pre-populates all keys.
    """
    mem_alloc = mem_size_mib // 2

    # Stop any system-started memcached (Ubuntu 24.04 starts memcached at boot
    # bound to 127.0.0.1 per /etc/memcached.conf).  Without this, our subsequent
    # memcached launch silently fails to bind (port taken) and the system service
    # keeps running on 127.0.0.1 only, making it unreachable from the host TAP.
    vm.ssh.check_output(
        "systemctl stop memcached 2>/dev/null || true; "
        "pkill -x memcached 2>/dev/null || true; "
        "sleep 0.3"
    )

    # Run memcached in the background via setsid so it outlives the SSH session.
    # The -u root flag is required because the guest runs as root; without it
    # memcached refuses to start when invoked directly as root.
    vm.ssh.check_output(
        f"setsid /usr/bin/memcached -m {mem_alloc} -t 2 -p 11211 -l 0.0.0.0 -u root "
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
    # -s 127.0.0.1 forces IPv4; without it, memtier may try ::1 (IPv6) first
    # and silently load 0 keys if memcached only listens on 0.0.0.0 (IPv4).
    # --key-maximum is required when using -n allkeys; without it
    # memtier_benchmark prints its help text and exits non-zero.
    vm.ssh.check_output(
        "memtier_benchmark -s 127.0.0.1 -p 11211 --protocol=memcache_text "
        "--key-maximum=500000 --data-size=512 "
        "-c 10 -t 2 --ratio=1:0 -n allkeys --hide-histogram "
        "--key-pattern=P:P",
        timeout=120,
    )

    # Verify that the host can reach guest port 11211 from within the VM's
    # network namespace — this is the path the timeseries sampler uses.
    netns_id = vm.netns.id
    guest_ip = vm.iface["eth0"]["iface"].guest_ip
    _assert_port_reachable(netns_id, guest_ip, 11211)


def _parse_memtier_output(output):
    """Parse memtier_benchmark summary output.

    Finds the 'Totals' line and extracts ops/sec, average latency, and
    latency percentiles.
    Returns (ops, avg_us, p50_us, p95_us, p99_us, p999_us).

    memtier's default text Totals line does not include a p95 column:
      Type  Ops/sec  Hits/sec  Misses/sec  Avg Lat  p50  p99  p99.9  p99.99  KB/sec
    p95 would require --print-percentiles and is not available here; it is
    returned as 0.0.  p99.9 (p999) is at index 7.
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

    # p95 is not in memtier's default text output; callers receive 0.0.
    p95 = 0.0
    return ops, avg_us, p50, p95, p99, p999


def _measure_memcached_baseline(vm, workload):
    """Run a 10-second memtier benchmark and return (ops, avg_us, p50, p95, p99, p999).

    Uses -s 127.0.0.1 to force IPv4 — memcached binds to 0.0.0.0 (IPv4 only)
    and some environments resolve 'localhost' to ::1 (IPv6) by default.
    """
    params = MEMCACHED_WORKLOAD_PARAMS[workload]
    clients = params["clients"]
    ratio = params["ratio"]

    _, out, _ = vm.ssh.check_output(
        f"memtier_benchmark -s 127.0.0.1 -p 11211 --protocol=memcache_text "
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
        f"nohup memtier_benchmark -s 127.0.0.1 -p 11211 --protocol=memcache_text "
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
        f"memtier_benchmark -s 127.0.0.1 -p 11211 --protocol=memcache_text "
        f"-c {clients} -t 2 --ratio={ratio} --test-time=30 --hide-histogram "
        f"> /tmp/memtier_during.log 2>&1; "
        f"touch /tmp/memtier_during.done' "
        f"</dev/null >/dev/null 2>&1 &"
    )
