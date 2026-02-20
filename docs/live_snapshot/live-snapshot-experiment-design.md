# Experiment Design: Live Snapshot Performance Under Varied Memory Workloads

## 1. Objective

Measure the performance impact of Firecracker's live snapshot feature across
a range of guest memory workload sizes, comparing it against full (paused)
snapshots as a baseline. The experiment quantifies trade-offs between VM
downtime, wall-clock snapshot duration, streaming throughput, and guest-visible
performance degradation.

The experiment includes two workload categories:

1. **Synthetic workloads** — controlled-rate `dd` writes to tmpfs, providing
   reproducible write rates for isolating snapshot mechanics.
2. **Application workloads** — Redis and Memcached under client-driven
   request load, providing realistic memory access patterns and
   application-level latency metrics.

---

## 2. Background

Firecracker supports three snapshot modes:

| Mode | Mechanism | VM state during snapshot |
|------|-----------|-------------------------|
| **Full** | Pause VM, dump all memory, save device state | Paused for entire duration |
| **Diff** | Pause VM, dump only dirty pages (requires `track_dirty_pages`) | Paused for entire duration |
| **Live** | Brief pause (~100 ms) to save device state and enable UFFD write-protect, then stream memory while VM runs | Running (writes to unsaved pages block at page granularity) |

Live snapshots use Linux userfaultfd write-protect (UFFD-WP, kernel >= 5.7)
to track guest writes during the RAM streaming phase. Pages that the guest
writes to before the linear scan reaches them trigger a write-protect fault,
causing the faulting vCPU to block until that specific page is saved and
unprotected. This trades total wall-clock time for drastically reduced VM
downtime.

Prior benchmarks on a 4 GiB idle VM showed:
- **31.6x downtime reduction** (3,435 ms -> 109 ms)
- **3x wall-clock increase** (3,435 ms -> 10,443 ms)
- **0.18% fault-driven pages** (idle), **0.62%** (under `dd` write load)

This experiment extends those results by systematically varying memory
workload intensity and VM memory size.

---

## 3. Hypotheses

**H1 — Downtime is workload-independent:** Live snapshot VM downtime (Phase 2
freeze duration) is primarily determined by guest memory size (number of pages
to write-protect), not by workload intensity. We expect downtime to scale
linearly with memory size but remain constant across workload levels for a
given memory size.

**H2 — Streaming throughput degrades with write intensity:** Higher guest
memory write rates increase the fraction of fault-driven page saves, each of
which requires an additional UFFD ioctl round-trip. We expect streaming
throughput (MiB/s) to decrease and wall-clock time to increase as write
intensity grows.

**H3 — Guest-visible latency correlates with write intensity:** Under live
snapshot, guest memory writes to unsaved pages stall at the vCPU. We expect
guest-visible tail latency (p99) to increase with workload intensity, while
full snapshots impose uniform downtime regardless of workload.

**H5 — Application workloads exhibit different fault patterns than synthetic
workloads:** Redis and Memcached scatter writes across many small allocations
(hash tables, slab allocators) rather than writing sequentially to a single
file. We expect higher fault-driven page fractions per byte written compared
to sequential `dd`, but lower absolute fault counts due to smaller working
set sizes.

**H6 — Application tail latency spikes during live snapshot:** During the
streaming phase, individual Redis/Memcached requests that write to
write-protected pages will stall until the page is saved. We expect p99/p999
latency to spike during the snapshot window, with the magnitude proportional
to the streaming phase duration (i.e., VM memory size).

**H7 — STREAM saturates memory bandwidth and maximizes fault rate:** The
STREAM benchmark performs sustained vector operations over large arrays,
touching every page repeatedly. We expect STREAM to produce the highest
fault-driven page fraction of any workload and to represent a worst-case
scenario for live snapshot streaming throughput degradation.

**H4 — Full snapshot time scales linearly with memory size:** Full snapshot
wall-clock time (which equals downtime) should scale linearly with guest
memory, serving as a predictable baseline.

---

## 4. Experimental Variables

### 4.1 Independent Variables

| Variable | Values | Rationale |
|----------|--------|-----------|
| **VM memory size** | 256, 512, 1024, 2048, 4096 MiB | Covers small-to-large serverless function sizes |
| **Workload** | Idle, Light, Medium, Heavy (synthetic); Redis light/mixed/heavy; Memcached light/heavy; STREAM | See Section 5 for details |
| **Snapshot mode** | Full, Live | Primary comparison axis |

**vCPU count** is fixed at 2 (matches prior benchmarks and typical serverless
configuration).

### 4.2 Dependent Variables (Metrics)

**Snapshot creation metrics** (from Firecracker log parsing):

| Metric | Source | Unit |
|--------|--------|------|
| Phase 1 duration (prepare / populate pages) | FC log `Phase 1 took <N> us` | us |
| Phase 2 duration (freeze = VM downtime) | FC log `Phase 2 (freeze) took <N> us` | us |
| Phase 2 sub-timings (pause, save_state, wp_enable, resume) | FC log | us |
| Phase 3 duration (stream RAM) | FC log `Phase 3 (stream) took <N> us` | us |
| Phase 3 fault-driven page count | FC log `<N> fault-driven` | count |
| Phase 3 linear-scan page count | FC log `<N> linear-scan` | count |
| Phase 4 duration (finalize) | FC log `Phase 4 (finalize) took <N> us` | us |
| Total wall-clock time | FC log `complete in <N> us` | us |
| Total downtime | FC log `freeze/downtime=<N> us` | us |
| Streaming throughput | Derived: `(total_pages * 4096) / stream_us` | MiB/s |
| Fault page fraction | Derived: `fault_pages / total_pages` | % |

**Full snapshot metrics** (measured externally):

| Metric | Source | Unit |
|--------|--------|------|
| Pause latency | `time.monotonic()` around `vm.pause()` | ms |
| Snapshot creation time | `time.monotonic()` around `snapshot_create` API | ms |
| Total downtime (= wall-clock) | Sum of pause + create | ms |
| Dump throughput | Derived: `mem_size_mib / create_time_s` | MiB/s |

**Restore metrics** (both modes produce identical snapshot format):

| Metric | Source | Unit |
|--------|--------|------|
| Restore API latency | `time.monotonic()` around `restore_from_snapshot` | ms |
| Time to SSH ready | `time.monotonic()` from restore to first successful SSH | ms |

**Guest-visible performance metrics — synthetic workloads** (measured inside guest via SSH):

| Metric | Source | Unit |
|--------|--------|------|
| Workload write throughput (pre-snapshot baseline) | `dd` output | MiB/s |
| Workload write throughput (during live snapshot) | `dd` output | MiB/s |
| Throughput degradation | Derived: `1 - (during / baseline)` | % |

**Guest-visible performance metrics — application workloads** (measured inside guest):

| Metric | Source | Unit |
|--------|--------|------|
| Ops/sec (baseline) | `redis-benchmark` / `memtier_benchmark` / `memcached` stats | ops/s |
| Ops/sec (during snapshot) | Same tool, sampled during streaming phase | ops/s |
| Ops/sec degradation | Derived: `1 - (during / baseline)` | % |
| GET latency p50 / p99 / p999 (baseline) | `redis-cli --latency-history` / `memtier` | us |
| GET latency p50 / p99 / p999 (during snapshot) | Same, sampled during streaming phase | us |
| SET latency p50 / p99 / p999 (baseline) | Same | us |
| SET latency p50 / p99 / p999 (during snapshot) | Same | us |

**Guest-visible performance metrics — STREAM benchmark** (measured inside guest):

| Metric | Source | Unit |
|--------|--------|------|
| Copy / Scale / Add / Triad bandwidth (baseline) | STREAM output | MiB/s |
| Copy / Scale / Add / Triad bandwidth (during snapshot) | STREAM output | MiB/s |
| Bandwidth degradation per kernel | Derived | % |

**Host resource metrics** (measured from host):

| Metric | Source | Unit |
|--------|--------|------|
| Firecracker peak RSS during snapshot | `/proc/<pid>/status` VmHWM | KiB |
| Snapshot memory file size on disk | `stat` on memory file | bytes |

### 4.3 Controlled Variables

| Variable | Fixed value | Rationale |
|----------|-------------|-----------|
| vCPU count | 2 | Match prior benchmarks |
| Guest kernel | Default CI kernel (5.10 or 6.1) | Consistent baseline |
| Host kernel | >= 5.7 (UFFD-WP required) | Feature requirement |
| Page size | 4 KiB (default, no huge pages) | Isolate snapshot behavior |
| Disk I/O | Local disk, virtio-blk | Minimize I/O variance |
| Build type | Release (LTO) | Realistic production performance |
| Balloon | Disabled | Avoid confounding memory reclaim |
| `track_dirty_pages` | Disabled | Not needed for Full vs Live comparison |

---

## 5. Workload Design

### 5.1 Guest Memory Write Workload

The workload writes random data to a file on tmpfs (guest RAM-backed
filesystem) at a controlled rate. Using tmpfs ensures writes go to guest
memory pages and will trigger UFFD-WP faults during live snapshot.

```bash
# Controlled-rate memory writer (run inside guest)
# Parameters: BLOCK_SIZE, BLOCK_COUNT, SLEEP_S
while true; do
    dd if=/dev/urandom of=/tmp/workload bs=$BLOCK_SIZE count=$BLOCK_COUNT 2>/dev/null
    sleep $SLEEP_S  # Throttle to target write rate (coreutils sleep supports fractions)
done
```

**Workload levels:**

| Level | Target write rate | `bs` | `count` | `sleep` | Pages/s (approx) |
|-------|-------------------|-------|---------|---------|-------------------|
| **Idle** | 0 MiB/s | N/A | N/A | N/A | 0 |
| **Light** | ~4 MiB/s | 4096 | 64 | 0.062 | ~1,024 |
| **Medium** | ~32 MiB/s | 4096 | 256 | 0.031 | ~8,192 |
| **Heavy** | ~128 MiB/s | 4096 | 1024 | 0.031 | ~32,768 |

**Calibration step:** Before each experiment run, measure the actual achieved
write rate by running the workload for 10 seconds without a snapshot and
measuring bytes written. Record the actual rate alongside the target rate.

### 5.2 Redis Workload

Redis is an in-memory key-value store whose data structures (hash tables,
skip lists, SDS strings) scatter writes across many small, randomly-placed
heap allocations. This produces a fundamentally different memory access
pattern than sequential `dd` writes.

**Prerequisites:** Redis must be available in the guest rootfs. Either:
- Build a custom ext4 rootfs with `redis-server` and `redis-benchmark`
  pre-installed (`apt install redis-server`).
- Copy a statically-linked `redis-server` binary into the guest via SCP
  after boot.

**Server startup:**

```bash
# Inside guest
redis-server --daemonize yes \
    --maxmemory ${REDIS_MAXMEM}mb \
    --maxmemory-policy allkeys-lru \
    --save "" \
    --appendonly no
```

`--save ""` and `--appendonly no` disable persistence so all data lives
purely in memory (no disk I/O confound). `maxmemory` is set proportional
to VM size (e.g., 50% of guest memory).

**Memory conditioning (replaces tmpfs prefill):**

```bash
# Pre-populate the key space — fills ~maxmemory with 512-byte values
redis-benchmark -t set -n $((REDIS_MAXMEM * 1024 / 512)) -d 512 -r 1000000 -q
```

This creates a realistic page table layout with many small allocations
scattered across the heap, unlike a single flat tmpfs file.

**Workload levels:**

| Level | Clients | GET:SET ratio | Description |
|-------|---------|---------------|-------------|
| **redis_light** | 2 | 9:1 | Read-heavy, few writes touching protected pages |
| **redis_mixed** | 10 | 3:1 | Balanced read/write, moderate fault potential |
| **redis_heavy** | 50 | 1:1 | Write-heavy, maximizes scattered page faults |

**Workload command:**

```bash
# Continuous mixed workload in background
nohup redis-benchmark -t set,get \
    -c $CLIENTS -r 1000000 -n 100000000 \
    -d 256 --ratio $RATIO -q \
    > /tmp/redis_bench.log 2>&1 &
```

**Baseline measurement:** Run `redis-benchmark` for a fixed number of
operations (e.g., 50,000) and record the reported ops/sec before starting
the snapshot.

**During-snapshot measurement:** After the live snapshot completes (VM is
still running), run another fixed burst and record ops/sec. For latency
percentiles, use `redis-cli --latency-history -i 1` running in the
background, sampling every second, and parse the output for the window
that overlaps with the streaming phase.

### 5.3 Memcached Workload

Memcached uses a slab allocator with fixed-size classes. Memory writes
during SET operations hit slab pages that are reused across keys,
producing a distinct pattern from both Redis (malloc-based) and `dd`
(sequential). Memcached is also multi-threaded, exercising concurrent
page faults across vCPUs.

**Prerequisites:** `memcached` and `memtier_benchmark` must be available
in the guest rootfs.

**Server startup:**

```bash
# Inside guest (-m = memory in MiB, -t = threads)
memcached -d -m $MEMCACHED_MEM -t 2 -u root -p 11211
```

**Memory conditioning:**

```bash
# Pre-populate with memtier_benchmark
# --key-maximum is required with -n allkeys; without it memtier exits non-zero.
memtier_benchmark -p 11211 --protocol=memcache_text \
    --key-maximum=500000 --data-size=512 \
    --ratio=1:0 -n allkeys -c 10 --hide-histogram -q
```

**Workload levels:**

| Level | Clients | GET:SET ratio | Description |
|-------|---------|---------------|-------------|
| **memcached_light** | 2 | 9:1 | Read-dominated slab access |
| **memcached_heavy** | 50 | 1:1 | Write-heavy, slab reallocation |

**Workload command:**

```bash
nohup memtier_benchmark -s 127.0.0.1 -p 11211 -P memcache_binary \
    --key-maximum=500000 --data-size=256 \
    --ratio=$RATIO -c $CLIENTS \
    --test-time=300 --hide-histogram -q \
    > /tmp/memtier.log 2>&1 &
```

**Metrics collection:** `memtier_benchmark` reports ops/sec and latency
percentiles (p50, p99, p999) per second in its output log. Parse the
output for the baseline window (pre-snapshot) and the during-snapshot
window (overlapping with Phase 3 streaming).

### 5.4 STREAM Benchmark

The STREAM benchmark (McCalpin, 1995) measures sustainable memory bandwidth
using four simple vector kernels operating on large arrays:

| Kernel | Operation | Bytes/element |
|--------|-----------|---------------|
| **Copy** | `a[i] = b[i]` | 16 (read + write) |
| **Scale** | `a[i] = q * b[i]` | 16 (read + write) |
| **Add** | `a[i] = b[i] + c[i]` | 24 (2 reads + write) |
| **Triad** | `a[i] = b[i] + q * c[i]` | 24 (2 reads + write) |

STREAM is uniquely valuable for this experiment because:
- It touches **every page** in its working set on each pass, guaranteeing
  maximum overlap with the linear scan.
- Writes are strided and predictable, representing a best-case scenario
  for hardware prefetching but a worst-case for UFFD-WP fault rate.
- It is CPU-bound (no I/O, no syscalls between writes), so any vCPU stall
  from a write-protect fault directly reduces measured bandwidth.

**Prerequisites:** Compile `stream.c` (single-file, no dependencies) and
place the binary in the guest rootfs, or compile inside the guest:

```bash
# Inside guest (or cross-compile and SCP)
gcc -O2 -fopenmp -DSTREAM_ARRAY_SIZE=$ARRAY_ELEMS \
    -DNTIMES=100 -o /tmp/stream stream.c -lm
```

**Array sizing:** The array size should be set so the three arrays
(a, b, c) together occupy a target fraction of guest memory:

| VM memory | Target array footprint | `STREAM_ARRAY_SIZE` (doubles) | Total (3 arrays) |
|-----------|------------------------|-------------------------------|-------------------|
| 256 MiB | ~128 MiB | 5,592,405 | ~128 MiB |
| 512 MiB | ~256 MiB | 11,184,810 | ~256 MiB |
| 1024 MiB | ~512 MiB | 22,369,621 | ~512 MiB |
| 2048 MiB | ~1024 MiB | 44,739,242 | ~1024 MiB |
| 4096 MiB | ~2048 MiB | 89,478,485 | ~2048 MiB |

The array footprint targets ~50% of guest memory to leave room for the
kernel and other overhead. Each element is a `double` (8 bytes), and
three arrays are allocated.

**Memory conditioning:** STREAM's first pass initializes all arrays,
which serves as memory conditioning. No separate prefill step is needed.

**Workload execution:**

```bash
# Run STREAM continuously in background
nohup sh -c 'while true; do /tmp/stream >> /tmp/stream.log 2>&1; done' &
```

Each invocation of STREAM runs `NTIMES` iterations and reports the best
bandwidth for each kernel. The continuous loop ensures the workload is
active throughout the snapshot.

**Baseline measurement:** Run one STREAM invocation before the snapshot
and record the reported bandwidth for each kernel (Copy, Scale, Add,
Triad).

**During-snapshot measurement:** Parse `/tmp/stream.log` for the STREAM
invocation whose execution overlapped with the snapshot streaming phase.
Compare its reported bandwidth against the baseline.

**Workload levels:** STREAM has a single level — it always runs at
maximum memory bandwidth. It is parametrized only by array size (which
tracks VM memory size).

| Level | Description |
|-------|-------------|
| **stream** | STREAM Triad at ~50% of guest memory |

### 5.5 Pre-snapshot Memory Conditioning

Before taking a snapshot, the guest should have a realistic memory footprint.
The conditioning method depends on the workload type:

**Synthetic workloads (dd):** Populate ~25% of guest memory with random data
on tmpfs:

```bash
head -c $((MEM_SIZE_MIB / 4))M /dev/urandom > /tmp/prefill 2>/dev/null
sync
```

**Redis:** Pre-populate the key-value store to fill ~50% of `maxmemory`
with 512-byte values (see Section 5.2). This creates many small heap
allocations scattered across guest memory.

**Memcached:** Pre-populate with `memtier_benchmark` to fill slab classes
(see Section 5.3). This warms the slab allocator and creates a realistic
memory layout.

**STREAM:** The first invocation of STREAM initializes all three arrays
(Copy/Scale/Add/Triad), which implicitly backs the memory pages. Run one
warmup pass before the measured workload begins.

In all cases, the goal is to ensure the VM has a non-trivial dirty page
set and a realistic page table structure before the snapshot is taken.

---

## 6. Test Matrix

### 6.1 Synthetic Workload Matrix

The cross product of memory sizes, synthetic workload levels, and snapshot
modes (unchanged from initial experiment):

| # | Memory (MiB) | Workload | Snapshot Mode | Iterations |
|---|-------------|----------|---------------|------------|
| 1 | 256 | Idle | Full | 10 |
| 2 | 256 | Idle | Live | 10 |
| 3 | 256 | Light | Full | 10 |
| 4 | 256 | Light | Live | 10 |
| 5 | 256 | Medium | Full | 10 |
| 6 | 256 | Medium | Live | 10 |
| 7 | 256 | Heavy | Full | 10 |
| 8 | 256 | Heavy | Live | 10 |
| 9 | 512 | Idle | Full | 10 |
| 10 | 512 | Idle | Live | 10 |
| 11 | 512 | Light | Full | 10 |
| 12 | 512 | Light | Live | 10 |
| 13 | 512 | Medium | Full | 10 |
| 14 | 512 | Medium | Live | 10 |
| 15 | 512 | Heavy | Full | 10 |
| 16 | 512 | Heavy | Live | 10 |
| 17 | 1024 | Idle | Full | 10 |
| 18 | 1024 | Idle | Live | 10 |
| 19 | 1024 | Light | Full | 10 |
| 20 | 1024 | Light | Live | 10 |
| 21 | 1024 | Medium | Full | 10 |
| 22 | 1024 | Medium | Live | 10 |
| 23 | 1024 | Heavy | Full | 10 |
| 24 | 1024 | Heavy | Live | 10 |
| 25 | 2048 | Idle | Full | 10 |
| 26 | 2048 | Idle | Live | 10 |
| 27 | 2048 | Light | Full | 10 |
| 28 | 2048 | Light | Live | 10 |
| 29 | 2048 | Medium | Full | 10 |
| 30 | 2048 | Medium | Live | 10 |
| 31 | 2048 | Heavy | Full | 10 |
| 32 | 2048 | Heavy | Live | 10 |
| 33 | 4096 | Idle | Full | 10 |
| 34 | 4096 | Idle | Live | 10 |
| 35 | 4096 | Light | Full | 10 |
| 36 | 4096 | Light | Live | 10 |
| 37 | 4096 | Medium | Full | 10 |
| 38 | 4096 | Medium | Live | 10 |
| 39 | 4096 | Heavy | Full | 10 |
| 40 | 4096 | Heavy | Live | 10 |

**Synthetic total: 40 configurations x 10 iterations = 400 test runs.**

### 6.2 Application Workload Matrix

Application workloads use a reduced set of memory sizes to keep run time
manageable. Only 512 and 2048 MiB are used — a small and a large VM —
since the synthetic matrix already establishes the memory-size scaling
relationship.

| # | Memory (MiB) | Workload | Snapshot Mode | Iterations |
|---|-------------|----------|---------------|------------|
| 41 | 512 | redis_light | Full | 10 |
| 42 | 512 | redis_light | Live | 10 |
| 43 | 512 | redis_mixed | Full | 10 |
| 44 | 512 | redis_mixed | Live | 10 |
| 45 | 512 | redis_heavy | Full | 10 |
| 46 | 512 | redis_heavy | Live | 10 |
| 47 | 512 | memcached_light | Full | 10 |
| 48 | 512 | memcached_light | Live | 10 |
| 49 | 512 | memcached_heavy | Full | 10 |
| 50 | 512 | memcached_heavy | Live | 10 |
| 51 | 512 | stream | Full | 10 |
| 52 | 512 | stream | Live | 10 |
| 53 | 2048 | redis_light | Full | 10 |
| 54 | 2048 | redis_light | Live | 10 |
| 55 | 2048 | redis_mixed | Full | 10 |
| 56 | 2048 | redis_mixed | Live | 10 |
| 57 | 2048 | redis_heavy | Full | 10 |
| 58 | 2048 | redis_heavy | Live | 10 |
| 59 | 2048 | memcached_light | Full | 10 |
| 60 | 2048 | memcached_light | Live | 10 |
| 61 | 2048 | memcached_heavy | Full | 10 |
| 62 | 2048 | memcached_heavy | Live | 10 |
| 63 | 2048 | stream | Full | 10 |
| 64 | 2048 | stream | Live | 10 |

**Application total: 24 configurations x 10 iterations = 240 test runs.**

### 6.3 Combined Totals

| Matrix | Configs | Iterations | Total runs | Est. time per run | Est. total |
|--------|---------|------------|------------|-------------------|------------|
| Synthetic | 40 | 10 | 400 | ~60-120 s | ~10-15 hrs |
| Application | 24 | 10 | 240 | ~90-180 s | ~8-12 hrs |
| **Combined** | **64** | **10** | **640** | | **~18-27 hrs** |

The synthetic and application matrices can be run independently. The
application matrix requires a custom rootfs with Redis, Memcached,
memtier_benchmark, and STREAM pre-installed (see Section 5 and
Section 11.1).

---

## 7. Test Procedure

Each test run follows this sequence. Steps that differ between synthetic
and application workloads are noted inline.

```
1. BOOT VM
   - Spawn Firecracker with configured memory size and 2 vCPUs
   - Disable memory monitor (live snapshot inflates RSS transiently)
   - Add network interface, start VM
   - Wait for SSH readiness

2. CONDITION MEMORY
   Synthetic workloads:
   - Write ~25% of guest memory via SSH (head /dev/urandom > /tmp/prefill)
   - Sync guest filesystem
   Redis workload:
   - Start redis-server with maxmemory = 50% of guest memory
   - Pre-populate key space with redis-benchmark (512-byte values)
   Memcached workload:
   - Start memcached with -m = 50% of guest memory
   - Pre-populate key space with memtier_benchmark (512-byte values)
   STREAM workload:
   - Compile or copy STREAM binary with array size = 50% of guest memory
   - Run one warmup pass (STREAM initializes arrays on first run)

3. START WORKLOAD (if not Idle)
   Synthetic:
   - Launch background dd write loop at target rate via SSH
   - Wait 2 seconds for workload to stabilize
   - Record baseline write throughput (MiB/s)
   Redis:
   - Launch redis-benchmark in background with configured clients/ratio
   - Wait 3 seconds for warmup
   - Record baseline ops/sec (run a fixed 50,000-op burst)
   - Start redis-cli --latency-history -i 1 in background for p99 tracking
   Memcached:
   - Launch memtier_benchmark in background with configured clients/ratio
   - Wait 3 seconds for warmup
   - Record baseline ops/sec and latency from memtier output
   STREAM:
   - Launch continuous STREAM loop in background
   - Wait for first completed run
   - Record baseline bandwidth (Copy/Scale/Add/Triad MiB/s)

4. SAMPLE HOST METRICS (pre-snapshot)
   - Record Firecracker PID RSS from /proc/<pid>/status

5. TAKE SNAPSHOT
   - If Full: pause VM, measure pause time, call snapshot_create(Full),
     measure create time
   - If Live: call snapshot_live() (VM remains running),
     parse Firecracker logs for phase breakdown

6. SAMPLE HOST METRICS (post-snapshot)
   - Record Firecracker PID peak RSS (VmHWM) from /proc/<pid>/status
   - Record snapshot memory file size

7. VERIFY SOURCE VM (Live only)
   - Confirm VM state is "Running"
   - SSH connectivity check
   - Sample during-snapshot performance:
     Synthetic: run a timed dd burst, record throughput
     Redis: run a 50,000-op burst, record ops/sec; parse latency log
            for the window overlapping Phase 3
     Memcached: parse memtier output for the snapshot-overlapping window
     STREAM: parse stream.log for the run overlapping Phase 3

8. RESTORE SNAPSHOT
   - Build new VM, restore from snapshot
   - Measure restore API latency and time-to-SSH-ready
   - Verify restored VM is functional (SSH check)

9. COLLECT AND STORE RESULTS
   - Parse all metrics into structured record
   - Append to results CSV/JSON
   - Kill source and restored VMs
```

---

## 8. Implementation

### 8.1 Test File Location

```
tests/integration_tests/functional/test_snapshot_live_experiment.py
```

### 8.2 Pytest Structure

```python
@pytest.mark.nonci
@pytest.mark.timeout(300)
@pytest.mark.parametrize("mem_size_mib", [256, 512, 1024, 2048, 4096])
@pytest.mark.parametrize("workload", ["idle", "light", "medium", "heavy"])
@pytest.mark.parametrize("iteration", range(10))
def test_snapshot_workload_experiment(
    uvm_plain, microvm_factory, mem_size_mib, workload, iteration, record_property
):
    """Collect snapshot metrics under controlled memory workload."""
    ...
```

Using `record_property` (pytest built-in) to attach metrics to JUnit XML
output, and additionally writing results to a CSV file for analysis.

### 8.3 Results Output Format

Each run produces a row in `experiment_results.csv`:

```
timestamp, mem_size_mib, workload, snapshot_mode, iteration,
# Snapshot timing
phase1_us, populate_pages_us, freeze_us, pause_us, save_state_us,
wp_enable_us, resume_us, stream_us, finalize_us, total_us, downtime_us,
# Page counts
total_pages, fault_pages, linear_pages,
# Derived
throughput_mibs, fault_fraction_pct,
# Full snapshot specific
full_pause_ms, full_create_ms, full_total_ms, full_throughput_mibs,
# Restore
restore_api_ms, ssh_ready_ms,
# Host
rss_pre_kib, rss_peak_kib, mem_file_bytes,
# Guest workload — synthetic (dd)
workload_baseline_mibs, workload_during_mibs, workload_degradation_pct,
actual_write_rate_mibs,
# Guest workload — application (Redis / Memcached)
app_baseline_ops, app_during_ops, app_ops_degradation_pct,
app_baseline_p50_us, app_baseline_p99_us, app_baseline_p999_us,
app_during_p50_us, app_during_p99_us, app_during_p999_us,
# Guest workload — STREAM
stream_baseline_copy_mibs, stream_baseline_scale_mibs,
stream_baseline_add_mibs, stream_baseline_triad_mibs,
stream_during_copy_mibs, stream_during_scale_mibs,
stream_during_add_mibs, stream_during_triad_mibs,
stream_triad_degradation_pct
```

Fields are populated based on workload type; unused fields are left empty.
The `extrasaction="ignore"` option in the CSV writer silently skips any
extra keys not in the field list.

### 8.4 Log Parsing

Reuse the existing `_parse_live_snapshot_log()` function from
`test_snapshot_live.py`, which extracts all phase timings and page counts
from Firecracker's structured log output.

### 8.5 Existing Infrastructure to Reuse

| Component | Location | Usage |
|-----------|----------|-------|
| `_parse_live_snapshot_log()` | `test_snapshot_live.py` | Parse FC log for live snapshot metrics |
| `_do_full_snapshot_timed()` | `test_snapshot_live.py` | Time full snapshot creation |
| `_do_restore_timed()` | `test_snapshot_live.py` | Time snapshot restore |
| `vm.snapshot_live()` | `framework/microvm.py` | Take live snapshot via API |
| `vm.restore_from_snapshot()` | `framework/microvm.py` | Restore from snapshot |
| `uvm_plain` fixture | `conftest.py` | Unconfigured VM fixture |
| `microvm_factory` fixture | `conftest.py` | VM factory for restore targets |

---

## 9. Analysis Plan

### 9.1 Primary Analysis

For each (memory_size, workload) pair, compute across 10 iterations:

- **Mean, median, stddev, min, max** for all timing metrics
- **95% confidence intervals** for means (assuming normal distribution or
  using bootstrapping)

### 9.2 Visualizations

1. **Downtime vs Memory Size** (line chart)
   - X: memory size (MiB), Y: downtime (ms)
   - Lines: Full snapshot (single line), Live snapshot per workload level
   - Expected: Full scales linearly; Live remains ~flat per memory size

2. **Wall-Clock Time vs Memory Size** (line chart)
   - X: memory size, Y: total wall-clock (ms)
   - Lines: Full and Live per workload level
   - Expected: Both scale linearly; Live 2-4x slower depending on workload

3. **Streaming Throughput vs Workload Intensity** (bar chart)
   - X: workload level, Y: throughput (MiB/s)
   - Grouped by memory size
   - Expected: Throughput decreases with higher write intensity

4. **Fault Page Fraction vs Workload Intensity** (bar chart)
   - X: workload level, Y: fault-driven pages (%)
   - Grouped by memory size
   - Expected: Increases with write intensity, low single-digit %

5. **Guest Throughput Degradation** (bar chart)
   - X: memory size, Y: workload throughput degradation (%)
   - Grouped by workload level
   - Expected: Higher degradation for larger VMs (longer streaming phase)

6. **Phase Breakdown Stacked Bar** (stacked bar chart)
   - X: (memory_size, workload) pair, Y: time (ms)
   - Stacks: Phase 1, Phase 2 (downtime), Phase 3, Phase 4
   - Shows relative time spent in each phase

7. **Downtime Breakdown** (stacked bar, zoomed to Phase 2)
   - X: memory_size, Y: freeze time (ms)
   - Stacks: pause, save_state, wp_enable, resume
   - Expected: wp_enable dominates, scales with memory size

8. **Application Ops/sec Degradation** (grouped bar chart)
   - X: workload type (redis_light, redis_mixed, redis_heavy,
     memcached_light, memcached_heavy), Y: ops/sec degradation (%)
   - Grouped by memory size (512, 2048)
   - Shows how different applications respond to live snapshot

9. **Application Tail Latency (p99) During Snapshot** (grouped bar chart)
   - X: workload type, Y: p99 latency (us)
   - Bars: baseline vs during-snapshot
   - Shows the latency spike caused by UFFD-WP page faults on
     individual requests

10. **STREAM Bandwidth Degradation** (grouped bar chart)
    - X: STREAM kernel (Copy, Scale, Add, Triad), Y: bandwidth (MiB/s)
    - Bars: baseline vs during-snapshot, grouped by memory size
    - Expected: significant degradation since STREAM touches every page

11. **Fault Fraction: Synthetic vs Application vs STREAM** (bar chart)
    - X: workload type (all), Y: fault-driven pages (%)
    - Grouped by memory size
    - Compares scattered (Redis/Memcached) vs sequential (dd) vs
      full-sweep (STREAM) fault patterns

### 9.3 Statistical Tests

- **Two-sample t-test** (or Mann-Whitney U): Compare live vs full downtime
  for each memory size to confirm the difference is statistically significant.
- **Linear regression**: Fit downtime vs memory size for both modes to
  quantify scaling behavior.
- **ANOVA**: Test whether workload intensity significantly affects live
  snapshot streaming throughput across memory sizes.

---

## 10. Threats to Validity

| Threat | Mitigation |
|--------|------------|
| **Host contention** | Run on a dedicated machine with no other workloads. Pin vCPUs to specific host cores if possible. |
| **Warm-up effects** | Discard first iteration or run a warm-up VM boot before the experiment. |
| **Disk I/O variance** | Use local SSD/NVMe storage. Consider writing snapshot files to tmpfs to isolate from disk variance. |
| **Guest workload rate variance** | Calibrate actual write rate before each snapshot. Record actual rate alongside target. |
| **Debug build overhead** | Use release build (`./tools/devtool build --release`) for all measurements. |
| **Memory monitor interference** | Disabled for all runs (live snapshot inflates RSS transiently). |
| **Small sample size** | 10 iterations per configuration. Increase to 30 if variance is high. |
| **UFFD overhead from bookkeeping structures** | Known: ~18 bytes/page overhead (~73 MiB for 4 GiB VM). Record RSS to quantify. |
| **Application startup variance** | Redis/Memcached startup time varies. Wait for readiness (ping/stats) before pre-populating. Record actual ready time. |
| **Client-side overhead in guest** | redis-benchmark / memtier_benchmark consume CPU in guest. Use 2 vCPUs and limit client threads to 1 to leave headroom for the server. |
| **STREAM compilation flags** | Bandwidth depends on optimization level and OpenMP threading. Fix gcc flags (`-O2 -fopenmp`) and thread count (= vCPU count) across all runs. |
| **Slab allocator warm-up (Memcached)** | Slab classes are allocated lazily. Pre-populate with diverse key sizes to warm all relevant slab classes before benchmarking. |

---

## 11. Running the Experiment

All commands in this section are run from the repository root on the
host machine. Tests execute inside a Docker container managed by
`./tools/devtool`; the repo is bind-mounted at `/firecracker` inside
the container.

### 11.1 Prerequisites

**Host requirements:**

```bash
# 1. Build the release Firecracker binary (used by all tests)
./tools/devtool build --release

# 2. Verify UFFD write-protect support (kernel >= 5.7 required)
uname -r   # must be >= 5.7; experiment was validated on 5.15

# 3. Check available host memory
free -h    # need >= 2x the largest VM size tested
           # >= 10 GiB free for 4096 MiB VM tests
```

**Application workload prerequisites (Redis, Memcached, STREAM):**

Application workload tests require a guest rootfs that has
`redis-server`, `redis-benchmark`, `memcached`, `memtier_benchmark`,
and the STREAM binary pre-installed. The test harness selects the
rootfs via the `EXPERIMENT_ROOTFS` environment variable (see §11.2).

To build the rootfs from scratch:

```bash
# Identify the base Ubuntu 24.04 ext4 image
ls build/artifacts/*/x86_64/ubuntu-24.04.ext4

# Copy it so the base image stays pristine
cp build/artifacts/*/x86_64/ubuntu-24.04.ext4 test_results/app-rootfs.ext4
cp build/artifacts/*/x86_64/ubuntu-24.04.id_rsa test_results/app-rootfs.id_rsa

# Mount and install tools (requires root on the host)
sudo mkdir -p /mnt/fc-rootfs
sudo mount -o loop test_results/app-rootfs.ext4 /mnt/fc-rootfs
sudo chroot /mnt/fc-rootfs /bin/bash -c '
    apt-get update
    apt-get install -y redis-server redis-tools memcached gcc libc6-dev

    # memtier_benchmark (not in Ubuntu repos; build from source)
    apt-get install -y git build-essential autoconf automake \
        libpcre3-dev libevent-dev pkg-config zlib1g-dev libssl-dev
    git clone https://github.com/RedisLabs/memtier_benchmark.git /tmp/memtier
    cd /tmp/memtier && autoreconf -ivf && ./configure && make -j$(nproc)
    cp /tmp/memtier/memtier_benchmark /usr/local/bin/
    rm -rf /tmp/memtier

    # STREAM benchmark — download stream.c from the official source,
    # then compile with a representative array size.
    # The array size below targets ~50% of a 512 MiB guest; the test
    # harness expects the binary at /usr/local/bin/stream.
    curl -O https://www.cs.virginia.edu/stream/FTP/Code/stream.c
    gcc -O2 -fopenmp -DSTREAM_ARRAY_SIZE=11184810 -DNTIMES=20 \
        -o /usr/local/bin/stream stream.c -lm
    rm stream.c

    apt-get clean
'
sudo umount /mnt/fc-rootfs
```

> **Note:** The SSH key for the app rootfs must have the same base name
> with a `.id_rsa` suffix (`test_results/app-rootfs.id_rsa`).  The
> framework derives the key path automatically from the rootfs path.

### 11.2 Passing `EXPERIMENT_ROOTFS` into the container

`devtool` runs tests inside Docker.  Environment variables are only
forwarded if they match a known prefix.  The `EXPERIMENT_` prefix was
added to the forwarding list (see `tools/devtool`, the `env.list`
generation step) so that `EXPERIMENT_ROOTFS` is passed through
automatically.

The path must be the **container-internal** path — the repository root
is mounted at `/firecracker` inside the container:

```bash
# On the host, point at the container-internal path:
export EXPERIMENT_ROOTFS=/firecracker/test_results/app-rootfs.ext4

# devtool then forwards this into the container automatically.
```

### 11.3 Execution

All `devtool test` commands require `sudo` (tests run Firecracker via
the jailer, which needs root to set up cgroups and network namespaces).

**Quick smoke test** — validates the full harness end-to-end in ~3
minutes (512 MiB, idle + medium workloads, plus a `redis_light` live
snapshot if `EXPERIMENT_ROOTFS` is set):

```bash
export EXPERIMENT_ROOTFS=/firecracker/test_results/app-rootfs.ext4
sudo -E ./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py::test_snapshot_experiment_quick \
    -s --log-cli-level=INFO \
    -m ""
```

**Full synthetic experiment** (400 runs, ~10–15 hrs):

```bash
sudo ./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "idle or light or medium or heavy" \
    -s --log-cli-level=INFO \
    -m "" \
    --timeout=300
```

**Full application experiment** (240 runs, ~8–12 hrs):

```bash
export EXPERIMENT_ROOTFS=/firecracker/test_results/app-rootfs.ext4
sudo -E ./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "app_experiment" \
    -s --log-cli-level=INFO \
    -m "" \
    --timeout=900
```

**Targeted runs:**

```bash
# Redis workloads only, 512 MiB VM
export EXPERIMENT_ROOTFS=/firecracker/test_results/app-rootfs.ext4
sudo -E ./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "app_experiment and redis and 512" \
    -s --log-cli-level=INFO -m ""

# STREAM only
sudo -E ./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "app_experiment and stream" \
    -s --log-cli-level=INFO -m ""
```

**Running in the background (survives SSH disconnects):**

Use `tmux` so the experiment persists if your SSH session drops.
Output is also tee'd to a log file for later review.

```bash
export EXPERIMENT_ROOTFS=/firecracker/test_results/app-rootfs.ext4

tmux new-session -d -s fc-experiment \
  'sudo -E ./tools/devtool -y test -- \
     integration_tests/functional/test_snapshot_live_experiment.py \
     -k "app_experiment" \
     -s --log-cli-level=INFO -m "" --timeout=900 \
   2>&1 | tee test_results/app_experiment.log; \
   echo "=== DONE: exit=$? ===" | tee -a test_results/app_experiment.log'

# Reconnect after disconnect
tmux attach -t fc-experiment

# Detach without killing
# Ctrl-b  d

# Watch progress from a separate terminal
tail -f test_results/app_experiment.log
```

### 11.4 CSV header note

`experiment_results.csv` is created the first time a test writes a
row, using the `CSV_FIELDS` list defined in the test file as the
header.  If the file already exists from a previous run with an older
version of the code (e.g., before app workload columns were added),
the header will be missing the new columns and those fields will not
be readable by `csv.DictReader` with the default fieldname detection.

To fix this for an existing CSV, re-read it with explicit fieldnames:

```python
import csv
CSV_FIELDS = [...]  # full list from test_snapshot_live_experiment.py
with open("test_results/experiment_results.csv") as f:
    reader = csv.DictReader(f, fieldnames=CSV_FIELDS)
    next(reader)  # skip the stale header row
    rows = list(reader)
```

Or simply delete the old CSV before a fresh run — the file will be
recreated with the correct header automatically.

### 11.5 Results Collection

Results are written to:
- `test_results/experiment_results.csv` — one row per test run
- `test_results/test-report.json` — per-test pass/fail/skip metadata
- Console (captured in `app_experiment.log` if using the tmux recipe)

**Analysis and visualization:**

```bash
# Print summary tables
python3 tests/integration_tests/functional/analyze_experiment_results.py \
    test_results/experiment_results.csv

# Generate plots (requires matplotlib, produces PNGs in test_results/)
python3 tests/integration_tests/functional/plot_experiment_results.py \
    test_results/experiment_results.csv
```

---

## 12. Success Criteria

The experiment is considered successful if:

1. All test runs complete without failure (snapshot + restore + SSH check)
2. Results for each configuration have a coefficient of variation (CV) < 20%
   for primary timing metrics (downtime, wall-clock, throughput)
3. Data is sufficient to confirm or reject all seven hypotheses (H1-H7)
4. Visualizations clearly show the trade-off between downtime reduction and
   wall-clock time increase across the parameter space
5. Application workload latency percentiles show a measurable difference
   between baseline and during-snapshot periods
6. STREAM bandwidth measurements show a clear degradation during live
   snapshot, establishing the worst-case bound

---

## 13. Expected Outcomes

### 13.1 Synthetic Workload Expectations

Based on prior 4 GiB benchmarks and the implementation architecture:

| Metric | Full (4 GiB) | Live Idle (4 GiB) | Live Heavy (4 GiB) |
|--------|-------------|-------------------|---------------------|
| Downtime | ~3,400 ms | ~100 ms | ~100 ms |
| Wall-clock | ~3,400 ms | ~10,000 ms | ~20,000+ ms |
| Throughput | ~1,200 MiB/s | ~400 MiB/s | ~150-200 MiB/s |
| Fault pages | N/A | ~0.2% | ~1-5% |
| Restore time | ~250 ms | ~250 ms | ~250 ms |

For smaller VMs, we expect all times to scale roughly linearly with memory
size, with live snapshot downtime dominated by the fixed-cost save_state step
(~23 ms) for VMs below ~512 MiB.

### 13.2 Application Workload Expectations

**Redis:** Scattered small writes across hash table buckets and SDS string
allocations. Expected fault fraction between `dd`-light and `dd`-medium
for equivalent ops/sec, because each SET touches only 1-2 pages but the
pages are randomly distributed. p99 latency should spike by 1-10 ms
during the streaming phase (the duration of a single page-fault round-trip).

**Memcached:** Slab allocator concentrates writes into slab pages of fixed
size classes. Fewer distinct pages touched per second than Redis (slab
reuse), so we expect a lower fault fraction than Redis at equivalent
request rate. However, Memcached is multi-threaded, so multiple vCPUs
may fault concurrently, potentially amplifying tail latency.

**STREAM:** Expected to produce the highest fault fraction (~5-20%) and
largest streaming throughput degradation of any workload. Because STREAM
sweeps the entire array on every pass, most pages will be written before
the linear scan reaches them. STREAM bandwidth should drop significantly
(50-80%) during the streaming phase. This establishes the worst-case
performance bound for live snapshots.

| Workload | Expected fault fraction | Expected ops/bandwidth degradation |
|----------|------------------------|------------------------------------|
| redis_light | ~0.1-0.3% | ~2-5% ops/sec drop |
| redis_heavy | ~0.5-2% | ~10-20% ops/sec drop, 5-10x p99 spike |
| memcached_light | ~0.1-0.2% | ~1-3% ops/sec drop |
| memcached_heavy | ~0.3-1% | ~5-15% ops/sec drop |
| stream | ~5-20% | ~50-80% bandwidth drop |

---

## 14. Preliminary Results (Synthetic Workloads)

The synthetic workload matrix has been executed (1 iteration per
configuration, 40 configs x 2 PCI modes = 80 test runs). Key findings:

### 14.1 Downtime Speedup

| VM Memory | Full Downtime | Live Downtime | Speedup |
|-----------|--------------|---------------|---------|
| 256 MiB | 156 ms | 25 ms | 6.3x |
| 512 MiB | 313 ms | 36 ms | 8.7x |
| 1024 MiB | 631 ms | 58 ms | 10.8x |
| 2048 MiB | 1,254 ms | 103 ms | 12.1x |
| 4096 MiB | 2,494 ms | 191 ms | 13.0x |

**H1 confirmed:** Live snapshot downtime is workload-independent. All four
workload levels produce nearly identical downtime for a given memory size.
`wp_enable` accounts for 97%+ of freeze time and scales linearly with
memory (~47 us/MiB).

### 14.2 Streaming Throughput

| VM Memory | Live Idle | Live Heavy | Full |
|-----------|-----------|------------|------|
| 256 MiB | 764 MiB/s | 626 MiB/s | 1,656 MiB/s |
| 512 MiB | 783 MiB/s | 645 MiB/s | 1,641 MiB/s |
| 1024 MiB | 783 MiB/s | 657 MiB/s | 1,625 MiB/s |
| 2048 MiB | 782 MiB/s | 670 MiB/s | 1,634 MiB/s |
| 4096 MiB | 792 MiB/s | 669 MiB/s | 1,643 MiB/s |

**H2 confirmed:** Streaming throughput drops ~15-20% from idle to heavy
workload. Full snapshot throughput is constant at ~1,640 MiB/s.

### 14.3 Fault-Driven Pages

| VM Memory | Idle | Light | Medium | Heavy |
|-----------|------|-------|--------|-------|
| 256 MiB | 0.23% | 0.80% | 1.16% | 2.32% |
| 512 MiB | 0.02% | 0.41% | 0.56% | 1.25% |
| 1024 MiB | 0.03% | 0.28% | 0.35% | 0.68% |
| 2048 MiB | 0.07% | 0.11% | 0.16% | 0.33% |
| 4096 MiB | 0.02% | 0.09% | 0.10% | 0.17% |

Fault fraction decreases with VM size because the linear scan time
grows (more pages to stream), giving more runway before the guest
writes outpace the scanner. Even under heavy load at 4 GiB, only
0.17% of pages are fault-driven.
