# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Experiment: Live vs Full snapshot performance under varied memory workloads.

Systematically measures snapshot performance across a matrix of:
  - VM memory sizes: 256, 512, 1024, 2048, 4096 MiB
  - Guest write workloads: idle, light (~4 MiB/s), medium (~32 MiB/s), heavy (~128 MiB/s)
  - Snapshot modes: full (paused) vs live (UFFD write-protect)

Results are written to experiment_results.csv and logged to console.
See docs/live_snapshot/live-snapshot-experiment-design.md for full design.
"""

import csv
import logging
import math
import os
import re
import statistics
import time
from pathlib import Path

import pytest

from framework.microvm import Snapshot, SnapshotType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VCPU_COUNT = 2

# Workload configurations: (block_size, block_count, sleep_seconds)
# Each iteration of the loop writes block_size * block_count bytes, then sleeps.
# sleep(1) supports fractional seconds on Ubuntu (coreutils).
WORKLOAD_PARAMS = {
    "idle": None,
    "light": (4096, 64, 0.062),       # ~4 MiB/s target
    "medium": (4096, 256, 0.031),     # ~32 MiB/s target
    "heavy": (4096, 1024, 0.031),     # ~128 MiB/s target
}

# Application workload experiment — reduced matrix per design doc §6.2
APP_MEM_SIZES = [512, 2048]

REDIS_WORKLOAD_PARAMS = {
    "redis_light": {"clients": 2,  "ops": "get"},       # read-heavy
    "redis_mixed": {"clients": 10, "ops": "set,get"},   # balanced
    "redis_heavy": {"clients": 50, "ops": "set"},       # write-heavy
}

MEMCACHED_WORKLOAD_PARAMS = {
    "memcached_light": {"clients": 2,  "ratio": "1:9"},  # 1 SET : 9 GETs
    "memcached_heavy": {"clients": 50, "ratio": "1:1"},  # equal SET/GET
}

# STREAM_ARRAY_SIZES: doubles, targeting ~50% guest RAM across 3 arrays
STREAM_ARRAY_SIZES = {
    256:  5_592_405,
    512:  11_184_810,
    1024: 22_369_621,
    2048: 44_739_242,
    4096: 89_478_485,
}

RESULTS_FILE = os.environ.get(
    "EXPERIMENT_RESULTS_CSV",
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        )))),
        "test_results",
        "experiment_results.csv",
    ),
)

CSV_FIELDS = [
    "timestamp",
    "mem_size_mib",
    "workload",
    "snapshot_mode",
    "iteration",
    # Live snapshot phase timings (us)
    "phase1_us",
    "populate_pages_us",
    "freeze_us",
    "pause_us",
    "save_state_us",
    "wp_enable_us",
    "resume_us",
    "stream_us",
    "finalize_us",
    "total_us",
    "downtime_us",
    # Page counts
    "total_pages",
    "fault_pages",
    "linear_pages",
    # Derived
    "throughput_mibs",
    "fault_fraction_pct",
    # Full snapshot specific (ms)
    "full_pause_ms",
    "full_create_ms",
    "full_total_ms",
    "full_throughput_mibs",
    # Restore
    "restore_api_ms",
    "ssh_ready_ms",
    # Host
    "rss_pre_kib",
    "rss_peak_kib",
    "mem_file_bytes",
    # Guest workload
    "workload_baseline_mibs",
    "workload_during_mibs",
    "workload_degradation_pct",
    "actual_write_rate_mibs",
    # Application workloads (Redis / Memcached) — per-window
    "app_baseline_ops", "app_baseline_avg_us",
    "app_baseline_p50_us", "app_baseline_p99_us", "app_baseline_p999_us",
    "app_during_ops", "app_during_avg_us",
    "app_during_p50_us",  "app_during_p99_us",  "app_during_p999_us",
    "app_ops_degradation_pct",
    # Post-snapshot measurements
    "post_snap_ops", "post_snap_avg_us",
    "post_snap_p50_us", "post_snap_p99_us", "post_snap_p999_us",
    "post_snap_throughput_mibs",
    # Overall run aggregates (across pre/during/post windows)
    "overall_ops_mean", "overall_ops_stddev",
    "overall_ops_min", "overall_ops_max",
    "overall_avg_latency_us_mean", "overall_avg_latency_us_stddev",
    "overall_p99_us_mean", "overall_p99_us_stddev",
    "overall_throughput_mean_mibs", "overall_throughput_stddev_mibs",
    "overall_triad_mean_mibs", "overall_triad_stddev_mibs",
    # STREAM benchmark — per-window
    "stream_baseline_copy_mibs",  "stream_baseline_scale_mibs",
    "stream_baseline_add_mibs",   "stream_baseline_triad_mibs",
    "stream_during_copy_mibs",    "stream_during_scale_mibs",
    "stream_during_add_mibs",     "stream_during_triad_mibs",
    "stream_triad_degradation_pct",
    "stream_post_copy_mibs", "stream_post_scale_mibs",
    "stream_post_add_mibs",  "stream_post_triad_mibs",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_overall_stats(values):
    """Compute (mean, stddev, min, max) from a list of finite floats.

    Filters out None, NaN, and non-finite values before computing. Returns
    (0.0, 0.0, 0.0, 0.0) if fewer than one valid value remains; stddev is
    0.0 when fewer than two valid values are present.
    """
    valid = [v for v in values if v is not None and math.isfinite(v)]
    if not valid:
        return 0.0, 0.0, 0.0, 0.0
    mean = statistics.mean(valid)
    stddev = statistics.stdev(valid) if len(valid) >= 2 else 0.0
    return mean, stddev, min(valid), max(valid)


def _parse_stream_log_all_runs(log_text):
    """Parse all completed STREAM benchmark runs from a cumulative log.

    Each run prints Copy/Scale/Add/Triad lines. Returns a list of dicts
    with keys "copy", "scale", "add", "triad" in MiB/s (converted from
    the MB/s values STREAM reports).
    """
    runs = []
    mb_to_mib = 1e6 / (1024 * 1024)
    # Split log into individual invocation blocks on the STREAM header line.
    blocks = re.split(r"-------------------------------------------------------------", log_text)
    for block in blocks:
        result = {}
        for kernel in ("Copy", "Scale", "Add", "Triad"):
            m = re.search(rf"^{kernel}:\s+([\d.]+)", block, re.MULTILINE | re.IGNORECASE)
            if m:
                result[kernel.lower()] = float(m.group(1)) * mb_to_mib
        if len(result) == 4:
            runs.append(result)
    return runs


def _parse_live_snapshot_log(log_data):
    """Extract timing metrics from Firecracker live-snapshot log lines.

    Returns a dict with timing values in microseconds.
    """
    metrics = {}

    m = re.search(r"populate_pages took (\d+) us", log_data)
    if m:
        metrics["populate_pages_us"] = int(m.group(1))

    m = re.search(r"Phase 1 took (\d+) us", log_data)
    if m:
        metrics["phase1_us"] = int(m.group(1))

    m = re.search(
        r"Phase 2 \(freeze\) took (\d+) us "
        r"\(pause=(\d+) us, save_state=(\d+) us, "
        r"wp_enable=(\d+) us, resume=(\d+) us\)",
        log_data,
    )
    if m:
        metrics["freeze_us"] = int(m.group(1))
        metrics["pause_us"] = int(m.group(2))
        metrics["save_state_us"] = int(m.group(3))
        metrics["wp_enable_us"] = int(m.group(4))
        metrics["resume_us"] = int(m.group(5))

    m = re.search(
        r"Phase 3 \(stream\) took (\d+) us, (\d+) pages total "
        r"\((\d+) fault-driven, (\d+) linear-scan\)",
        log_data,
    )
    if m:
        metrics["stream_us"] = int(m.group(1))
        metrics["total_pages"] = int(m.group(2))
        metrics["fault_pages"] = int(m.group(3))
        metrics["linear_pages"] = int(m.group(4))

    m = re.search(r"Phase 4 \(finalize\) took (\d+) us", log_data)
    if m:
        metrics["finalize_us"] = int(m.group(1))

    m = re.search(
        r"Live snapshot: complete in (\d+) us \(freeze/downtime=(\d+) us\)",
        log_data,
    )
    if m:
        metrics["total_us"] = int(m.group(1))
        metrics["downtime_us"] = int(m.group(2))

    return metrics


def _get_rss_kib(pid):
    """Read current RSS from /proc/<pid>/status in KiB."""
    try:
        status = Path(f"/proc/{pid}/status").read_text("utf-8")
        m = re.search(r"VmRSS:\s+(\d+)\s+kB", status)
        if m:
            return int(m.group(1))
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _get_peak_rss_kib(pid):
    """Read peak RSS (VmHWM) from /proc/<pid>/status in KiB."""
    try:
        status = Path(f"/proc/{pid}/status").read_text("utf-8")
        m = re.search(r"VmHWM:\s+(\d+)\s+kB", status)
        if m:
            return int(m.group(1))
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _do_full_snapshot_timed(vm):
    """Take a full snapshot, returning (snapshot, timing_dict)."""
    t0 = time.monotonic()
    vm.pause()
    t_paused = time.monotonic()
    vm.api.snapshot_create.put(
        mem_file_path="mem",
        snapshot_path="vmstate",
        snapshot_type="Full",
    )
    t_created = time.monotonic()

    root = Path(vm.chroot())
    snapshot = Snapshot(
        vmstate=root / "vmstate",
        mem=root / "mem",
        disks=vm.disks,
        net_ifaces=[x["iface"] for _, x in vm.iface.items()],
        ssh_key=vm.ssh_key,
        snapshot_type=SnapshotType.FULL,
        meta={
            "kernel_file": str(vm.kernel_file),
            "vcpus_count": vm.vcpus_count,
        },
    )

    timings = {
        "full_pause_ms": (t_paused - t0) * 1000,
        "full_create_ms": (t_created - t_paused) * 1000,
        "full_total_ms": (t_created - t0) * 1000,
    }
    return snapshot, timings


def _do_restore_timed(factory, snapshot):
    """Restore a snapshot, returning (vm, timing_dict)."""
    rvm = factory.build(monitor_memory=False)
    rvm.spawn()

    t0 = time.monotonic()
    rvm.restore_from_snapshot(snapshot, resume=True)
    t_restored = time.monotonic()

    rvm.ssh.check_output("true")
    t_ssh = time.monotonic()

    timings = {
        "restore_api_ms": (t_restored - t0) * 1000,
        "ssh_ready_ms": (t_ssh - t0) * 1000,
    }
    return rvm, timings


def _start_workload(vm, workload):
    """Start a controlled-rate memory write workload inside the guest.

    Returns the measured baseline write rate in MiB/s, or 0.0 for idle.
    """
    if workload == "idle":
        return 0.0

    bs, count, sleep_s = WORKLOAD_PARAMS[workload]

    # Calibrate: run one burst and measure throughput.
    total_bytes = bs * count
    _, stdout, _ = vm.ssh.check_output(
        f"dd if=/dev/urandom of=/tmp/calibrate bs={bs} count={count} 2>&1 "
        "| tail -1",
        timeout=30,
    )
    # Parse dd output for throughput (e.g. "... 1048576 bytes ... copied, 0.123 s, 8.1 MB/s")
    baseline_mibs = 0.0
    m = re.search(r"([\d.]+)\s+s,", stdout)
    if m:
        elapsed = float(m.group(1))
        if elapsed > 0:
            baseline_mibs = (total_bytes / (1024 * 1024)) / elapsed

    # Clean up calibration file.
    vm.ssh.check_output("rm -f /tmp/calibrate")

    # Start the continuous workload in the background.
    vm.ssh.check_output(
        f"nohup sh -c '"
        f"while true; do "
        f"  dd if=/dev/urandom of=/tmp/workload bs={bs} count={count} 2>/dev/null; "
        f"  sleep {sleep_s}; "
        f"done' </dev/null >/dev/null 2>&1 &"
    )

    # Let the workload stabilise.
    time.sleep(2)
    return baseline_mibs


def _measure_workload_throughput(vm, workload):
    """Measure current write throughput inside the guest.

    Runs a single timed burst matching the workload parameters.
    Returns throughput in MiB/s, or 0.0 for idle.
    """
    if workload == "idle":
        return 0.0

    bs, count, _ = WORKLOAD_PARAMS[workload]
    total_bytes = bs * count

    _, stdout, _ = vm.ssh.check_output(
        f"dd if=/dev/urandom of=/tmp/measure bs={bs} count={count} 2>&1 "
        "| tail -1",
        timeout=30,
    )
    vm.ssh.check_output("rm -f /tmp/measure")

    m = re.search(r"([\d.]+)\s+s,", stdout)
    if m:
        elapsed = float(m.group(1))
        if elapsed > 0:
            return (total_bytes / (1024 * 1024)) / elapsed
    return 0.0


def _stop_workload(vm):
    """Kill any background dd/sh workload processes in the guest."""
    vm.ssh.check_output("pkill -f 'dd if=/dev/urandom' 2>/dev/null || true")
    vm.ssh.check_output("pkill -f 'of=/tmp/workload' 2>/dev/null || true")


# ---------------------------------------------------------------------------
# Application workload classification helpers
# ---------------------------------------------------------------------------


def _is_redis_workload(wl):
    """Return True if the workload name is a Redis variant."""
    return wl in REDIS_WORKLOAD_PARAMS


def _is_memcached_workload(wl):
    """Return True if the workload name is a Memcached variant."""
    return wl in MEMCACHED_WORKLOAD_PARAMS


def _is_stream_workload(wl):
    """Return True if the workload is the STREAM benchmark."""
    return wl == "stream"


# ---------------------------------------------------------------------------
# VM boot for application workloads
# ---------------------------------------------------------------------------


def _boot_app_experiment_vm(uvm_plain, microvm_factory, mem_size_mib):
    """Boot a VM for application workload experiments.

    If EXPERIMENT_ROOTFS env var is set, builds a fresh VM using that rootfs.
    Otherwise uses uvm_plain directly.  Disables memory_monitor, spawns,
    configures with VCPU_COUNT vCPUs and mem_size_mib RAM, adds a net iface,
    and waits until SSH is ready.
    """
    if os.environ.get("EXPERIMENT_ROOTFS"):
        vm = microvm_factory.build(
            kernel=uvm_plain.kernel_file,
            rootfs=Path(os.environ["EXPERIMENT_ROOTFS"]),
        )
    else:
        vm = uvm_plain

    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None

    vm.spawn()
    vm.basic_config(vcpu_count=VCPU_COUNT, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    return vm


# ---------------------------------------------------------------------------
# Tool availability check
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Redis helpers
# ---------------------------------------------------------------------------


def _setup_redis(vm, mem_size_mib):
    """Start redis-server and pre-populate it.

    Allocates half of guest RAM as Redis maxmemory (allkeys-lru), then
    pre-populates roughly 50 % of that budget with 512-byte string values.
    Returns redis_maxmem in MiB.
    """
    redis_maxmem = mem_size_mib // 2

    vm.ssh.check_output(
        f"redis-server --daemonize yes "
        f"--maxmemory {redis_maxmem}mb "
        f"--maxmemory-policy allkeys-lru "
        f"--save '' --appendonly no"
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


# ---------------------------------------------------------------------------
# Memcached helpers
# ---------------------------------------------------------------------------


def _setup_memcached(vm, mem_size_mib):
    """Start memcached and pre-populate it.

    Uses half guest RAM, 2 threads, port 11211.  Pre-populates all keys.
    """
    mem_alloc = mem_size_mib // 2

    # Run memcached in the background via nohup rather than with -d, because
    # memcached on Ubuntu 24.04 may refuse to daemonize when already running as
    # root inside the guest.  Use setsid to fully detach from the SSH session.
    vm.ssh.check_output(
        f"setsid /usr/bin/memcached -m {mem_alloc} -t 2 -p 11211 "
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


# ---------------------------------------------------------------------------
# STREAM helpers
# ---------------------------------------------------------------------------


def _parse_stream_output(output):
    """Parse STREAM benchmark output into a dict of MiB/s values.

    Converts MB/s → MiB/s (×1e6 / 1024²).
    Returns {"copy": X, "scale": X, "add": X, "triad": X}.
    """
    results = {}
    mb_to_mib = 1e6 / (1024 * 1024)
    for kernel in ("Copy", "Scale", "Add", "Triad"):
        m = re.search(rf"{kernel}:\s+([\d.]+)", output, re.IGNORECASE)
        if m:
            results[kernel.lower()] = float(m.group(1)) * mb_to_mib
    return results


def _run_stream_benchmark(vm):
    """Execute /usr/local/bin/stream and return (copy, scale, add, triad) in MiB/s."""
    _, out, _ = vm.ssh.check_output("/usr/local/bin/stream", timeout=120)
    r = _parse_stream_output(out)
    return r.get("copy", 0.0), r.get("scale", 0.0), r.get("add", 0.0), r.get("triad", 0.0)


def _start_stream_during_burst(vm):
    """Launch a STREAM benchmark burst in the background.

    Writes output to /tmp/stream_during.log and touches /tmp/stream_during.done.
    """
    vm.ssh.check_output(
        "nohup sh -c '"
        "/usr/local/bin/stream > /tmp/stream_during.log 2>&1; "
        "touch /tmp/stream_during.done' "
        "</dev/null >/dev/null 2>&1 &"
    )


# ---------------------------------------------------------------------------
# Common utilities for application workloads
# ---------------------------------------------------------------------------


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


def _log_app_summary(row):
    """Log a human-readable summary of application workload metrics."""
    mode = row["snapshot_mode"]
    mem  = row["mem_size_mib"]
    wl   = row["workload"]
    it   = row["iteration"]

    logger.info("=" * 70)
    logger.info("APP RUN: %s MiB, %s workload, %s snapshot, iteration %s", mem, wl, mode, it)
    logger.info("-" * 70)

    if _is_redis_workload(wl) or _is_memcached_workload(wl):
        logger.info(
            "  Baseline ops/sec:       %8.0f  (avg lat: %.0f µs)",
            row.get("app_baseline_ops", 0),
            row.get("app_baseline_avg_us", 0),
        )
        logger.info(
            "  During-snap ops/sec:    %8.0f  (avg lat: %.0f µs)",
            row.get("app_during_ops", 0),
            row.get("app_during_avg_us", 0),
        )
        logger.info(
            "  Post-snap  ops/sec:     %8.0f  (avg lat: %.0f µs)",
            row.get("post_snap_ops", 0),
            row.get("post_snap_avg_us", 0),
        )
        logger.info(
            "  Ops degradation:        %8.1f %%", row.get("app_ops_degradation_pct", 0)
        )
        logger.info(
            "  Overall ops mean/stddev: %.0f / %.0f ops/s",
            row.get("overall_ops_mean", 0),
            row.get("overall_ops_stddev", 0),
        )
        logger.info(
            "  Overall avg lat mean/stddev: %.0f / %.0f µs",
            row.get("overall_avg_latency_us_mean", 0),
            row.get("overall_avg_latency_us_stddev", 0),
        )
        logger.info(
            "  Overall p99  mean/stddev: %.0f / %.0f µs",
            row.get("overall_p99_us_mean", 0),
            row.get("overall_p99_us_stddev", 0),
        )
        logger.info(
            "  Baseline latency avg/p50/p99/p999: %.0f / %.0f / %.0f / %.0f µs",
            row.get("app_baseline_avg_us", 0),
            row.get("app_baseline_p50_us", 0),
            row.get("app_baseline_p99_us", 0),
            row.get("app_baseline_p999_us", 0),
        )
        logger.info(
            "  During   latency avg/p50/p99/p999: %.0f / %.0f / %.0f / %.0f µs",
            row.get("app_during_avg_us", 0),
            row.get("app_during_p50_us", 0),
            row.get("app_during_p99_us", 0),
            row.get("app_during_p999_us", 0),
        )
        logger.info(
            "  Post-snap latency avg/p50/p99/p999: %.0f / %.0f / %.0f / %.0f µs",
            row.get("post_snap_avg_us", 0),
            row.get("post_snap_p50_us", 0),
            row.get("post_snap_p99_us", 0),
            row.get("post_snap_p999_us", 0),
        )
    elif _is_stream_workload(wl):
        logger.info(
            "  Baseline Copy/Scale/Add/Triad: %.0f / %.0f / %.0f / %.0f MiB/s",
            row.get("stream_baseline_copy_mibs", 0),
            row.get("stream_baseline_scale_mibs", 0),
            row.get("stream_baseline_add_mibs", 0),
            row.get("stream_baseline_triad_mibs", 0),
        )
        logger.info(
            "  During   Copy/Scale/Add/Triad: %.0f / %.0f / %.0f / %.0f MiB/s",
            row.get("stream_during_copy_mibs", 0),
            row.get("stream_during_scale_mibs", 0),
            row.get("stream_during_add_mibs", 0),
            row.get("stream_during_triad_mibs", 0),
        )
        logger.info(
            "  Post-snap Copy/Scale/Add/Triad: %.0f / %.0f / %.0f / %.0f MiB/s",
            row.get("stream_post_copy_mibs", 0),
            row.get("stream_post_scale_mibs", 0),
            row.get("stream_post_add_mibs", 0),
            row.get("stream_post_triad_mibs", 0),
        )
        logger.info(
            "  Triad degradation:      %8.1f %%", row.get("stream_triad_degradation_pct", 0)
        )
        logger.info(
            "  Overall Triad mean/stddev: %.0f / %.0f MiB/s",
            row.get("overall_triad_mean_mibs", 0),
            row.get("overall_triad_stddev_mibs", 0),
        )

    logger.info(
        "  RSS pre/peak:           %s / %s KiB",
        row.get("rss_pre_kib", "?"),
        row.get("rss_peak_kib", "?"),
    )
    logger.info(
        "  Mem file size:          %s bytes", row.get("mem_file_bytes", "?")
    )
    logger.info("=" * 70)


def _write_csv_row(row):
    """Append a single row to the experiment CSV, creating it if needed."""
    file_exists = os.path.isfile(RESULTS_FILE)

    with open(RESULTS_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def _log_summary(row):
    """Log a human-readable summary of one experiment run."""
    mode = row["snapshot_mode"]
    mem = row["mem_size_mib"]
    wl = row["workload"]
    it = row["iteration"]

    logger.info("=" * 70)
    logger.info(
        "RUN: %s MiB, %s workload, %s snapshot, iteration %s",
        mem, wl, mode, it,
    )
    logger.info("-" * 70)

    if mode == "live":
        logger.info(
            "  Phase 1 (prepare):      %8.1f ms  [populate: %.1f ms]",
            row.get("phase1_us", 0) / 1000,
            row.get("populate_pages_us", 0) / 1000,
        )
        logger.info(
            "  Phase 2 (freeze/DT):    %8.1f ms",
            row.get("downtime_us", 0) / 1000,
        )
        logger.info(
            "    pause=%7.1f ms  save_state=%7.1f ms  wp_enable=%7.1f ms  resume=%7.1f ms",
            row.get("pause_us", 0) / 1000,
            row.get("save_state_us", 0) / 1000,
            row.get("wp_enable_us", 0) / 1000,
            row.get("resume_us", 0) / 1000,
        )
        logger.info(
            "  Phase 3 (stream):       %8.1f ms  [%s pages, %.0f MiB/s]",
            row.get("stream_us", 0) / 1000,
            row.get("total_pages", "?"),
            row.get("throughput_mibs", 0),
        )
        logger.info(
            "    fault-driven: %s (%.2f%%)  linear: %s",
            row.get("fault_pages", "?"),
            row.get("fault_fraction_pct", 0),
            row.get("linear_pages", "?"),
        )
        logger.info(
            "  Phase 4 (finalize):     %8.1f ms",
            row.get("finalize_us", 0) / 1000,
        )
        logger.info(
            "  TOTAL wall-clock:       %8.1f ms",
            row.get("total_us", 0) / 1000,
        )
        logger.info(
            "  VM DOWNTIME:            %8.1f ms",
            row.get("downtime_us", 0) / 1000,
        )
    else:
        logger.info(
            "  Pause:                  %8.1f ms", row.get("full_pause_ms", 0)
        )
        logger.info(
            "  Create (mem dump):      %8.1f ms", row.get("full_create_ms", 0)
        )
        logger.info(
            "  TOTAL (= DOWNTIME):     %8.1f ms", row.get("full_total_ms", 0)
        )
        logger.info(
            "  Throughput:             %8.0f MiB/s", row.get("full_throughput_mibs", 0)
        )

    logger.info(
        "  Restore API:            %8.1f ms", row.get("restore_api_ms", 0)
    )
    logger.info(
        "  Restore SSH ready:      %8.1f ms", row.get("ssh_ready_ms", 0)
    )
    logger.info(
        "  RSS pre/peak:           %s / %s KiB",
        row.get("rss_pre_kib", "?"),
        row.get("rss_peak_kib", "?"),
    )
    logger.info(
        "  Mem file size:          %s bytes", row.get("mem_file_bytes", "?")
    )

    if wl != "idle":
        logger.info(
            "  Workload baseline:      %8.1f MiB/s", row.get("workload_baseline_mibs", 0)
        )
        logger.info(
            "  Workload during snap:   %8.1f MiB/s", row.get("workload_during_mibs", 0)
        )
        logger.info(
            "  Workload post-snap:     %8.1f MiB/s", row.get("post_snap_throughput_mibs", 0)
        )
        logger.info(
            "  Workload degradation:   %8.1f %%", row.get("workload_degradation_pct", 0)
        )
        logger.info(
            "  Overall throughput mean/stddev: %.1f / %.1f MiB/s",
            row.get("overall_throughput_mean_mibs", 0),
            row.get("overall_throughput_stddev_mibs", 0),
        )

    logger.info("=" * 70)


# ---------------------------------------------------------------------------
# Experiment: Full snapshot path
# ---------------------------------------------------------------------------


def _run_full_snapshot(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one full-snapshot experiment run. Returns the result row dict."""
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib": mem_size_mib,
        "workload": workload,
        "snapshot_mode": "full",
        "iteration": iteration,
    }

    # Start workload and record baseline.
    baseline_mibs = _start_workload(vm, workload)
    row["workload_baseline_mibs"] = round(baseline_mibs, 2)
    row["actual_write_rate_mibs"] = round(baseline_mibs, 2)

    # Pre-snapshot host metrics.
    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    # Take full snapshot (pauses VM).
    snapshot, timings = _do_full_snapshot_timed(vm)
    row.update(timings)

    # Full snapshot: downtime == total time.
    row["downtime_us"] = int(timings["full_total_ms"] * 1000)
    row["total_us"] = int(timings["full_total_ms"] * 1000)

    # Throughput.
    create_s = timings["full_create_ms"] / 1000
    if create_s > 0:
        row["full_throughput_mibs"] = round(mem_size_mib / create_s, 1)
    else:
        row["full_throughput_mibs"] = 0

    # Post-snapshot host metrics.
    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    # Memory file size.
    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    # Restore.
    rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
    row.update(restore_timings)

    # Full snapshot: VM was paused, so no workload served during snapshot.
    row["workload_during_mibs"] = 0
    row["workload_degradation_pct"] = 0

    # Post-snapshot: measure throughput on the restored VM (which resumed the
    # workload process from the pre-pause state).
    post_snap_mibs = _measure_workload_throughput(rvm, workload)
    row["post_snap_throughput_mibs"] = round(post_snap_mibs, 2)

    # Overall stats across [baseline, during=0 (paused), post] windows.
    overall_vals = [baseline_mibs, 0.0, post_snap_mibs]
    mean, stddev, _, _ = _compute_overall_stats(overall_vals)
    row["overall_throughput_mean_mibs"] = round(mean, 2)
    row["overall_throughput_stddev_mibs"] = round(stddev, 2)

    # Clean up restored VM.
    rvm.kill()

    # Attach snapshot object for callers that need to restore from it
    # (not written to CSV — extrasaction="ignore" skips unknown keys).
    row["_snapshot"] = snapshot

    return row


# ---------------------------------------------------------------------------
# Experiment: Live snapshot path
# ---------------------------------------------------------------------------


def _run_live_snapshot(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one live-snapshot experiment run. Returns the result row dict."""
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib": mem_size_mib,
        "workload": workload,
        "snapshot_mode": "live",
        "iteration": iteration,
    }

    # Start workload and record baseline.
    baseline_mibs = _start_workload(vm, workload)
    row["workload_baseline_mibs"] = round(baseline_mibs, 2)
    row["actual_write_rate_mibs"] = round(baseline_mibs, 2)

    # Pre-snapshot host metrics.
    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    # Take live snapshot (VM keeps running).
    assert vm.state == "Running"
    snapshot = vm.snapshot_live()
    assert vm.state == "Running"

    # Post-snapshot host metrics.
    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    # Memory file size.
    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    # Parse Firecracker log for detailed phase breakdown.
    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)

    # Derived metrics.
    total_pages = live_metrics.get("total_pages", 0)
    fault_pages = live_metrics.get("fault_pages", 0)
    stream_us = live_metrics.get("stream_us", 0)

    if total_pages > 0:
        row["fault_fraction_pct"] = round(fault_pages / total_pages * 100, 3)
    else:
        row["fault_fraction_pct"] = 0

    if stream_us > 0:
        mem_bytes = total_pages * 4096
        row["throughput_mibs"] = round(
            (mem_bytes / (1024 * 1024)) / (stream_us / 1e6), 1
        )
    else:
        row["throughput_mibs"] = 0

    # Measure guest-visible write throughput during/after live snapshot
    # (the workload was running throughout).
    if workload != "idle":
        during_mibs = _measure_workload_throughput(vm, workload)
        row["workload_during_mibs"] = round(during_mibs, 2)
        if baseline_mibs > 0:
            row["workload_degradation_pct"] = round(
                (1 - during_mibs / baseline_mibs) * 100, 1
            )
        else:
            row["workload_degradation_pct"] = 0

        # Post-snapshot: a third measurement after the snapshot has fully
        # completed to check whether throughput recovers.
        post_snap_mibs = _measure_workload_throughput(vm, workload)
        row["post_snap_throughput_mibs"] = round(post_snap_mibs, 2)

        # Overall stats across [baseline, during, post] windows.
        overall_vals = [baseline_mibs, during_mibs, post_snap_mibs]
        mean, stddev, _, _ = _compute_overall_stats(overall_vals)
        row["overall_throughput_mean_mibs"] = round(mean, 2)
        row["overall_throughput_stddev_mibs"] = round(stddev, 2)
    else:
        row["workload_during_mibs"] = 0
        row["workload_degradation_pct"] = 0
        row["post_snap_throughput_mibs"] = 0
        row["overall_throughput_mean_mibs"] = 0
        row["overall_throughput_stddev_mibs"] = 0

    # VM should still be responsive.
    vm.ssh.check_output("true")

    # Restore from the live snapshot.
    rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
    row.update(restore_timings)
    rvm.ssh.check_output("true")
    rvm.kill()

    return row


# ---------------------------------------------------------------------------
# Experiment: Full snapshot path — application workloads
# ---------------------------------------------------------------------------


def _run_full_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one full-snapshot experiment run for an application workload.

    The VM is paused for the entire snapshot, so during-snapshot metrics are
    always zero / 100 % degradation.
    """
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib": mem_size_mib,
        "workload": workload,
        "snapshot_mode": "full",
        "iteration": iteration,
    }

    ops = avg_us = p50 = p99 = p999 = 0.0

    if _is_redis_workload(workload):
        _setup_redis(vm, mem_size_mib)
        ops, avg_us, p50, p99, p999 = _measure_redis_baseline(vm, workload)
        _start_redis_background_workload(vm, workload)
    elif _is_memcached_workload(workload):
        _setup_memcached(vm, mem_size_mib)
        ops, avg_us, p50, p99, p999 = _measure_memcached_baseline(vm, workload)
        _start_memcached_background_workload(vm, workload)
    elif _is_stream_workload(workload):
        b_copy, b_scale, b_add, b_triad = _run_stream_benchmark(vm)
        row["stream_baseline_copy_mibs"]  = round(b_copy, 1)
        row["stream_baseline_scale_mibs"] = round(b_scale, 1)
        row["stream_baseline_add_mibs"]   = round(b_add, 1)
        row["stream_baseline_triad_mibs"] = round(b_triad, 1)

    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        row["app_baseline_ops"]     = round(ops, 1)
        row["app_baseline_avg_us"]  = round(avg_us, 1)
        row["app_baseline_p50_us"]  = round(p50, 1)
        row["app_baseline_p99_us"]  = round(p99, 1)
        row["app_baseline_p999_us"] = round(p999, 1)

    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    # Take full snapshot — pauses the VM.
    snapshot, timings = _do_full_snapshot_timed(vm)
    row.update(timings)
    row["downtime_us"] = int(timings["full_total_ms"] * 1000)
    row["total_us"]    = int(timings["full_total_ms"] * 1000)

    create_s = timings["full_create_ms"] / 1000
    row["full_throughput_mibs"] = round(mem_size_mib / create_s, 1) if create_s > 0 else 0

    # VM is paused — no requests served during snapshot.
    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        row["app_during_ops"]          = 0
        row["app_during_avg_us"]       = 0
        row["app_during_p50_us"]       = 0
        row["app_during_p99_us"]       = 0
        row["app_during_p999_us"]      = 0
        row["app_ops_degradation_pct"] = 100.0
    elif _is_stream_workload(workload):
        row["stream_during_copy_mibs"]      = 0
        row["stream_during_scale_mibs"]     = 0
        row["stream_during_add_mibs"]       = 0
        row["stream_during_triad_mibs"]     = 0
        row["stream_triad_degradation_pct"] = 100.0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
    row.update(restore_timings)

    # Post-snapshot: measure on the restored VM (resumed from pre-pause state).
    if _is_redis_workload(workload):
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_redis(rvm, workload)
        row["post_snap_ops"]    = round(ps_ops, 1)
        row["post_snap_avg_us"] = round(ps_avg, 1)
        row["post_snap_p50_us"] = round(ps_p50, 1)
        row["post_snap_p99_us"] = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)
        # Overall stats: during=0 (paused), so use [baseline, 0, post].
        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, 0.0, ps_ops])
        mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, 0.0, ps_avg])
        mean_p99, std_p99, _, _ = _compute_overall_stats([p99, 0.0, ps_p99])
        row["overall_ops_mean"]             = round(mean_ops, 1)
        row["overall_ops_stddev"]           = round(std_ops, 1)
        row["overall_ops_min"]              = round(min_ops, 1)
        row["overall_ops_max"]              = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]  = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]          = round(mean_p99, 1)
        row["overall_p99_us_stddev"]        = round(std_p99, 1)
    elif _is_memcached_workload(workload):
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_memcached(rvm, workload)
        row["post_snap_ops"]    = round(ps_ops, 1)
        row["post_snap_avg_us"] = round(ps_avg, 1)
        row["post_snap_p50_us"] = round(ps_p50, 1)
        row["post_snap_p99_us"] = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)
        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, 0.0, ps_ops])
        mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, 0.0, ps_avg])
        mean_p99, std_p99, _, _ = _compute_overall_stats([p99, 0.0, ps_p99])
        row["overall_ops_mean"]             = round(mean_ops, 1)
        row["overall_ops_stddev"]           = round(std_ops, 1)
        row["overall_ops_min"]              = round(min_ops, 1)
        row["overall_ops_max"]              = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]  = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]          = round(mean_p99, 1)
        row["overall_p99_us_stddev"]        = round(std_p99, 1)
    elif _is_stream_workload(workload):
        ps_copy, ps_scale, ps_add, ps_triad = _run_stream_benchmark(rvm)
        row["stream_post_copy_mibs"]  = round(ps_copy, 1)
        row["stream_post_scale_mibs"] = round(ps_scale, 1)
        row["stream_post_add_mibs"]   = round(ps_add, 1)
        row["stream_post_triad_mibs"] = round(ps_triad, 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        # Overall triad across [baseline, 0 (paused), post].
        mean_tr, std_tr, _, _ = _compute_overall_stats([b_triad, 0.0, ps_triad])
        row["overall_triad_mean_mibs"]   = round(mean_tr, 1)
        row["overall_triad_stddev_mibs"] = round(std_tr, 1)

    rvm.kill()

    return row


# ---------------------------------------------------------------------------
# Experiment: Live snapshot path — application workloads
# ---------------------------------------------------------------------------


def _run_live_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one live-snapshot experiment run for an application workload.

    The VM keeps running during the snapshot, so we can measure real
    during-snapshot application performance.
    """
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib": mem_size_mib,
        "workload": workload,
        "snapshot_mode": "live",
        "iteration": iteration,
    }

    ops = avg_us = p50 = p99 = p999 = 0.0

    if _is_redis_workload(workload):
        _setup_redis(vm, mem_size_mib)
        ops, avg_us, p50, p99, p999 = _measure_redis_baseline(vm, workload)
        _start_redis_background_workload(vm, workload)
        _start_redis_during_burst(vm, workload, ops)
    elif _is_memcached_workload(workload):
        _setup_memcached(vm, mem_size_mib)
        ops, avg_us, p50, p99, p999 = _measure_memcached_baseline(vm, workload)
        _start_memcached_background_workload(vm, workload)
        _start_memcached_during_burst(vm, workload)
    elif _is_stream_workload(workload):
        b_copy, b_scale, b_add, b_triad = _run_stream_benchmark(vm)
        row["stream_baseline_copy_mibs"]  = round(b_copy, 1)
        row["stream_baseline_scale_mibs"] = round(b_scale, 1)
        row["stream_baseline_add_mibs"]   = round(b_add, 1)
        row["stream_baseline_triad_mibs"] = round(b_triad, 1)
        _start_stream_during_burst(vm)

    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        row["app_baseline_ops"]     = round(ops, 1)
        row["app_baseline_avg_us"]  = round(avg_us, 1)
        row["app_baseline_p50_us"]  = round(p50, 1)
        row["app_baseline_p99_us"]  = round(p99, 1)
        row["app_baseline_p999_us"] = round(p999, 1)

    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    # Take live snapshot — VM keeps running.
    assert vm.state == "Running"
    snapshot = vm.snapshot_live()
    assert vm.state == "Running"

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    # Parse Firecracker log for phase breakdown.
    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)

    total_pages = live_metrics.get("total_pages", 0)
    fault_pages = live_metrics.get("fault_pages", 0)
    stream_us   = live_metrics.get("stream_us", 0)

    row["fault_fraction_pct"] = (
        round(fault_pages / total_pages * 100, 3) if total_pages > 0 else 0
    )
    if stream_us > 0:
        mem_bytes = total_pages * 4096
        row["throughput_mibs"] = round(
            (mem_bytes / (1024 * 1024)) / (stream_us / 1e6), 1
        )
    else:
        row["throughput_mibs"] = 0

    # Wait for during-snapshot benchmark to complete and collect results.
    if _is_redis_workload(workload):
        _wait_for_sentinel(vm, "/tmp/redis_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/redis_during.log")
        d_ops, d_avg, d_p50, d_p99, d_p999 = _parse_redis_benchmark_output(log_out)
        row["app_during_ops"]    = round(d_ops, 1)
        row["app_during_avg_us"] = round(d_avg, 1)
        row["app_during_p50_us"] = round(d_p50, 1)
        row["app_during_p99_us"] = round(d_p99, 1)
        row["app_during_p999_us"] = round(d_p999, 1)
        row["app_ops_degradation_pct"] = (
            round((1 - d_ops / ops) * 100, 1) if ops > 0 else 0
        )

        # Post-snapshot: measure recovery on the still-running source VM.
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_redis(vm, workload)
        row["post_snap_ops"]     = round(ps_ops, 1)
        row["post_snap_avg_us"]  = round(ps_avg, 1)
        row["post_snap_p50_us"]  = round(ps_p50, 1)
        row["post_snap_p99_us"]  = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)

        # Overall stats across [baseline, during, post] windows.
        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, d_ops, ps_ops])
        mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, d_avg, ps_avg])
        mean_p99, std_p99, _, _ = _compute_overall_stats([p99, d_p99, ps_p99])
        row["overall_ops_mean"]              = round(mean_ops, 1)
        row["overall_ops_stddev"]            = round(std_ops, 1)
        row["overall_ops_min"]               = round(min_ops, 1)
        row["overall_ops_max"]               = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]   = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]           = round(mean_p99, 1)
        row["overall_p99_us_stddev"]         = round(std_p99, 1)

    elif _is_memcached_workload(workload):
        _wait_for_sentinel(vm, "/tmp/memtier_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/memtier_during.log")
        d_ops, d_avg, d_p50, d_p99, d_p999 = _parse_memtier_output(log_out)
        row["app_during_ops"]    = round(d_ops, 1)
        row["app_during_avg_us"] = round(d_avg, 1)
        row["app_during_p50_us"] = round(d_p50, 1)
        row["app_during_p99_us"] = round(d_p99, 1)
        row["app_during_p999_us"] = round(d_p999, 1)
        row["app_ops_degradation_pct"] = (
            round((1 - d_ops / ops) * 100, 1) if ops > 0 else 0
        )

        # Post-snapshot: measure recovery.
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_memcached(vm, workload)
        row["post_snap_ops"]     = round(ps_ops, 1)
        row["post_snap_avg_us"]  = round(ps_avg, 1)
        row["post_snap_p50_us"]  = round(ps_p50, 1)
        row["post_snap_p99_us"]  = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)

        # Also read the long-running background memtier log for overall stats.
        _, overall_log, _ = vm.ssh.check_output(
            "cat /tmp/memtier_overall.log 2>/dev/null || true"
        )
        ov_ops, ov_avg, _, ov_p99, _ = _parse_memtier_output(overall_log)

        # Use the overall log if it parsed successfully; otherwise fall back to
        # the mean of the three window measurements.
        if ov_ops > 0:
            row["overall_ops_mean"]              = round(ov_ops, 1)
            row["overall_avg_latency_us_mean"]   = round(ov_avg, 1)
            row["overall_p99_us_mean"]           = round(ov_p99, 1)
            # stddev not available from the summary Totals line.
            row["overall_ops_stddev"]            = 0
            row["overall_avg_latency_us_stddev"] = 0
            row["overall_p99_us_stddev"]         = 0
            row["overall_ops_min"]               = 0
            row["overall_ops_max"]               = 0
        else:
            mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, d_ops, ps_ops])
            mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, d_avg, ps_avg])
            mean_p99, std_p99, _, _ = _compute_overall_stats([p99, d_p99, ps_p99])
            row["overall_ops_mean"]              = round(mean_ops, 1)
            row["overall_ops_stddev"]            = round(std_ops, 1)
            row["overall_ops_min"]               = round(min_ops, 1)
            row["overall_ops_max"]               = round(max_ops, 1)
            row["overall_avg_latency_us_mean"]   = round(mean_avg, 1)
            row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
            row["overall_p99_us_mean"]           = round(mean_p99, 1)
            row["overall_p99_us_stddev"]         = round(std_p99, 1)

    elif _is_stream_workload(workload):
        _wait_for_sentinel(vm, "/tmp/stream_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/stream_during.log")
        d = _parse_stream_output(log_out)
        d_copy  = d.get("copy", 0.0)
        d_scale = d.get("scale", 0.0)
        d_add   = d.get("add", 0.0)
        d_triad = d.get("triad", 0.0)
        row["stream_during_copy_mibs"]  = round(d_copy, 1)
        row["stream_during_scale_mibs"] = round(d_scale, 1)
        row["stream_during_add_mibs"]   = round(d_add, 1)
        row["stream_during_triad_mibs"] = round(d_triad, 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        row["stream_triad_degradation_pct"] = (
            round((1 - d_triad / b_triad) * 100, 1) if b_triad > 0 else 0
        )

        # Post-snapshot: run a fresh STREAM benchmark on the still-running VM.
        ps_copy, ps_scale, ps_add, ps_triad = _run_stream_benchmark(vm)
        row["stream_post_copy_mibs"]  = round(ps_copy, 1)
        row["stream_post_scale_mibs"] = round(ps_scale, 1)
        row["stream_post_add_mibs"]   = round(ps_add, 1)
        row["stream_post_triad_mibs"] = round(ps_triad, 1)

        # Parse every completed STREAM run from the background log for overall stats.
        _, stream_all_log, _ = vm.ssh.check_output(
            "cat /tmp/stream.log 2>/dev/null || true"
        )
        all_runs = _parse_stream_log_all_runs(stream_all_log)
        all_triads = [r["triad"] for r in all_runs if "triad" in r]
        mean_tr, std_tr, _, _ = _compute_overall_stats(all_triads)
        row["overall_triad_mean_mibs"]   = round(mean_tr, 1)
        row["overall_triad_stddev_mibs"] = round(std_tr, 1)

    # VM should still be responsive.
    vm.ssh.check_output("true")

    # Restore from the live snapshot.
    rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
    row.update(restore_timings)
    rvm.ssh.check_output("true")
    rvm.kill()

    return row


# ---------------------------------------------------------------------------
# Boot and condition a VM for the experiment
# ---------------------------------------------------------------------------


def _boot_experiment_vm(uvm_plain, mem_size_mib):
    """Boot a VM with the given memory size and condition its memory.

    Returns the running, SSH-ready VM.
    """
    vm = uvm_plain

    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None

    vm.spawn()
    vm.basic_config(vcpu_count=VCPU_COUNT, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()

    # Wait for SSH.
    vm.ssh.check_output("true")

    # Condition memory: populate ~25% of guest RAM so there are backed pages.
    prefill_mib = max(mem_size_mib // 4, 16)
    vm.ssh.check_output(
        f"head -c {prefill_mib}M /dev/urandom > /tmp/prefill 2>/dev/null; sync",
        timeout=120,
    )

    return vm


# ---------------------------------------------------------------------------
# Parametrized experiment tests
# ---------------------------------------------------------------------------


@pytest.mark.nonci
@pytest.mark.timeout(300)
@pytest.mark.parametrize("mem_size_mib", [256, 512, 1024, 2048, 4096])
@pytest.mark.parametrize("workload", ["idle", "light", "medium", "heavy"])
@pytest.mark.parametrize("iteration", range(10))
def test_full_snapshot_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect full-snapshot metrics under controlled memory workload."""
    vm = _boot_experiment_vm(uvm_plain, mem_size_mib)

    row = _run_full_snapshot(vm, microvm_factory, mem_size_mib, workload, iteration)

    # Attach key metrics to JUnit XML.
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("total_us", row.get("total_us", 0))
    record_property("full_throughput_mibs", row.get("full_throughput_mibs", 0))
    record_property("restore_api_ms", row.get("restore_api_ms", 0))

    _write_csv_row(row)
    _log_summary(row)


@pytest.mark.nonci
@pytest.mark.timeout(300)
@pytest.mark.parametrize("mem_size_mib", [256, 512, 1024, 2048, 4096])
@pytest.mark.parametrize("workload", ["idle", "light", "medium", "heavy"])
@pytest.mark.parametrize("iteration", range(10))
def test_live_snapshot_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect live-snapshot metrics under controlled memory workload."""
    vm = _boot_experiment_vm(uvm_plain, mem_size_mib)

    row = _run_live_snapshot(vm, microvm_factory, mem_size_mib, workload, iteration)

    # Attach key metrics to JUnit XML.
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("total_us", row.get("total_us", 0))
    record_property("throughput_mibs", row.get("throughput_mibs", 0))
    record_property("fault_fraction_pct", row.get("fault_fraction_pct", 0))
    record_property("restore_api_ms", row.get("restore_api_ms", 0))

    _write_csv_row(row)
    _log_summary(row)


# ---------------------------------------------------------------------------
# Quick single-run comparison (useful for development / smoke testing)
# ---------------------------------------------------------------------------


@pytest.mark.nonci
@pytest.mark.timeout(600)
@pytest.mark.parametrize("mem_size_mib", [512])
@pytest.mark.parametrize("workload", ["idle", "medium"])
def test_snapshot_experiment_quick(
    uvm_plain, microvm_factory, mem_size_mib, workload
):
    """Quick single-iteration comparison of full vs live for one config.

    Use this to verify the experiment harness before running the full matrix.
    """
    # --- Full snapshot ---
    vm_full = _boot_experiment_vm(uvm_plain, mem_size_mib)
    full_row = _run_full_snapshot(
        vm_full, microvm_factory, mem_size_mib, workload, iteration=0
    )
    _write_csv_row(full_row)
    _log_summary(full_row)

    # Boot a fresh VM for the live snapshot path.
    # We cannot reuse the full-snapshot VM (it's paused) or a restored VM
    # (file-backed memory doesn't support UFFD-WP on kernel < 6.x).
    vm_live = microvm_factory.build(
        kernel=vm_full.kernel_file,
        rootfs=vm_full.rootfs_file,
    )
    vm_live.monitors = [m for m in vm_live.monitors if m is not vm_live.memory_monitor]
    vm_live.memory_monitor = None
    vm_live.spawn()
    vm_live.basic_config(vcpu_count=VCPU_COUNT, mem_size_mib=mem_size_mib)
    vm_live.add_net_iface()
    vm_live.start()
    vm_live.ssh.check_output("true")
    # Condition memory.
    prefill_mib = max(mem_size_mib // 4, 16)
    vm_live.ssh.check_output(
        f"head -c {prefill_mib}M /dev/urandom > /tmp/prefill 2>/dev/null; sync",
        timeout=120,
    )

    live_row = _run_live_snapshot(
        vm_live, microvm_factory, mem_size_mib, workload, iteration=0
    )
    _write_csv_row(live_row)
    _log_summary(live_row)

    # --- Side-by-side summary ---
    full_dt = full_row.get("downtime_us", 0)
    live_dt = live_row.get("downtime_us", 0)
    speedup = full_dt / live_dt if live_dt > 0 else float("inf")

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "COMPARISON: %d MiB, %s workload, %d vCPUs", mem_size_mib, workload, VCPU_COUNT
    )
    logger.info("=" * 70)
    logger.info("                        Full          Live         Speedup")
    logger.info(
        "  Downtime:        %8.1f ms    %8.1f ms    %8.1fx",
        full_dt / 1000,
        live_dt / 1000,
        speedup,
    )
    logger.info(
        "  Wall-clock:      %8.1f ms    %8.1f ms",
        full_row.get("total_us", 0) / 1000,
        live_row.get("total_us", 0) / 1000,
    )
    logger.info(
        "  Restore→SSH:     %8.1f ms    %8.1f ms",
        full_row.get("ssh_ready_ms", 0),
        live_row.get("ssh_ready_ms", 0),
    )
    if workload != "idle":
        logger.info(
            "  Workload degr:        N/A         %6.1f %%",
            live_row.get("workload_degradation_pct", 0),
        )
    logger.info("=" * 70)

    # --- Smoke test: redis_light at 512 MiB (single iteration) ---
    # Only runs when EXPERIMENT_ROOTFS is set to a rootfs that has Redis.
    # Builds a completely fresh VM — we cannot reuse uvm_plain (already paused)
    # or vm_live (live snapshot taken from it).
    experiment_rootfs = os.environ.get("EXPERIMENT_ROOTFS")
    if mem_size_mib == 512 and experiment_rootfs:
        vm_redis = microvm_factory.build(
            kernel=vm_full.kernel_file,
            rootfs=Path(experiment_rootfs),
        )
        vm_redis.monitors = [m for m in vm_redis.monitors if m is not vm_redis.memory_monitor]
        vm_redis.memory_monitor = None
        vm_redis.spawn()
        vm_redis.basic_config(vcpu_count=VCPU_COUNT, mem_size_mib=mem_size_mib)
        vm_redis.add_net_iface()
        vm_redis.start()
        vm_redis.ssh.check_output("true")
        _check_workload_tools(vm_redis, "redis_light")
        redis_row = _run_live_snapshot_app(
            vm_redis, microvm_factory, mem_size_mib, "redis_light", iteration=0
        )
        _write_csv_row(redis_row)
        _log_app_summary(redis_row)


# ---------------------------------------------------------------------------
# Parametrized experiment tests — application workloads
# ---------------------------------------------------------------------------


@pytest.mark.nonci
@pytest.mark.timeout(900)
@pytest.mark.parametrize("mem_size_mib", APP_MEM_SIZES)
@pytest.mark.parametrize("workload", [
    "redis_light", "redis_mixed", "redis_heavy",
    "memcached_light", "memcached_heavy", "stream",
])
@pytest.mark.parametrize("iteration", range(10))
def test_full_snapshot_app_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect full-snapshot metrics under Redis, Memcached, or STREAM workload."""
    vm = _boot_app_experiment_vm(uvm_plain, microvm_factory, mem_size_mib)
    _check_workload_tools(vm, workload)
    row = _run_full_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration)
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("full_throughput_mibs", row.get("full_throughput_mibs", 0))
    record_property("restore_api_ms", row.get("restore_api_ms", 0))
    _write_csv_row(row)
    _log_app_summary(row)


@pytest.mark.nonci
@pytest.mark.timeout(900)
@pytest.mark.parametrize("mem_size_mib", APP_MEM_SIZES)
@pytest.mark.parametrize("workload", [
    "redis_light", "redis_mixed", "redis_heavy",
    "memcached_light", "memcached_heavy", "stream",
])
@pytest.mark.parametrize("iteration", range(10))
def test_live_snapshot_app_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect live-snapshot metrics under Redis, Memcached, or STREAM workload."""
    vm = _boot_app_experiment_vm(uvm_plain, microvm_factory, mem_size_mib)
    _check_workload_tools(vm, workload)
    row = _run_live_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration)
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("throughput_mibs", row.get("throughput_mibs", 0))
    record_property("fault_fraction_pct", row.get("fault_fraction_pct", 0))
    record_property("restore_api_ms", row.get("restore_api_ms", 0))
    _write_csv_row(row)
    _log_app_summary(row)
