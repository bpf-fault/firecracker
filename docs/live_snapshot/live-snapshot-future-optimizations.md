# Live Snapshot — Future Optimizations

Potential improvements identified during review of the live snapshot feature
(rounds 1–4). These are not blocking issues — the feature is functional and
performant — but could further reduce memory overhead, improve throughput,
or tighten worst-case fault latency.

Baseline numbers (4 GiB VM, PCI_ON, release build, 2 vCPUs):

| Metric | Value |
|---|---|
| Full snapshot downtime | 3,448 ms |
| Live idle: wall / downtime | 3,458 ms / 44.3 ms |
| Live idle: Phase 3 throughput | 1,436 MiB/s |
| Live load: wall / downtime | 3,491 ms / 53.3 ms |
| Live load: Phase 3 throughput | 1,560 MiB/s |
| Downtime reduction vs full | 77.8x |

---

## 1. Eliminate `PageEntry` vec and `BTreeMap` — use arithmetic + BitVec

**Impact: high (memory + performance) · Effort: medium**

The current streaming loop allocates a `Vec<PageEntry>` with one entry per
guest page (1,048,576 entries for 4 GiB) and a `BTreeMap<usize, usize>` for
O(log n) fault-address lookup. Each `PageEntry` is ~25 bytes and each
BTreeMap node is ~48 bytes, totalling **~73 MiB of transient allocations**.
This is the reason the test memory monitor had to be disabled.

Since guest memory slots are contiguous and pages within a slot are uniform
size, a page's index can be computed from a fault address by arithmetic:

```
page_idx = (fault_addr - slot_base) / page_size + slot_page_offset
```

A compact `BitVec` (128 KiB for 1M pages) replaces the per-page `saved`
flag and the entire `BTreeMap`. The slot metadata (base pointer, base file
offset, length) is a small vec of slot descriptors — typically 1–3 entries
for DRAM regions.

This would:
- Reduce bookkeeping memory from ~73 MiB to ~128 KiB
- Replace O(log n) BTreeMap lookups with O(1) arithmetic
- Potentially allow re-enabling the memory monitor for smaller VMs

## 2. Increase `BATCH_SIZE` from 64 to 256

**Impact: medium (throughput) · Effort: trivial**

`BATCH_SIZE = 64` (256 KiB) results in ~16K write + WP-remove syscall
pairs for a 4 GiB VM. Increasing to 256 (1 MiB batches) would reduce
this to ~4K pairs — a 4x reduction in syscall overhead.

The tradeoff is longer maximum fault latency between event checks. At
1.5 GiB/s streaming throughput, writing 1 MiB takes ~0.7 ms, which is
well within acceptable bounds (the full snapshot writes 4 GiB in a single
call). This is a one-line constant change.

## 3. Drain all pending UFFD events before each linear-scan batch

**Impact: medium (worst-case fault latency) · Effort: low**

The streaming loop currently reads one UFFD event per iteration. If
multiple WP faults queue up while a linear-scan batch is being written,
only one fault is serviced before the next batch begins. Under heavy write
load, a faulting vCPU could wait for a full batch write before being
unblocked.

A tighter pattern: drain **all** pending faults in a loop (until
`read_event()` returns `Ok(None)`), then perform one linear batch. This
bounds worst-case fault latency to one batch write regardless of fault
queue depth.

## 4. Hoist `uffd_ref` out of the streaming loop

**Impact: low (cleanup) · Effort: trivial**

`guard.uffd.as_ref().expect("uffd exists")` is evaluated on every loop
iteration (~16K+ times). The uffd reference never changes during Phase 3.
Binding it once before the `while` loop avoids the repeated
`Option::as_ref` and panic path check.

## 5. Use `sync_data()` instead of `sync_all()` for the memory file

**Impact: low (correctness) · Effort: trivial**

`sync_all()` flushes both data and metadata (size, timestamps). Since the
file was freshly created with `set_len()` before writing, the metadata is
already correct on disk. `sync_data()` (`fdatasync`) skips the redundant
metadata flush. Unlikely to be measurable for a 4 GiB file, but it is the
semantically correct call.

## 6. Remove `.read(true)` when opening the memory output file

**Impact: low (correctness) · Effort: trivial**

The memory output file is opened with `.read(true)` but never read. This
causes `O_RDWR` instead of `O_WRONLY`. Removing it is more correct and
allows potential kernel-level optimizations for write-only descriptors.

## 7. Unify `snapshot_live()` with `make_snapshot()` in microvm.py

**Impact: low (maintainability) · Effort: low**

`snapshot_full()` and `snapshot_diff()` delegate to `make_snapshot()`, but
`snapshot_live()` duplicates the API call and `Snapshot` construction
inline. If `make_snapshot()` ever gains additional logic (path validation,
metadata), `snapshot_live()` would be out of sync. Since `make_snapshot()`
does not pause the VM (the caller does for full/diff), `snapshot_live()`
can simply call `self.make_snapshot(SnapshotType.LIVE, ...)`.

## 8. Add integration test for live snapshot with active balloon device

**Impact: low (test coverage) · Effort: medium**

The `Event::Remove` handler in the streaming loop handles balloon-driven
`madvise(MADV_DONTNEED)` events by marking affected pages as saved (the
output file is sparse, so unwritten regions are zero). No test currently
exercises this path. A test that enables balloon deflation during a live
snapshot would verify the sparse-file handling works correctly.
