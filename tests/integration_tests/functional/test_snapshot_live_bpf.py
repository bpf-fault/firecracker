# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for bpf_fault-based live snapshot (SnapshotType::LiveBpf).

Requires Linux >= 6.12 with CONFIG_BPF_FAULT=y. All tests are marked @nonci
since CI kernels may not have bpf_fault support.
"""

import logging
import time

import pytest

from framework.microvm import SnapshotType

logger = logging.getLogger(__name__)


@pytest.mark.nonci
def test_live_bpf_snapshot_basic(
    microvm_factory, guest_kernel_linux_5_10, rootfs
):
    """Test that a bpf_fault live snapshot can be taken and restored.

    Takes a snapshot while the VM is running using bpf_fault write-protect,
    restores it, and verifies the restored VM works correctly.
    """
    # BPF requires CAP_BPF — need uid=0/gid=0 in the jailer.
    vm = microvm_factory.build(
        guest_kernel_linux_5_10,
        rootfs,
        jailer_kwargs={"uid": 0, "gid": 0},
        monitor_memory=False,
    )
    vm.spawn()
    vm.basic_config(vcpu_count=2, mem_size_mib=256)
    vm.add_net_iface()
    vm.start()

    # Verify VM is running and responsive before snapshot.
    vm.ssh.check_output("true")

    # Take live-bpf snapshot while VM is running — no pause!
    assert vm.state == "Running"
    snapshot = vm.snapshot_live_bpf()
    assert snapshot.snapshot_type == SnapshotType.LIVE_BPF

    # VM should still be running after live snapshot.
    assert vm.state == "Running"
    vm.ssh.check_output("true")

    # Restore snapshot in a new VM.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)
    assert restored_vm.state == "Running"
    restored_vm.ssh.check_output("true")


@pytest.mark.nonci
def test_live_bpf_snapshot_memory_integrity(
    microvm_factory, guest_kernel_linux_5_10, rootfs
):
    """Verify that guest memory contents survive a bpf_fault live snapshot.

    Writes a known pattern into guest tmpfs, takes a snapshot, restores,
    and checks the pattern is intact.
    """
    # BPF requires CAP_BPF — need uid=0/gid=0 in the jailer.
    vm = microvm_factory.build(
        guest_kernel_linux_5_10,
        rootfs,
        jailer_kwargs={"uid": 0, "gid": 0},
        monitor_memory=False,
    )
    vm.spawn()
    vm.basic_config(vcpu_count=2, mem_size_mib=512)
    vm.add_net_iface()
    vm.start()

    # Write a known pattern into a file in guest memory (tmpfs).
    pattern = "LIVE_BPF_SNAPSHOT_INTEGRITY_CHECK_" + "A" * 200
    vm.ssh.check_output(f"echo -n '{pattern}' > /tmp/test_pattern")
    _, stdout, _ = vm.ssh.check_output("cat /tmp/test_pattern")
    assert stdout.strip() == pattern

    # Take live-bpf snapshot while VM is running.
    snapshot = vm.snapshot_live_bpf()

    # Restore and verify the pattern is intact.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)
    _, stdout, _ = restored_vm.ssh.check_output("cat /tmp/test_pattern")
    assert stdout.strip() == pattern


@pytest.mark.nonci
def test_live_bpf_snapshot_under_load(
    microvm_factory, guest_kernel_linux_5_10, rootfs
):
    """Test bpf_fault live snapshot while the VM is actively writing memory.

    Starts a memory workload in the guest, takes a snapshot, and verifies
    the VM and restored snapshot work correctly.
    """
    # BPF requires CAP_BPF — need uid=0/gid=0 in the jailer.
    vm = microvm_factory.build(
        guest_kernel_linux_5_10,
        rootfs,
        jailer_kwargs={"uid": 0, "gid": 0},
        monitor_memory=False,
    )
    vm.spawn()
    vm.basic_config(vcpu_count=2, mem_size_mib=256)
    vm.add_net_iface()
    vm.start()

    # Start a memory-write workload in the background.
    vm.ssh.check_output(
        "nohup sh -c '"
        "while true; do dd if=/dev/urandom of=/tmp/workload bs=4096 count=256 2>/dev/null; "
        "done' </dev/null >/dev/null 2>&1 &"
    )

    # Give the workload a moment to start.
    time.sleep(1)

    # Take live-bpf snapshot while workload is running.
    snapshot = vm.snapshot_live_bpf()

    # VM should still be running and responsive.
    assert vm.state == "Running"
    vm.ssh.check_output("true")

    # Restore and verify.
    restored_vm = microvm_factory.build(monitor_memory=False)
    restored_vm.spawn()
    restored_vm.restore_from_snapshot(snapshot, resume=True)
    assert restored_vm.state == "Running"
    restored_vm.ssh.check_output("true")
