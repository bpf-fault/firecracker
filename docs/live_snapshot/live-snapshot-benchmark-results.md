# Live Snapshot Benchmark Results

## Overview

Firecracker's new **live snapshot** mode uses Linux userfaultfd write-protect (UFFD-WP) to capture a point-in-time-consistent VM snapshot with only a brief pause (for device/vCPU state save), while the VM continues running during the RAM dump phase.

This document reports benchmark results comparing live snapshots to full (paused) snapshots on a 4 GiB VM.

**Test environment:** Linux 6.8.0, 2 vCPUs, debug build, local disk I/O.

---

## Idle VM: Full vs Live Snapshot (4 GiB, 2 vCPUs)

### Full Snapshot

The VM is fully paused for the entire duration of the memory dump.

| Phase | Time |
|-------|------|
| Pause vCPUs | 4.5 ms |
| Create snapshot (memory dump + state save) | 3,430.3 ms |
| **Total (= VM downtime)** | **3,434.8 ms** |
| Throughput | 1,194 MiB/s |

### Live Snapshot

The VM is paused only during Phase 2 (freeze). It continues running during the RAM streaming phase.

| Phase | Time | Notes |
|-------|------|-------|
| **Phase 1 — Prepare** | **315.0 ms** | |
| &emsp;Populate pages | 314.9 ms | Read 1 byte/page to ensure PTEs exist |
| **Phase 2 — Freeze (= VM downtime)** | **108.8 ms** | |
| &emsp;Pause vCPUs | 0.1 ms | |
| &emsp;Save device state | 23.0 ms | Serialize vCPU + device state to memory |
| &emsp;Enable write-protect | 85.5 ms | UFFD register + WP all guest pages |
| &emsp;Resume vCPUs | 0.1 ms | |
| **Phase 3 — Stream RAM** | **9,939.7 ms** | VM running, writes to unsaved pages fault-block |
| &emsp;Total pages | 1,048,576 (4,096 MiB) | |
| &emsp;Throughput | 412 MiB/s | |
| &emsp;Fault-driven pages | 1,876 (0.18%) | Pages the guest wrote to before linear scan reached them |
| &emsp;Linear-scan pages | 1,046,700 (99.82%) | |
| **Phase 4 — Finalize** | **79.8 ms** | Kick devices, unregister UFFD, sync files |
| **Total wall-clock** | **10,443.4 ms** | |
| **VM downtime** | **108.8 ms (1.04%)** | |

### Restore Performance

| Metric | Full Snapshot | Live Snapshot |
|--------|--------------|---------------|
| Restore API call | 228.8 ms | 266.7 ms |
| SSH ready | 243.9 ms | 281.0 ms |

### Comparison

| Metric | Full | Live | Speedup |
|--------|------|------|---------|
| **VM downtime** | **3,434.8 ms** | **108.8 ms** | **31.6x** |
| Wall-clock time | 3,434.8 ms | 10,443.4 ms | 0.33x (3x slower) |
| Restore (SSH ready) | 243.9 ms | 281.0 ms | ~same |

---

## Under Memory-Write Load (4 GiB, 2 vCPUs)

A background workload continuously writes random data to guest memory (`dd if=/dev/urandom bs=4096 count=4096` in a loop), exercising the UFFD write-protect fault path during snapshot.

| Phase | Time | Notes |
|-------|------|-------|
| **Phase 1 — Prepare** | **1,025.9 ms** | Longer due to active memory allocation |
| **Phase 2 — Freeze (= VM downtime)** | **95.9 ms** | |
| &emsp;Pause vCPUs | 0.1 ms | |
| &emsp;Save device state | 27.9 ms | |
| &emsp;Enable write-protect | 67.8 ms | |
| &emsp;Resume vCPUs | 0.1 ms | |
| **Phase 3 — Stream RAM** | **21,615.0 ms** | Slower due to fault handling overhead |
| &emsp;Throughput | 189 MiB/s | |
| &emsp;Fault-driven pages | 6,450 (0.62%) | 3.4x more faults than idle |
| &emsp;Linear-scan pages | 1,042,126 (99.38%) | |
| **Phase 4 — Finalize** | **79.8 ms** | |
| **Total wall-clock** | **22,816.7 ms** | |
| **VM downtime** | **95.9 ms (0.42%)** | |

### Restore from Under-Load Snapshot

| Metric | Time |
|--------|------|
| Restore API call | 269.6 ms |
| SSH ready | 284.4 ms |

---

## Key Takeaways

1. **31x downtime reduction**: Live snapshots reduce VM downtime from ~3.4 seconds to ~100 milliseconds for a 4 GiB VM — the VM is paused for only 1% of the total snapshot duration.

2. **Downtime is dominated by write-protect setup**: The ~86ms `enable WP` step (UFFD register + write-protect all pages) accounts for ~80% of the freeze window. The actual vCPU pause/resume is sub-millisecond.

3. **Trade-off: wall-clock time increases ~3x**: The live snapshot takes ~10s vs ~3.4s for a full snapshot. This is because:
   - Per-page fault handling adds overhead to the streaming phase
   - Each page must be flushed to disk before removing write-protection (to prevent data races)
   - UFFD event polling adds latency between page saves

4. **Under load, streaming throughput drops ~2x**: From 412 MiB/s (idle) to 189 MiB/s (under load), because fault-driven page saves interrupt the linear scan and require additional UFFD ioctl calls.

5. **Restore performance is identical**: Snapshots produced by either method are in the same format. Restore times are comparable (~230-280ms to SSH ready).

6. **Fault-driven pages are a small fraction**: Even under active memory writes, only 0.6% of pages triggered write-protect faults. The linear scan handles the vast majority of pages.

---

## Architecture

```
Phase 1: PREPARE (VM running)
  ├─ Validate kernel supports UFFD-WP
  ├─ Open memory output file
  └─ Populate all RAM pages (read 1 byte/page to ensure PTEs exist)

Phase 2: FREEZE (VM briefly paused, ~100ms)
  ├─ Pause vCPUs
  ├─ Save device + vCPU + KVM state to in-memory buffer
  ├─ Create UFFD fd, register all guest memory in WP mode
  ├─ Write-protect all guest memory
  └─ Resume vCPUs (without kicking virtio devices)

Phase 3: STREAM RAM (VM running, vCPU writes to unsaved pages fault and block)
  └─ Loop until all pages saved:
      ├─ Non-blocking read of UFFD fd for WP faults
      ├─ If fault: save that page, flush, remove WP (unblocks vCPU)
      └─ Otherwise: save next linear page, flush, remove WP

Phase 4: FINALIZE
  ├─ Kick virtio devices (deferred from resume)
  ├─ Unregister memory from UFFD, drop UFFD fd
  ├─ Write buffered device state to snapshot file
  └─ Sync files to disk
```
