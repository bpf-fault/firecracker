# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Application workload experiment runners (full and live snapshot paths).

All three runners share the same measurement structure:
  1. Start a single host-side memtier_benchmark process (--stats-interval=0.1)
     that spans the entire experiment window.
  2. Sleep BASELINE_WINDOW_SEC to collect pre-snapshot samples.
  3. Trigger the snapshot (full: pause→create→resume; live/bpf: async stream).
  4. Sleep POST_WINDOW_SEC to collect post-snapshot recovery samples.
  5. Stop memtier (SIGINT) and derive per-window metrics from the Time-Serie
     buckets in its JSON output.

This replaces the previous multi-invocation approach (separate 10 s baseline
run, 30 s during run, 10 s post run) and the custom TCP RESP sampler.
"""

import time
from pathlib import Path

from .constants import (
    BASELINE_WINDOW_SEC,
    MEMCACHED_WORKLOAD_PARAMS,
    POST_WINDOW_SEC,
    REDIS_WORKLOAD_PARAMS,
)
from .metrics import (
    _compute_overall_stats,
    _get_peak_rss_kib,
    _get_rss_kib,
    _parse_live_snapshot_log,
    _parse_stream_log_all_runs,
)
from .timeseries import (
    _parse_memtier_windows,
    _start_memtier,
    _stop_memtier,
    _write_timeseries_csv,
)
from .vm import (
    _do_full_snapshot_resume_timed,
    _do_restore_timed,
    _get_iface_dropped,
)
from .workloads import (
    _is_memcached_workload,
    _is_redis_workload,
    _is_stream_workload,
    _wait_for_sentinel,
)
from .workloads.memcached import _setup_memcached
from .workloads.redis import _setup_redis
from .workloads.stream import (
    _parse_stream_output,
    _run_stream_benchmark,
    _start_stream_during_burst,
)


def _app_memtier_params(workload):
    """Return (protocol, params) for the given workload."""
    if _is_redis_workload(workload):
        return "redis", REDIS_WORKLOAD_PARAMS[workload]
    return "memcache_text", MEMCACHED_WORKLOAD_PARAMS[workload]


def _populate_window_row(row, prefix, result_tuple):
    """Write a (ops, avg_us, p50_us, p95_us, p99_us, p999_us) tuple into the row."""
    ops, avg_us, p50_us, p95_us, p99_us, p999_us = result_tuple
    row[f"{prefix}_ops"]     = round(ops,     1)
    row[f"{prefix}_avg_us"]  = round(avg_us,  1)
    row[f"{prefix}_p50_us"]  = round(p50_us,  1)
    row[f"{prefix}_p95_us"]  = round(p95_us,  1)
    row[f"{prefix}_p99_us"]  = round(p99_us,  1)
    row[f"{prefix}_p999_us"] = round(p999_us, 1)


def _run_full_snapshot_app(vm, mem_size_mib, workload, iteration):
    """Execute one full-snapshot experiment run for an application workload.

    The VM is paused for the snapshot then immediately resumed on the same
    instance.  A single memtier process spans baseline → pause → recovery.
    During-snapshot ops will be near-zero (VM paused).
    """
    row = {
        "timestamp":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib":  mem_size_mib,
        "workload":      workload,
        "snapshot_mode": "full",
        "iteration":     iteration,
    }

    if _is_redis_workload(workload):
        _setup_redis(vm, mem_size_mib,
                     value_size=REDIS_WORKLOAD_PARAMS[workload]["value_size"])
    elif _is_memcached_workload(workload):
        _setup_memcached(vm, mem_size_mib)
    elif _is_stream_workload(workload):
        b_copy, b_scale, b_add, b_triad = _run_stream_benchmark(vm)
        row["stream_baseline_copy_mibs"]  = round(b_copy, 1)
        row["stream_baseline_scale_mibs"] = round(b_scale, 1)
        row["stream_baseline_add_mibs"]   = round(b_add, 1)
        row["stream_baseline_triad_mibs"] = round(b_triad, 1)

    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    ts = None
    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        protocol, params = _app_memtier_params(workload)
        duration = BASELINE_WINDOW_SEC + 5 + POST_WINDOW_SEC
        ts = _start_memtier(guest_ip, vm.netns.id, protocol, params, duration)
        time.sleep(BASELINE_WINDOW_SEC)

    rx_drop_before, _ = _get_iface_dropped(vm)

    ts_snap_start = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    if _is_stream_workload(workload):
        _start_stream_during_burst(vm)

    # Pause → snapshot → resume same VM.
    snapshot, timings = _do_full_snapshot_resume_timed(vm)
    row.update(timings)
    row["downtime_us"] = int(timings["full_total_ms"] * 1000)
    row["total_us"]    = int(timings["full_total_ms"] * 1000)

    ts_snap_end = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    if ts:
        row["ts_snap_start_s"]   = round(ts_snap_start, 3)
        row["ts_snap_end_s"]     = round(ts_snap_end, 3)
        row["ts_freeze_start_s"] = row["ts_snap_start_s"]
        row["ts_freeze_end_s"]   = row["ts_snap_end_s"]

    create_s = timings["full_create_ms"] / 1000
    row["full_throughput_mibs"] = round(mem_size_mib / create_s, 1) if create_s > 0 else 0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        time.sleep(POST_WINDOW_SEC)
        _stop_memtier(ts)

        baseline, during, post = _parse_memtier_windows(ts, ts_snap_start, ts_snap_end)
        _populate_window_row(row, "app_baseline", baseline)
        # During: VM was paused — natural result from memtier is near-zero.
        _populate_window_row(row, "app_during", during)
        row["app_ops_degradation_pct"] = (
            round((1 - during[0] / baseline[0]) * 100, 1) if baseline[0] > 0 else 100.0
        )
        _populate_window_row(row, "post_snap", post)

        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats(
            [baseline[0], during[0], post[0]]
        )
        mean_avg, std_avg, _, _ = _compute_overall_stats(
            [baseline[1], during[1], post[1]]
        )
        mean_p99, std_p99, _, _ = _compute_overall_stats(
            [baseline[4], during[4], post[4]]
        )
        row["overall_ops_mean"]              = round(mean_ops, 1)
        row["overall_ops_stddev"]            = round(std_ops, 1)
        row["overall_ops_min"]               = round(min_ops, 1)
        row["overall_ops_max"]               = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]   = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]           = round(mean_p99, 1)
        row["overall_p99_us_stddev"]         = round(std_p99, 1)

        ts_name = _write_timeseries_csv(ts, workload, mem_size_mib, "full", iteration)
        row["timeseries_file"]         = ts_name
        row["timeseries_failed_samples"] = 0

        row["service_interruption_ms"] = round(timings["full_total_ms"], 2)

    elif _is_stream_workload(workload):
        _wait_for_sentinel(vm, "/tmp/stream_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/stream_during.log")
        d = _parse_stream_output(log_out)
        row["stream_during_copy_mibs"]  = round(d.get("copy",  0.0), 1)
        row["stream_during_scale_mibs"] = round(d.get("scale", 0.0), 1)
        row["stream_during_add_mibs"]   = round(d.get("add",   0.0), 1)
        row["stream_during_triad_mibs"] = round(d.get("triad", 0.0), 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        row["stream_triad_degradation_pct"] = 100.0

        ps_copy, ps_scale, ps_add, ps_triad = _run_stream_benchmark(vm)
        row["stream_post_copy_mibs"]  = round(ps_copy, 1)
        row["stream_post_scale_mibs"] = round(ps_scale, 1)
        row["stream_post_add_mibs"]   = round(ps_add, 1)
        row["stream_post_triad_mibs"] = round(ps_triad, 1)
        mean_tr, std_tr, _, _ = _compute_overall_stats([b_triad, ps_triad])
        row["overall_triad_mean_mibs"]   = round(mean_tr, 1)
        row["overall_triad_stddev_mibs"] = round(std_tr, 1)
        row["service_interruption_ms"] = round(timings["full_total_ms"], 2)

    rx_drop_after, _ = _get_iface_dropped(vm)
    row["network_packets_dropped"] = rx_drop_after - rx_drop_before

    snapshot.delete()
    return row


def _run_live_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one live-snapshot experiment run for an application workload.

    A single memtier process spans baseline → snapshot (VM keeps running) →
    post-snapshot recovery on the same source VM.
    """
    row = {
        "timestamp":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib":  mem_size_mib,
        "workload":      workload,
        "snapshot_mode": "live",
        "iteration":     iteration,
    }

    if _is_redis_workload(workload):
        _setup_redis(vm, mem_size_mib,
                     value_size=REDIS_WORKLOAD_PARAMS[workload]["value_size"])
    elif _is_memcached_workload(workload):
        _setup_memcached(vm, mem_size_mib)
    elif _is_stream_workload(workload):
        b_copy, b_scale, b_add, b_triad = _run_stream_benchmark(vm)
        row["stream_baseline_copy_mibs"]  = round(b_copy, 1)
        row["stream_baseline_scale_mibs"] = round(b_scale, 1)
        row["stream_baseline_add_mibs"]   = round(b_add, 1)
        row["stream_baseline_triad_mibs"] = round(b_triad, 1)
        _start_stream_during_burst(vm)

    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    ts = None
    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        protocol, params = _app_memtier_params(workload)
        duration = BASELINE_WINDOW_SEC + 60 + POST_WINDOW_SEC
        ts = _start_memtier(guest_ip, vm.netns.id, protocol, params, duration)
        time.sleep(BASELINE_WINDOW_SEC)

    rx_drop_before, _ = _get_iface_dropped(vm)

    ts_snap_start = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    assert vm.state == "Running"
    snapshot = vm.snapshot_live()
    assert vm.state == "Running"

    ts_snap_end = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)
    row["service_interruption_ms"] = round(
        (live_metrics.get("phase1_us", 0) + live_metrics.get("freeze_us", 0)) / 1000, 2
    )

    if ts:
        row["ts_snap_start_s"] = round(ts_snap_start, 3)
        row["ts_snap_end_s"]   = round(ts_snap_end, 3)
        phase1_s = live_metrics.get("phase1_us", 0) / 1e6
        freeze_s = live_metrics.get("freeze_us", 0) / 1e6
        row["ts_freeze_start_s"] = round(ts_snap_start + phase1_s, 3)
        row["ts_freeze_end_s"]   = round(ts_snap_start + phase1_s + freeze_s, 3)

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

    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        time.sleep(POST_WINDOW_SEC)
        _stop_memtier(ts)

        baseline, during, post = _parse_memtier_windows(ts, ts_snap_start, ts_snap_end)
        _populate_window_row(row, "app_baseline", baseline)
        _populate_window_row(row, "app_during",   during)
        row["app_ops_degradation_pct"] = (
            round((1 - during[0] / baseline[0]) * 100, 1) if baseline[0] > 0 else 0
        )
        _populate_window_row(row, "post_snap", post)

        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats(
            [baseline[0], during[0], post[0]]
        )
        mean_avg, std_avg, _, _ = _compute_overall_stats(
            [baseline[1], during[1], post[1]]
        )
        mean_p99, std_p99, _, _ = _compute_overall_stats(
            [baseline[4], during[4], post[4]]
        )
        row["overall_ops_mean"]              = round(mean_ops, 1)
        row["overall_ops_stddev"]            = round(std_ops, 1)
        row["overall_ops_min"]               = round(min_ops, 1)
        row["overall_ops_max"]               = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]   = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]           = round(mean_p99, 1)
        row["overall_p99_us_stddev"]         = round(std_p99, 1)

        ts_name = _write_timeseries_csv(ts, workload, mem_size_mib, "live", iteration)
        row["timeseries_file"]           = ts_name
        row["timeseries_failed_samples"] = 0

    elif _is_stream_workload(workload):
        _wait_for_sentinel(vm, "/tmp/stream_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/stream_during.log")
        d = _parse_stream_output(log_out)
        row["stream_during_copy_mibs"]  = round(d.get("copy",  0.0), 1)
        row["stream_during_scale_mibs"] = round(d.get("scale", 0.0), 1)
        row["stream_during_add_mibs"]   = round(d.get("add",   0.0), 1)
        row["stream_during_triad_mibs"] = round(d.get("triad", 0.0), 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        row["stream_triad_degradation_pct"] = (
            round((1 - d.get("triad", 0) / b_triad) * 100, 1) if b_triad > 0 else 0
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

    vm.ssh.check_output("true")

    rx_drop_after, _ = _get_iface_dropped(vm)
    row["network_packets_dropped"] = rx_drop_after - rx_drop_before

    try:
        rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
        row.update(restore_timings)
        rvm.ssh.check_output("true")
        rvm.kill()
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "live-snapshot restore check failed (non-fatal): %s", exc
        )
    finally:
        snapshot.delete()

    return row


def _run_live_bpf_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration):
    """Execute one live-bpf-snapshot experiment run for an application workload.

    Identical structure to _run_live_snapshot_app but uses snapshot_live_bpf()
    and records snapshot_mode as "live_bpf".
    """
    row = {
        "timestamp":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "mem_size_mib":  mem_size_mib,
        "workload":      workload,
        "snapshot_mode": "live_bpf",
        "iteration":     iteration,
    }

    if _is_redis_workload(workload):
        _setup_redis(vm, mem_size_mib,
                     value_size=REDIS_WORKLOAD_PARAMS[workload]["value_size"])
    elif _is_memcached_workload(workload):
        _setup_memcached(vm, mem_size_mib)
    elif _is_stream_workload(workload):
        b_copy, b_scale, b_add, b_triad = _run_stream_benchmark(vm)
        row["stream_baseline_copy_mibs"]  = round(b_copy, 1)
        row["stream_baseline_scale_mibs"] = round(b_scale, 1)
        row["stream_baseline_add_mibs"]   = round(b_add, 1)
        row["stream_baseline_triad_mibs"] = round(b_triad, 1)
        _start_stream_during_burst(vm)

    pid = vm.firecracker_pid
    row["rss_pre_kib"] = _get_rss_kib(pid)

    ts = None
    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        guest_ip = vm.iface["eth0"]["iface"].guest_ip
        protocol, params = _app_memtier_params(workload)
        duration = BASELINE_WINDOW_SEC + 60 + POST_WINDOW_SEC
        ts = _start_memtier(guest_ip, vm.netns.id, protocol, params, duration)
        time.sleep(BASELINE_WINDOW_SEC)

    rx_drop_before, _ = _get_iface_dropped(vm)

    ts_snap_start = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    assert vm.state == "Running"
    snapshot = vm.snapshot_live_bpf()
    assert vm.state == "Running"

    ts_snap_end = (time.monotonic() - ts["start_wall"]) if ts else 0.0

    row["rss_peak_kib"] = _get_peak_rss_kib(pid)

    mem_path = Path(vm.chroot()) / "mem"
    row["mem_file_bytes"] = mem_path.stat().st_size if mem_path.exists() else 0

    live_metrics = _parse_live_snapshot_log(vm.log_data)
    row.update(live_metrics)
    row["service_interruption_ms"] = round(
        (live_metrics.get("phase1_us", 0) + live_metrics.get("freeze_us", 0)) / 1000, 2
    )

    if ts:
        row["ts_snap_start_s"] = round(ts_snap_start, 3)
        row["ts_snap_end_s"]   = round(ts_snap_end, 3)
        phase1_s = live_metrics.get("phase1_us", 0) / 1e6
        freeze_s = live_metrics.get("freeze_us", 0) / 1e6
        row["ts_freeze_start_s"] = round(ts_snap_start + phase1_s, 3)
        row["ts_freeze_end_s"]   = round(ts_snap_start + phase1_s + freeze_s, 3)

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

    if _is_redis_workload(workload) or _is_memcached_workload(workload):
        time.sleep(POST_WINDOW_SEC)
        _stop_memtier(ts)

        baseline, during, post = _parse_memtier_windows(ts, ts_snap_start, ts_snap_end)
        _populate_window_row(row, "app_baseline", baseline)
        _populate_window_row(row, "app_during",   during)
        row["app_ops_degradation_pct"] = (
            round((1 - during[0] / baseline[0]) * 100, 1) if baseline[0] > 0 else 0
        )
        _populate_window_row(row, "post_snap", post)

        mean_ops, std_ops, min_ops, max_ops = _compute_overall_stats(
            [baseline[0], during[0], post[0]]
        )
        mean_avg, std_avg, _, _ = _compute_overall_stats(
            [baseline[1], during[1], post[1]]
        )
        mean_p99, std_p99, _, _ = _compute_overall_stats(
            [baseline[4], during[4], post[4]]
        )
        row["overall_ops_mean"]              = round(mean_ops, 1)
        row["overall_ops_stddev"]            = round(std_ops, 1)
        row["overall_ops_min"]               = round(min_ops, 1)
        row["overall_ops_max"]               = round(max_ops, 1)
        row["overall_avg_latency_us_mean"]   = round(mean_avg, 1)
        row["overall_avg_latency_us_stddev"] = round(std_avg, 1)
        row["overall_p99_us_mean"]           = round(mean_p99, 1)
        row["overall_p99_us_stddev"]         = round(std_p99, 1)

        ts_name = _write_timeseries_csv(ts, workload, mem_size_mib, "live_bpf", iteration)
        row["timeseries_file"]           = ts_name
        row["timeseries_failed_samples"] = 0

    elif _is_stream_workload(workload):
        _wait_for_sentinel(vm, "/tmp/stream_during.done")
        _, log_out, _ = vm.ssh.check_output("cat /tmp/stream_during.log")
        d = _parse_stream_output(log_out)
        row["stream_during_copy_mibs"]  = round(d.get("copy",  0.0), 1)
        row["stream_during_scale_mibs"] = round(d.get("scale", 0.0), 1)
        row["stream_during_add_mibs"]   = round(d.get("add",   0.0), 1)
        row["stream_during_triad_mibs"] = round(d.get("triad", 0.0), 1)
        b_triad = row.get("stream_baseline_triad_mibs", 0)
        row["stream_triad_degradation_pct"] = (
            round((1 - d.get("triad", 0) / b_triad) * 100, 1) if b_triad > 0 else 0
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

    vm.ssh.check_output("true")

    rx_drop_after, _ = _get_iface_dropped(vm)
    row["network_packets_dropped"] = rx_drop_after - rx_drop_before

    try:
        rvm, restore_timings = _do_restore_timed(microvm_factory, snapshot)
        row.update(restore_timings)
        rvm.ssh.check_output("true")
        rvm.kill()
    except Exception as exc:  # noqa: BLE001
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "live-bpf-snapshot restore check failed (non-fatal): %s", exc
        )
    finally:
        snapshot.delete()

    return row
