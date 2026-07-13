# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Result logging and CSV writing for the snapshot live experiment."""

import csv
import logging
import os

import pytest

from .constants import CSV_FIELDS, RESULTS_FILE
from .workloads import _is_memcached_workload, _is_redis_workload, _is_stream_workload

logger = logging.getLogger(__name__)


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


def _existing_configs():
    """Set of (workload, snapshot_mode, mem_size_mib, iteration) rows
    already present in the experiment CSV."""
    configs = set()
    if not os.path.isfile(RESULTS_FILE):
        return configs
    with open(RESULTS_FILE, newline="") as f:
        for row in csv.DictReader(f):
            # A row whose timeseries file is gone is incomplete data:
            # treat it as missing so the configuration reruns.
            ts_rel = row.get("timeseries_file") or ""
            if ts_rel and not os.path.exists(
                    os.path.join(os.path.dirname(RESULTS_FILE), ts_rel)):
                continue
            try:
                configs.add((row["workload"], row["snapshot_mode"],
                             int(row["mem_size_mib"]), int(row["iteration"])))
            except (KeyError, ValueError, TypeError):
                continue
    return configs


def _start_or_skip(workload, mem_size_mib, mode, iteration):
    """Announce the configuration about to run, or skip it when its
    results are already in the CSV (EXPERIMENT_REUSE=0 disables reuse)."""
    if os.environ.get("EXPERIMENT_REUSE", "1") != "0" and (
            workload, mode, mem_size_mib, iteration) in _existing_configs():
        print(f"Skipping {workload} {mode} mem={mem_size_mib} "
              f"iteration={iteration} (already in results)", flush=True)
        pytest.skip("already in results")
    print(f"Running config: {workload} {mode} mem={mem_size_mib} "
          f"iteration={iteration}", flush=True)


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

    if mode in ("live", "live_bpf"):
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
