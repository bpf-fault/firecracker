# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Memcached workload helpers for the snapshot live experiment.

Measurement (baseline / during / post) is handled by a single host-side
memtier_benchmark process started from app_runners.py via timeseries.py.
This module only contains the guest-side setup helper.
"""

import os
import socket
import time

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
                pass
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

    Uses half guest RAM, 2 threads, port 11211.  Pre-populates 500 000 keys
    with 512-byte values using a guest-side memtier_benchmark run.
    """
    mem_alloc = mem_size_mib // 2

    vm.ssh.check_output(
        "systemctl stop memcached 2>/dev/null || true; "
        "pkill -x memcached 2>/dev/null || true; "
        "sleep 0.3"
    )

    vm.ssh.check_output(
        f"setsid /usr/bin/memcached -m {mem_alloc} -t 2 -p 11211 -l 0.0.0.0 -u root "
        f"</dev/null >/tmp/memcached.log 2>&1 &"
    )

    vm.ssh.check_output(
        "for i in $(seq 1 50); do "
        "  nc -z 127.0.0.1 11211 2>/dev/null && break; "
        "  sleep 0.2; "
        "done; "
        "nc -z 127.0.0.1 11211 || { echo 'memcached log:'; cat /tmp/memcached.log; exit 1; }",
        timeout=20,
    )

    vm.ssh.check_output(
        "memtier_benchmark -s 127.0.0.1 -p 11211 --protocol=memcache_text "
        "--key-maximum=500000 --data-size=512 "
        "-c 10 -t 2 --ratio=1:0 -n allkeys --hide-histogram "
        "--key-pattern=P:P",
        timeout=120,
    )

    netns_id = vm.netns.id
    guest_ip = vm.iface["eth0"]["iface"].guest_ip
    _assert_port_reachable(netns_id, guest_ip, 11211)
