#!/bin/bash
set -eu -o pipefail

# Sets up everything needed for the snapshot benchmarks that lives in this
# repo: system prerequisites (docker, AWS CLI), the BPF fault-ops object, the
# Firecracker release build, CI guest artifacts, and the app rootfs.
#
# Usage:
#   ./setup_experiment.sh [options]
#
# Options:
#   --skip-build      Skip Firecracker release build (reuse existing binary)
#   --skip-memtier    Skip the host memtier_benchmark check
#   --skip-rootfs     Skip app rootfs build (reuse existing image)
#   --no-smoke-test   Skip the quick synthetic smoke test at the end
#
# Override with env vars:
#   APP_ROOTFS_SIZE (default: 2G)
#   BPF_VMLINUX     Path to the bpf-fault patched kernel ELF, used to generate
#                   vmlinux.h when not booted into the patched kernel. The BPF
#                   program uses types (bpf_fault_ops_ctx, fault_ops) that only
#                   exist in the patched kernel; a stock kernel BTF will cause
#                   a build error.

APP_ROOTFS_SIZE="${APP_ROOTFS_SIZE:-2G}"
BPF_VMLINUX="${BPF_VMLINUX:-}"

SCRIPT_PATH=$(realpath $0)
BASE_DIR=$(dirname $SCRIPT_PATH)
cd "$BASE_DIR"

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
		*) echo "Unknown option: $arg"; exit 1 ;;
	esac
done

# Install system prerequisites
for tool in git sudo curl; do
	if ! command -v "$tool" &>/dev/null; then
		echo "Required tool not found: $tool. Please install it first."
		exit 1
	fi
done

# devtool uses the host AWS CLI to sync test artifacts from S3
if ! command -v aws &>/dev/null; then
	echo "Installing AWS CLI v2..."
	curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
	sudo apt-get install -y -qq unzip
	cd /tmp && unzip -q awscliv2.zip
	sudo /tmp/aws/install
	cd "$BASE_DIR"
fi

if ! command -v docker &>/dev/null; then
	echo "Installing docker.io..."
	sudo apt-get update -qq
	sudo apt-get install -y docker.io
fi

# Re-exec the script under the docker group so a fresh membership takes
# effect. Guarded so a non-permission failure (e.g. daemon not running)
# can't re-exec in an infinite loop.
if ! docker ps &>/dev/null; then
	if [[ "${_SETUP_REEXECED:-}" == "1" ]]; then
		echo "Docker is still inaccessible after re-exec. Is the daemon running?"
		exit 1
	fi
	sudo usermod -aG docker "$USER"
	echo "Re-executing script under the docker group..."
	exec sg docker -c "$(printf '%q ' env _SETUP_REEXECED=1 "$SCRIPT_PATH" "$@")"
fi

# Compile the BPF object on the host, before devtool: the devtool container
# cannot see BPF_VMLINUX outside the repo mount.
BPF_DIR="$BASE_DIR/resources/bpf"
BPF_SRC="$BPF_DIR/snapshot_fault_ops.bpf.c"
BPF_OBJ="$BPF_DIR/snapshot_fault_ops.bpf.o"
VMLINUX_H="$BPF_DIR/vmlinux.h"
KERNEL_BTF="/sys/kernel/btf/vmlinux"

if [[ -f "$BPF_OBJ" ]]; then
	echo "BPF object already exists: $BPF_OBJ, skipping."
else
	if ! command -v clang &>/dev/null; then
		echo "clang not found. Please install it: sudo apt-get install clang"
		exit 1
	fi

	# Ubuntu's /usr/sbin/bpftool is a wrapper that rejects non-packaged kernels.
	# Fall back to the versioned binary directly if the wrapper fails.
	BPFTOOL=bpftool
	if ! bpftool version &>/dev/null; then
		BPFTOOL=$(find /usr/lib/linux-tools -name bpftool 2>/dev/null | head -1)
		if [[ -z "$BPFTOOL" ]]; then
			echo "bpftool not found. Please install it: sudo apt-get install linux-tools-common"
			exit 1
		fi
	fi

	# Locate libbpf headers; install libbpf-dev if missing.
	BPF_INCLUDE=$(find /usr/include /usr/local/include \
			-path "*/bpf/bpf_helpers.h" 2>/dev/null | head -1)
	if [[ -z "$BPF_INCLUDE" ]]; then
		echo "Installing libbpf-dev..."
		sudo apt-get install -y libbpf-dev
		BPF_INCLUDE=$(find /usr/include /usr/local/include \
				-path "*/bpf/bpf_helpers.h" 2>/dev/null | head -1)
	fi
	BPF_INCLUDE="${BPF_INCLUDE%/bpf/bpf_helpers.h}"  # strip to parent include dir

	# Map uname -m to the BPF arch define (matches build.rs logic).
	case "$(uname -m)" in
		x86_64)  BPF_ARCH="__TARGET_ARCH_x86" ;;
		aarch64) BPF_ARCH="__TARGET_ARCH_aarch64" ;;
		*) echo "Unsupported architecture for BPF compilation: $(uname -m)"; exit 1 ;;
	esac

	# Generate vmlinux.h. Prefer the running kernel's BTF when the patched
	# kernel is booted; otherwise fall back to the static vmlinux ELF so the
	# object can be pre-built on a stock kernel. Dump to a file and grep that;
	# a `dump | grep -q` pipeline is unreliable under pipefail (grep's early
	# exit SIGPIPEs bpftool), and the successful dump doubles as the header.
	echo "Generating vmlinux.h..."
	if [[ -f "$KERNEL_BTF" ]] \
			&& $BPFTOOL btf dump file "$KERNEL_BTF" format c > "$VMLINUX_H.tmp" 2>/dev/null \
			&& grep -q "bpf_fault_ops_ctx" "$VMLINUX_H.tmp"; then
		echo "Using running patched kernel BTF: $KERNEL_BTF"
	elif [[ -n "$BPF_VMLINUX" && -f "$BPF_VMLINUX" ]] \
			&& $BPFTOOL btf dump file "$BPF_VMLINUX" format c > "$VMLINUX_H.tmp" \
			&& grep -q "bpf_fault_ops_ctx" "$VMLINUX_H.tmp"; then
		echo "Using static patched kernel BTF: $BPF_VMLINUX"
	else
		rm -f "$VMLINUX_H.tmp"
		echo "No BTF source with bpf_fault types found."
		echo "Boot the bpf-fault kernel or set BPF_VMLINUX=/path/to/vmlinux."
		exit 1
	fi
	mv "$VMLINUX_H.tmp" "$VMLINUX_H"

	echo "Compiling BPF program (arch=$BPF_ARCH)..."
	clang -O2 -g -target bpf \
		"-D$BPF_ARCH" \
		"-I$BPF_INCLUDE" \
		"-I$BPF_DIR" \
		-c "$BPF_SRC" -o "$BPF_OBJ"
fi

# Build Firecracker release
FC_BINARY="build/cargo_target/x86_64-unknown-linux-musl/release/firecracker"
if $SKIP_BUILD && [[ -f "$FC_BINARY" ]]; then
	echo "Skipping Firecracker build (--skip-build, binary exists)."
else
	echo "Building Firecracker (release)..."
	./tools/devtool build --release
fi

# Download test artifacts. devtool ensure_current_artifacts syncs squashfs
# files from S3, runs setup-ci-artifacts.sh inside the container to convert
# them to ext4 and inject SSH keys, and writes the path to
# build/current_artifacts.
#
# The artifact directory may be root-owned (written by the container). If it
# exists but has no ext4 files (incomplete download), we must remove it with
# sudo before re-running, otherwise devtool skips the conversion step.
EXT4=$(find build/artifacts -name "ubuntu-24.04.ext4" 2>/dev/null | head -1 || true)
if [[ -n "$EXT4" ]]; then
	echo "Artifacts already set up at: $(dirname "$EXT4")"
else
	if [[ -d build/artifacts ]]; then
		echo "Removing incomplete artifact directory..."
		sudo rm -rf build/artifacts
	fi
	echo "Downloading and converting test artifacts..."
	./tools/devtool -y ensure_current_artifacts
	EXT4=$(find build/artifacts -name "ubuntu-24.04.ext4" 2>/dev/null | head -1 || true)
	if [[ -z "$EXT4" ]]; then
		echo "Artifact setup failed: ubuntu-24.04.ext4 not found after download."
		exit 1
	fi
fi
ARTIFACTS_DIR=$(dirname "$EXT4")

BASE_ROOTFS="$ARTIFACTS_DIR/ubuntu-24.04.ext4"
APP_ROOTFS="$ARTIFACTS_DIR/ubuntu-24.04-app.ext4"

# The snapshot benchmark requires the fork's --stats-interval flag.
if $SKIP_MEMTIER; then
	echo "Skipping memtier_benchmark check (--skip-memtier)."
else
	# --help exits non-zero by design, so don't pipe it directly into grep under pipefail.
	help_output=$(memtier_benchmark --help 2>&1 || true)
	if ! grep -q "stats-interval" <<<"$help_output"; then
		echo "memtier_benchmark with --stats-interval not found on PATH."
		echo "Please install the memtier_benchmark fork with --stats-interval support first."
		exit 1
	fi
fi

# Build the app rootfs
if $SKIP_ROOTFS && [[ -f "$APP_ROOTFS" ]]; then
	echo "Skipping app rootfs build (--skip-rootfs, image exists)."
else
	if [[ ! -f "$BASE_ROOTFS" ]]; then
		echo "Base rootfs not found: $BASE_ROOTFS"
		exit 1
	fi

	echo "Copying base rootfs to $APP_ROOTFS..."
	sudo cp "$BASE_ROOTFS" "$APP_ROOTFS"
	sudo chmod 644 "$APP_ROOTFS"

	# Guard against truncate silently shrinking (and corrupting) the image.
	TARGET_BYTES=$(numfmt --from=iec "$APP_ROOTFS_SIZE")
	BASE_BYTES=$(stat -c%s "$APP_ROOTFS")
	if (( TARGET_BYTES < BASE_BYTES )); then
		echo "APP_ROOTFS_SIZE=$APP_ROOTFS_SIZE is smaller than the base image ($BASE_BYTES bytes)."
		exit 1
	fi

	echo "Expanding image to $APP_ROOTFS_SIZE..."
	sudo truncate -s "$APP_ROOTFS_SIZE" "$APP_ROOTFS"
	# e2fsck exits 1/2 when it corrected errors; only larger codes are failures.
	sudo e2fsck -f -y "$APP_ROOTFS" || [[ $? -le 2 ]]
	sudo resize2fs "$APP_ROOTFS"

	MNT=$(mktemp -d)

	# Mount with cleanup trap so mounts are always released.
	cleanup_mounts() {
		sudo umount "$MNT/dev/pts" 2>/dev/null || true
		sudo umount "$MNT/dev"     2>/dev/null || true
		sudo umount "$MNT/sys"     2>/dev/null || true
		sudo umount "$MNT/proc"    2>/dev/null || true
		sudo umount "$MNT"         2>/dev/null || true
	}
	trap cleanup_mounts EXIT

	echo "Mounting image at $MNT..."
	sudo mount -o loop "$APP_ROOTFS" "$MNT"

	# Fix permissions and missing dirs that break apt inside chroot.
	sudo chmod 1777 "$MNT/tmp"
	sudo mkdir -p \
		"$MNT/var/cache/apt/archives/partial" \
		"$MNT/var/lib/apt/lists/partial" \
		"$MNT/var/log/apt"
	sudo touch \
		"$MNT/var/log/dpkg.log" \
		"$MNT/var/log/apt/history.log" \
		"$MNT/var/log/apt/term.log"

	# Bind-mount kernel filesystems so apt/dpkg work correctly inside chroot.
	sudo mount -t proc  proc   "$MNT/proc"
	sudo mount -t sysfs sysfs  "$MNT/sys"
	sudo mount -o bind  /dev   "$MNT/dev"
	sudo mount -o bind  /dev/pts "$MNT/dev/pts"

	# DNS: replace the chroot's /etc/resolv.conf symlink (-> systemd stub at
	# 127.0.0.53) with the host's real resolver so apt can reach the internet.
	REAL_RESOLV=""
	[[ -f /run/systemd/resolve/resolv.conf ]] && REAL_RESOLV=/run/systemd/resolve/resolv.conf
	[[ -z "$REAL_RESOLV" && -f /etc/resolv.conf ]] && REAL_RESOLV=/etc/resolv.conf
	if [[ -n "$REAL_RESOLV" ]]; then
		sudo rm -f "$MNT/etc/resolv.conf"
		sudo cp "$REAL_RESOLV" "$MNT/etc/resolv.conf"
	fi

	# STREAM source is vendored in resources/ so the chroot needs no network
	# access beyond apt.
	sudo cp "$BASE_DIR/resources/stream.c" "$MNT/tmp/stream.c"

	echo "Installing packages inside chroot (this takes several minutes)..."
	sudo chroot "$MNT" /bin/bash <<'CHROOT'
set -e
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq

# SSH dependency: Kerberos / GSS-API libs. Ubuntu 24.04's sshd links against
# these. If absent, sshd-socket-generator exits 127, ssh.socket is stopped,
# and all SSH connections are refused.
apt-get install -y libgssapi-krb5-2 libkrb5-3 libkeyutils1

# Redis
apt-get install -y redis-server redis-tools

# Memcached + nc (memcached for the server, nc for health checks)
apt-get install -y memcached netcat-openbsd

# memtier_benchmark, built from upstream source. The guest only uses memtier
# for pre-population; it doesn't need --stats-interval.
apt-get install -y git build-essential autoconf automake \
	libpcre3-dev libevent-dev pkg-config zlib1g-dev libssl-dev
git clone --depth=1 https://github.com/RedisLabs/memtier_benchmark.git /tmp/memtier
cd /tmp/memtier
autoreconf -ivf && ./configure && make -j"$(nproc)"
cp memtier_benchmark /usr/local/bin/
cd / && rm -rf /tmp/memtier

# STREAM memory-bandwidth benchmark
apt-get install -y gcc
gcc -O2 -fopenmp \
	-DSTREAM_ARRAY_SIZE=11184810 \
	-DNTIMES=20 \
	-o /usr/local/bin/stream /tmp/stream.c -lm
rm /tmp/stream.c

apt-get clean
CHROOT

	# Release bind mounts (trap will also run but this makes the verify step cleaner).
	cleanup_mounts
	trap - EXIT

	echo "Verifying rootfs..."
	sudo mount -o loop,ro "$APP_ROOTFS" "$MNT"
	ls \
		"$MNT/usr/bin/redis-server" \
		"$MNT/usr/bin/memcached" \
		"$MNT/usr/local/bin/memtier_benchmark" \
		"$MNT/usr/local/bin/stream" \
		"$MNT/usr/bin/nc"
	MISSING=$(ldd "$MNT/usr/sbin/sshd" 2>/dev/null | grep "not found" || true)
	if [[ -n "$MISSING" ]]; then
		sudo umount "$MNT"
		echo -e "sshd has missing libraries:\n$MISSING"
		exit 1
	fi
	sudo umount "$MNT"
	rmdir "$MNT"

	echo "App rootfs ready: $APP_ROOTFS"
fi

# The test framework derives the SSH key path as rootfs.with_suffix(".id_rsa").
# The app rootfs reuses the base image's authorized_keys, so copy its key.
# (Outside the build branch so --skip-rootfs still gets a key.)
APP_KEY="${APP_ROOTFS%.ext4}.id_rsa"
BASE_KEY="${BASE_ROOTFS%.ext4}.id_rsa"
if [[ ! -f "$APP_KEY" ]]; then
	if [[ ! -f "$BASE_KEY" ]]; then
		echo "Base SSH key not found: $BASE_KEY"
		exit 1
	fi
	cp "$BASE_KEY" "$APP_KEY"
fi

# Smoke test
if $NO_SMOKE; then
	echo "Skipping smoke test (--no-smoke-test)."
else
	echo "Running quick synthetic smoke test (all 3 modes, 512 MiB, no app rootfs needed)..."
	sudo ./tools/devtool -y test -- \
		-k "test_snapshot_experiment_quick" \
		integration_tests/functional/test_snapshot_live_experiment.py \
		-s --log-cli-level=INFO -m ""
	echo "Smoke test passed."
fi

echo ""
echo "Setup complete. App rootfs: $APP_ROOTFS"
echo ""
echo "Run a quick experiment (single workload, 1 iteration):"
echo ""
echo "  python3 tests/integration_tests/functional/run_snapshot_benchmark.py \\"
echo "    --workload redis_light \\"
echo "    --mem-sizes 2048 \\"
echo "    --iterations 1"
echo ""
echo "Run the full benchmark (all workloads, all mem sizes, 3 iterations each):"
echo ""
echo "  for wl in redis_light redis_mixed redis_heavy memcached_light memcached_heavy stream; do"
echo "    python3 tests/integration_tests/functional/run_snapshot_benchmark.py --workload \"\$wl\""
echo "  done"
