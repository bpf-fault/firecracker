# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared constants for the snapshot live experiment."""

import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VCPU_COUNT = 2

# Fraction of guest RAM to touch during memory pre-conditioning.  Matches the
# QEMU benchmark's --guest-memory-fill-bytes default of ~75 % of guest RAM so
# that snapshot streaming time is comparable across the two hypervisors.
MEMORY_FILL_FRACTION = 0.75

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
APP_MEM_SIZES = [2048, 4096, 8192]

APP_ITERATIONS = int(os.environ.get("APP_ITERATIONS", "10"))

REDIS_WORKLOAD_PARAMS = {
    # value_size=128 and pipeline=1 match the QEMU benchmark defaults
    # (--benchmark-value-size 128, --benchmark-pipeline 1) for cross-hypervisor
    # comparability.
    "redis_light": {"clients": 2,  "ops": "get",     "value_size": 128, "pipeline": 1},
    "redis_mixed": {"clients": 10, "ops": "set,get", "value_size": 128, "pipeline": 1},
    "redis_heavy": {"clients": 50, "ops": "set",     "value_size": 128, "pipeline": 1},
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
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)
        ))))),
        "test_results",
        "experiment_results.csv",
    ),
)

TIMESERIES_DIR = os.path.join(os.path.dirname(RESULTS_FILE), "timeseries")

# Timeseries resolution: passed as --stats-interval to memtier_benchmark.
# At 0.1 s each bucket captures 100 ms of traffic; the live-snapshot freeze
# (~20 ms) appears as a near-zero-throughput bucket rather than a full gap.
TIMESERIES_INTERVAL_S = 0.1

# Per-window measurement durations (seconds).
BASELINE_WINDOW_SEC = 10   # pre-snapshot steady-state window
POST_WINDOW_SEC     = 10   # post-snapshot recovery window

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
    # p95 is extracted from redis-benchmark's nearest power-of-2 histogram
    # bucket (≥95%).  For memcached (memtier text output) p95 is not available
    # in the default Totals line and will be 0.
    "app_baseline_ops", "app_baseline_avg_us",
    "app_baseline_p50_us", "app_baseline_p95_us", "app_baseline_p99_us", "app_baseline_p999_us",
    "app_during_ops", "app_during_avg_us",
    "app_during_p50_us",  "app_during_p95_us",  "app_during_p99_us",  "app_during_p999_us",
    "app_ops_degradation_pct",
    # Post-snapshot measurements
    "post_snap_ops", "post_snap_avg_us",
    "post_snap_p50_us", "post_snap_p95_us", "post_snap_p99_us", "post_snap_p999_us",
    "post_snap_throughput_mibs",
    # Overall run aggregates (across pre/during/post windows)
    "overall_ops_mean", "overall_ops_stddev",
    "overall_ops_min", "overall_ops_max",
    "overall_avg_latency_us_mean", "overall_avg_latency_us_stddev",
    "overall_p99_us_mean", "overall_p99_us_stddev",
    "overall_throughput_mean_mibs", "overall_throughput_stddev_mibs",
    "overall_triad_mean_mibs", "overall_triad_stddev_mibs",
    "service_interruption_ms",   # time server was fully unresponsive (full=full_total_ms, live=downtime_us/1000)
    # STREAM benchmark — per-window
    "stream_baseline_copy_mibs",  "stream_baseline_scale_mibs",
    "stream_baseline_add_mibs",   "stream_baseline_triad_mibs",
    "stream_during_copy_mibs",    "stream_during_scale_mibs",
    "stream_during_add_mibs",     "stream_during_triad_mibs",
    "stream_triad_degradation_pct",
    "stream_post_copy_mibs", "stream_post_scale_mibs",
    "stream_post_add_mibs",  "stream_post_triad_mibs",
    # Timeseries (throughput timeline — live snapshot only)
    "timeseries_file",
    "ts_snap_start_s",
    "ts_snap_end_s",
    "ts_freeze_start_s",
    "ts_freeze_end_s",
    "timeseries_failed_samples",   # count of samples where host couldn't connect
    "network_packets_dropped",
]
