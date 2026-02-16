# Live Snapshot Code Review — Round 2

Follow-up review of the live snapshot feature after all P0–P3 improvements from
`docs/live-snapshot-review.md` have been implemented and verified.

Key files (unchanged from round 1):

- `src/vmm/src/persist.rs` — `create_live_snapshot()`, `LiveSnapshotGuard`,
  `save_page`, `uffd_write_protect`, `uffd_register_wp`, UFFD helper functions
- `src/vmm/src/lib.rs` — `resume_vm_inner()`, `kick_devices()`
- `src/vmm/src/vstate/memory.rs` — `populate_pages()`
- `src/vmm/src/rpc_interface.rs` — dispatch to `create_live_snapshot`
- `src/vmm/Cargo.toml` — userfaultfd dependency
- `tests/integration_tests/functional/test_snapshot_live.py` — integration tests
  and benchmarks
- `tests/host_tools/memory.py` — `MemoryMonitor` class

---

## 1. Replace raw UFFD ioctls with crate's `linux5_7` high-level API

**Priority:** P1 — code safety, maintainability

**Files:** `src/vmm/Cargo.toml:49`, `src/vmm/src/persist.rs:253–313`

The userfaultfd crate (0.9.0, already the pinned version) provides full
write-protect support behind its `linux5_7` feature flag, including:

- `RegisterMode::WRITE_PROTECT` — the `WP` registration mode
- `Uffd::write_protect(start, len)` — enable write-protection on a range
- `Uffd::remove_write_protection(start, len, wake)` — remove write-protection

Currently, the live snapshot code manually reimplements all of this:

```rust
// persist.rs:253–262 — Manual kernel struct definition
#[repr(C)]
struct UffdioWriteprotect {
    range_start: u64,
    range_len: u64,
    mode: u64,
}

// persist.rs:264–269 — Manual ioctl constant with platform-specific cast
#[allow(clippy::cast_possible_wrap)]
const UFFDIO_WRITEPROTECT: libc::Ioctl = 0xC018_AA06_u32 as libc::Ioctl;

// persist.rs:271–272 — Manual register mode constant
const UFFDIO_REGISTER_MODE_WP: u64 = 2;

// persist.rs:277–299 — Manual ioctl wrapper with unsafe block
fn uffd_write_protect(
    uffd_fd: libc::c_int,
    start: *mut libc::c_void,
    len: usize,
    protect: bool,
) -> Result<(), io::Error> {
    let mode: u64 = if protect { 1 } else { 0 };
    let mut wp = UffdioWriteprotect {
        range_start: start as u64,
        range_len: len as u64,
        mode,
    };
    // SAFETY: ...
    let ret = unsafe { libc::ioctl(uffd_fd, UFFDIO_WRITEPROTECT, &mut wp) };
    if ret < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

// persist.rs:302–313 — Registration using from_bits_retain() hack
fn uffd_register_wp(
    uffd: &Uffd,
    start: *mut libc::c_void,
    len: usize,
) -> Result<(), userfaultfd::Error> {
    use userfaultfd::RegisterMode;
    let mode = RegisterMode::MISSING
        | RegisterMode::from_bits_retain(UFFDIO_REGISTER_MODE_WP);
    uffd.register_with_mode(start, len, mode)?;
    Ok(())
}
```

This is ~60 lines of hand-rolled ioctl plumbing, including one `unsafe` block,
a `#[repr(C)]` struct that must match the kernel's layout, a platform-dependent
cast (`libc::Ioctl` is `c_int` on musl vs `c_ulong` on glibc), and a
`from_bits_retain()` hack to inject a mode bit the crate doesn't expose without
the feature flag.

**Fix:** Enable the `linux5_7` feature flag on the userfaultfd dependency and
replace all manual code with the crate's API:

```toml
# Cargo.toml
userfaultfd = { version = "0.9.0", features = ["linux5_7"] }
```

Registration becomes:

```rust
use userfaultfd::RegisterMode;

let mode = RegisterMode::MISSING | RegisterMode::WRITE_PROTECT;
uffd.register_with_mode(start, len, mode)?;
```

Write-protect enable/disable becomes:

```rust
// Enable WP:
uffd.write_protect(start, len)?;

// Remove WP (and wake blocked vCPUs):
uffd.remove_write_protection(start, len, true)?;
```

Delete entirely:

- `UffdioWriteprotect` struct (lines 253–262)
- `UFFDIO_WRITEPROTECT` constant (lines 264–269)
- `UFFDIO_REGISTER_MODE_WP` constant (lines 271–272)
- `uffd_write_protect()` function (lines 274–299)
- `uffd_register_wp()` function (lines 301–313)

Update all call sites (lines 222, 419, 502, 555) to use `uffd.write_protect()`
/ `uffd.remove_write_protection()` instead of `uffd_write_protect(uffd_fd, ...)`.

This also eliminates the need to extract `uffd_fd` via `as_raw_fd()` (line 410)
and thread the raw fd through the streaming loop, since the high-level methods
operate on `&Uffd` directly.

The `LiveSnapshotGuard::drop` implementation (line 222) will need adjustment
since it currently uses the raw fd. With the crate API, this becomes:

```rust
for region in &self.regions {
    if let Err(err) = uffd.remove_write_protection(region.ptr, region.len, false) {
        warn!("LiveSnapshotGuard: failed to remove write-protection ...");
    }
}
```

**Impact:** Removes ~60 lines of unsafe ioctl plumbing. Eliminates the only
`unsafe` block related to UFFD operations. Delegates the ioctl ABI to the
maintained crate, which handles the musl/glibc difference internally.

**Note:** The `linux5_7` feature flag is a _compile-time_ flag that enables the
WP API in the crate's type definitions. It does not add a runtime kernel version
check — the crate will still return errors at runtime if the kernel is too old.
Since Firecracker's minimum supported host kernel is 5.10 (which has WP
support), this is safe to enable unconditionally.

---

## 2. Fix memory monitor teardown errors in live snapshot tests

**Priority:** P1 — test reliability

**Files:** `tests/integration_tests/functional/test_snapshot_live.py`,
`tests/host_tools/memory.py:46`, `tests/framework/microvm.py:260`

Every live snapshot test run produces teardown errors like:

```
host_tools.memory.MemoryUsageExceededError:
    Memory usage (20.76 MiB) exceeded maximum threshold (5.0 MiB)
```

The `MemoryMonitor` (host_tools/memory.py:26) polls the Firecracker process's
RSS at 10 ms intervals, skipping mappings identified as guest memory by size.
The default threshold is 5 MiB (line 46: `threshold=5 << 20`), intended to catch
VMM memory leaks.

During a live snapshot, the VMM thread allocates transient data structures that
inflate process RSS well above 5 MiB:

| Structure | Per-entry | 4 GiB VM (1M pages) | 512 MiB VM (128K pages) |
|-----------|-----------|---------------------|-------------------------|
| `Vec<PageEntry>` (ptr + offset + size + saved) | ~33 bytes | ~33 MiB | ~4 MiB |
| `BTreeMap<usize, usize>` (B-tree nodes) | ~48 bytes | ~48 MiB | ~6 MiB |
| **Total transient overhead** | | **~81 MiB** | **~10 MiB** |

These allocations are freed when `create_live_snapshot()` returns, but
`MemoryMonitor` records the peak RSS and asserts on it during `kill()` in
teardown (microvm.py:394). This is the expected behavior for a feature that
temporarily needs large auxiliary data structures.

**Fix:** The established Firecracker pattern for tests with known elevated
memory usage is to pass `monitor_memory=False` when building the microVM.
This pattern is already used in 12 existing test sites:

```python
# test_snapshot_phase1.py:35
vm = microvm_factory.build(guest_kernel, rootfs, monitor_memory=False)

# test_pmem.py:173
guest_kernel_acpi, rootfs_rw, pci=True, monitor_memory=False

# performance/test_snapshot.py:52
monitor_memory=False,

# performance/test_memory_overhead.py:48
guest_kernel_acpi, rootfs, pci=pci_enabled, monitor_memory=False
```

Apply the same pattern to the live snapshot benchmark tests which use large VMs
(4 GiB):

```python
# test_snapshot_live.py — benchmark tests
@pytest.mark.nonci
@pytest.mark.parametrize("mem_size_mib", [4096])
@pytest.mark.timeout(600)
def test_live_vs_full_benchmark(uvm_plain, microvm_factory, mem_size_mib):
```

For the benchmark tests (`test_live_vs_full_benchmark`,
`test_live_snapshot_under_load_benchmark`, `test_live_snapshot_quick_benchmark`),
the `uvm_plain` fixture creates VMs with memory monitoring enabled by default.
The fix is to build these VMs explicitly with `monitor_memory=False` instead of
using the `uvm_plain` fixture, or to override the monitor after creation.

For the correctness tests (`test_live_snapshot_basic`,
`test_live_snapshot_memory_integrity`, `test_live_snapshot_under_load`), they use
`uvm_nano` (very small VMs) where the overhead is small enough that the 5 MiB
threshold might not be hit. If it is hit, the same fix applies.

Alternatively, if we want to keep memory monitoring active (to detect actual
leaks while tolerating expected transient usage), pass a higher threshold:

```python
vm.memory_monitor = MemoryMonitor(vm, threshold=100 << 20)  # 100 MiB
```

But `monitor_memory=False` is simpler and matches the existing convention for
snapshot-related tests.

---

## 3. Return a descriptive error when UFFD WP is not supported

**Priority:** P2 — user experience

**File:** `src/vmm/src/persist.rs:372–379`

If a user requests `snapshot_type: Live` on a kernel that does not support
userfaultfd write-protect (< Linux 5.7), the `UffdBuilder::create()` call
fails with a generic `userfaultfd::Error` because it cannot negotiate the
required `PAGEFAULT_FLAG_WP` feature:

```rust
// persist.rs:372–379
let uffd = UffdBuilder::new()
    .require_features(FeatureFlags::PAGEFAULT_FLAG_WP | FeatureFlags::EVENT_REMOVE)
    .close_on_exec(true)
    .non_blocking(true)
    .user_mode_only(false)
    .create()
    .map_err(CreateSnapshotError::UffdCreate)?;
```

The resulting API error is:

```json
{
  "fault_message": "Failed to create userfaultfd: <opaque nix error>"
}
```

This gives no indication that the problem is an unsupported kernel feature, nor
that the fix is to upgrade the host kernel.

Firecracker's supported host kernels are 5.10 and 6.1
(`tests/framework/defs.py:33`), both of which support UFFD WP. So in practice,
users on supported configurations will never hit this. But for users on
non-standard kernels (or future kernel changes), a clear error saves
significant debugging time.

**Fix:** Add a dedicated error variant and check:

```rust
/// Live snapshots require userfaultfd write-protect (Linux >= 5.7)
UffdWpNotSupported,
```

Then, before creating the UFFD, probe for WP support:

```rust
// Probe whether the kernel supports UFFD write-protect.
let probe_uffd = UffdBuilder::new()
    .close_on_exec(true)
    .create()
    .map_err(CreateSnapshotError::UffdCreate)?;

let api_features = probe_uffd.features();
if !api_features.contains(FeatureFlags::PAGEFAULT_FLAG_WP) {
    return Err(CreateSnapshotError::UffdWpNotSupported);
}
```

Or, simpler: catch the error from `create()` when `require_features` includes
`PAGEFAULT_FLAG_WP` and wrap it with a more descriptive message:

```rust
let uffd = UffdBuilder::new()
    .require_features(FeatureFlags::PAGEFAULT_FLAG_WP | FeatureFlags::EVENT_REMOVE)
    .close_on_exec(true)
    .non_blocking(true)
    .user_mode_only(false)
    .create()
    .map_err(|err| {
        CreateSnapshotError::UffdCreate(err)
        // The displaydoc message "Failed to create userfaultfd" is sufficient
        // if we also add a note in the doc comment:
    })?;
```

The more impactful fix is improving the `/// Failed to create userfaultfd: {0}`
doc comment (which becomes the user-facing error message via `displaydoc`) to:

```rust
/// Failed to create userfaultfd (live snapshots require Linux >= 5.7 with
/// write-protect support): {0}
UffdCreate(userfaultfd::Error),
```

This approach requires no runtime behavior change — just a better error string.

---

## 4. Add unit tests for live snapshot helpers

**Priority:** P2 — test coverage

**File:** `src/vmm/src/persist.rs:1052–1239`

The `#[cfg(test)]` module in persist.rs (line 1052) contains three tests:

1. `test_microvm_state_snapshot` — round-trip serialize/deserialize of
   `MicrovmState` (lines 1133–1176)
2. `test_create_guest_memory` — basic `create_guest_memory()` (lines 1178–1196)
3. `test_send_uffd_handshake` — UDS message format (lines 1198–1238)

None of these test any live snapshot code. The live snapshot is exclusively
tested via Python integration tests, which require a full VM boot cycle and
take 5–10 seconds each. Several components are pure functions that can be unit
tested in isolation:

### 4a. `save_page` — file I/O correctness

```rust
fn save_page(file: &File, page: &PageEntry) -> Result<(), CreateSnapshotError>
```

Test: create a tempfile, construct a `PageEntry` pointing at a known byte
buffer, call `save_page`, read back the file and verify contents at the correct
offset.

### 4b. BTreeMap page index construction and lookup

The page index construction (lines 477–481) and the fault lookup pattern
(lines 490–504) are pure logic operating on `Vec<PageEntry>` and
`BTreeMap<usize, usize>`. Test: construct a synthetic page list with known
addresses, build the index, and verify that lookups for various fault addresses
(start of page, middle of page, between pages, before first page, after last
page) return the correct page index or no match.

### 4c. Batch contiguity check

The batching logic (lines 529–558) checks that consecutive pages are contiguous
in host virtual address space before coalescing WP removal. Test: construct page
lists with gaps and verify the batch boundaries are correct.

### 4d. `LiveSnapshotGuard` drop semantics

The guard's drop implementation (lines 216–251) must:

- Remove write-protection for all registered regions
- Unregister all regions from UFFD
- Resume vCPUs if `self.paused` is true
- Kick devices if `self.devices_kicked` is false

This can be tested by creating a guard with a mock VMM and UFFD, setting various
states (`paused = true/false`, `devices_kicked = true/false`), dropping it, and
verifying the expected cleanup actions occurred. This requires factoring the
VMM dependency behind a trait or using the existing `default_vmm()` test helper,
which may be complex. At minimum, the "does it compile and not panic" test is
valuable:

```rust
#[test]
fn test_live_snapshot_guard_drop_no_uffd() {
    let mut vmm = default_vmm();
    let guard = LiveSnapshotGuard::new(&mut vmm);
    drop(guard); // Should not panic — no uffd, no regions, not paused.
}
```

### 4e. UFFD write-protect round-trip (requires real kernel support)

If the test host supports UFFD WP (Linux >= 5.7), a test can:

1. `mmap` an anonymous region
2. Create a UFFD, register with WP mode
3. Enable write-protection via `uffd_write_protect`
4. Verify that a write to the region generates a WP fault event
5. Remove write-protection and verify the write succeeds

This is closer to an integration test but runs in milliseconds without a VM.
Mark it with `#[cfg_attr(not(target_os = "linux"), ignore)]` for portability.

---

## 5. Fix doc comment typo in `GuestMemoryFromUffdError`

**Priority:** P3 — style compliance

**File:** `src/vmm/src/persist.rs:932`

The second variant of `GuestMemoryFromUffdError` has a truncated doc comment:

```rust
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum GuestMemoryFromUffdError {
    /// Failed to restore guest memory: {0}
    Restore(#[from] MemoryError),
    /// Failed to UFFD object: {0}          // <-- missing verb
    Create(userfaultfd::Error),
    /// Failed to register memory address range with the userfaultfd object: {0}
    Register(userfaultfd::Error),
    /// Failed to connect to UDS Unix stream: {0}
    Connect(#[from] std::io::Error),
    /// Failed to sends file descriptor: {0} // <-- "sends" should be "send"
    Send(#[from] vmm_sys_util::errno::Error),
}
```

Two issues:

1. Line 932: `"Failed to UFFD object"` — missing verb, should be
   `"Failed to create UFFD object"`
2. Line 938: `"Failed to sends file descriptor"` — should be
   `"Failed to send file descriptor"`

These doc comments are user-facing error messages (via `displaydoc`).

**Fix:**

```rust
/// Failed to create UFFD object: {0}
Create(userfaultfd::Error),
// ...
/// Failed to send file descriptor: {0}
Send(#[from] vmm_sys_util::errno::Error),
```

Note: this enum is for snapshot _restore_ from UFFD, not for live snapshot
creation, so it predates our changes. But since we're reviewing the UFFD code
path, it's worth fixing.

---

## 6. Use `madvise(MADV_POPULATE_READ)` for faster PTE population

**Priority:** P2 — performance

**File:** `src/vmm/src/vstate/memory.rs:769–784`

Phase 1 of the live snapshot calls `populate_pages()` to ensure all guest PTEs
are present before enabling write-protection (UFFDIO_WRITEPROTECT silently skips
pages without PTEs). The current implementation reads one byte per page in a
loop:

```rust
// memory.rs:769–784
fn populate_pages(&self, page_size: usize) {
    for region in self.iter() {
        for slot in region.plugged_slots() {
            let ptr = slot.slice.ptr_guard().as_ptr();
            let len = slot.slice.len();
            for offset in (0..len).step_by(page_size) {
                // SAFETY: ptr+offset is within the mapped guest memory region
                unsafe {
                    std::ptr::read_volatile(ptr.add(offset));
                }
            }
        }
    }
}
```

For a 4 GiB VM with 4 KiB pages, this performs 1,048,576 volatile reads. Each
read faults in one page, requiring a kernel entry, page table walk, page
allocation, and TLB fill. The 4 GiB benchmark shows Phase 1 taking **323 ms
idle** and **1,201 ms under memory-write load** (where guest writes cause TLB
shootdowns and page table contention).

Linux 5.14 introduced `madvise(MADV_POPULATE_READ)` ([commit 4ca9b385](https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git/commit/?id=4ca9b3859dac)), which populates all PTEs for a given
address range in a single syscall, with the kernel performing the page walk
internally in a tight loop without repeated user-kernel transitions.

Firecracker's supported host kernels:

- **5.10** — does NOT have `MADV_POPULATE_READ` (added in 5.14)
- **6.1** — has `MADV_POPULATE_READ`

**Fix:** Add a runtime check with fallback:

```rust
fn populate_pages(&self, page_size: usize) {
    for region in self.iter() {
        for slot in region.plugged_slots() {
            let ptr = slot.slice.ptr_guard().as_ptr();
            let len = slot.slice.len();

            // Try madvise(MADV_POPULATE_READ) first (Linux 5.14+).
            // Falls back to per-page volatile reads on older kernels.
            let ret = unsafe {
                libc::madvise(ptr as *mut libc::c_void, len, libc::MADV_POPULATE_READ)
            };
            if ret == 0 {
                continue; // All PTEs populated in one syscall.
            }

            // Fallback: touch each page individually.
            for offset in (0..len).step_by(page_size) {
                // SAFETY: ptr+offset is within the mapped guest memory region
                unsafe {
                    std::ptr::read_volatile(ptr.add(offset));
                }
            }
        }
    }
}
```

Note: `MADV_POPULATE_READ` (value 22) is defined in `libc` crate 0.2.132+.
Firecracker pins libc 0.2.180 (Cargo.toml:37), so the constant is available.

The `madvise` call returns `-1` with `errno = EINVAL` on kernels that don't
recognize the advice value, so the fallback is safe and automatic.

**Expected impact:** On 6.1 hosts, Phase 1 should drop from 323 ms to
~10–50 ms for a 4 GiB idle VM (based on published benchmarks of
`MADV_POPULATE_READ` showing ~10 GB/s population throughput on modern hardware).
Under memory-write load, the improvement may be larger since the kernel can
batch-populate without contention from the per-page user-kernel transitions.

**Seccomp note:** `madvise` (syscall 28 on x86_64) is already in the seccomp
allowlists for both architectures, used by the memory allocator. No filter
changes needed.

---

## Summary

| # | Priority | Issue | Effort | Impact |
|---|----------|-------|--------|--------|
| 1 | **P1** | Replace raw UFFD ioctls with crate's `linux5_7` API | Small | Removes ~60 lines of unsafe ioctl plumbing |
| 2 | **P1** | Fix memory monitor teardown errors in tests | Trivial | Eliminates false-positive test errors |
| 3 | **P2** | Descriptive error when UFFD WP not supported | Trivial | Better UX on unsupported kernels |
| 4 | **P2** | Add unit tests for live snapshot helpers | Medium | Test coverage for untested code |
| 5 | **P3** | Fix doc comment typos in `GuestMemoryFromUffdError` | Trivial | Corrects user-facing error messages |
| 6 | **P2** | Use `madvise(MADV_POPULATE_READ)` in `populate_pages` | Small | ~5–10x faster Phase 1 on 6.1 hosts |
