# Snapshot Benchmark Runbook

How to run the Firecracker snapshot benchmark and where its results go.

---

## Running the Benchmark

**Entry point:** `./tools/devtool bench` (runs `tests/bench/run_snapshot_bench.py`
inside the test container via `tools/bench.sh`).

Run from the repo root. Sweeps all three snapshot modes (`full`, `live`,
`live_bpf`) for the given workloads, memory sizes, and iterations in a single
container session, writing each configuration's record directly into the
results store as it completes.

```bash
# Default sweep: redis_heavy + memcached_heavy, 4/8 GiB, 3 iterations
./tools/devtool -y bench

# Store results outside the repo: mount a host directory at /bench_results
EXPERIMENT_RESULTS_DIR=/path/to/results ./tools/devtool -y bench -- \
    --results-dir /bench_results
```

### Options (after `--`)

| Flag | Default | Description |
|------|---------|-------------|
| `--workloads NAME…` | `redis_heavy memcached_heavy` | Workloads: `redis_light`, `redis_heavy`, `redis_mixed`, `memcached_light`, `memcached_heavy`, `stream` |
| `--mem-sizes N…` | `4096 8192` | VM memory sizes in MiB |
| `--iterations N` | `3` | Iterations per (mode × mem size) configuration |
| `--results-dir PATH` | `test_results/` | Results store (container-internal path; use `/bench_results` with `EXPERIMENT_RESULTS_DIR`) |
| `--rootfs PATH` | `/srv/test_artifacts/ubuntu-24.04-app.ext4` | Guest rootfs (container-internal path — normally don't change this) |
| `--no-reuse-results` | off | Rerun everything instead of skipping configurations already present |

### Common invocations

```bash
# Quick smoke: one workload, one mem size, 1 iteration (~5 min)
./tools/devtool -y bench -- --workloads redis_heavy --mem-sizes 4096 --iterations 1

# Full paper sweep (Figures 8 and 9, ~30 min)
./tools/devtool -y bench -- --workloads redis_heavy memcached_heavy
```

### Results store

The results store is the single source of truth — there is no separate
export step:

| Path | Description |
|------|-------------|
| `<results-dir>/snapshot_benchmark_<workload>.json` | One record per (mode × mem size × iteration), checkpointed after every configuration |
| `<results-dir>/timeseries/<workload>_<mem>mib_<mode>_iterNN.csv` | Per-run throughput/latency timeseries (100 ms samples) |

An interrupted sweep resumes where it left off: configurations whose record
*and* timeseries file are present are skipped. Deleting a timeseries file
re-measures that configuration; deleting a workload's JSON re-measures the
whole workload.

---

## Snapshot Modes

| Mode | Mechanism | VM Downtime |
|------|-----------|-------------|
| `full` | Pause VM, dump all memory synchronously | Full dump duration |
| `live` | UFFD write-protect; faults block vCPU until handler services them | WP setup only |
| `live_bpf` | eBPF write-protect; faults handled non-blocking in kernel | WP setup only |

> **Note:** `live_bpf` requires the patched kernel (`bpf-fault`) to be booted.

---

## Plotting

The results store is plain JSON and CSV, designed for downstream plotting
tooling: each record carries the configuration, snapshot timings, phase
breakdown, throughput/latency summaries, freeze-window statistics, and a
relative path to its timeseries CSV.
