#!/usr/bin/env bash
# setup_experiment.sh — from-scratch setup for Firecracker snapshot benchmarks.
#
# Usage:
#   ./setup_experiment.sh [options]
#
# Options:
#   --skip-build      Skip Firecracker release build (reuse existing binary)
#   --skip-memtier    Skip host memtier_benchmark install
#   --skip-rootfs     Skip app rootfs build (reuse existing image)
#   --no-smoke-test   Skip the quick synthetic smoke test at the end
#
# Override paths with env vars:
#   MEMTIER_REPO   (default: git@github.com:bpf-fault/memtier_benchmark.git)
#   MEMTIER_SRC    (default: /mydata/memtier_benchmark)
#   BPFFAULT_REPO  (default: git@github.com:bpf-fault/bpf-fault.git)
#   BPFFAULT_DIR   (default: /mydata/bpf-fault)
#   BENCH_DIR      (default: /mydata/bpf-fault/bench)
#   APP_ROOTFS_SIZE (default: 2G)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MEMTIER_REPO="${MEMTIER_REPO:-git@github.com:bpf-fault/memtier_benchmark.git}"
MEMTIER_SRC="${MEMTIER_SRC:-/mydata/memtier_benchmark}"
BPFFAULT_REPO="${BPFFAULT_REPO:-git@github.com:bpf-fault/bpf-fault.git}"
BPFFAULT_DIR="${BPFFAULT_DIR:-/mydata/bpf-fault}"
BENCH_DIR="${BENCH_DIR:-/mydata/bpf-fault/bench}"
APP_ROOTFS_SIZE="${APP_ROOTFS_SIZE:-2G}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_step() { echo ""; echo "══════════════════════════════════════════════"; echo "  $*"; echo "══════════════════════════════════════════════"; }
_log()  { echo "[setup] $*"; }
_warn() { echo "[WARN]  $*" >&2; }
_die()  { echo "[ERROR] $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
SKIP_BUILD=false
SKIP_MEMTIER=false
SKIP_ROOTFS=false
NO_SMOKE=false

for arg in "$@"; do
    case "$arg" in
        --skip-build)    SKIP_BUILD=true ;;
        --skip-memtier)  SKIP_MEMTIER=true ;;
        --skip-rootfs)   SKIP_ROOTFS=true ;;
        --no-smoke-test) NO_SMOKE=true ;;
        *) _die "Unknown option: $arg" ;;
    esac
done

# ---------------------------------------------------------------------------
# Step 1 — System prerequisites
# ---------------------------------------------------------------------------
_step "Step 1: System prerequisites"

for tool in git sudo make; do
    command -v "$tool" &>/dev/null || _die "Required tool not found: $tool — install it first."
done

if ! command -v docker &>/dev/null; then
    _log "Docker not found. Installing docker.io..."
    sudo apt-get update -qq
    sudo apt-get install -y docker.io
    sudo usermod -aG docker "$USER"
    _warn "Added $USER to the 'docker' group. You may need to log out and back in for this to take effect."
    _warn "If devtool fails with a permission error, run: newgrp docker"
else
    _log "Docker already installed: $(docker --version)"
fi

# ---------------------------------------------------------------------------
# Step 2 — Build Firecracker release
# ---------------------------------------------------------------------------
_step "Step 2: Build Firecracker (release)"

FC_BINARY="build/cargo_target/x86_64-unknown-linux-musl/release/firecracker"
if $SKIP_BUILD && [[ -f "$FC_BINARY" ]]; then
    _log "Skipping build (--skip-build, binary exists)."
else
    _log "Running ./tools/devtool build --release ..."
    ./tools/devtool build --release
fi

# ---------------------------------------------------------------------------
# Step 3 — Download test artifacts and locate them
# ---------------------------------------------------------------------------
_step "Step 3: Download test artifacts"

# A collect-only run triggers artifact download without executing any tests.
_log "Triggering artifact download via devtool (collect-only)..."
./tools/devtool -y test -- \
    --collect-only integration_tests/functional/test_api.py -q 2>&1 | head -10 || true

# Locate the artifact directory.
if [[ -f build/current_artifacts ]]; then
    ARTIFACTS_DIR=$(cat build/current_artifacts)
    _log "Artifacts at: $ARTIFACTS_DIR"
elif ls build/artifacts/ 2>/dev/null | grep -qv '^x86_64$'; then
    HASH=$(ls build/artifacts/ | grep -v '^x86_64$' | tail -1)
    ARTIFACTS_DIR="build/artifacts/$HASH/x86_64"
    _log "Artifacts at: $ARTIFACTS_DIR (resolved from build/artifacts/)"
else
    _die "Cannot find test artifacts. Ensure 'devtool build --release' completed successfully."
fi

BASE_ROOTFS="$ARTIFACTS_DIR/ubuntu-24.04.ext4"
[[ -f "$BASE_ROOTFS" ]] || _die "Base rootfs not found: $BASE_ROOTFS"

APP_ROOTFS="$ARTIFACTS_DIR/ubuntu-24.04-app.ext4"

# ---------------------------------------------------------------------------
# Step 4 — Host memtier_benchmark (bpf-fault fork with --stats-interval)
# ---------------------------------------------------------------------------
_step "Step 4: Host memtier_benchmark (bpf-fault fork)"

if $SKIP_MEMTIER; then
    _log "Skipping memtier_benchmark install (--skip-memtier)."
elif memtier_benchmark --help 2>&1 | grep -q "stats-interval"; then
    _log "memtier_benchmark with --stats-interval already installed at $(command -v memtier_benchmark)."
else
    _log "Installing build dependencies..."
    sudo apt-get install -y \
        build-essential autoconf automake \
        libpcre3-dev libevent-dev pkg-config zlib1g-dev libssl-dev

    if [[ ! -d "$MEMTIER_SRC" ]]; then
        _log "Cloning $MEMTIER_REPO → $MEMTIER_SRC ..."
        git clone "$MEMTIER_REPO" "$MEMTIER_SRC"
    else
        _log "$MEMTIER_SRC exists, pulling latest..."
        git -C "$MEMTIER_SRC" pull --ff-only || \
            _warn "Could not pull memtier_benchmark; building from current state."
    fi

    _log "Building memtier_benchmark..."
    cd "$MEMTIER_SRC"
    autoreconf -ivf
    ./configure
    make -j"$(nproc)"
    sudo cp memtier_benchmark /usr/local/bin/
    cd "$REPO_ROOT"

    memtier_benchmark --help 2>&1 | grep -q "stats-interval" || \
        _die "Built memtier_benchmark does not have --stats-interval. Check the repo."
    _log "memtier_benchmark installed: $(command -v memtier_benchmark)"
fi

# ---------------------------------------------------------------------------
# Step 5 — bpf-fault bench repo
# ---------------------------------------------------------------------------
_step "Step 5: bpf-fault bench repo"

if [[ ! -d "$BPFFAULT_DIR" ]]; then
    _log "Cloning $BPFFAULT_REPO → $BPFFAULT_DIR ..."
    git clone "$BPFFAULT_REPO" "$BPFFAULT_DIR"
else
    _log "$BPFFAULT_DIR already exists, skipping clone."
fi
mkdir -p "$BENCH_DIR/results"
_log "Bench dir ready: $BENCH_DIR"

# ---------------------------------------------------------------------------
# Step 6 — Build app rootfs
# ---------------------------------------------------------------------------
_step "Step 6: Build app rootfs (ubuntu-24.04-app.ext4)"

if $SKIP_ROOTFS && [[ -f "$APP_ROOTFS" ]]; then
    _log "Skipping rootfs build (--skip-rootfs, image exists at $APP_ROOTFS)."
else
    [[ -f "$BASE_ROOTFS" ]] || _die "Base rootfs not found: $BASE_ROOTFS"

    _log "Copying base rootfs → $APP_ROOTFS ..."
    cp "$BASE_ROOTFS" "$APP_ROOTFS"

    _log "Expanding image to $APP_ROOTFS_SIZE ..."
    sudo truncate -s "$APP_ROOTFS_SIZE" "$APP_ROOTFS"
    sudo e2fsck -f -y "$APP_ROOTFS" || true   # e2fsck exits 1 on corrected errors
    sudo resize2fs "$APP_ROOTFS"

    # Mount with cleanup trap so mounts are always released.
    _cleanup_mounts() {
        _log "Cleaning up mounts..."
        sudo umount /mnt/dev/pts 2>/dev/null || true
        sudo umount /mnt/dev     2>/dev/null || true
        sudo umount /mnt/sys     2>/dev/null || true
        sudo umount /mnt/proc    2>/dev/null || true
        sudo umount /mnt         2>/dev/null || true
    }
    trap _cleanup_mounts EXIT

    _log "Mounting image at /mnt ..."
    sudo mount -o loop "$APP_ROOTFS" /mnt

    # Fix permissions and missing dirs that break apt inside chroot.
    sudo chmod 1777 /mnt/tmp
    sudo mkdir -p \
        /mnt/var/cache/apt/archives/partial \
        /mnt/var/lib/apt/lists/partial \
        /mnt/var/log/apt
    sudo touch \
        /mnt/var/log/dpkg.log \
        /mnt/var/log/apt/history.log \
        /mnt/var/log/apt/term.log

    # Bind-mount kernel filesystems so apt/dpkg work correctly inside chroot.
    sudo mount -t proc  proc   /mnt/proc
    sudo mount -t sysfs sysfs  /mnt/sys
    sudo mount -o bind  /dev   /mnt/dev
    sudo mount -o bind  /dev/pts /mnt/dev/pts

    # DNS: copy the host resolver config so apt can reach the internet.
    sudo mkdir -p /mnt/run/systemd/resolve
    if [[ -f /run/systemd/resolve/resolv.conf ]]; then
        sudo cp /run/systemd/resolve/resolv.conf /mnt/run/systemd/resolve/resolv.conf
    else
        sudo cp /etc/resolv.conf /mnt/etc/resolv.conf
    fi

    _log "Installing packages inside chroot (this takes several minutes)..."
    sudo chroot /mnt /bin/bash <<'CHROOT'
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq

# --- SSH dependency: Kerberos / GSS-API libs ---
# Ubuntu 24.04's sshd links against these. If absent, sshd-socket-generator
# exits 127, ssh.socket is stopped, and all SSH connections are refused.
apt-get install -y libgssapi-krb5-2 libkrb5-3 libkeyutils1

# --- Redis ---
apt-get install -y redis-server redis-tools

# --- Memcached + nc ---
# Required by memcached workloads (memcached for the server, nc for health checks).
apt-get install -y memcached netcat-openbsd

# --- memtier_benchmark (built from upstream source) ---
# The guest only uses memtier for pre-population; it doesn't need --stats-interval.
apt-get install -y git build-essential autoconf automake \
    libpcre3-dev libevent-dev pkg-config zlib1g-dev libssl-dev
git clone --depth=1 https://github.com/RedisLabs/memtier_benchmark.git /tmp/memtier
cd /tmp/memtier
autoreconf -ivf && ./configure && make -j"$(nproc)"
cp memtier_benchmark /usr/local/bin/
cd / && rm -rf /tmp/memtier

# --- STREAM memory-bandwidth benchmark ---
apt-get install -y gcc curl
curl -fsSL https://www.cs.virginia.edu/stream/FTP/Code/stream.c -o /tmp/stream.c
gcc -O2 -fopenmp \
    -DSTREAM_ARRAY_SIZE=11184810 \
    -DNTIMES=20 \
    -o /usr/local/bin/stream /tmp/stream.c -lm
rm /tmp/stream.c

apt-get clean
CHROOT

    # Release bind mounts (trap will also run but this makes the verify step cleaner).
    _cleanup_mounts
    trap - EXIT

    _log "Verifying rootfs..."
    sudo mount -o loop,ro "$APP_ROOTFS" /mnt
    ls \
        /mnt/usr/bin/redis-server \
        /mnt/usr/bin/memcached \
        /mnt/usr/local/bin/memtier_benchmark \
        /mnt/usr/local/bin/stream \
        /mnt/usr/bin/nc
    MISSING=$(ldd /mnt/usr/sbin/sshd 2>/dev/null | grep "not found" || true)
    if [[ -n "$MISSING" ]]; then
        sudo umount /mnt
        _die "sshd has missing libraries:\n$MISSING"
    fi
    _log "sshd library check: OK"
    sudo umount /mnt

    _log "App rootfs ready: $APP_ROOTFS"
fi

# ---------------------------------------------------------------------------
# Step 7 — Smoke test
# ---------------------------------------------------------------------------
_step "Step 7: Smoke test"

if $NO_SMOKE; then
    _log "Skipping smoke test (--no-smoke-test)."
else
    _log "Running quick synthetic smoke test (all 3 modes, 512 MiB, no app rootfs needed)..."
    ./tools/devtool -y test -- \
        -k "test_snapshot_experiment_quick" \
        integration_tests/functional/test_snapshot_live_experiment.py \
        -s --log-cli-level=INFO -m ""
    _log "Smoke test passed."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
_step "Setup complete"

echo ""
echo "App rootfs:  $APP_ROOTFS"
echo "Bench dir:   $BENCH_DIR"
echo ""
echo "Run a quick experiment (single workload, 1 iteration):"
echo ""
echo "  python3 tests/integration_tests/functional/run_snapshot_benchmark.py \\"
echo "    --workload redis_light \\"
echo "    --mem-sizes 2048 \\"
echo "    --iterations 1 \\"
echo "    --rootfs $APP_ROOTFS \\"
echo "    --bench-dir $BENCH_DIR"
echo ""
echo "Run the full benchmark (all workloads, all mem sizes, 3 iterations each):"
echo ""
echo "  for wl in redis_light redis_mixed redis_heavy memcached_light memcached_heavy stream; do"
echo "    python3 tests/integration_tests/functional/run_snapshot_benchmark.py \\"
echo "      --workload \"\$wl\" \\"
echo "      --rootfs $APP_ROOTFS \\"
echo "      --bench-dir $BENCH_DIR"
echo "  done"
