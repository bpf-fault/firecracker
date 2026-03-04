# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""3-way snapshot benchmark: Full vs Live (uffd) vs LiveBpf (bpf_fault)."""

import logging
import re
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from framework.microvm import SnapshotType

logger = logging.getLogger(__name__)


# ── Guest-side write throughput measurement helpers ──────────────────────


# C program compiled and run inside the guest.  Each writer:
#   1. mmap's an anonymous region of `size_mb` MiB
#   2. Loops forever, writing 4 KiB pages at random offsets (LCG PRNG)
#   3. After every BATCH pages written, atomically updates a counter file
#
# Using mmap + direct memory writes avoids dd/shell overhead and ensures
# every write is a genuine 4 KiB dirty at a random guest physical page.
_WRITER_C = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 4) return 1;
    long size_mb = atol(argv[1]);
    int batch    = atoi(argv[2]);
    const char *counter_path = argv[3];

    long size = size_mb << 20;
    long n_pages = size >> 12;

    char *mem = mmap(NULL, size, PROT_READ|PROT_WRITE,
                     MAP_PRIVATE|MAP_ANONYMOUS|MAP_POPULATE, -1, 0);
    if (mem == MAP_FAILED) return 2;

    /* Simple LCG PRNG — good enough for spreading writes. */
    unsigned long rng = getpid() ^ 0xdeadbeef;

    char buf[4096];
    memset(buf, 0xAA, sizeof(buf));

    long counter = 0;
    char tmp[256];
    snprintf(tmp, sizeof(tmp), "%s.tmp", counter_path);

    for (;;) {
        for (int i = 0; i < batch; i++) {
            rng = rng * 6364136223846793005ULL + 1442695040888963407ULL;
            long pg = (long)((rng >> 16) % (unsigned long)n_pages);
            /* Dirty one 4 KiB page at a random offset. */
            memcpy(mem + (pg << 12), buf, 4096);
        }
        counter++;
        /* Atomic counter update: write to tmp, rename over. */
        int fd = open(tmp, O_WRONLY|O_CREAT|O_TRUNC, 0644);
        if (fd >= 0) {
            dprintf(fd, "%ld\n", counter);
            close(fd);
            rename(tmp, counter_path);
        }
    }
}
"""

# Pages written per batch by each writer.
_BATCH_PAGES = 256  # 1 MiB worth of 4 KiB page touches per batch


def _kill_writers(vm):
    """Kill any existing writer processes in the guest."""
    # The [w]riter bracket trick prevents pgrep from matching its own cmdline.
    vm.ssh.check_output(
        "pgrep -f '[w]riter' | xargs -r kill 2>/dev/null; true"
    )
    time.sleep(0.5)


# Lazily-built host path to the static writer binary.
_writer_bin_path = None


def _get_writer_binary():
    """Compile the writer C program on the host (once), return path."""
    global _writer_bin_path
    if _writer_bin_path and Path(_writer_bin_path).exists():
        return _writer_bin_path

    tmpdir = tempfile.mkdtemp(prefix="fc_writer_")
    src = Path(tmpdir) / "writer.c"
    binary = Path(tmpdir) / "writer"
    src.write_text(_WRITER_C)
    subprocess.check_call(
        ["gcc", "-static", "-O2", "-o", str(binary), str(src)]
    )
    _writer_bin_path = str(binary)
    return _writer_bin_path


def _start_writers(vm, n_writers, file_mb_each):
    """Kill any old writers, upload binary if needed, start N fresh writers."""
    _kill_writers(vm)
    # Upload the static binary if not already present.
    ret = vm.ssh.run("test -x /tmp/writer")
    if ret.returncode != 0:
        host_bin = _get_writer_binary()
        vm.ssh.scp_put(host_bin, "/tmp/writer")
        vm.ssh.check_output("chmod +x /tmp/writer")
    for i in range(n_writers):
        vm.ssh.check_output(
            f"nohup /tmp/writer {file_mb_each} {_BATCH_PAGES} /tmp/wc_{i} "
            f"</dev/null >/dev/null 2>&1 &"
        )


def _read_counters(vm, n_writers):
    """Read all writer counters, returns list of ints."""
    out = vm.ssh.check_output(
        f"cat /tmp/wc_{{0..{n_writers - 1}}}"
    ).stdout
    return [int(x) for x in out.strip().split()]


def _measure_baseline(vm, n_writers, duration_s=3):
    """Measure guest write throughput with no snapshot running."""
    c0 = _read_counters(vm, n_writers)
    t0 = time.monotonic()
    time.sleep(duration_s)
    c1 = _read_counters(vm, n_writers)
    t1 = time.monotonic()
    dt = t1 - t0
    total_batches = sum(b - a for a, b in zip(c0, c1))
    return {
        "batches": total_batches,
        "duration_s": dt,
        "pages_per_sec": total_batches * _BATCH_PAGES / dt,
        "mib_per_sec": total_batches * _BATCH_PAGES * 4096 / (1024 * 1024) / dt,
    }


def _snapshot_with_guest_throughput(vm, n_writers, mode):
    """Take a live snapshot while measuring guest write throughput.

    The snapshot API is blocking, so the counter delta between the SSH reads
    bracketing the call shows how much writing the guest completed *during*
    the snapshot.

    Returns (snapshot, metrics_dict, guest_throughput_dict).
    """
    c_before = _read_counters(vm, n_writers)
    t_before = time.monotonic()

    if mode == "Live":
        snap = vm.snapshot_live()
        prefix = "Live snapshot"
    else:
        snap = vm.snapshot_live_bpf()
        prefix = "Live-BPF snapshot"

    t_after = time.monotonic()
    c_after = _read_counters(vm, n_writers)

    dt = t_after - t_before
    total_batches = sum(b - a for a, b in zip(c_before, c_after))

    metrics = _parse_live_snapshot_log(vm.log_data, prefix)
    guest = {
        "batches": total_batches,
        "duration_s": dt,
        "pages_per_sec": total_batches * _BATCH_PAGES / dt if dt > 0 else 0,
        "mib_per_sec": total_batches * _BATCH_PAGES * 4096 / (1024 * 1024) / dt
        if dt > 0
        else 0,
    }
    return snap, metrics, guest


def _parse_live_snapshot_log(log_data, prefix="Live snapshot"):
    """Extract timing metrics from Firecracker live-snapshot log lines."""
    metrics = {}

    m = re.search(rf"{prefix}: Phase 2 \(freeze\) took (\d+) us "
                  r"\(pause=(\d+) us, save_state=(\d+) us, "
                  r"wp_enable=(\d+) us, resume=(\d+) us\)", log_data)
    if m:
        metrics["freeze_total_us"] = int(m.group(1))
        metrics["pause_us"] = int(m.group(2))
        metrics["save_state_us"] = int(m.group(3))
        metrics["wp_enable_us"] = int(m.group(4))
        metrics["resume_us"] = int(m.group(5))

    m = re.search(rf"{prefix}: Phase 3 \(stream\) took (\d+) us, (\d+) pages total "
                  r"\((\d+) (?:fault-driven|ring-buffer), (\d+) linear-scan(?:, (\d+) ring-drops)?\)",
                  log_data)
    if m:
        metrics["stream_us"] = int(m.group(1))
        metrics["total_pages"] = int(m.group(2))
        metrics["fault_pages"] = int(m.group(3))
        metrics["linear_pages"] = int(m.group(4))
        if m.group(5) is not None:
            metrics["ring_drops"] = int(m.group(5))

    m = re.search(rf"{prefix}: Phase 4 \(finalize\) took (\d+) us", log_data)
    if m:
        metrics["finalize_us"] = int(m.group(1))

    m = re.search(rf"{prefix}: complete in (\d+) us \(freeze/downtime=(\d+) us\)", log_data)
    if m:
        metrics["total_us"] = int(m.group(1))
        metrics["downtime_us"] = int(m.group(2))

    return metrics


def _do_full_snapshot_timed(vm):
    """Take a full snapshot, returning (snapshot, timing_dict)."""
    from pathlib import Path
    from framework.microvm import Snapshot

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
            "kernel_file": vm.kernel_file,
            "initrd_file": vm.initrd_file,
            "vcpus_count": vm.vcpus_count,
        },
    )
    return snapshot, {
        "pause_ms": (t_paused - t0) * 1000,
        "create_ms": (t_created - t_paused) * 1000,
        "total_ms": (t_created - t0) * 1000,
    }


def _do_restore_timed(factory, snapshot):
    """Restore a snapshot, returning (vm, timing_dict)."""
    rvm = factory.build(monitor_memory=False)
    rvm.spawn()
    t0 = time.monotonic()
    rvm.restore_from_snapshot(snapshot, resume=True)
    t_restored = time.monotonic()
    rvm.ssh.check_output("true")
    t_ssh = time.monotonic()
    return rvm, {
        "restore_api_ms": (t_restored - t0) * 1000,
        "ssh_ready_ms": (t_ssh - t0) * 1000,
    }


def _log_live_metrics(label, metrics):
    """Pretty-print timing metrics for a live snapshot mode."""
    if not metrics:
        logger.warning("%s: no timing metrics found in log", label)
        return
    total_ms = metrics.get("total_us", 0) / 1000
    downtime_ms = metrics.get("downtime_us", 0) / 1000
    pages = metrics.get("total_pages", 0)
    mem_mb = pages * 4096 / (1024 * 1024)
    stream_s = metrics.get("stream_us", 1) / 1e6
    throughput = mem_mb / stream_s if stream_s > 0 else 0

    logger.info("  %s — Timing Breakdown", label)
    logger.info("    freeze (downtime):    %8.1f ms", downtime_ms)
    logger.info("      pause:              %8.1f ms", metrics.get("pause_us", 0) / 1000)
    logger.info("      save_state:         %8.1f ms", metrics.get("save_state_us", 0) / 1000)
    logger.info("      wp_enable:          %8.1f ms", metrics.get("wp_enable_us", 0) / 1000)
    logger.info("      resume:             %8.1f ms", metrics.get("resume_us", 0) / 1000)
    logger.info("    stream:               %8.1f ms  [%.0f MiB, %.0f MiB/s]",
                metrics.get("stream_us", 0) / 1000, mem_mb, throughput)
    logger.info("      fault/ring pages:   %8d", metrics.get("fault_pages", 0))
    logger.info("      linear pages:       %8d", metrics.get("linear_pages", 0))
    logger.info("    finalize:             %8.1f ms", metrics.get("finalize_us", 0) / 1000)
    logger.info("    TOTAL wall-clock:     %8.1f ms", total_ms)


@pytest.mark.skip(reason="Use test_snapshot_3way_under_load for benchmarking")
@pytest.mark.nonci
@pytest.mark.timeout(300)
@pytest.mark.parametrize("mem_size_mib", [2048])
def test_snapshot_3way_benchmark(uvm_plain, microvm_factory, mem_size_mib):
    """3-way benchmark: Full vs Live (uffd) vs LiveBpf (bpf_fault).

    For each mode, takes a snapshot from a running VM, restores it,
    and reports timing metrics.
    """
    vcpus = 2

    vm = uvm_plain
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    # Back some pages so the test is meaningful.
    vm.ssh.check_output(
        "head -c 64M /dev/urandom > /tmp/fill 2>/dev/null; sync"
    )

    logger.info("")
    logger.info("=" * 72)
    logger.info("3-WAY BENCHMARK: %d MiB VM, %d vCPUs — IDLE", mem_size_mib, vcpus)
    logger.info("=" * 72)

    # ── 1. Full Snapshot ──────────────────────────────────────────────────
    logger.info("")
    logger.info("── FULL snapshot ──")
    full_snap, full_t = _do_full_snapshot_timed(vm)
    logger.info("  pause:                  %8.1f ms", full_t["pause_ms"])
    logger.info("  create:                 %8.1f ms", full_t["create_ms"])
    logger.info("  TOTAL (= DOWNTIME):     %8.1f ms", full_t["total_ms"])

    # Restore from full to get a running VM for live tests.
    full_rvm, full_restore = _do_restore_timed(microvm_factory, full_snap)
    full_rvm.monitors = [m for m in full_rvm.monitors if m is not full_rvm.memory_monitor]
    full_rvm.memory_monitor = None
    logger.info("  restore → SSH:          %8.1f ms", full_restore["ssh_ready_ms"])

    # ── 2. Live (uffd) Snapshot ───────────────────────────────────────────
    logger.info("")
    logger.info("── LIVE (uffd) snapshot ──")
    t0 = time.monotonic()
    live_snap = full_rvm.snapshot_live()
    live_wall_ms = (time.monotonic() - t0) * 1000
    assert full_rvm.state == "Running"

    live_metrics = _parse_live_snapshot_log(full_rvm.log_data, "Live snapshot")
    _log_live_metrics("Live (uffd)", live_metrics)

    live_rvm, live_restore = _do_restore_timed(microvm_factory, live_snap)
    live_rvm.monitors = [m for m in live_rvm.monitors if m is not live_rvm.memory_monitor]
    live_rvm.memory_monitor = None
    logger.info("  restore → SSH:          %8.1f ms", live_restore["ssh_ready_ms"])

    # ── 3. LiveBpf (bpf_fault) Snapshot ───────────────────────────────────
    # BPF requires CAP_BPF — need uid=0/gid=0 in the jailer.
    # First, take a fresh snapshot from the live-restored VM so we can
    # restore into a root-privileged jailer.
    bpf_source_snap = live_rvm.snapshot_live()
    bpf_source_vm = microvm_factory.build(
        jailer_kwargs={"uid": 0, "gid": 0}, monitor_memory=False
    )
    bpf_source_vm.spawn()
    bpf_source_vm.restore_from_snapshot(bpf_source_snap, resume=True)
    bpf_source_vm.ssh.check_output("true")

    logger.info("")
    logger.info("── LIVE-BPF (bpf_fault) snapshot ──")
    t0 = time.monotonic()
    bpf_snap = bpf_source_vm.snapshot_live_bpf()
    bpf_wall_ms = (time.monotonic() - t0) * 1000
    assert bpf_source_vm.state == "Running"

    bpf_metrics = _parse_live_snapshot_log(bpf_source_vm.log_data, "Live-BPF snapshot")
    _log_live_metrics("LiveBpf (bpf_fault)", bpf_metrics)

    bpf_rvm, bpf_restore = _do_restore_timed(microvm_factory, bpf_snap)
    logger.info("  restore → SSH:          %8.1f ms", bpf_restore["ssh_ready_ms"])

    # ── Summary Table ─────────────────────────────────────────────────────
    full_downtime_ms = full_t["total_ms"]
    live_downtime_ms = live_metrics.get("downtime_us", 0) / 1000
    bpf_downtime_ms = bpf_metrics.get("downtime_us", 0) / 1000

    live_total_ms = live_metrics.get("total_us", 0) / 1000
    bpf_total_ms = bpf_metrics.get("total_us", 0) / 1000

    def _thruput(m):
        pages = m.get("total_pages", 0)
        stream_us = m.get("stream_us", 0)
        if stream_us == 0:
            return 0
        return (pages * 4096 / 1048576) / (stream_us / 1e6)

    logger.info("")
    logger.info("=" * 72)
    logger.info("SUMMARY: %d MiB VM, %d vCPUs (idle)", mem_size_mib, vcpus)
    logger.info("=" * 72)
    logger.info("                         Full         Live(uffd)   LiveBpf")
    logger.info("  VM downtime:       %8.1f ms   %8.1f ms   %8.1f ms",
                full_downtime_ms, live_downtime_ms, bpf_downtime_ms)
    logger.info("  Wall-clock:        %8.1f ms   %8.1f ms   %8.1f ms",
                full_t["total_ms"], live_total_ms, bpf_total_ms)
    logger.info("  Stream thruput:         N/A      %8.0f MiB/s %7.0f MiB/s",
                _thruput(live_metrics), _thruput(bpf_metrics))
    logger.info("  Fault/ring pages:       N/A      %8d      %8d",
                live_metrics.get("fault_pages", 0),
                bpf_metrics.get("fault_pages", 0))
    logger.info("  Restore → SSH:     %8.1f ms   %8.1f ms   %8.1f ms",
                full_restore["ssh_ready_ms"],
                live_restore["ssh_ready_ms"],
                bpf_restore["ssh_ready_ms"])

    if live_downtime_ms > 0:
        logger.info("  Downtime speedup vs Full:")
        logger.info("    Live(uffd):      %8.1fx", full_downtime_ms / live_downtime_ms)
    if bpf_downtime_ms > 0:
        logger.info("    LiveBpf:         %8.1fx", full_downtime_ms / bpf_downtime_ms)
    if bpf_downtime_ms > 0 and live_downtime_ms > 0:
        logger.info("  LiveBpf vs Live(uffd) downtime: %.1fx",
                    live_downtime_ms / bpf_downtime_ms)
    logger.info("=" * 72)


@pytest.mark.nonci
@pytest.mark.timeout(600)
@pytest.mark.parametrize("mem_size_mib", [2048])
def test_snapshot_3way_under_load(uvm_plain, microvm_factory, mem_size_mib):
    """3-way benchmark under memory-write load.

    Measures guest-side write throughput (pages/sec, MiB/s) before and
    during each snapshot type to quantify the impact on the guest workload.
    """
    vcpus = 2
    n_writers = 8
    file_mb_each = int(mem_size_mib * 0.9) // n_writers  # ~90% of VM RAM

    vm = uvm_plain
    vm.monitors = [m for m in vm.monitors if m is not vm.memory_monitor]
    vm.memory_monitor = None
    vm.spawn()
    vm.basic_config(vcpu_count=vcpus, mem_size_mib=mem_size_mib)
    vm.add_net_iface()
    vm.start()
    vm.ssh.check_output("true")

    logger.info("")
    logger.info("=" * 72)
    logger.info("GUEST-IMPACT BENCHMARK: %d MiB VM, %d vCPUs, %d writers",
                mem_size_mib, vcpus, n_writers)
    logger.info("=" * 72)

    # ── Full snapshot (baseline, idle — no writers) ───────────────────────
    logger.info("")
    logger.info("── FULL snapshot (baseline, idle) ──")
    full_snap, full_t = _do_full_snapshot_timed(vm)
    logger.info("  TOTAL (= DOWNTIME):     %8.1f ms", full_t["total_ms"])

    # Restore into a root-privileged jailer (needed for BPF's CAP_BPF).
    # Both uffd and BPF tests run on identically-provisioned VMs restored
    # from the same full snapshot, ensuring fair baseline comparison.
    def _restore_root_vm(snap):
        rvm = microvm_factory.build(
            jailer_kwargs={"uid": 0, "gid": 0}, monitor_memory=False
        )
        rvm.spawn()
        rvm.restore_from_snapshot(snap, resume=True)
        rvm.ssh.check_output("true")
        return rvm

    # ── uffd test ─────────────────────────────────────────────────────────
    uffd_vm = _restore_root_vm(full_snap)

    _start_writers(uffd_vm, n_writers, file_mb_each)
    time.sleep(3)  # Let writers reach steady state.

    logger.info("")
    logger.info("── Baseline guest write throughput (no snapshot) ──")
    baseline = _measure_baseline(uffd_vm, n_writers, duration_s=3)
    logger.info("  %d batches in %.1f s", baseline["batches"], baseline["duration_s"])
    logger.info("  %8.0f pages/sec   (%6.1f MiB/s)",
                baseline["pages_per_sec"], baseline["mib_per_sec"])

    logger.info("")
    logger.info("── LIVE (uffd) snapshot — measuring guest impact ──")
    live_snap, live_metrics, live_guest = _snapshot_with_guest_throughput(
        uffd_vm, n_writers, "Live"
    )
    assert uffd_vm.state == "Running"
    _log_live_metrics("Live (uffd)", live_metrics)
    logger.info("  Guest writes during snapshot:")
    logger.info("    %d batches in %.1f s", live_guest["batches"], live_guest["duration_s"])
    logger.info("    %8.0f pages/sec   (%6.1f MiB/s)",
                live_guest["pages_per_sec"], live_guest["mib_per_sec"])
    if baseline["pages_per_sec"] > 0:
        logger.info("    Throughput retained: %5.1f%%",
                    live_guest["pages_per_sec"] / baseline["pages_per_sec"] * 100)

    uffd_vm.kill()

    # ── BPF test ──────────────────────────────────────────────────────────
    bpf_vm = _restore_root_vm(full_snap)

    _start_writers(bpf_vm, n_writers, file_mb_each)
    time.sleep(3)

    logger.info("")
    logger.info("── BPF baseline guest write throughput (no snapshot) ──")
    bpf_baseline = _measure_baseline(bpf_vm, n_writers, duration_s=3)
    logger.info("  %d batches in %.1f s", bpf_baseline["batches"], bpf_baseline["duration_s"])
    logger.info("  %8.0f pages/sec   (%6.1f MiB/s)",
                bpf_baseline["pages_per_sec"], bpf_baseline["mib_per_sec"])

    logger.info("")
    logger.info("── LIVE-BPF (bpf_fault) snapshot — measuring guest impact ──")
    bpf_snap, bpf_metrics, bpf_guest = _snapshot_with_guest_throughput(
        bpf_vm, n_writers, "LiveBpf"
    )
    _log_live_metrics("LiveBpf", bpf_metrics)
    logger.info("  Guest writes during snapshot:")
    logger.info("    %d batches in %.1f s", bpf_guest["batches"], bpf_guest["duration_s"])
    logger.info("    %8.0f pages/sec   (%6.1f MiB/s)",
                bpf_guest["pages_per_sec"], bpf_guest["mib_per_sec"])
    if bpf_baseline["pages_per_sec"] > 0:
        logger.info("    Throughput retained: %5.1f%%",
                    bpf_guest["pages_per_sec"] / bpf_baseline["pages_per_sec"] * 100)

    # ── Summary Table ─────────────────────────────────────────────────────
    full_downtime_ms = full_t["total_ms"]
    live_downtime_ms = live_metrics.get("downtime_us", 0) / 1000
    bpf_downtime_ms = bpf_metrics.get("downtime_us", 0) / 1000

    def _thruput(m):
        pages = m.get("total_pages", 0)
        stream_us = m.get("stream_us", 0)
        if stream_us == 0:
            return 0
        return (pages * 4096 / 1048576) / (stream_us / 1e6)

    live_retained = (live_guest["pages_per_sec"] / baseline["pages_per_sec"] * 100
                     if baseline["pages_per_sec"] > 0 else 0)
    bpf_retained = (bpf_guest["pages_per_sec"] / bpf_baseline["pages_per_sec"] * 100
                    if bpf_baseline["pages_per_sec"] > 0 else 0)

    logger.info("")
    logger.info("=" * 72)
    logger.info("SUMMARY: %d MiB VM, %d vCPUs, %d writers × %d MiB each",
                mem_size_mib, vcpus, n_writers, file_mb_each)
    logger.info("=" * 72)
    logger.info("                             Full(idle)   Live(uffd)   LiveBpf")
    logger.info("  VM downtime:            %8.1f ms   %8.1f ms   %8.1f ms",
                full_downtime_ms, live_downtime_ms, bpf_downtime_ms)
    logger.info("  VMM stream thruput:         N/A      %8.0f MiB/s %7.0f MiB/s",
                _thruput(live_metrics), _thruput(bpf_metrics))
    logger.info("  Fault/ring pages:           N/A      %8d      %8d",
                live_metrics.get("fault_pages", 0),
                bpf_metrics.get("fault_pages", 0))
    logger.info("  ─── Guest-side impact ───")
    logger.info("  Baseline write thruput:     N/A      %8.0f MiB/s %7.0f MiB/s",
                baseline["mib_per_sec"], bpf_baseline["mib_per_sec"])
    logger.info("  During-snap write thruput:  N/A      %8.0f MiB/s %7.0f MiB/s",
                live_guest["mib_per_sec"], bpf_guest["mib_per_sec"])
    logger.info("  Throughput retained:        N/A      %7.1f%%      %6.1f%%",
                live_retained, bpf_retained)
    if bpf_downtime_ms > 0 and live_downtime_ms > 0:
        logger.info("  LiveBpf vs Live(uffd) downtime: %.1fx",
                    live_downtime_ms / bpf_downtime_ms)
    logger.info("=" * 72)
