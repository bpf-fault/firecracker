# Snapshot Benchmark Runbook

How to run the Firecracker snapshot benchmarks and plot the results.

---

## Running Benchmarks

**Script:** `tests/integration_tests/functional/run_snapshot_benchmark.py`

Run from the repo root. Runs all three snapshot modes (`full`, `live`, `live_bpf`) via `devtool`
for the given workload and memory sizes, then exports results to the bpf-fault bench repo.

```bash
python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
    --workload redis_heavy \
    --bench-dir /mydata/bpf-fault/bench
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--workload NAME` | `redis_light` | Workload: `redis_light`, `redis_heavy`, `redis_mixed`, `memcached_light`, `memcached_heavy`, `stream` |
| `--mem-sizes N…` | `2048 4096 8192` | VM memory sizes in MiB (space-separated) |
| `--iterations N` | `3` | Iterations per (mode × mem_size) configuration |
| `--bench-dir PATH` | `/mydata/bpf-fault/bench` | bpf-fault bench directory; JSON and timeseries CSVs are written here |
| `--skip-run` | off | Skip pytest; re-export results from existing `test_results/` |
| `--max-iteration N` | `2` | Export only iterations 0..N (default exports iterations 0, 1, 2 from 3 runs) |
| `--keep-artifacts` | off | Keep per-test VM artifact dirs after a successful run (needed for post-failure debugging) |
| `--rootfs PATH` | `/srv/test_artifacts/ubuntu-24.04-app.ext4` | Guest rootfs (container-internal path — normally don't change this) |

### Common invocations

```bash
# Quick smoke: single mem size, 1 iteration (~15 min)
python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
    --workload redis_heavy --mem-sizes 4096 --iterations 1 \
    --bench-dir /mydata/bpf-fault/bench

# Full 3-iteration run for one workload (~2 h)
python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
    --workload redis_heavy \
    --bench-dir /mydata/bpf-fault/bench

# Re-export only — regenerate JSON without re-running tests
python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
    --workload redis_heavy \
    --bench-dir /mydata/bpf-fault/bench \
    --skip-run

# All workloads back-to-back
for wl in redis_light redis_heavy redis_mixed memcached_light memcached_heavy stream; do
    python3 tests/integration_tests/functional/run_snapshot_benchmark.py \
        --workload "$wl" --bench-dir /mydata/bpf-fault/bench
done
```

### Output

| Path | Description |
|------|-------------|
| `<bench-dir>/results/snapshot_benchmark_<workload>.json` | 27-record JSON index (3 modes × 3 mem sizes × 3 iterations) |
| `<bench-dir>/results/timeseries/*.csv` | Per-run throughput/latency timeseries (100 ms samples) |
| `test_results/experiment_results.csv` | Cumulative raw results (all runs, all time) |

Per-test VM artifact directories (`test_results/test_*_snapshot_app_experiment/`) are deleted
automatically on success. Pass `--keep-artifacts` to retain them for debugging.

---

## Plotting Results

**Script:** `/mydata/bpf-fault/bench/plot_snapshot_benchmark.py`

Takes the JSON produced above and generates PNGs and summary CSVs.

```bash
cd /mydata/bpf-fault/bench
python3 plot_snapshot_benchmark.py results/snapshot_benchmark_redis_heavy.json \
    --outdir fc_redis_heavy_figures
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `json` (positional) | — | Path to `snapshot_benchmark_<workload>.json` |
| `--outdir PATH` | `figures` | Directory for output PNGs and CSVs |
| `--mem-sizes N…` | auto-detect | Restrict to specific mem sizes; default uses all sizes found in the JSON |
| `--log-latency` | off | Log scale on latency axes (useful when UFFD latency dwarfs eBPF) |

### What it produces

| File | Description |
|------|-------------|
| `total_snapshot_time.csv` | Full vs live total wall-clock time, mean ± std per mem size |
| `downtime_comparison.csv` | Full vs live vs live_bpf downtime, mean ± std per mem size |
| `phase_breakdown.csv` | Per-phase µs averages per (mode, mem_size) |
| `tail_latency_comparison.csv` | Freeze-window p99 latency per (mode, mem_size) |
| `timeseries_<mem>mib_<mode>.png` | 9 plots (3 mem × 3 mode): throughput + avg/p99 latency over time with snapshot markers |
| `throughput_during_snapshot.png` | Bar chart: baseline vs live vs live_bpf throughput during phases 2–4 |
| `tail_latency_comparison.png` | Bar chart: p99 latency during phases 2–4 per mem size |

All timeseries plots share the same y-axis scale for cross-plot comparison.

### Common invocations

```bash
cd /mydata/bpf-fault/bench

# Standard run
python3 plot_snapshot_benchmark.py results/snapshot_benchmark_redis_heavy.json \
    --outdir fc_redis_heavy_figures

# Log scale on latency (makes UFFD vs eBPF easier to compare)
python3 plot_snapshot_benchmark.py results/snapshot_benchmark_redis_heavy.json \
    --outdir fc_redis_heavy_figures --log-latency

# Only 4 GiB results
python3 plot_snapshot_benchmark.py results/snapshot_benchmark_redis_heavy.json \
    --outdir fc_redis_heavy_figures --mem-sizes 4096
```

---

## Snapshot Modes

| Mode | Flag | Mechanism | VM Downtime |
|------|------|-----------|-------------|
| `full` | `test_full_snapshot_app_experiment` | Pause VM, dump all memory synchronously | Full dump duration (~30–80 s) |
| `live` | `test_live_snapshot_app_experiment` | UFFD write-protect; faults block vCPU until handler services them | ~19 ms (WP setup only) |
| `live_bpf` | `test_live_bpf_snapshot_app_experiment` | eBPF write-protect; faults handled non-blocking in kernel | ~19 ms (WP setup only) |

> **Note:** `live_bpf` requires the patched kernel (`6.17.0-bpf-fault+`) to be booted.

---

## Environment

- Repo root: `/mydata/firecracker`
- Bench dir: `/mydata/bpf-fault/bench`
- App rootfs (host path): `build/artifacts/s3:--spec.ccfc.min-firecracker-ci-20260513-90d69327f9f2-0/x86_64/ubuntu-24.04-app.ext4`
- Raw results accumulate in: `test_results/experiment_results.csv`
- Timeseries CSVs: `test_results/timeseries/`
