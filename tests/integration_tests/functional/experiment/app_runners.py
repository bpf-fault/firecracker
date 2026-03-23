# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Application workload experiment runners (full and live snapshot paths)."""

import time
from pathlib import Path

from .metrics import (
    _compute_overall_stats,
    _get_peak_rss_kib,
    _get_rss_kib,
    _parse_live_snapshot_log,
    _parse_stream_log_all_runs,
)
from .timeseries import (
    _start_timeseries_sampler,
    _stop_timeseries_sampler,
    _write_timeseries_csv,
)
from .vm import _do_full_snapshot_timed, _do_restore_timed, _get_iface_dropped
from .workloads import (
    _is_memcached_workload,
    _is_redis_workload,
    _is_stream_workload,
    _wait_for_sentinel,
)
from .workloads.memcached import (
    _measure_memcached_baseline,
    _measure_post_snapshot_memcached,
    _parse_memtier_output,
    _setup_memcached,
    _start_memcached_background_workload,
    _start_memcached_during_burst,
)
from .workloads.redis import (
    _measure_post_snapshot_redis,
    _measure_redis_baseline,
    _parse_redis_benchmark_output,
    _setup_redis,
    _start_redis_background_workload,
    _start_redis_during_burst,
)
from .workloads.stream import (
    _parse_stream_output,
    _run_stream_benchmark,
    _start_stream_during_burst,
)


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

    # Start timeseries sampler BEFORE full snapshot (only for redis workloads).
    # During the snapshot the VM is paused, so all sampler hits will fail →
    # the resulting CSV shows flat baseline → drop to 0 → still zero (paused).
    ts_handle = None
    if _is_redis_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        ts_handle = _start_timeseries_sampler(guest_ip, workload, netns_id=vm.netns.id)
        time.sleep(5)   # ~30 pre-snapshot baseline samples

    ts_snap_start_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    rx_drop_before, _ = _get_iface_dropped(vm)

    # Take full snapshot — pauses the VM.
    snapshot, timings = _do_full_snapshot_timed(vm)
    row.update(timings)
    row["downtime_us"] = int(timings["full_total_ms"] * 1000)
    row["total_us"]    = int(timings["full_total_ms"] * 1000)

    ts_snap_end_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    if ts_handle:
        row["ts_snap_start_s"]   = round(ts_snap_start_rel, 3)
        row["ts_snap_end_s"]     = round(ts_snap_end_rel, 3)
        # Full snapshot: the entire snapshot is the freeze window.
        row["ts_freeze_start_s"] = row["ts_snap_start_s"]
        row["ts_freeze_end_s"]   = row["ts_snap_end_s"]
        # Keep sampler running through restore — it will keep failing since the
        # original VM remains paused, filling the pause+restore gap with dense
        # failed samples.  Stopped below, after _do_restore_timed returns.

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

    try:
        rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
        row.update(restore_timings)

        rx_drop_after, _ = _get_iface_dropped(rvm)
        row["network_packets_dropped"] = rx_drop_after - rx_drop_before

        # Stop the original sampler now — restore is done, the restored VM is up.
        # CSV write is deferred until after the restored VM has been sampled.
        if ts_handle:
            _stop_timeseries_sampler(ts_handle)
    finally:
        # Delete snapshot files — mem file is VM-memory-size bytes.
        # Done in finally so cleanup happens even if restore raises.
        snapshot.delete()

    # Sample the restored VM so the timeline shows post-restore recovery.
    # Share start_wall with the original sampler for a continuous time axis.
    if ts_handle and _is_redis_workload(workload):
        rvm_ip = rvm.iface["eth0"]["iface"].guest_ip
        ts_rvm = _start_timeseries_sampler(
            rvm_ip, workload, netns_id=rvm.netns.id,
            start_wall=ts_handle["start_wall"],
        )
        time.sleep(10)   # ~100 post-restore samples
        _stop_timeseries_sampler(ts_rvm)
        ts_handle["samples"].extend(ts_rvm["samples"])
        ts_name = _write_timeseries_csv(ts_handle, workload, mem_size_mib, "full", iteration)
        row["timeseries_file"] = ts_name
        failed_count = sum(1 for s in ts_handle["samples"] if s[6])
        row["timeseries_failed_samples"] = failed_count
    elif ts_handle:
        # Non-redis workload: just write what was collected before restore.
        ts_name = _write_timeseries_csv(ts_handle, workload, mem_size_mib, "full", iteration)
        row["timeseries_file"] = ts_name
        failed_count = sum(1 for s in ts_handle["samples"] if s[6])
        row["timeseries_failed_samples"] = failed_count

    # Post-snapshot: measure on the restored VM (resumed from pre-pause state).
    if _is_redis_workload(workload):
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_redis(rvm, workload)
        row["post_snap_ops"]    = round(ps_ops, 1)
        row["post_snap_avg_us"] = round(ps_avg, 1)
        row["post_snap_p50_us"] = round(ps_p50, 1)
        row["post_snap_p99_us"] = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)
        # Overall stats: during window excluded (VM was paused), use [baseline, post].
        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, ps_ops])
        mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, ps_avg])
        mean_p99, std_p99, _, _ = _compute_overall_stats([p99, ps_p99])
        row["overall_ops_mean"]             = round(mean_ops, 1)
        row["overall_ops_stddev"]           = round(std_ops, 1)
        row["overall_ops_min"]              = round(min_ops, 1)
        row["overall_ops_max"]              = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]  = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]          = round(mean_p99, 1)
        row["overall_p99_us_stddev"]        = round(std_p99, 1)
        row["service_interruption_ms"] = round(row["full_total_ms"], 2)
    elif _is_memcached_workload(workload):
        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_memcached(rvm, workload)
        row["post_snap_ops"]    = round(ps_ops, 1)
        row["post_snap_avg_us"] = round(ps_avg, 1)
        row["post_snap_p50_us"] = round(ps_p50, 1)
        row["post_snap_p99_us"] = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)
        # Overall stats: during window excluded (VM was paused), use [baseline, post].
        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats([ops, ps_ops])
        mean_avg, std_avg, _, _ = _compute_overall_stats([avg_us, ps_avg])
        mean_p99, std_p99, _, _ = _compute_overall_stats([p99, ps_p99])
        row["overall_ops_mean"]             = round(mean_ops, 1)
        row["overall_ops_stddev"]           = round(std_ops, 1)
        row["overall_ops_min"]              = round(min_ops, 1)
        row["overall_ops_max"]              = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]  = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]          = round(mean_p99, 1)
        row["overall_p99_us_stddev"]        = round(std_p99, 1)
        row["service_interruption_ms"] = round(row["full_total_ms"], 2)
    elif _is_stream_workload(workload):
        ps_copy, ps_scale, ps_add, ps_triad = _run_stream_benchmark(rvm)
        row["stream_post_copy_mibs"]  = round(ps_copy, 1)
        row["stream_post_scale_mibs"] = round(ps_scale, 1)
        row["stream_post_add_mibs"]   = round(ps_add, 1)
        row["stream_post_triad_mibs"] = round(ps_triad, 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        # Overall triad: during window excluded (VM was paused), use [baseline, post].
        mean_tr, std_tr, _, _ = _compute_overall_stats([b_triad, ps_triad])
        row["overall_triad_mean_mibs"]   = round(mean_tr, 1)
        row["overall_triad_stddev_mibs"] = round(std_tr, 1)
        row["service_interruption_ms"] = round(row["full_total_ms"], 2)

    rvm.kill()

    return row


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

    # Start timeseries sampler for redis workloads (used by plot 16).
    # We sleep briefly before and after the snapshot so the sampler captures
    # pre-snapshot baseline samples and post-snapshot streaming-phase samples.
    # The sampler is stopped BEFORE _wait_for_sentinel, which holds a blocking
    # SSH call and would starve the sampler thread.
    ts_handle = None
    if _is_redis_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        ts_handle = _start_timeseries_sampler(guest_ip, workload, netns_id=vm.netns.id)
        time.sleep(5)   # collect ~30 pre-snapshot baseline samples

    ts_snap_start_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    rx_drop_before, _ = _get_iface_dropped(vm)

    # Take live snapshot — VM keeps running.
    assert vm.state == "Running"
    snapshot = vm.snapshot_live()
    assert vm.state == "Running"

    ts_snap_end_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    # Parse Firecracker log for phase breakdown.
    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)
    row["service_interruption_ms"] = round(
        (live_metrics.get("phase1_us", 0) + live_metrics.get("freeze_us", 0)) / 1000, 2
    )

    if ts_handle:
        row["ts_snap_start_s"] = round(ts_snap_start_rel, 3)
        row["ts_snap_end_s"] = round(ts_snap_end_rel, 3)
        phase1_s = live_metrics.get("phase1_us", 0) / 1e6
        freeze_s = live_metrics.get("freeze_us", 0) / 1e6
        row["ts_freeze_start_s"] = round(row["ts_snap_start_s"] + phase1_s, 3)
        row["ts_freeze_end_s"]   = round(row["ts_snap_start_s"] + phase1_s + freeze_s, 3)
        # Collect ~60 post-snapshot samples during UFFD streaming, then stop
        # before _wait_for_sentinel blocks the SSH channel.
        try:
            time.sleep(10)
        finally:
            _stop_timeseries_sampler(ts_handle)
        ts_name = _write_timeseries_csv(ts_handle, workload, mem_size_mib, "live", iteration)
        row["timeseries_file"] = ts_name
        failed_count = sum(1 for s in ts_handle["samples"] if s[6])
        row["timeseries_failed_samples"] = failed_count

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

    rx_drop_after, _ = _get_iface_dropped(vm)
    row["network_packets_dropped"] = rx_drop_after - rx_drop_before

    # Restore from the live snapshot.
    try:
        rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
        row.update(restore_timings)
        rvm.ssh.check_output("true")
        rvm.kill()
    finally:
        # Delete snapshot files — mem file is VM-memory-size bytes.
        # Done in finally so cleanup happens even if restore raises.
        snapshot.delete()

    return row


def _run_live_bpf_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one live-bpf-snapshot experiment run for an application workload.

    Near-identical to _run_live_snapshot_app but uses snapshot_live_bpf() and
    records snapshot_mode as "live_bpf".
    """
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib": mem_size_mib,
        "workload": workload,
        "snapshot_mode": "live_bpf",
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

    ts_handle = None
    if _is_redis_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        ts_handle = _start_timeseries_sampler(guest_ip, workload, netns_id=vm.netns.id)
        time.sleep(5)   # collect ~30 pre-snapshot baseline samples

    ts_snap_start_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    rx_drop_before, _ = _get_iface_dropped(vm)

    # Take live-bpf snapshot — VM keeps running.
    assert vm.state == "Running"
    snapshot = vm.snapshot_live_bpf()
    assert vm.state == "Running"

    ts_snap_end_rel = (time.monotonic() - ts_handle["start_wall"]) if ts_handle else 0.0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    # Parse Firecracker log for phase breakdown.
    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)
    row["service_interruption_ms"] = round(
        (live_metrics.get("phase1_us", 0) + live_metrics.get("freeze_us", 0)) / 1000, 2
    )

    if ts_handle:
        row["ts_snap_start_s"] = round(ts_snap_start_rel, 3)
        row["ts_snap_end_s"] = round(ts_snap_end_rel, 3)
        phase1_s = live_metrics.get("phase1_us", 0) / 1e6
        freeze_s = live_metrics.get("freeze_us", 0) / 1e6
        row["ts_freeze_start_s"] = round(row["ts_snap_start_s"] + phase1_s, 3)
        row["ts_freeze_end_s"]   = round(row["ts_snap_start_s"] + phase1_s + freeze_s, 3)
        try:
            time.sleep(10)
        finally:
            _stop_timeseries_sampler(ts_handle)
        ts_name = _write_timeseries_csv(ts_handle, workload, mem_size_mib, "live_bpf", iteration)
        row["timeseries_file"] = ts_name
        failed_count = sum(1 for s in ts_handle["samples"] if s[6])
        row["timeseries_failed_samples"] = failed_count

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

        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_redis(vm, workload)
        row["post_snap_ops"]     = round(ps_ops, 1)
        row["post_snap_avg_us"]  = round(ps_avg, 1)
        row["post_snap_p50_us"]  = round(ps_p50, 1)
        row["post_snap_p99_us"]  = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)

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

        ps_ops, ps_avg, ps_p50, ps_p99, ps_p999 = _measure_post_snapshot_memcached(vm, workload)
        row["post_snap_ops"]     = round(ps_ops, 1)
        row["post_snap_avg_us"]  = round(ps_avg, 1)
        row["post_snap_p50_us"]  = round(ps_p50, 1)
        row["post_snap_p99_us"]  = round(ps_p99, 1)
        row["post_snap_p999_us"] = round(ps_p999, 1)

        _, overall_log, _ = vm.ssh.check_output(
            "cat /tmp/memtier_overall.log 2>/dev/null || true"
        )
        ov_ops, ov_avg, _, ov_p99, _ = _parse_memtier_output(overall_log)

        if ov_ops > 0:
            row["overall_ops_mean"]              = round(ov_ops, 1)
            row["overall_avg_latency_us_mean"]   = round(ov_avg, 1)
            row["overall_p99_us_mean"]           = round(ov_p99, 1)
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

        ps_copy, ps_scale, ps_add, ps_triad = _run_stream_benchmark(vm)
        row["stream_post_copy_mibs"]  = round(ps_copy, 1)
        row["stream_post_scale_mibs"] = round(ps_scale, 1)
        row["stream_post_add_mibs"]   = round(ps_add, 1)
        row["stream_post_triad_mibs"] = round(ps_triad, 1)

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

    rx_drop_after, _ = _get_iface_dropped(vm)
    row["network_packets_dropped"] = rx_drop_after - rx_drop_before

    # Restore from the live-bpf snapshot.
    try:
        rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
        row.update(restore_timings)
        rvm.ssh.check_output("true")
        rvm.kill()
    finally:
        # Delete snapshot files — mem file is VM-memory-size bytes.
        # Done in finally so cleanup happens even if restore raises.
        snapshot.delete()

    return row
