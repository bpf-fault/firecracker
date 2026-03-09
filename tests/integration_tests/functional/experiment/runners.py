# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Synthetic workload experiment runners (full and live snapshot paths)."""

import time
from pathlib import Path

from .metrics import (
    _compute_overall_stats,
    _get_peak_rss_kib,
    _get_rss_kib,
    _parse_live_snapshot_log,
)
from .vm import _do_full_snapshot_timed, _do_restore_timed
from .workloads.synthetic import (
    _measure_workload_throughput,
    _start_workload,
)


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

    # Overall stats across [baseline, post] windows (during=0 skipped — VM was paused).
    overall_vals = [baseline_mibs, post_snap_mibs]
    row["service_interruption_ms"] = round(row["full_total_ms"], 2)
    mean, stddev, _, _ = _compute_overall_stats(overall_vals)
    row["overall_throughput_mean_mibs"] = round(mean, 2)
    row["overall_throughput_stddev_mibs"] = round(stddev, 2)

    # Clean up restored VM.
    rvm.kill()

    # Delete snapshot files immediately — mem file is VM-memory-size bytes and
    # accumulates quickly across iterations if left until chroot teardown.
    snapshot.delete()

    return row


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
        row["service_interruption_ms"] = round(row.get("downtime_us", 0) / 1000, 2)
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

    # Delete snapshot files immediately — mem file is VM-memory-size bytes and
    # accumulates quickly across iterations if left until chroot teardown.
    snapshot.delete()

    return row
