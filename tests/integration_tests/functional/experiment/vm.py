# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""VM boot and snapshot helpers for the snapshot live experiment."""

import json
import os
import subprocess
import time
from pathlib import Path

from framework.microvm import Snapshot, SnapshotType

from .constants import MEMORY_FILL_FRACTION, VCPU_COUNT


def _get_iface_dropped(vm):
    """Return (rx_dropped, tx_dropped) for the VM's TAP device."""
    result = subprocess.run(
        ["ip", "netns", "exec", vm.netns.id, "ip", "-s", "-j", "link"],
        capture_output=True, text=True, check=True,
    )
    for iface in json.loads(result.stdout):
        stats = iface.get("stats64", iface.get("stats", {}))
        rx = stats.get("rx", {})
        tx = stats.get("tx", {})
        return int(rx.get("dropped", 0)), int(tx.get("dropped", 0))
    return 0, 0


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


def _do_full_snapshot_resume_timed(vm):
    """Take a full snapshot then resume the same VM, returning (snapshot, timing_dict).

    Unlike _do_full_snapshot_timed this does not leave the VM paused: after the
    snapshot files are written the VM is immediately resumed so the workload
    continues on the same instance.  Callers are responsible for deleting the
    returned snapshot when it is no longer needed.
    """
    t0 = time.monotonic()
    vm.pause()
    t_paused = time.monotonic()
    vm.api.snapshot_create.put(
        mem_file_path="mem",
        snapshot_path="vmstate",
        snapshot_type="Full",
    )
    t_created = time.monotonic()
    vm.resume()

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
        "full_pause_ms":  (t_paused  - t0) * 1000,
        "full_create_ms": (t_created - t_paused) * 1000,
        "full_total_ms":  (t_created - t0) * 1000,
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


def _boot_app_experiment_vm(uvm_plain, microvm_factory, mem_size_mib, *, bpf=False):
    """Boot a VM for application workload experiments.

    If EXPERIMENT_ROOTFS env var is set, builds a fresh VM using that rootfs.
    Otherwise uses uvm_plain directly.  Disables memory_monitor, spawns,
    configures with VCPU_COUNT vCPUs and mem_size_mib RAM, adds a net iface,
    and waits until SSH is ready.

    Pass bpf=True to run the jailer as uid=0/gid=0 (required for CAP_BPF /
    snapshot_live_bpf).
    """
    if os.environ.get("EXPERIMENT_ROOTFS"):
        build_kwargs = {}
        if bpf:
            build_kwargs["jailer_kwargs"] = {"uid": 0, "gid": 0}
        vm = microvm_factory.build(
            kernel=uvm_plain.kernel_file,
            rootfs=Path(os.environ["EXPERIMENT_ROOTFS"]),
            **build_kwargs,
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


def _boot_bpf_experiment_vm(microvm_factory, kernel_file, rootfs_file, mem_size_mib):
    """Boot a VM with uid=0/gid=0 jailer (required for CAP_BPF / bpf_fault)."""
    vm = microvm_factory.build(
        kernel=kernel_file,
        rootfs=rootfs_file,
        jailer_kwargs={"uid": 0, "gid": 0},
        monitor_memory=False,
    )
    vm.spawn()
    vm.basic_config(vcpu_count=VCPU_COUNT, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    # Condition memory: populate MEMORY_FILL_FRACTION of guest RAM.
    prefill_mib = max(int(mem_size_mib * MEMORY_FILL_FRACTION), 16)
    vm.ssh.check_output(
        f"head -c {prefill_mib}M /dev/urandom > /tmp/prefill 2>/dev/null; sync",
        timeout=120,
    )

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

    # Condition memory: populate MEMORY_FILL_FRACTION of guest RAM so the
    # snapshot streaming time is representative and matches the QEMU benchmark's
    # --guest-memory-fill-bytes target (default ~75 % of guest RAM).
    prefill_mib = max(int(mem_size_mib * MEMORY_FILL_FRACTION), 16)
    vm.ssh.check_output(
        f"head -c {prefill_mib}M /dev/urandom > /tmp/prefill 2>/dev/null; sync",
        timeout=120,
    )

    return vm
