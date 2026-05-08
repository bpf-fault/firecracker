# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Redis workload helpers for the snapshot live experiment.

Measurement (baseline / during / post) is handled by a single host-side
memtier_benchmark process started from app_runners.py via timeseries.py.
This module only contains the guest-side setup helper.
"""


def _setup_redis(vm, mem_size_mib, value_size=128):
    """Start redis-server and pre-populate it.

    Allocates half of guest RAM as Redis maxmemory (allkeys-lru), then
    pre-populates roughly 50 % of that budget.  ``value_size`` (bytes) should
    match the workload's value_size so that key sizes and eviction behaviour
    are consistent between pre-population and measurement.
    Returns redis_maxmem in MiB.
    """
    redis_maxmem = mem_size_mib // 2

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

    vm.ssh.check_output(
        "for i in $(seq 1 30); do "
        "  redis-cli ping | grep -q PONG && break; "
        "  sleep 0.2; "
        "done",
        timeout=15,
    )

    prefill_ops = redis_maxmem * 1024
    vm.ssh.check_output(
        f"redis-benchmark -t set -n {prefill_ops} -d {value_size} -r 1000000 -q",
        timeout=120,
    )

    return redis_maxmem
