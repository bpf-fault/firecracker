# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Workload classification utilities and shared helpers for the snapshot live experiment."""

import pytest

from ..constants import MEMCACHED_WORKLOAD_PARAMS, REDIS_WORKLOAD_PARAMS
from .memcached import (
    _measure_memcached_baseline,
    _measure_post_snapshot_memcached,
    _parse_memtier_output,
    _setup_memcached,
    _start_memcached_background_workload,
    _start_memcached_during_burst,
)
from .redis import (
    _collect_redis_during_results,
    _measure_post_snapshot_redis,
    _measure_redis_baseline,
    _parse_memtier_json,
    _setup_redis,
    _start_redis_during_burst,
)
from .stream import (
    _parse_stream_output,
    _run_stream_benchmark,
    _start_stream_during_burst,
)
from .synthetic import (
    _measure_workload_throughput,
    _start_workload,
    _stop_workload,
)


def _is_redis_workload(wl):
    """Return True if the workload name is a Redis variant."""
    return wl in REDIS_WORKLOAD_PARAMS


def _is_memcached_workload(wl):
    """Return True if the workload name is a Memcached variant."""
    return wl in MEMCACHED_WORKLOAD_PARAMS


def _is_stream_workload(wl):
    """Return True if the workload is the STREAM benchmark."""
    return wl == "stream"


def _check_workload_tools(vm, workload):
    """Skip the test if the required binaries are absent from the guest rootfs."""
    if _is_redis_workload(workload):
        tools = "redis-server redis-cli redis-benchmark"
    elif _is_memcached_workload(workload):
        tools = "memcached memtier_benchmark nc"
    elif _is_stream_workload(workload):
        tools = "/usr/local/bin/stream"
    else:
        return  # synthetic workloads need no extra tools

    _, out, _ = vm.ssh.check_output(
        f"command -v {tools} >/dev/null 2>&1 && echo AVAILABLE || echo MISSING"
    )
    if "MISSING" in out:
        pytest.skip(f"Required tools for workload '{workload}' not found in guest: {tools}")


def _wait_for_sentinel(vm, path, timeout=180):
    """Block until the sentinel file at `path` appears inside the guest."""
    vm.ssh.check_output(
        f"until test -f {path}; do sleep 0.3; done",
        timeout=timeout,
    )


def _stop_all_app_workloads(vm):
    """Kill all running application benchmark processes in the guest."""
    vm.ssh.check_output(
        "pkill -f redis-benchmark 2>/dev/null || true; "
        "pkill -f memtier_benchmark 2>/dev/null || true; "
        "pkill -f stream 2>/dev/null || true"
    )


__all__ = [
    "_is_redis_workload",
    "_is_memcached_workload",
    "_is_stream_workload",
    "_check_workload_tools",
    "_wait_for_sentinel",
    "_stop_all_app_workloads",
    # re-exported from submodules
    "_start_workload",
    "_measure_workload_throughput",
    "_stop_workload",
    "_setup_redis",
    "_parse_memtier_json",
    "_collect_redis_during_results",
    "_measure_redis_baseline",
    "_measure_post_snapshot_redis",
    "_start_redis_during_burst",
    "_setup_memcached",
    "_parse_memtier_output",
    "_measure_memcached_baseline",
    "_measure_post_snapshot_memcached",
    "_start_memcached_background_workload",
    "_start_memcached_during_burst",
    "_parse_stream_output",
    "_run_stream_benchmark",
    "_start_stream_during_burst",
]
