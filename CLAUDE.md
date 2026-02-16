# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Firecracker is a lightweight virtual machine monitor (VMM) that uses KVM to create and manage microVMs. It is designed for serverless computing and container workloads with minimal overhead. Written in Rust, it targets `x86_64-unknown-linux-musl` and `aarch64-unknown-linux-musl`.

## Build & Development Commands

All development happens inside a Docker container managed by `./tools/devtool`. Key commands:

```bash
./tools/devtool build              # Debug build (output: build/debug/)
./tools/devtool build --release    # Release build with LTO (output: build/release/)
./tools/devtool checkstyle         # All style checks (Rust, Python, Markdown)
./tools/devtool fmt                # Auto-format (cargo fmt, black, isort, mdformat)
./tools/devtool checkbuild --all   # Clippy checks for all architectures
./tools/devtool shell              # Interactive shell in dev container
./tools/devtool shell -p           # Privileged shell (needed to run Firecracker)
```

Use `-y` flag for unattended mode: `./tools/devtool -y test`.

## Testing

**Python integration tests** (primary test suite, pytest-based):
```bash
./tools/devtool -y test -- integration_tests/functional/test_api.py -s --log-cli-level=INFO   # One file
./tools/devtool -y test -- integration_tests/functional/test_api.py::test_api_happy_start -s --log-cli-level=INFO  # One test
./tools/devtool -y test -- -k "pattern" integration_tests/ -s --log-cli-level=INFO            # Filter by name
./tools/devtool -y test -- -k "test_name" integration_tests/path.py -s --log-cli-level=INFO -m ""  # Run nonci tests
```

Always pass `-s --log-cli-level=INFO` so that Firecracker log output and test logging are visible. Tests marked `@pytest.mark.nonci` are excluded by default; pass `-m ""` to override marker filtering and include them.

**Rust unit tests:**
```bash
cargo test --lib           # Unit tests in library crates
cargo test --test integration_tests --all  # Rust integration tests (src/vmm/tests/)
```

**Test markers:** `@pytest.mark.nonci` (schedule-only), `@pytest.mark.no_block_pr` (optional in CI). Unmarked tests are required to pass PR CI.

**Networking:** `172.16.0.0/12` is reserved for the virtual node control network — do NOT use it for TAP device IPs. Use the `192.168.0.0/16` range for TAP IPs instead.

## Architecture

Firecracker runs one process per microVM with three thread types:

- **API Thread** — HTTP server on a Unix socket, receives configuration and control requests
- **VMM Thread** — Device emulation, event loop (epoll-based), handles MMIO/PIO exits
- **vCPU Thread(s)** — One per virtual CPU, runs the KVM_RUN loop

### Key Crates (in `src/`)

- **`firecracker`** — Binary entrypoint. CLI parsing, API server setup, seccomp filter installation
- **`vmm`** — Core VMM logic. This is where most development happens:
  - `builder.rs` — MicroVM construction and boot sequence
  - `rpc_interface.rs` — API request dispatch to VMM operations
  - `resources.rs` — VM resource configuration container
  - `vstate/` — KVM wrappers: VM lifecycle (`vm.rs`), vCPU management (`vcpu.rs`), guest memory (`memory.rs`), device buses (`bus.rs`)
  - `devices/virtio/` — Virtio device implementations (block, net, balloon, vsock, pmem, rng, mem)
  - `devices/virtio/queue.rs` — Virtio queue implementation shared by all virtio devices
  - `arch/` — Platform-specific code (x86_64, aarch64): boot parameters, interrupt controllers, memory layout
  - `mmds/` — MicroVM Metadata Service (minimal TCP/IP stack in `dumbo/`)
  - `persist.rs`, `snapshot/` — Save/restore (snapshotting) infrastructure
  - `device_manager/` — Device creation and lifecycle management
- **`jailer`** — Sandboxing binary (excluded from default workspace build, requires musl)
- **`seccompiler`** — Seccomp BPF filter compilation from JSON definitions
- **`utils`** — Shared utilities (tempfile, kernel version, time, signal handling)

### Device I/O Flow

Guest I/O → KVM exit → vCPU thread → MMIO/PIO bus → device handler → virtio queue processing → host I/O (TAP, file, etc.)

## Code Conventions

- **Rust 1.93.0** pinned in `rust-toolchain.toml`, edition 2024
- **No `unwrap()`** — use `?` operator or `map_err` for error propagation
- **Unsafe blocks** require `// SAFETY:` and `// JUSTIFICATION:` comments
- **All public functions** must have doc comments (`///`)
- **Panic behavior:** `panic = "abort"` in all profiles
- **Import style:** `imports_granularity = "Module"`, grouped as std/external/crate
- **Workspace lints** in root `Cargo.toml` apply to all crates — casts, missing debug impls, undocumented unsafe are warnings
- **Build output:** `build/cargo_target/` (configured in `.cargo/config.toml`)
- **Commits:** signed with DCO (`git commit -s`), ≤72 char title, one logical change per commit
