# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""VM boot and snapshot helpers for the snapshot live experiment."""

import os
import time
from pathlib import Path

from framework.microvm import Snapshot, SnapshotType

from .constants import VCPU_COUNT


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
