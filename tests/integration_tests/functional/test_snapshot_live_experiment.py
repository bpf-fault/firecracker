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
import os
import re
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
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
            "  Workload degradation:   %8.1f %%", row.get("workload_degradation_pct", 0)
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

    # Measure workload throughput on restored VM (only meaningful if workload
    # was running, but the restored VM won't have the workload process — so
    # this field is 0 for full snapshots since the VM was paused).
    row["workload_during_mibs"] = 0
    row["workload_degradation_pct"] = 0

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
    else:
        row["workload_during_mibs"] = 0
        row["workload_degradation_pct"] = 0

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
