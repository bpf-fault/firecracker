# Live Snapshot Experiment — Runbook

This document is the single source of truth for building the app rootfs, running the
benchmark, and generating analysis plots.  Read it top-to-bottom the first time you
set up on a new machine.

---

## 1. Overview

The benchmark measures how live (UFFD write-protect) snapshotting compares to full
snapshotting across:

* **VM memory sizes** — 256 MiB … 8 GiB
* **Guest write workloads** — idle, light (~4 MiB/s), medium, heavy (~128 MiB/s)
* **Application workloads** — Redis (redis_light / redis_mixed / redis_heavy),
  Memcached (memcached_light / memcached_heavy), STREAM

Key outputs:

| Metric | CSV column |
|--------|-----------|
| VM downtime | `downtime_us` (live) / `full_total_ms` (full) |
| Streaming throughput | `throughput_mibs` |
| App baseline throughput | `app_baseline_ops` |
| App during-snapshot throughput | `app_during_ops` |
| App tail latency (p99) | `app_baseline_p99_us`, `app_during_p99_us` |

All results land in a single `test_results/experiment_results.csv`.  Plotting and
analysis scripts read from that CSV independently — they are fully decoupled from the
tests.

**File map:**

```
tests/integration_tests/functional/
├── test_snapshot_live_experiment.py   # all pytest tests (synthetic + app)
├── experiment/                        # helpers used by the tests
│   ├── constants.py                   # CSV schema, parametrize values, workload params
│   ├── app_runners.py                 # full/live/live-bpf runners for app workloads
│   ├── runners.py                     # runners for synthetic (dd) workloads
│   ├── vm.py                          # VM boot + snapshot/restore helpers
│   ├── timeseries.py                  # per-second Redis throughput sampler
│   └── workloads/                     # redis.py, memcached.py, stream.py, dd.py
├── plot_experiment_results.py         # standalone: CSV → PNG plots
├── analyze_experiment_results.py      # standalone: CSV → text summary tables
└── analysis/
    ├── io.py                          # shared CSV load + schema constants
    └── stats.py                       # avg, stdev, linear_regression helpers
```

---

## 2. Prerequisites

* Firecracker repo cloned, `./tools/devtool build --release` completed
* Docker available (devtool uses it for the test container)
* **`memtier_benchmark` installed on the test HOST** — see section 2a
* For app workload tests: an `ubuntu-24.04-app.ext4` image (section 3)

---

## 2a. Installing memtier_benchmark on the host

All Redis per-window measurements (baseline, during-snapshot, post-snapshot) run
`memtier_benchmark` from the HOST connecting to guest Redis over the TAP IP.  This
mirrors the QEMU benchmark methodology: the load generator keeps running during VM
pause events, so latency spikes from the snapshot freeze are directly observable.

```bash
# Install build deps
sudo apt-get install -y git build-essential autoconf automake \
    libpcre3-dev libevent-dev pkg-config zlib1g-dev libssl-dev

# Build and install
cd /tmp
git clone --depth=1 https://github.com/RedisLabs/memtier_benchmark.git memtier_host
cd memtier_host
autoreconf -ivf && ./configure && make -j$(nproc)
sudo cp memtier_benchmark /usr/local/bin/
cd / && rm -rf /tmp/memtier_host

# Verify
memtier_benchmark --version
```

> **Note:** `memtier_benchmark` is also installed inside the guest rootfs (used by
> Memcached workloads).  The host installation is separate and only needed for Redis
> workloads.  On this Cloudlab machine the host binary is already at
> `/usr/local/bin/memtier_benchmark`.

---

## 3. Building the app rootfs

Use `setup_experiment.sh` in the repo root — it handles artifact download, rootfs
provisioning (Redis, memtier_benchmark, STREAM, Kerberos libs), SSH key copy, BPF
pre-build, and a smoke test end-to-end:

```bash
bash setup_experiment.sh
```

The script is idempotent: re-running it skips steps that are already done.
See its inline comments for the full step list and any manual overrides.

---

## 4. Running the tests

All tests run inside the Firecracker devtool container.  The repo is mounted at
`/firecracker` inside the container.  The `tools/test.sh` wrapper copies
`build/artifacts/<hash>/x86_64/` → `/srv/test_artifacts/` at startup, so use the
container-internal path for `EXPERIMENT_ROOTFS`.

### 4a. Quick smoke test — no app rootfs needed (~3 min)

Runs all three snapshot modes (full / live / live_bpf) against synthetic workloads at
512 MiB, 1 iteration each.  This is the fastest way to confirm the framework works.

```bash
./tools/devtool -y test -- \
  -k "test_snapshot_experiment_quick" \
  integration_tests/functional/test_snapshot_live_experiment.py \
  -s --log-cli-level=INFO -m ""
```

Expected console output ends with a comparison table:

```
                        Full          Live         LiveBpf      Speedup
  Downtime:          383.6 ms        19.7 ms        19.5 ms      19.5x /   19.6x
```

### 4b. App workload smoke test — all 3 modes, redis_light, 2 GiB (~7 min)

Runs full / live / live_bpf for redis_light at 2048 MiB, 1 iteration each,
across both PCI configs (6 tests total).

```bash
EXPERIMENT_ROOTFS=/srv/test_artifacts/ubuntu-24.04-app.ext4 \
./tools/devtool -y test -- \
  -k "redis_light and 2048 and app_experiment and 0-redis" \
  integration_tests/functional/test_snapshot_live_experiment.py \
  -s --log-cli-level=INFO -m ""
```

Expected: `APP RUN: 2048 MiB, redis_light workload` for each mode with non-zero
`Baseline ops/sec` and `Ops degradation` fields.  BPF degradation should be
lower than UFFD (typically ~2% vs ~4–6%).

### 4c. Full synthetic experiment matrix (@nonci, several hours)

```bash
./tools/devtool -y test -- \
  -k "test_live_snapshot_experiment or test_full_snapshot_experiment or test_live_bpf_snapshot_experiment" \
  integration_tests/functional/test_snapshot_live_experiment.py \
  -s --log-cli-level=INFO -m ""
```

### 4d. Full app workload matrix (@nonci, many hours)

```bash
EXPERIMENT_ROOTFS=/srv/test_artifacts/ubuntu-24.04-app.ext4 \
./tools/devtool -y test -- \
  -k "test_live_snapshot_app_experiment or test_full_snapshot_app_experiment or test_live_bpf_snapshot_app_experiment" \
  integration_tests/functional/test_snapshot_live_experiment.py \
  -s --log-cli-level=INFO -m ""
```

### Useful filter patterns

```bash
# One workload, all iterations
-k "redis_heavy and live_snapshot_app"

# One memory size across all app tests
-k "2048 and app_experiment"

# Specific parametrized test (use exact collected ID from --collect-only)
"test_live_snapshot_app_experiment[vmlinux-5.10.245-PCI_ON-0-redis_heavy-4096]"
```

---

## 5. Generating plots and analysis

Use `run_snapshot_benchmark.py` to run tests and export results, then
`plot_snapshot_benchmark.py` (in the bpf-fault bench repo) to produce figures.
See `docs/snapshot-benchmark-runbook.md` for the full option reference.

```bash
# Run one workload and export to bpf-fault bench repo
python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
    --workload redis_heavy \
    --bench-dir /mydata/bpf-fault/bench

# Plot (from inside the bench repo)
cd /mydata/bpf-fault/bench
python3 plot_snapshot_benchmark.py results/snapshot_benchmark_redis_heavy.json \
    --outdir fc_redis_heavy_figures
```

Key output figures:

| File | Content |
|------|---------|
| `timeseries_<mem>mib_<mode>.png` | Throughput + latency over time with snapshot markers |
| `throughput_during_snapshot.png` | Baseline vs live vs live_bpf throughput during phases 2–4 |
| `tail_latency_comparison.png` | p99 latency during phases 2–4 per memory size |
| `downtime_comparison.csv` | Full vs live vs live_bpf downtime, mean ± std |

---

## 6. CSV output format

All experiments append to a single 157-column CSV.  Override the path with
`EXPERIMENT_RESULTS_CSV=/path/to/custom.csv`.

**Key columns:**

| Column | Description |
|--------|-------------|
| `mem_size_mib`, `workload`, `snapshot_mode`, `iteration` | Run identity |
| `downtime_us` | VM pause duration — the primary live-snapshot metric (µs) |
| `full_total_ms` | Full-snapshot total time (ms) |
| `throughput_mibs` | Memory streaming throughput (MiB/s) |
| `app_baseline_ops` | Redis/Memcached ops/sec before snapshot |
| `app_during_ops` | Redis/Memcached ops/sec during snapshot streaming |
| `app_ops_degradation_pct` | `(baseline - during) / baseline × 100` |
| `app_baseline_p99_us` / `app_during_p99_us` | Tail latency before and during snapshot (µs) |
| `post_snap_ops` / `post_snap_p99_us` | Recovery window measurements |
| `timeseries_file` | Path to per-run throughput-timeline CSV (100 ms samples) |

Timeseries CSVs live at `test_results/timeseries/<workload>_<mib>mib_<mode>_iter<nn>.csv`
with columns: `t_ms, t_rel_s, throughput, avg_ms, p50_ms, p99_ms, p999_ms, failed`.

---

## 7. Benchmark organization

All tests live in one file:

```
tests/integration_tests/functional/test_snapshot_live_experiment.py
```

| Test function | Type | Marker |
|---------------|------|--------|
| `test_full_snapshot_experiment` | Synthetic (dd) | `@nonci` |
| `test_live_snapshot_experiment` | Synthetic (dd) | `@nonci` |
| `test_live_bpf_snapshot_experiment` | Synthetic (dd) | `@nonci` |
| `test_full_snapshot_app_experiment` | App (Redis/Memcached/STREAM) | `@nonci` |
| `test_live_snapshot_app_experiment` | App (Redis/Memcached/STREAM) | `@nonci` |
| `test_live_bpf_snapshot_app_experiment` | App (Redis only) | `@nonci` |
| `test_snapshot_experiment_quick` | Smoke (synthetic + optional app) | no marker |

All tests write to the **same** CSV schema, so a single run of the analysis scripts
covers both synthetic and app results.

Benchmarking is **decoupled** from analysis: tests only write CSV rows; the plot and
analyze scripts only read CSV rows.  Neither calls the other.

---

## 8. Architecture notes

### Thread model during live snapshot

Firecracker runs one process per microVM with these threads:

| Thread | Name | Role during snapshot |
|--------|------|---------------------|
| API | `fc_api` | Receives snapshot HTTP request, returns response |
| VMM / event loop | `fc_vmm` | Validates request, submits job to worker, returns to epoll |
| vCPU (x N) | `fc_vcpu N` | Continue running with write-protection enabled |
| Snapshot worker | `fc_snap_work` | Executes all 4 snapshot phases |

The worker thread is spawned before seccomp filters are applied and also runs
`populate_pages` (MADV_POPULATE_READ) at boot before entering its receive loop.
When a live snapshot is requested, the VMM thread submits a `SnapshotJob` to the
worker via a bounded channel and immediately returns to its epoll loop.  The
worker does:

1. **Phase 1 (no Vmm lock)** — file creation, fallocate, BPF/UFFD setup
2. **Phase 2 (brief Vmm lock)** — pause vCPUs, save state, enable WP, resume
3. **Phase 3 (no Vmm lock)** — stream RAM to disk
4. **Phase 4 (no Vmm lock)** — finalize, drop WP, sync file

This keeps the event loop responsive for device I/O throughout the snapshot.

### BPF streaming optimizations

The BPF streaming path (`bpf_live_snapshot.rs`) uses two key optimizations
modelled after QEMU's `bpf-fault-snapshot.c`:

* **Drain-before-scan** — The ring buffer is drained *before* each linear scan
  batch.  Pre-images captured by the BPF handler are saved and marked in a
  bitmap; the linear scan skips those pages.  This eliminates the double-write
  problem (linear scan saving stale data, then ring buffer overwriting with the
  correct pre-image).

* **Per-run wp_resolve** — Write-protection is resolved immediately after saving
  each contiguous run of pages, not after the entire 4096-page batch.  This
  minimises the window where guest writes to already-saved clean pages trigger
  unnecessary BPF fault handler invocations.

### BPF object compilation

The BPF program (`resources/bpf/snapshot_fault_ops.bpf.c`) must be compiled
against the running kernel's BTF.  The dev container includes `bpftool` and
`libbpf-dev` for this purpose.  The `build.rs` script:

1. Generates `vmlinux.h` from `/sys/kernel/btf/vmlinux` via `bpftool btf dump`
2. Compiles the BPF C source with `clang -target bpf`
3. Falls back to a pre-existing `.bpf.o` if either tool is missing

To recompile on the host (outside the container):

```bash
cd resources/bpf
bpftool btf dump file /sys/kernel/btf/vmlinux format c > vmlinux.h
clang -O2 -g -target bpf -D__TARGET_ARCH_x86 \
  -I/usr/local/include -I. -c snapshot_fault_ops.bpf.c -o snapshot_fault_ops.bpf.o
```

Then rebuild Firecracker so the new object is embedded via `include_bytes!`.
