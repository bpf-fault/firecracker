# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Redis workload helpers for the snapshot live experiment.

All per-window load generation (baseline, during-snapshot, post-snapshot) runs
from the HOST using ``memtier_benchmark`` connecting to the guest Redis over its
TAP IP via ``nsenter``.  Running the load generator outside the VM means it
keeps running during VM pause events, so latency spikes from the snapshot
freeze are directly observable in per-window metrics — matching the QEMU
benchmark methodology.

``_setup_redis`` still uses SSH to configure redis-server inside the guest.
"""

import json
import shutil
import subprocess
import tempfile
import time

from ..constants import REDIS_WORKLOAD_PARAMS

# Duration for baseline and post-snapshot measurement windows (seconds).
_MEASURE_DURATION_SEC = 10

# Duration for the during-snapshot measurement window (seconds).
# Long enough to span the snapshot + UFFD streaming + recovery phases.
_DURING_DURATION_SEC = 30

# Warm-up delay after starting the during-snapshot process before returning.
# Gives memtier time to ramp up to steady-state load before the snapshot fires.
_DURING_WARMUP_SEC = 2


def _memtier_ratio(ops_str):
    """Convert a REDIS_WORKLOAD_PARAMS ops string to a memtier ``--ratio`` value."""
    key = ops_str.lower()
    if key == "set":
        return "1:0"
    if key == "get":
        return "0:1"
    return "1:1"  # "set,get" or any mixed form


def _parse_memtier_json(json_path):
    """Parse a memtier ``--json-out-file`` and return ``(ops, avg_us, p50_us, p95_us, p99_us, p999_us)``.

    All latency values are converted from milliseconds to microseconds.
    Returns all-zeros on parse failure.
    """
    try:
        with open(json_path) as f:
            data = json.load(f)
        totals = data["ALL STATS"]["Totals"]

        ops = float(totals.get("Ops/sec") or 0)

        count = float(totals.get("Count") or 0)
        accumulated = float(totals.get("Accumulated Latency") or 0)
        if count > 0 and accumulated > 0:
            avg_ms = accumulated / count
        else:
            avg_ms = float(totals.get("Average Latency") or totals.get("Latency") or 0)

        pcts = totals.get("Percentile Latencies", {})
        p50_ms  = float(pcts.get("p50.00") or 0)
        p95_ms  = float(pcts.get("p95.00") or 0)
        p99_ms  = float(pcts.get("p99.00") or 0)
        p999_ms = float(pcts.get("p99.90") or 0)

        # Convert ms → µs (our CSV fields use _us suffix).
        return ops, avg_ms * 1000, p50_ms * 1000, p95_ms * 1000, p99_ms * 1000, p999_ms * 1000
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0


def _memtier_cmd(guest_ip, netns_id, workload, json_path, duration_sec):
    """Build a ``memtier_benchmark`` command list for host-side execution via ``nsenter``."""
    params = REDIS_WORKLOAD_PARAMS[workload]
    return [
        "nsenter", f"--net=/var/run/netns/{netns_id}",
        "memtier_benchmark",
        "--server", guest_ip,
        "--port", "6379",
        "--protocol", "redis",
        "--threads", "1",
        "--clients", str(params["clients"]),
        "--pipeline", str(params["pipeline"]),
        "--ratio", _memtier_ratio(params["ops"]),
        "--data-size", str(params["value_size"]),
        "--test-time", str(int(duration_sec)),
        "--json-out-file", str(json_path),
        "--hide-histogram",
    ]


def _setup_redis(vm, mem_size_mib, value_size=128):
    """Start redis-server and pre-populate it.

    Allocates half of guest RAM as Redis maxmemory (allkeys-lru), then
    pre-populates roughly 50 % of that budget.  ``value_size`` (bytes) should
    match the workload's value_size so that key sizes and eviction behaviour
    are consistent between pre-population and measurement.  Defaults to 128 to
    match the QEMU benchmark's --benchmark-value-size default.
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

    # Pre-populate: target ~50 % of maxmemory.  Use the same value_size as the
    # workload measurements so eviction behaviour and memory pressure are
    # consistent throughout the experiment.
    prefill_ops = redis_maxmem * 1024  # each op stores ~1 KiB key+value overhead
    vm.ssh.check_output(
        f"redis-benchmark -t set -n {prefill_ops} -d {value_size} -r 1000000 -q",
        timeout=120,
    )

    return redis_maxmem


def _measure_redis_baseline(vm, workload):
    """Run a {_MEASURE_DURATION_SEC}s memtier_benchmark from the host.

    Returns ``(ops, avg_us, p50_us, p95_us, p99_us, p999_us)``.
    """
    guest_ip = vm.iface["eth0"]["iface"].guest_ip
    netns_id = vm.netns.id
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        json_path = f.name
    cmd = _memtier_cmd(guest_ip, netns_id, workload, json_path, _MEASURE_DURATION_SEC)
    try:
        subprocess.run(cmd, capture_output=True, timeout=_MEASURE_DURATION_SEC + 30)
        return _parse_memtier_json(json_path)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    finally:
        try:
            import os
            os.unlink(json_path)
        except OSError:
            pass


def _measure_post_snapshot_redis(vm, workload):
    """Run a post-snapshot memtier_benchmark burst from the host.

    Returns ``(ops, avg_us, p50_us, p95_us, p99_us, p999_us)``.
    """
    return _measure_redis_baseline(vm, workload)


def _start_redis_during_burst(vm, workload, baseline_ops):  # noqa: ARG001  baseline_ops unused (fixed duration)
    """Start a host-side memtier_benchmark run that spans the snapshot window.

    The process runs for ``_DURING_DURATION_SEC`` seconds total.  This function
    waits ``_DURING_WARMUP_SEC`` seconds before returning so the load is at
    steady state when the caller triggers the snapshot.

    Returns a handle dict for ``_collect_redis_during_results``.
    """
    guest_ip = vm.iface["eth0"]["iface"].guest_ip
    netns_id = vm.netns.id
    tmp_dir = tempfile.mkdtemp(prefix="memtier_during_")
    json_path = f"{tmp_dir}/result.json"
    cmd = _memtier_cmd(guest_ip, netns_id, workload, json_path, _DURING_DURATION_SEC)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(_DURING_WARMUP_SEC)
    return {"proc": proc, "json_path": json_path, "tmp_dir": tmp_dir}


def _collect_redis_during_results(handle):
    """Wait for the during-snapshot memtier process to finish and return metrics.

    Returns ``(ops, avg_us, p50_us, p95_us, p99_us, p999_us)``.
    Cleans up the temporary directory regardless of outcome.
    """
    proc = handle["proc"]
    json_path = handle["json_path"]
    tmp_dir = handle["tmp_dir"]
    try:
        try:
            proc.wait(timeout=_DURING_DURATION_SEC + 30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return _parse_memtier_json(json_path)
    except Exception:  # noqa: BLE001
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
