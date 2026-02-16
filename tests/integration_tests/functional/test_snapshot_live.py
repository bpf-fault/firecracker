# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for live snapshot (UFFD write-protect based)."""

import logging
import re
import time

import pytest

from framework.microvm import SnapshotType

logger = logging.getLogger(__name__)


def test_live_snapshot_basic(uvm_nano, microvm_factory):
    """Test that a live snapshot can be taken and restored.

    Takes a snapshot while the VM is running (no explicit pause),
    restores it, and verifies the restored VM works correctly.
    """
    vm = uvm_nano
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.add_net_iface()
    vm.start()

    # Verify VM is running and responsive before snapshot.
    vm.ssh.check_output("true")

    # Take live snapshot while VM is running — no pause!
    assert vm.state == "Running"
    snapshot = vm.snapshot_live()
    assert snapshot.snapshot_type == SnapshotType.LIVE

    # VM should still be running after live snapshot.
    assert vm.state == "Running"
    vm.ssh.check_output("true")

    # Restore snapshot in a new VM.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)
    assert restored_vm.state == "Running"
    restored_vm.ssh.check_output("true")


def test_live_snapshot_memory_integrity(uvm_plain, microvm_factory):
    """Verify that memory contents are preserved across live snapshot/restore.

    Writes a known pattern to guest memory before the snapshot, then
    verifies it is still present after restoring.
    """
    vm = uvm_plain
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=2, mem_size_mib=512)
    vm.add_net_iface()
    vm.start()

    # Write a known pattern into a file in guest memory (tmpfs).
    pattern = "LIVE_SNAPSHOT_INTEGRITY_CHECK_" + "A" * 200
    vm.ssh.check_output(f"echo -n '{pattern}' > /tmp/test_pattern")
    _, stdout, _ = vm.ssh.check_output("cat /tmp/test_pattern")
    assert stdout.strip() == pattern

    # Take live snapshot while VM is running.
    snapshot = vm.snapshot_live()

    # Restore and verify the pattern is intact.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)

    _, stdout, _ = restored_vm.ssh.check_output("cat /tmp/test_pattern")
    assert stdout.strip() == pattern


def test_live_snapshot_under_load(uvm_plain, microvm_factory):
    """Test live snapshot while the guest is actively writing memory.

    Runs a memory-intensive workload in the guest during the snapshot to
    exercise the UFFD write-protect fault path.
    """
    vm = uvm_plain
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=2, mem_size_mib=512)
    vm.add_net_iface()
    vm.start()

    # Start a background memory workload in the guest that writes to many pages.
    # This will trigger WP faults during the snapshot.
    vm.ssh.check_output(
        "nohup sh -c '"
        "while true; do "
        "  dd if=/dev/urandom of=/tmp/loadfile bs=4096 count=1024 2>/dev/null; "
        "done' </dev/null >/dev/null 2>&1 &"
    )

    # Give the workload a moment to start.
    time.sleep(1)

    # Take live snapshot while workload is running.
    snapshot = vm.snapshot_live()

    # VM should still be running and responsive.
    assert vm.state == "Running"
    vm.ssh.check_output("true")

    # Restore and verify the restored VM works.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)
    assert restored_vm.state == "Running"
    restored_vm.ssh.check_output("true")


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
        metrics["freeze_total_us"] = int(m.group(1))
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


def _log_metrics(label, metrics):
    """Pretty-print timing metrics."""
    if not metrics:
        logger.warning("%s: no timing metrics found in log", label)
        return

    total_ms = metrics.get("total_us", 0) / 1000
    downtime_ms = metrics.get("downtime_us", 0) / 1000
    pages = metrics.get("total_pages", 0)
    page_size = 4096
    mem_mb = pages * page_size / (1024 * 1024)

    logger.info("=" * 70)
    logger.info("%s — Timing Breakdown", label)
    logger.info("=" * 70)
    logger.info(
        "  Phase 1 (prepare):      %8.1f ms  [populate_pages: %.1f ms]",
        metrics.get("phase1_us", 0) / 1000,
        metrics.get("populate_pages_us", 0) / 1000,
    )
    logger.info(
        "  Phase 2 (freeze):       %8.1f ms  ← VM DOWNTIME",
        downtime_ms,
    )
    logger.info(
        "    ├─ pause vCPUs:       %8.1f ms",
        metrics.get("pause_us", 0) / 1000,
    )
    logger.info(
        "    ├─ save device state: %8.1f ms",
        metrics.get("save_state_us", 0) / 1000,
    )
    logger.info(
        "    ├─ enable WP:         %8.1f ms",
        metrics.get("wp_enable_us", 0) / 1000,
    )
    logger.info(
        "    └─ resume vCPUs:      %8.1f ms",
        metrics.get("resume_us", 0) / 1000,
    )
    logger.info(
        "  Phase 3 (stream RAM):   %8.1f ms  [%d pages = %.0f MiB]",
        metrics.get("stream_us", 0) / 1000,
        pages,
        mem_mb,
    )
    if pages > 0:
        stream_s = metrics.get("stream_us", 1) / 1e6
        throughput = mem_mb / stream_s if stream_s > 0 else 0
        logger.info(
            "    ├─ throughput:        %8.0f MiB/s",
            throughput,
        )
    logger.info(
        "    ├─ fault-driven:      %8d pages",
        metrics.get("fault_pages", 0),
    )
    logger.info(
        "    └─ linear-scan:       %8d pages",
        metrics.get("linear_pages", 0),
    )
    logger.info(
        "  Phase 4 (finalize):     %8.1f ms",
        metrics.get("finalize_us", 0) / 1000,
    )
    logger.info("-" * 70)
    logger.info(
        "  TOTAL wall-clock:       %8.1f ms",
        total_ms,
    )
    logger.info(
        "  VM DOWNTIME:            %8.1f ms  (%.2f%%)",
        downtime_ms,
        downtime_ms / total_ms * 100 if total_ms > 0 else 0,
    )
    logger.info("=" * 70)


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

    from pathlib import Path
    from framework.microvm import Snapshot

    root = Path(vm.chroot())
    snapshot = Snapshot(
        vmstate=root / "vmstate",
        mem=root / "mem",
        disks=vm.disks,
        net_ifaces=[x["iface"] for _, x in vm.iface.items()],
        ssh_key=vm.ssh_key,
        snapshot_type=SnapshotType.FULL,
        meta={
            "kernel_file": vm.kernel_file,
            "initrd_file": vm.initrd_file,
            "vcpus_count": vm.vcpus_count,
        },
    )

    timings = {
        "pause_ms": (t_paused - t0) * 1000,
        "create_ms": (t_created - t_paused) * 1000,
        "total_ms": (t_created - t0) * 1000,
    }
    return snapshot, timings


def _do_restore_timed(factory, snapshot):
    """Restore a snapshot, returning (vm, timing_dict)."""
    rvm = factory.build(monitor_memory=False)
    rvm.spawn()

    t0 = time.monotonic()
    rvm.restore_from_snapshot(snapshot, resume=True)
    t_restored = time.monotonic()

    # Measure time until SSH is responsive.
    rvm.ssh.check_output("true")
    t_ssh = time.monotonic()

    timings = {
        "restore_api_ms": (t_restored - t0) * 1000,
        "ssh_ready_ms": (t_ssh - t0) * 1000,
    }
    return rvm, timings


@pytest.mark.nonci
@pytest.mark.parametrize("mem_size_mib", [4096])
@pytest.mark.timeout(600)
def test_live_vs_full_benchmark(uvm_plain, microvm_factory, mem_size_mib):
    """Detailed performance comparison: live vs full snapshot on a large VM.

    Measures, for both snapshot types:
      - Snapshot creation: pause time, memory dump time, total time
      - Internal phase breakdown (live only, from Firecracker logs)
      - Restore: API load time, time to SSH ready
    """
    vcpus = 2

    # ── Boot the VM ──────────────────────────────────────────────────────
    vm = uvm_plain
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    # Touch some memory so the pages are actually backed.
    vm.ssh.check_output(
        "head -c 128M /dev/urandom > /tmp/fill 2>/dev/null; sync"
    )

    # ── Full Snapshot ────────────────────────────────────────────────────
    logger.info("=" * 70)
    logger.info(
        "BENCHMARK: %d MiB VM, %d vCPUs — FULL snapshot",
        mem_size_mib,
        vcpus,
    )

    full_snap, full_timings = _do_full_snapshot_timed(vm)

    logger.info("  Full snapshot — Timing Breakdown")
    logger.info("  ├─ pause vCPUs:         %8.1f ms", full_timings["pause_ms"])
    logger.info(
        "  ├─ create snapshot:     %8.1f ms  (memory dump + state save)",
        full_timings["create_ms"],
    )
    logger.info("  └─ TOTAL (= DOWNTIME):  %8.1f ms", full_timings["total_ms"])

    mem_size_bytes = mem_size_mib * 1024 * 1024
    dump_s = full_timings["create_ms"] / 1000
    if dump_s > 0:
        logger.info(
            "  Throughput:             %8.0f MiB/s",
            mem_size_mib / dump_s,
        )

    # Restore from full snapshot.
    full_rvm, full_restore = _do_restore_timed(microvm_factory, full_snap)
    logger.info("  Full restore:")
    logger.info(
        "  ├─ restore API:         %8.1f ms", full_restore["restore_api_ms"]
    )
    logger.info(
        "  └─ SSH ready:           %8.1f ms", full_restore["ssh_ready_ms"]
    )

    # ── Live Snapshot ────────────────────────────────────────────────────
    logger.info("")
    logger.info(
        "BENCHMARK: %d MiB VM, %d vCPUs — LIVE snapshot",
        mem_size_mib,
        vcpus,
    )

    # Take live snapshot from the restored VM (which is fresh and running).
    t_live_start = time.monotonic()
    live_snap = full_rvm.snapshot_live()
    t_live_end = time.monotonic()
    live_wall_ms = (t_live_end - t_live_start) * 1000

    # VM should still be running.
    assert full_rvm.state == "Running"
    full_rvm.ssh.check_output("true")

    # Parse internal timing from Firecracker log.
    live_metrics = _parse_live_snapshot_log(full_rvm.log_data)
    _log_metrics(f"LIVE snapshot ({mem_size_mib} MiB)", live_metrics)

    # Restore from live snapshot.
    live_rvm, live_restore = _do_restore_timed(microvm_factory, live_snap)
    logger.info("  Live restore:")
    logger.info(
        "  ├─ restore API:         %8.1f ms", live_restore["restore_api_ms"]
    )
    logger.info(
        "  └─ SSH ready:           %8.1f ms", live_restore["ssh_ready_ms"]
    )

    # Verify restored VM works.
    live_rvm.ssh.check_output("true")

    # ── Summary Comparison ───────────────────────────────────────────────
    live_downtime_ms = live_metrics.get("downtime_us", 0) / 1000
    full_downtime_ms = full_timings["total_ms"]

    logger.info("")
    logger.info("=" * 70)
    logger.info("SUMMARY: %d MiB VM, %d vCPUs", mem_size_mib, vcpus)
    logger.info("=" * 70)
    logger.info(
        "                       Full          Live         Speedup"
    )
    logger.info(
        "  VM downtime:     %8.1f ms    %8.1f ms    %8.1fx",
        full_downtime_ms,
        live_downtime_ms,
        full_downtime_ms / live_downtime_ms if live_downtime_ms > 0 else float("inf"),
    )
    logger.info(
        "  Wall-clock:      %8.1f ms    %8.1f ms    %8.1fx",
        full_timings["total_ms"],
        live_wall_ms,
        live_wall_ms / full_timings["total_ms"]
        if full_timings["total_ms"] > 0
        else float("inf"),
    )
    logger.info(
        "  Restore API:     %8.1f ms    %8.1f ms",
        full_restore["restore_api_ms"],
        live_restore["restore_api_ms"],
    )
    logger.info(
        "  Restore→SSH:     %8.1f ms    %8.1f ms",
        full_restore["ssh_ready_ms"],
        live_restore["ssh_ready_ms"],
    )
    logger.info("=" * 70)


@pytest.mark.nonci
@pytest.mark.parametrize("mem_size_mib", [4096])
@pytest.mark.timeout(600)
def test_live_snapshot_under_load_benchmark(uvm_plain, microvm_factory, mem_size_mib):
    """Live snapshot of a large VM under memory-write load.

    Exercises the UFFD write-protect fault path heavily by having the
    guest actively write to memory during the snapshot.  Reports the
    number of fault-driven vs linear-scan page saves.
    """
    vcpus = 2

    vm = uvm_plain
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    # Fill some memory first.
    vm.ssh.check_output(
        "head -c 256M /dev/urandom > /tmp/fill 2>/dev/null; sync"
    )

    # Start background workload that continuously writes pages.
    vm.ssh.check_output(
        "nohup sh -c '"
        "while true; do "
        "  dd if=/dev/urandom of=/tmp/loadfile bs=4096 count=4096 2>/dev/null; "
        "done' </dev/null >/dev/null 2>&1 &"
    )
    time.sleep(1)

    # Take live snapshot under load.
    t0 = time.monotonic()
    snapshot = vm.snapshot_live()
    wall_ms = (time.monotonic() - t0) * 1000

    # VM should still be running and responsive.
    assert vm.state == "Running"
    vm.ssh.check_output("true")

    # Parse and report internal timing.
    metrics = _parse_live_snapshot_log(vm.log_data)
    _log_metrics(f"LIVE under load ({mem_size_mib} MiB)", metrics)

    # Restore and verify.
    rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
    rvm.ssh.check_output("true")

    logger.info("  Restore from live-under-load snapshot:")
    logger.info(
        "  ├─ restore API:         %8.1f ms", restore_timings["restore_api_ms"]
    )
    logger.info(
        "  └─ SSH ready:           %8.1f ms", restore_timings["ssh_ready_ms"]
    )


@pytest.mark.nonci
@pytest.mark.parametrize("mem_size_mib", [512])
@pytest.mark.timeout(120)
def test_live_snapshot_quick_benchmark(uvm_plain, microvm_factory, mem_size_mib):
    """Quick benchmark for iterative performance testing (512 MiB VM).

    Similar to test_live_vs_full_benchmark but uses a smaller VM for faster
    turnaround during development.
    """
    vcpus = 2

    vm = uvm_plain
    # Disable memory monitoring — live snapshot allocates transient data
    # structures (page index, page tracking) that temporarily inflate RSS
    # well above the default 5 MiB threshold.
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    # Touch some memory so the pages are actually backed.
    vm.ssh.check_output(
        "head -c 64M /dev/urandom > /tmp/fill 2>/dev/null; sync"
    )

    # ── Full Snapshot ────────────────────────────────────────────────────
    logger.info("=" * 70)
    logger.info(
        "QUICK BENCHMARK: %d MiB VM, %d vCPUs — FULL snapshot",
        mem_size_mib,
        vcpus,
    )
    full_snap, full_timings = _do_full_snapshot_timed(vm)
    logger.info("  Full snapshot — Timing Breakdown")
    logger.info("  ├─ pause vCPUs:         %8.1f ms", full_timings["pause_ms"])
    logger.info(
        "  ├─ create snapshot:     %8.1f ms", full_timings["create_ms"]
    )
    logger.info("  └─ TOTAL (= DOWNTIME):  %8.1f ms", full_timings["total_ms"])

    # Restore from full snapshot for live snapshot source.
    full_rvm, full_restore = _do_restore_timed(microvm_factory, full_snap)
    logger.info("  Full restore → SSH:     %8.1f ms", full_restore["ssh_ready_ms"])

    # ── Live Snapshot (idle) ─────────────────────────────────────────────
    logger.info("")
    logger.info(
        "QUICK BENCHMARK: %d MiB VM, %d vCPUs — LIVE snapshot (idle)",
        mem_size_mib,
        vcpus,
    )
    t0 = time.monotonic()
    live_snap = full_rvm.snapshot_live()
    live_wall_ms = (time.monotonic() - t0) * 1000
    assert full_rvm.state == "Running"

    live_metrics = _parse_live_snapshot_log(full_rvm.log_data)
    _log_metrics(f"LIVE idle ({mem_size_mib} MiB)", live_metrics)

    # Restore from live snapshot.
    live_rvm, live_restore = _do_restore_timed(microvm_factory, live_snap)
    live_rvm.ssh.check_output("true")
    logger.info("  Live restore → SSH:     %8.1f ms", live_restore["ssh_ready_ms"])

    # ── Live Snapshot (under load) ───────────────────────────────────────
    logger.info("")
    logger.info(
        "QUICK BENCHMARK: %d MiB VM, %d vCPUs — LIVE snapshot (under load)",
        mem_size_mib,
        vcpus,
    )
    live_rvm.ssh.check_output(
        "nohup sh -c '"
        "while true; do "
        "  dd if=/dev/urandom of=/tmp/loadfile bs=4096 count=1024 2>/dev/null; "
        "done' </dev/null >/dev/null 2>&1 &"
    )
    time.sleep(1)

    t0 = time.monotonic()
    load_snap = live_rvm.snapshot_live()
    load_wall_ms = (time.monotonic() - t0) * 1000
    assert live_rvm.state == "Running"

    load_metrics = _parse_live_snapshot_log(live_rvm.log_data)
    _log_metrics(f"LIVE under load ({mem_size_mib} MiB)", load_metrics)

    load_rvm, load_restore = _do_restore_timed(microvm_factory, load_snap)
    load_rvm.ssh.check_output("true")

    # ── Summary ──────────────────────────────────────────────────────────
    live_downtime_ms = live_metrics.get("downtime_us", 0) / 1000
    load_downtime_ms = load_metrics.get("downtime_us", 0) / 1000
    full_downtime_ms = full_timings["total_ms"]

    logger.info("")
    logger.info("=" * 70)
    logger.info("SUMMARY: %d MiB VM, %d vCPUs", mem_size_mib, vcpus)
    logger.info("=" * 70)
    logger.info(
        "                       Full       Live(idle)   Live(load)"
    )
    logger.info(
        "  VM downtime:     %8.1f ms   %8.1f ms   %8.1f ms",
        full_downtime_ms, live_downtime_ms, load_downtime_ms,
    )
    logger.info(
        "  Wall-clock:      %8.1f ms   %8.1f ms   %8.1f ms",
        full_timings["total_ms"], live_wall_ms, load_wall_ms,
    )
    logger.info(
        "  Stream thruput:       N/A      %8.0f MiB/s %7.0f MiB/s",
        (live_metrics.get("total_pages", 0) * 4096 / 1048576)
        / (live_metrics.get("stream_us", 1) / 1e6)
        if live_metrics.get("stream_us", 0) > 0 else 0,
        (load_metrics.get("total_pages", 0) * 4096 / 1048576)
        / (load_metrics.get("stream_us", 1) / 1e6)
        if load_metrics.get("stream_us", 0) > 0 else 0,
    )
    logger.info(
        "  Fault pages:          N/A      %8d      %8d",
        live_metrics.get("fault_pages", 0),
        load_metrics.get("fault_pages", 0),
    )
    logger.info("=" * 70)
