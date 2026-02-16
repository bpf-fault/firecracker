# Experiment Design: Live Snapshot Performance Under Varied Memory Workloads

## 1. Objective

Measure the performance impact of Firecracker's live snapshot feature across
a range of guest memory workload sizes, comparing it against full (paused)
snapshots as a baseline. The experiment quantifies trade-offs between VM
downtime, wall-clock snapshot duration, streaming throughput, and guest-visible
performance degradation.

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

**H4 — Full snapshot time scales linearly with memory size:** Full snapshot
wall-clock time (which equals downtime) should scale linearly with guest
memory, serving as a predictable baseline.

---

## 4. Experimental Variables

### 4.1 Independent Variables

| Variable | Values | Rationale |
|----------|--------|-----------|
| **VM memory size** | 256, 512, 1024, 2048, 4096 MiB | Covers small-to-large serverless function sizes |
| **Workload write intensity** | Idle, Light (4 MiB/s), Medium (32 MiB/s), Heavy (128 MiB/s) | Spans realistic serverless write patterns |
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

**Guest-visible performance metrics** (measured inside guest via SSH):

| Metric | Source | Unit |
|--------|--------|------|
| Workload write throughput (pre-snapshot baseline) | `dd` output or custom tool | MiB/s |
| Workload write throughput (during live snapshot) | `dd` output or custom tool | MiB/s |
| Throughput degradation | Derived: `1 - (during / baseline)` | % |

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

### 5.2 Pre-snapshot Memory Conditioning

Before taking a snapshot, the guest should have a realistic memory footprint.
Populate a fraction of guest memory proportional to VM size:

```bash
# Write to ~25% of guest memory to create backed pages
head -c $((MEM_SIZE_MIB / 4))M /dev/urandom > /tmp/prefill 2>/dev/null
sync
```

This ensures the VM has a non-trivial dirty page set and a realistic page
table structure.

---

## 6. Test Matrix

The full test matrix is the cross product of memory sizes, workload levels,
and snapshot modes:

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

**Total: 40 configurations x 10 iterations = 400 test runs.**

Estimated wall-clock time per run (including VM boot, workload start, snapshot,
restore, verification): ~60 seconds for small VMs, ~120 seconds for 4 GiB VMs.
**Total experiment time: ~10-15 hours** (single-threaded execution).

---

## 7. Test Procedure

Each test run follows this sequence:

```
1. BOOT VM
   - Spawn Firecracker with configured memory size and 2 vCPUs
   - Disable memory monitor (live snapshot inflates RSS transiently)
   - Add network interface, start VM
   - Wait for SSH readiness

2. CONDITION MEMORY
   - Write ~25% of guest memory via SSH (head /dev/urandom > /tmp/prefill)
   - Sync guest filesystem

3. START WORKLOAD (if not Idle)
   - Launch background write workload at target rate via SSH
   - Wait 2 seconds for workload to stabilize
   - Record baseline write throughput

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
   - If workload running: sample post-snapshot write throughput

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
# Guest workload
workload_baseline_mibs, workload_during_mibs, workload_degradation_pct,
# Calibration
actual_write_rate_mibs
```

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

---

## 11. Running the Experiment

### 11.1 Prerequisites

```bash
# Build release binary
./tools/devtool build --release

# Verify UFFD-WP support (kernel >= 5.7)
uname -r  # Should be >= 5.7

# Ensure sufficient host memory (need ~2x largest VM size)
free -h   # Need at least 10 GiB free for 4096 MiB VM tests
```

### 11.2 Execution

```bash
# Run the full experiment (all 400 test runs)
./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -s --log-cli-level=INFO \
    -m "" \
    --timeout=300 \
    --junit-xml=experiment_results.xml

# Run a single configuration for debugging
./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "mem_size_mib_512-workload_medium-iteration_0" \
    -s --log-cli-level=INFO \
    -m ""

# Run just one memory size
./tools/devtool -y test -- \
    integration_tests/functional/test_snapshot_live_experiment.py \
    -k "mem_size_mib_1024" \
    -s --log-cli-level=INFO \
    -m ""
```

### 11.3 Results Collection

Results are written to:
- `experiment_results.csv` — machine-readable, one row per test run
- `experiment_results.xml` — JUnit XML with metrics as `<property>` elements
- Console output — human-readable summary per configuration

---

## 12. Success Criteria

The experiment is considered successful if:

1. All 400 test runs complete without failure (snapshot + restore + SSH check)
2. Results for each configuration have a coefficient of variation (CV) < 20%
   for primary timing metrics (downtime, wall-clock, throughput)
3. Data is sufficient to confirm or reject all four hypotheses (H1-H4)
4. Visualizations clearly show the trade-off between downtime reduction and
   wall-clock time increase across the parameter space

---

## 13. Expected Outcomes

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
