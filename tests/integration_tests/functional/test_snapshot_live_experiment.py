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

import logging
import os
from pathlib import Path

import pytest

from .experiment import (
    APP_MEM_SIZES,
    VCPU_COUNT,
    _boot_app_experiment_vm,
    _boot_bpf_experiment_vm,
    _boot_experiment_vm,
    _check_workload_tools,
    _log_app_summary,
    _log_summary,
    _run_full_snapshot,
    _run_full_snapshot_app,
    _run_live_bpf_snapshot,
    _run_live_bpf_snapshot_app,
    _run_live_snapshot,
    _run_live_snapshot_app,
    _write_csv_row,
)

logger = logging.getLogger(__name__)


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


@pytest.mark.nonci
@pytest.mark.timeout(300)
@pytest.mark.parametrize("mem_size_mib", [256, 512, 1024, 2048, 4096])
@pytest.mark.parametrize("workload", ["idle", "light", "medium", "heavy"])
@pytest.mark.parametrize("iteration", range(10))
def test_live_bpf_snapshot_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect LiveBpf snapshot metrics under controlled memory workload."""
    vm = _boot_bpf_experiment_vm(
        microvm_factory, uvm_plain.kernel_file, uvm_plain.rootfs_file, mem_size_mib
    )

    row = _run_live_bpf_snapshot(vm, microvm_factory, mem_size_mib, workload, iteration)

    # Attach key metrics to JUnit XML.
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("throughput_mibs", row.get("throughput_mibs", 0))
    record_property("fault_fraction_pct", row.get("fault_fraction_pct", 0))

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

    # Boot a fresh VM for the BPF live snapshot path (requires uid=0 jailer).
    vm_bpf = _boot_bpf_experiment_vm(
        microvm_factory, vm_full.kernel_file, vm_full.rootfs_file, mem_size_mib
    )
    bpf_row = _run_live_bpf_snapshot(
        vm_bpf, microvm_factory, mem_size_mib, workload, iteration=0
    )
    _write_csv_row(bpf_row)
    _log_summary(bpf_row)

    # --- Side-by-side summary ---
    full_dt = full_row.get("downtime_us", 0)
    live_dt = live_row.get("downtime_us", 0)
    bpf_dt = bpf_row.get("downtime_us", 0)
    speedup_live = full_dt / live_dt if live_dt > 0 else float("inf")
    speedup_bpf = full_dt / bpf_dt if bpf_dt > 0 else float("inf")

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "COMPARISON: %d MiB, %s workload, %d vCPUs", mem_size_mib, workload, VCPU_COUNT
    )
    logger.info("=" * 70)
    logger.info("                        Full          Live         LiveBpf      Speedup")
    logger.info(
        "  Downtime:       %8.1f ms    %8.1f ms    %8.1f ms    %6.1fx / %6.1fx",
        full_dt / 1000,
        live_dt / 1000,
        bpf_dt / 1000,
        speedup_live,
        speedup_bpf,
    )
    logger.info(
        "  Wall-clock:     %8.1f ms    %8.1f ms    %8.1f ms",
        full_row.get("total_us", 0) / 1000,
        live_row.get("total_us", 0) / 1000,
        bpf_row.get("total_us", 0) / 1000,
    )
    logger.info(
        "  Restore→SSH:    %8.1f ms    %8.1f ms    %8.1f ms",
        full_row.get("ssh_ready_ms", 0),
        live_row.get("ssh_ready_ms", 0),
        bpf_row.get("ssh_ready_ms", 0),
    )
    if workload != "idle":
        logger.info(
            "  Workload degr:       N/A         %6.1f %%      %6.1f %%",
            live_row.get("workload_degradation_pct", 0),
            bpf_row.get("workload_degradation_pct", 0),
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


@pytest.mark.nonci
@pytest.mark.timeout(900)
@pytest.mark.parametrize("mem_size_mib", APP_MEM_SIZES)
@pytest.mark.parametrize("workload", ["redis_light", "redis_mixed", "redis_heavy"])
@pytest.mark.parametrize("iteration", range(10))
def test_live_bpf_snapshot_app_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect live-bpf-snapshot metrics under Redis workload."""
    vm = _boot_app_experiment_vm(uvm_plain, microvm_factory, mem_size_mib, bpf=True)
    _check_workload_tools(vm, workload)
    row = _run_live_bpf_snapshot_app(vm, microvm_factory, mem_size_mib, workload, iteration)
    record_property("downtime_us", row.get("downtime_us", 0))
    record_property("throughput_mibs", row.get("throughput_mibs", 0))
    _write_csv_row(row)
    _log_app_summary(row)
