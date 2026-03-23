// Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! bpf_fault-based live snapshot implementation.
//!
//! Uses the `bpf_fault` kernel interface (Linux >= 6.12, `CONFIG_BPF_FAULT=y`)
//! to write-protect guest memory and capture pre-images of dirty pages via an
//! in-kernel BPF ring buffer. Unlike the userfaultfd path, vCPUs are **never
//! blocked** on a write fault — the BPF handler copies the pre-image and allows
//! the write atomically.
//!
//! # BPF Loading
//!
//! The compiled BPF ELF (`snapshot_fault_ops.bpf.o`) is embedded at build time
//! and loaded via `libbpf-rs`, which handles ELF parsing, kernel BTF lookup,
//! program loading, map creation (ring buffer + struct_ops), and relocation.
//! Only the fault_ops-specific link management (attach, WP enable/resolve)
//! uses raw `bpf()` syscalls, since libbpf has no built-in fault_ops API.

use std::fs::OpenOptions;
use std::os::fd::AsFd;
use std::os::unix::fs::FileExt;
use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicU8, AtomicUsize, Ordering};
use std::sync::Arc;

use libbpf_rs::MapCore;
use vm_memory::GuestMemory;

use crate::Vmm;
use crate::logger::{info, warn};
use crate::persist::{CreateSnapshotError, VmInfo, snapshot_state_to_file};
use crate::vmm_config::snapshot::CreateSnapshotParams;
use crate::vstate::memory::GuestMemoryExtension;

/// Page tracking entry for the bpf_fault live snapshot streaming phase.
struct PageEntry {
    /// Host pointer to the start of this page.
    ptr: *const u8,
    /// File offset where this page should be written.
    file_offset: u64,
    /// Size of this page.
    size: usize,
    /// Index into the links vec for the bpf_fault link owning this page.
    link_index: usize,
}

// SAFETY: PageEntry contains a raw pointer to guest memory that is valid and
// immovable for the lifetime of the snapshot operation. The pointer is only
// used for reads (never written through).
unsafe impl Send for PageEntry {}
unsafe impl Sync for PageEntry {}

/// Per-page atomic states for coordinating the linear scan and ring buffer
/// drain threads.  The CAS protocol ensures that for any page with a ring
/// buffer event, the pre-image is the final write to the snapshot file.
mod page_state {
    use std::sync::atomic::{AtomicU8, Ordering};

    /// Page has not been written to the snapshot file.
    pub const UNSAVED: u8 = 0;
    /// Linear scan is currently pwriting this page (transient).
    pub const SCAN_WRITING: u8 = 1;
    /// Linear scan completed pwriting this page.
    pub const SCAN_DONE: u8 = 2;
    /// Ring buffer drain thread owns this page (pre-image written or pending).
    pub const RING_SAVED: u8 = 3;

    /// Attempts to claim `page` for the linear scan.
    ///
    /// Returns `true` if the CAS UNSAVED→SCAN_WRITING succeeded (caller must
    /// pwrite and then call [`mark_scan_done`]).  Returns `false` if the ring
    /// buffer thread already claimed the page.
    #[inline(always)]
    pub fn try_claim_for_scan(state: &AtomicU8) -> bool {
        state
            .compare_exchange(UNSAVED, SCAN_WRITING, Ordering::Acquire, Ordering::Relaxed)
            .is_ok()
    }

    /// Marks a page as SCAN_DONE after the linear scan pwrite completes.
    #[inline(always)]
    pub fn mark_scan_done(state: &AtomicU8) {
        state.store(SCAN_DONE, Ordering::Release);
    }

    /// Ring buffer thread: claim page and ensure the pre-image pwrite is safe.
    ///
    /// Spins briefly if the page is in SCAN_WRITING (linear scan mid-pwrite),
    /// ensuring the pre-image pwrite happens strictly after the linear scan's
    /// pwrite.  Returns `true` if this is the first save (caller should
    /// increment saved_count).
    #[inline]
    pub fn claim_for_ring(state: &AtomicU8) -> bool {
        loop {
            let prev = state.swap(RING_SAVED, Ordering::AcqRel);
            match prev {
                UNSAVED => return true,
                SCAN_WRITING => {
                    // Linear scan is mid-pwrite — restore SCAN_WRITING and
                    // spin until it transitions to SCAN_DONE, then overwrite.
                    state.store(SCAN_WRITING, Ordering::Release);
                    while state.load(Ordering::Acquire) == SCAN_WRITING {
                        std::hint::spin_loop();
                    }
                    // Now it's SCAN_DONE (or another ring event raced in).
                    // Loop back to swap again.
                }
                SCAN_DONE | RING_SAVED => return false,
                _ => unreachable!(),
            }
        }
    }
}

// ── BPF constants (fault_ops-specific, not handled by libbpf) ───────────────

/// BPF commands for fault_ops link management.
const BPF_LINK_CREATE: u32 = 28;
const BPF_LINK_FAULT_OPS_CMD: u32 = 38;

/// BPF attach type for fault_ops.
const BPF_FAULT_OPS: u32 = 58;

/// bpf_fault WP enable flag.
const BPF_FAULT_WP_ENABLE: u32 = 1;
/// bpf_fault WP flag for link creation.
const BPF_FAULT_FLAG_WP: u32 = 1;

/// Minimum ring buffer size (16 MiB).
const MIN_RING_BUF_SIZE: usize = 16 * 1024 * 1024;
/// Maximum ring buffer size (256 MiB).
const MAX_RING_BUF_SIZE: usize = 256 * 1024 * 1024;

/// Size of a page event record in the ring buffer (8-byte address + 4096-byte data).
const PAGE_EVENT_SIZE: usize = 4104;

/// The compiled BPF ELF object, embedded at build time.
const BPF_PROG_ELF: &[u8] =
    include_bytes!("../../../resources/bpf/snapshot_fault_ops.bpf.o");

// ── Raw syscall wrapper ─────────────────────────────────────────────────────

/// Calls the `bpf()` syscall.
///
/// # Safety
/// `attr` must point to a validly initialised `bpf_attr` union of at least
/// `attr_size` bytes.
unsafe fn sys_bpf(cmd: u32, attr: *mut u8, attr_size: u32) -> Result<i64, String> {
    // SAFETY: Caller guarantees the attr pointer and size are valid.
    let ret = unsafe { libc::syscall(libc::SYS_bpf, cmd, attr, attr_size) };
    if ret < 0 {
        Err(format!(
            "bpf(cmd={}) failed: {}",
            cmd,
            std::io::Error::last_os_error()
        ))
    } else {
        Ok(ret)
    }
}

// ── BPF fault link wrapper ──────────────────────────────────────────────────

/// Wraps a raw bpf_fault link file descriptor.
struct BpfFaultLink {
    fd: OwnedFd,
}

impl BpfFaultLink {
    /// Creates a new link from a raw fd.
    ///
    /// # Safety
    /// `raw_fd` must be a valid, open bpf_fault link fd.
    unsafe fn from_raw_fd(raw_fd: RawFd) -> Self {
        Self {
            // SAFETY: Caller guarantees the fd is valid.
            fd: unsafe { OwnedFd::from_raw_fd(raw_fd) },
        }
    }

    /// Returns the raw fd.
    fn as_raw_fd(&self) -> RawFd {
        self.fd.as_raw_fd()
    }
}

// ── RAII guard ──────────────────────────────────────────────────────────────

/// RAII guard for bpf_fault live snapshot.
struct BpfLiveSnapshotGuard<'a> {
    vmm: &'a mut Vmm,
    links: Vec<BpfFaultLink>,
    paused: bool,
    devices_kicked: bool,
}

impl<'a> BpfLiveSnapshotGuard<'a> {
    fn new(vmm: &'a mut Vmm) -> Self {
        Self {
            vmm,
            links: Vec::new(),
            paused: false,
            devices_kicked: false,
        }
    }
}

impl Drop for BpfLiveSnapshotGuard<'_> {
    fn drop(&mut self) {
        self.links.clear();
        if self.paused
            && let Err(err) = self.vmm.resume_vcpus_only()
        {
            warn!("BpfLiveSnapshotGuard: failed to resume vCPUs: {}", err);
        }
        if !self.devices_kicked {
            self.vmm.kick_devices();
        }
    }
}

// ── BPF object loading (libbpf-rs) ─────────────────────────────────────────

/// Loaded BPF object holding the struct_ops map and ring buffer.
struct BpfFaultObject {
    /// The loaded libbpf object (owns all map/program fds).
    _obj: libbpf_rs::Object,
    /// struct_ops map fd (borrowed from `_obj`, used for link creation).
    struct_ops_map_fd: RawFd,
    /// Ring buffer map fd (borrowed from `_obj`, used for consuming pre-images).
    ring_buf_fd: RawFd,
}

/// Loads the BPF fault_ops program using libbpf-rs.
///
/// This replaces ~500 lines of hand-rolled ELF parsing, BTF parsing,
/// raw BPF syscalls for map/prog/BTF creation, relocation handling,
/// and struct_ops map setup.
fn bpf_load_object(elf_bytes: &[u8], ring_buf_size: usize) -> Result<BpfFaultObject, String> {
    let mut open_obj = libbpf_rs::ObjectBuilder::default()
        .open_memory(elf_bytes)
        .map_err(|e| format!("Failed to open BPF object: {e}"))?;

    // Set ring buffer max_entries before loading.
    let mut found_ring_buf = false;
    for mut map in open_obj.maps_mut() {
        if map.name().to_string_lossy() == "page_events" {
            map.set_max_entries(
                u32::try_from(ring_buf_size)
                    .map_err(|_| format!("ring_buf_size {ring_buf_size} exceeds u32::MAX"))?,
            )
            .map_err(|e| format!("Failed to set page_events max_entries: {e}"))?;
            found_ring_buf = true;
            break;
        }
    }
    if !found_ring_buf {
        return Err("page_events map not found in BPF ELF".to_string());
    }

    info!("Live-BPF: loading BPF object via libbpf-rs");
    let obj = open_obj
        .load()
        .map_err(|e| format!("Failed to load BPF object: {e}"))?;

    // Find struct_ops map (named "snapshot_fault_ops" from the BPF source).
    let struct_ops_map = obj
        .maps()
        .find(|m| m.name().to_string_lossy() == "snapshot_fault_ops")
        .ok_or_else(|| {
            let names: Vec<_> = obj
                .maps()
                .map(|m| m.name().to_string_lossy().into_owned())
                .collect();
            format!("struct_ops map 'snapshot_fault_ops' not found; available maps: {names:?}")
        })?;
    let struct_ops_map_fd = struct_ops_map.as_fd().as_raw_fd();
    let value_size = struct_ops_map.value_size() as usize;

    // Populate the struct_ops map to transition its state from INIT to READY.
    //
    // libbpf's open_and_load() creates the struct_ops map and loads programs,
    // but does NOT call bpf_map_update_elem — that only happens during
    // attach_struct_ops() / attach_fault_ops().  Since our vendored libbpf
    // (1.6.3) lacks attach_fault_ops(), we must do the update ourselves.
    //
    // The map value layout (struct bpf_struct_ops_fault_ops_value):
    //   [0..8]        bpf_struct_ops_common_value { refcnt, state } — must be zero
    //   [8..DATA_OFF] padding (zeros, due to ____cacheline_aligned_in_smp)
    //   [DATA_OFF..]  struct fault_ops { handle_page_fault, handle_wp_fault }
    //
    // DATA_OFF = SMP_CACHE_BYTES (64 on x86_64, see register_bpf_struct_ops macro).
    // Each function pointer slot holds a program fd as u64.
    let page_fault_fd = obj
        .progs()
        .find(|p| p.name() == "handle_page_fault")
        .ok_or("BPF program 'handle_page_fault' not found")?
        .as_fd()
        .as_raw_fd();
    let wp_fault_fd = obj
        .progs()
        .find(|p| p.name() == "handle_wp_fault")
        .ok_or("BPF program 'handle_wp_fault' not found")?
        .as_fd()
        .as_raw_fd();

    // Derive the data offset from the value size.
    //
    // The kernel's `register_bpf_struct_ops` macro aligns the `data` field
    // to `SMP_CACHE_BYTES` via `____cacheline_aligned_in_smp`.  The layout:
    //
    //   struct bpf_struct_ops_fault_ops_value {
    //       bpf_struct_ops_common_value common;   // 8 bytes (1st cache line)
    //       struct fault_ops data                  // 16 bytes (2nd cache line)
    //           ____cacheline_aligned_in_smp;
    //   };
    //
    // Total size = 2 × SMP_CACHE_BYTES (common in 1st line, data+padding in
    // 2nd), so data_offset = value_size / 2.
    //
    // Known layouts:
    //   L1_CACHE_SHIFT=6 (most x86_64/aarch64): data_off=64,  value_size=128
    //   L1_CACHE_SHIFT=7 (some aarch64 SoCs):   data_off=128, value_size=256
    if value_size < 64 || !value_size.is_power_of_two() {
        return Err(format!(
            "struct_ops map value_size ({value_size}) unexpected; \
             expected a power of two >= 64"
        ));
    }
    let data_offset = value_size / 2;

    if page_fault_fd < 0 || wp_fault_fd < 0 {
        return Err(format!(
            "BPF program fds are invalid: page_fault_fd={page_fault_fd}, \
             wp_fault_fd={wp_fault_fd}"
        ));
    }

    let mut value = vec![0u8; value_size];
    value[data_offset..data_offset + 8]
        .copy_from_slice(&(page_fault_fd as u64).to_ne_bytes());
    value[data_offset + 8..data_offset + 16]
        .copy_from_slice(&(wp_fault_fd as u64).to_ne_bytes());

    let key = 0u32.to_ne_bytes();
    info!(
        "Live-BPF: populating struct_ops map (value_size={}, data_off={}, \
         page_fault_fd={}, wp_fault_fd={})",
        value_size, data_offset, page_fault_fd, wp_fault_fd
    );
    struct_ops_map
        .update(&key, &value, libbpf_rs::MapFlags::ANY)
        .map_err(|e| format!("Failed to populate struct_ops map: {e}"))?;

    info!("Live-BPF: struct_ops map populated (state → READY)");

    // Find ring buffer map fd.
    let ring_buf_fd = obj
        .maps()
        .find(|m| m.name().to_string_lossy() == "page_events")
        .ok_or("page_events map not found in loaded BPF object")?
        .as_fd()
        .as_raw_fd();

    info!(
        "Live-BPF: loaded — struct_ops_map_fd={}, ring_buf_fd={}",
        struct_ops_map_fd, ring_buf_fd
    );

    Ok(BpfFaultObject {
        _obj: obj,
        struct_ops_map_fd,
        ring_buf_fd,
    })
}

// ── Link creation and WP management ────────────────────────────────────────

/// Creates a bpf_fault link for a memory region.
fn bpf_create_fault_link(
    struct_ops_map_fd: RawFd,
    addr: u64,
    len: u64,
    flags: u32,
) -> Result<RawFd, String> {
    // BPF_LINK_CREATE attr for fault_ops:
    //   u32 prog_fd            (offset 0) — actually struct_ops map fd
    //   u32 target_fd          (offset 4) = 0 (unused for fault_ops)
    //   u32 attach_type        (offset 8) = BPF_FAULT_OPS
    //   u32 flags              (offset 12) = 0
    //   -- fault_ops specific (in the union at offset 16):
    //   u64 fault.start        (offset 16)
    //   u64 fault.len          (offset 24)
    //   u32 fault.flags        (offset 32)
    let mut attr = [0u8; 64];
    attr[0..4].copy_from_slice(&struct_ops_map_fd.cast_unsigned().to_ne_bytes());
    // target_fd = 0
    attr[8..12].copy_from_slice(&BPF_FAULT_OPS.to_ne_bytes());
    // link flags = 0
    attr[16..24].copy_from_slice(&addr.to_ne_bytes());
    attr[24..32].copy_from_slice(&len.to_ne_bytes());
    attr[32..36].copy_from_slice(&flags.to_ne_bytes());

    // SAFETY: attr is a valid BPF_LINK_CREATE attribute.
    let link_fd = unsafe {
        sys_bpf(BPF_LINK_CREATE, attr.as_mut_ptr(), 64).map_err(|e| {
            format!(
                "BPF_LINK_CREATE(fault_ops) failed: {e} \
                 (map_fd={struct_ops_map_fd}, addr=0x{addr:x}, len=0x{len:x})"
            )
        })?
    };
    Ok(link_fd as RawFd)
}

/// Enables write-protection on a memory range via an existing bpf_fault link.
fn bpf_fault_wp_enable(link_fd: RawFd, addr: u64, len: u64) -> Result<(), String> {
    let mut attr = [0u8; 32];
    attr[0..4].copy_from_slice(&link_fd.cast_unsigned().to_ne_bytes());
    attr[4..8].copy_from_slice(&BPF_FAULT_WP_ENABLE.to_ne_bytes());
    attr[8..16].copy_from_slice(&addr.to_ne_bytes());
    attr[16..24].copy_from_slice(&len.to_ne_bytes());

    // SAFETY: attr is a valid BPF_LINK_FAULT_OPS_CMD attribute.
    unsafe { sys_bpf(BPF_LINK_FAULT_OPS_CMD, attr.as_mut_ptr(), 32)? };
    Ok(())
}

/// Resolves write-protection on a memory range via bpf_fault.
fn bpf_fault_wp_resolve(link_fd: RawFd, addr: u64, len: u64) -> Result<(), String> {
    let mut attr = [0u8; 32];
    attr[0..4].copy_from_slice(&link_fd.cast_unsigned().to_ne_bytes());
    // flags = 0 means resolve (remove WP)
    attr[8..16].copy_from_slice(&addr.to_ne_bytes());
    attr[16..24].copy_from_slice(&len.to_ne_bytes());

    // SAFETY: attr is a valid BPF_LINK_FAULT_OPS_CMD attribute.
    unsafe { sys_bpf(BPF_LINK_FAULT_OPS_CMD, attr.as_mut_ptr(), 32)? };
    Ok(())
}

// ── Ring buffer consumer ────────────────────────────────────────────────────

/// A consumer for a BPF ring buffer map, using mmap.
struct RingBufConsumer {
    /// Pointer to the mmap'd ring buffer data area.
    data_ptr: *const u8,
    /// Pointer to the consumer position (in the control page).
    consumer_pos: *mut u64,
    /// Pointer to the producer position (in the control page).
    producer_pos: *const u64,
    /// Size of the data area (power of two).
    data_size: usize,
    /// Total mmap size (for munmap).
    mmap_size: usize,
    /// Base mmap pointer (for munmap).
    mmap_base: *mut u8,
}

// SAFETY: `RingBufConsumer` is only used within `create_live_bpf_snapshot`,
// which runs on the VMM thread (single-threaded access). The mmap'd memory
// regions are valid for the lifetime of the ring buffer fd held by
// `BpfFaultObject._obj`, which outlives the consumer.
unsafe impl Send for RingBufConsumer {}

impl RingBufConsumer {
    /// Creates a new ring buffer consumer by mmap'ing the ring buffer map fd.
    fn new(ring_buf_fd: RawFd, ring_buf_size: usize) -> Result<Self, String> {
        let page_size = 4096usize;

        // mmap consumer page (read-write).
        // SAFETY: We're mapping the ring buffer fd at the correct offset.
        let consumer_page = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                page_size,
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                ring_buf_fd,
                0,
            )
        };
        if consumer_page == libc::MAP_FAILED {
            return Err(format!(
                "Failed to mmap ring buffer consumer page: {}",
                std::io::Error::last_os_error()
            ));
        }

        // mmap producer page + data area (read-only).
        let data_mmap_size = page_size + 2 * ring_buf_size;
        // SAFETY: We're mapping the ring buffer fd at the correct offset.
        let producer_page = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                data_mmap_size,
                libc::PROT_READ,
                libc::MAP_SHARED,
                ring_buf_fd,
                page_size as libc::off_t,
            )
        };
        if producer_page == libc::MAP_FAILED {
            // SAFETY: consumer_page is a valid mmap region.
            unsafe {
                libc::munmap(consumer_page, page_size);
            }
            return Err(format!(
                "Failed to mmap ring buffer producer/data area: {}",
                std::io::Error::last_os_error()
            ));
        }

        Ok(Self {
            // SAFETY: producer_page + page_size is the start of the data area.
            data_ptr: unsafe { (producer_page as *const u8).add(page_size) },
            consumer_pos: consumer_page.cast::<u64>(),
            producer_pos: producer_page as *const u64,
            data_size: ring_buf_size,
            mmap_size: data_mmap_size,
            mmap_base: producer_page.cast::<u8>(),
        })
    }

    /// Processes all available ring buffer records in-place without copying.
    ///
    /// Calls `callback(addr, data_slice)` for each valid page event, where
    /// `data_slice` is a 4096-byte reference directly into the mmap'd ring
    /// buffer — no heap allocation per event.
    ///
    /// Returns `Err` if `callback` returns `Err` for any event, after first
    /// advancing the consumer position past all records examined so far.
    fn for_each<F>(&mut self, mut callback: F) -> Result<(), std::io::Error>
    where
        F: FnMut(u64, &[u8]) -> Result<(), std::io::Error>,
    {
        // SAFETY: consumer_pos and producer_pos point to valid positions.
        let cons = unsafe { std::ptr::read_volatile(self.consumer_pos) };
        let prod = unsafe { std::ptr::read_volatile(self.producer_pos) };

        if cons >= prod {
            return Ok(());
        }

        // Acquire fence: ensures all ring buffer data writes made by the
        // kernel (producer) before updating producer_pos are visible to us.
        // Required on weakly-ordered architectures (aarch64); a no-op on
        // x86_64 TSO.
        std::sync::atomic::fence(std::sync::atomic::Ordering::Acquire);

        let mask = (self.data_size - 1) as u64;
        let mut pos = cons;
        let mut result = Ok(());

        while pos < prod {
            let offset = (pos & mask) as usize;
            // SAFETY: data_ptr + offset points into the mmap'd data area.
            let hdr = unsafe {
                std::ptr::read_volatile(self.data_ptr.add(offset).cast::<u32>())
            };

            let len = hdr & 0x0FFF_FFFF;
            let is_busy = (hdr >> 31) & 1 != 0;
            let is_discard = (hdr >> 30) & 1 != 0;

            if is_busy {
                break;
            }

            let data_len = len as usize;
            let padded_len = (data_len + 7) & !7;
            let record_size = 8 + padded_len;

            if !is_discard && data_len >= PAGE_EVENT_SIZE {
                let data_offset = ((pos + 8) & mask) as usize;
                // SAFETY: data_ptr + data_offset is within the mmap'd region.
                let addr = unsafe {
                    std::ptr::read_volatile(self.data_ptr.add(data_offset).cast::<u64>())
                };
                let page_data_offset = data_offset + 8;
                // SAFETY: The ring buffer data area is mmap'd with 2× size
                // for wrap-around, so data_ptr[page_data_offset..+4096] is
                // always valid.
                let data_slice = unsafe {
                    std::slice::from_raw_parts(self.data_ptr.add(page_data_offset), 4096)
                };
                if let Err(err) = callback(addr, data_slice) {
                    // Advance consumer past this record before returning,
                    // so a retry won't re-process already-seen events.
                    pos += record_size as u64;
                    result = Err(err);
                    break;
                }
            }

            pos += record_size as u64;
        }

        // Update consumer position.
        // SAFETY: consumer_pos is valid and we're the sole consumer.
        unsafe {
            std::ptr::write_volatile(self.consumer_pos, pos);
        }
        std::sync::atomic::fence(std::sync::atomic::Ordering::Release);

        result
    }
}

impl Drop for RingBufConsumer {
    fn drop(&mut self) {
        let page_size = 4096usize;
        // SAFETY: mmap_base and consumer_pos were obtained from valid mmap calls.
        unsafe {
            libc::munmap(self.mmap_base.cast::<libc::c_void>(), self.mmap_size);
            libc::munmap(self.consumer_pos.cast::<libc::c_void>(), page_size);
        }
    }
}

// ── BPF drop counter ────────────────────────────────────────────────────────

/// Reads the total drop count from the BPF per-CPU drop_counter map.
///
/// Returns 0 if the map is not found or on any error.
fn read_bpf_drop_counter(bpf_obj: &BpfFaultObject) -> u64 {
    let map = match bpf_obj
        ._obj
        .maps()
        .find(|m| m.name().to_string_lossy() == "drop_counter")
    {
        Some(m) => m,
        None => return 0,
    };
    let key = 0u32.to_ne_bytes();
    match map.lookup_percpu(&key, libbpf_rs::MapFlags::ANY) {
        Ok(Some(per_cpu_values)) => {
            let mut total = 0u64;
            for val in &per_cpu_values {
                if val.len() >= 8 {
                    total += u64::from_ne_bytes(val[..8].try_into().unwrap_or([0; 8]));
                }
            }
            total
        }
        _ => 0,
    }
}

// ── Compute ring buffer size ────────────────────────────────────────────────

/// Computes ring buffer size: min(guest_mem / 4, 256MB), clamped and power of two.
fn compute_ring_buf_size(total_mem_size: u64) -> usize {
    let target = (total_mem_size as usize) / 4;
    let clamped = target.clamp(MIN_RING_BUF_SIZE, MAX_RING_BUF_SIZE);
    clamped.next_power_of_two().min(MAX_RING_BUF_SIZE)
}

// ── Main entry point ────────────────────────────────────────────────────────

/// Creates a live snapshot using the bpf_fault kernel interface.
///
/// The VM is paused only briefly to save device/vCPU state and attach bpf_fault
/// write-protection. Memory is then streamed to the output file while the VM
/// continues running. Unlike the userfaultfd path, vCPUs are **never blocked**
/// on a write fault.
pub fn create_live_bpf_snapshot(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<(), CreateSnapshotError> {
    // The BPF program hardcodes 4096-byte page pre-images; huge pages are not
    // supported.
    if vm_info.huge_pages.is_hugetlbfs() {
        return Err(CreateSnapshotError::BpfLoad(
            "LiveBpf snapshots do not support huge pages; the BPF program \
             captures 4 KiB pre-images only"
                .to_string(),
        ));
    }
    let page_size = vm_info.huge_pages.page_size();
    let t_start = std::time::Instant::now();

    // === Phase 1: PREPARE (VM still running) ===
    info!("Live-BPF snapshot: Phase 1 - Prepare");

    let t_populate_start = std::time::Instant::now();
    vmm.vm.guest_memory().populate_pages(page_size);
    info!(
        "Live-BPF snapshot: populate_pages took {} us",
        t_populate_start.elapsed().as_micros()
    );

    let total_mem_size: u64 = vmm
        .vm
        .guest_memory()
        .iter()
        .flat_map(|region| region.slots())
        .map(|(slot, _)| slot.slice.len() as u64)
        .sum();

    let ring_buf_size = compute_ring_buf_size(total_mem_size);
    info!(
        "Live-BPF snapshot: loading BPF object, ring_buf_size={} MiB",
        ring_buf_size / (1024 * 1024)
    );
    let bpf_obj =
        bpf_load_object(BPF_PROG_ELF, ring_buf_size).map_err(CreateSnapshotError::BpfLoad)?;

    let mem_file = OpenOptions::new()
        .create(true)
        .write(true)
        .read(true)
        .truncate(true)
        .open(&params.mem_file_path)
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("open", err))?;
    mem_file
        .set_len(total_mem_size)
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("set_len", err))?;

    // Pre-allocate the snapshot file's backing pages.  On tmpfs (the common
    // case), pwrite() would otherwise allocate + zero a fresh page for every
    // 4 KiB written, which shows up as ~42% of VMM-thread time in perf
    // (shmem_alloc_and_add_folio + clear_page_rep).  fallocate() does this
    // upfront in one batch, so the streaming pwrite() path becomes a simple
    // memcpy into already-resident pages.
    {
        use std::os::unix::io::AsRawFd;
        // SAFETY: mem_file is a valid open fd, and FALLOC_FL_KEEP_SIZE is a
        // standard fallocate mode.
        let ret = unsafe {
            libc::fallocate(mem_file.as_raw_fd(), 0, 0, total_mem_size as libc::off_t)
        };
        if ret != 0 {
            // Non-fatal: some filesystems don't support fallocate.
            // The streaming path will still work, just slower.
            info!(
                "Live-BPF snapshot: fallocate failed ({}), continuing without pre-alloc",
                std::io::Error::last_os_error()
            );
        }
    }

    let mut guard = BpfLiveSnapshotGuard::new(vmm);

    // Debug: test thread spawn BEFORE bpf_fault attachment
    {
        info!("testing thread spawn before bpf_fault");
        match std::thread::Builder::new()
            .stack_size(64 * 1024)
            .spawn(|| 42usize)
        {
            Ok(h) => {
                info!("thread spawned successfully, joining...");
                match h.join() {
                    Ok(r) => info!("thread join returned {r}"),
                    Err(_) => info!("thread join panicked"),
                }
            }
            Err(err) => {
                info!("thread spawn FAILED: {err}");
            }
        }
        info!("thread spawn test complete");
    }

    // === Phase 2: FREEZE (brief pause) ===
    let t_phase1 = t_start.elapsed();
    info!("Live-BPF snapshot: Phase 1 took {} us", t_phase1.as_micros());
    info!("Live-BPF snapshot: Phase 2 - Freeze");
    let t_freeze_start = std::time::Instant::now();

    guard
        .vmm
        .pause_vm()
        .map_err(CreateSnapshotError::VmmError)?;
    guard.paused = true;
    let t_after_pause = t_freeze_start.elapsed();

    let microvm_state = guard
        .vmm
        .save_state(vm_info)
        .map_err(CreateSnapshotError::MicrovmState)?;
    let t_after_save_state = t_freeze_start.elapsed();

    // Attach bpf_fault to all plugged memory slots and enable WP.
    // Build slot_ranges simultaneously — each entry records the link index,
    // base address, and page range so we can map ring-buffer fault addresses
    // to page indices and wp_resolve to the correct link fd.
    struct SlotRange {
        base_addr: usize,
        page_count: usize,
        page_index_start: usize,
        link_index: usize,
    }
    let mut slot_ranges: Vec<SlotRange> = Vec::new();
    let mut page_index = 0usize;
    {
        let guest_memory = guard.vmm.vm.guest_memory();
        for region in guest_memory.iter() {
            for slot in region.plugged_slots() {
                let ptr = slot.slice.ptr_guard().as_ptr() as u64;
                let len = slot.slice.len() as u64;

                let link_fd = bpf_create_fault_link(
                    bpf_obj.struct_ops_map_fd,
                    ptr,
                    len,
                    BPF_FAULT_FLAG_WP,
                )
                .map_err(CreateSnapshotError::BpfAttach)?;

                // SAFETY: bpf_create_fault_link returned a valid link fd.
                let link = unsafe { BpfFaultLink::from_raw_fd(link_fd) };

                bpf_fault_wp_enable(link.as_raw_fd(), ptr, len)
                    .map_err(CreateSnapshotError::BpfWriteProtect)?;

                let link_idx = guard.links.len();
                guard.links.push(link);

                let n_pages = (len as usize + page_size - 1) / page_size;
                slot_ranges.push(SlotRange {
                    base_addr: ptr as usize,
                    page_count: n_pages,
                    page_index_start: page_index,
                    link_index: link_idx,
                });
                page_index += n_pages;
            }
        }
    }
    let t_after_wp = t_freeze_start.elapsed();

    guard
        .vmm
        .resume_vcpus_only()
        .map_err(CreateSnapshotError::VmmError)?;
    guard.paused = false;
    let t_freeze_total = t_freeze_start.elapsed();
    info!(
        "Live-BPF snapshot: Phase 2 (freeze) took {} us \
         (pause={} us, save_state={} us, wp_enable={} us, resume={} us)",
        t_freeze_total.as_micros(),
        t_after_pause.as_micros(),
        (t_after_save_state - t_after_pause).as_micros(),
        (t_after_wp - t_after_save_state).as_micros(),
        (t_freeze_total - t_after_wp).as_micros(),
    );

    // === Phase 3: STREAM RAM (VM running, vCPUs never blocked) ===
    info!("Live-BPF snapshot: Phase 3 - Stream RAM");
    let t_stream_start = std::time::Instant::now();

    let total_page_estimate: usize = slot_ranges.iter().map(|sr| sr.page_count).sum();
    let mut pages: Vec<PageEntry> = Vec::with_capacity(total_page_estimate);
    let mut file_offset: u64 = 0;
    let mut slot_range_cursor = 0usize;
    let guest_memory = guard.vmm.vm.guest_memory();
    for region in guest_memory.iter() {
        for (slot, plugged) in region.slots() {
            let slot_len = slot.slice.len();
            if plugged {
                let base_ptr = slot.slice.ptr_guard().as_ptr();
                let sr = &slot_ranges[slot_range_cursor];
                debug_assert_eq!(sr.base_addr, base_ptr as usize);
                let link_index = sr.link_index;
                slot_range_cursor += 1;
                for off in (0..slot_len).step_by(page_size) {
                    let actual_size = std::cmp::min(page_size, slot_len - off);
                    pages.push(PageEntry {
                        // SAFETY: base_ptr + off is within the guest memory slot.
                        ptr: unsafe { base_ptr.add(off) },
                        file_offset: file_offset + off as u64,
                        size: actual_size,
                        link_index,
                    });
                }
            }
            file_offset += slot_len as u64;
        }
    }

    let total_pages = pages.len();

    // Per-page atomic state for coordinating the linear scan thread and
    // ring buffer drain thread.  See `page_state` module for the CAS
    // protocol that ensures pre-images always overwrite linear scan data.
    let page_states: Arc<Vec<AtomicU8>> = Arc::new(
        (0..total_pages)
            .map(|_| AtomicU8::new(page_state::UNSAVED))
            .collect(),
    );
    let saved_count = Arc::new(AtomicUsize::new(0));

    // ── Ring buffer drain thread ───────────────────────────────────────
    // Runs concurrently with the linear scan, continuously draining the
    // BPF ring buffer.  For each pre-image event it:
    //   1. Claims the page via CAS (spinning briefly if the linear scan
    //      is mid-pwrite on SCAN_WRITING).
    //   2. Pwrites the pre-image to the snapshot file.
    // This prevents ring buffer overflow and ensures pre-images always
    // overwrite any stale linear scan data.

    // These are moved into the drain thread; we keep Arc clones for the
    // main thread.
    // Copy slot_ranges data for the drain thread (small vec, ~few entries).
    let ring_slot_ranges: Vec<(usize, usize, usize)> = slot_ranges
        .iter()
        .map(|sr| (sr.base_addr, sr.page_count, sr.page_index_start))
        .collect();

    // Duplicate the file descriptor so the drain thread has its own handle.
    // We use libc::dup() instead of File::try_clone() because the latter
    // calls fcntl(F_DUPFD_CLOEXEC) which is blocked by our seccomp filter.
    let dup_fd = unsafe { libc::dup(mem_file.as_raw_fd()) };
    if dup_fd < 0 {
        return Err(CreateSnapshotError::MemoryBackingFile(
            "dup",
            std::io::Error::last_os_error(),
        ));
    }
    // SAFETY: dup_fd is a valid file descriptor returned by dup().
    let ring_mem_fd: std::fs::File = unsafe { FromRawFd::from_raw_fd(dup_fd) };

    // SAFETY: pages vec contains raw pointers to guest memory.  Guest
    // memory is pinned (mmap'd, KVM-registered) and will not be
    // unmapped until after the snapshot completes (guard ensures this).
    // Both threads read through these pointers (no writes).  The Arc
    // ensures the vec itself lives long enough.
    let pages = Arc::new(pages);
    let ring_pages_vec = Arc::clone(&pages);

    // Channel: main thread signals the drain thread to stop.
    let (stop_tx, stop_rx) = std::sync::mpsc::channel::<()>();

    let _ring_slot_ranges = ring_slot_ranges;
    let _ring_pages_vec = ring_pages_vec;
    let _ring_mem_fd = ring_mem_fd;
    drop(stop_rx);

    const LINEAR_BATCH: usize = 4096;

    // ── Phase 3a: Linear scan ──────────────────────────────────────────
    // Scan all pages sequentially.  For each page, try to claim it via
    // CAS(UNSAVED → SCAN_WRITING).  If claimed, pwrite from guest
    // memory and mark SCAN_DONE.  If the ring buffer thread already
    // claimed it (RING_SAVED), skip — the pre-image is already written.
    // wp_resolve is called per-batch to clear write-protection.
    {
        info!("starting linear scan, total_pages={total_pages}");
        let mut linear_cursor = 0usize;
        while linear_cursor < total_pages {
            let batch_start = linear_cursor;
            let mut run_start = linear_cursor;
            let mut run_pages = 0usize;

            let batch_end = std::cmp::min(linear_cursor + LINEAR_BATCH, total_pages);
            while linear_cursor < batch_end {
                if !page_state::try_claim_for_scan(&page_states[linear_cursor]) {
                    // Ring buffer thread owns this page — flush pending run.
                    if run_pages > 0 {
                        let run_len = run_pages * page_size;
                        // SAFETY: pages within a run are contiguous in host memory.
                        let data = unsafe {
                            std::slice::from_raw_parts(pages[run_start].ptr, run_len)
                        };
                        mem_file
                            .write_all_at(data, pages[run_start].file_offset)
                            .map_err(|err| {
                                CreateSnapshotError::MemoryBackingFile("write", err)
                            })?;
                        // Mark all pages in the run as SCAN_DONE.
                        for i in run_start..run_start + run_pages {
                            page_state::mark_scan_done(&page_states[i]);
                        }
                        run_pages = 0;
                    }
                    linear_cursor += 1;
                    continue;
                }

                // Page claimed for scan.  Check contiguity to extend the run.
                if run_pages > 0
                    && pages[linear_cursor].ptr
                        != unsafe { pages[run_start].ptr.add(run_pages * page_size) }
                {
                    // Not contiguous — flush the run.
                    let run_len = run_pages * page_size;
                    let data = unsafe {
                        std::slice::from_raw_parts(pages[run_start].ptr, run_len)
                    };
                    mem_file
                        .write_all_at(data, pages[run_start].file_offset)
                        .map_err(|err| {
                            CreateSnapshotError::MemoryBackingFile("write", err)
                        })?;
                    for i in run_start..run_start + run_pages {
                        page_state::mark_scan_done(&page_states[i]);
                    }
                    run_pages = 0;
                }

                if run_pages == 0 {
                    run_start = linear_cursor;
                }
                saved_count.fetch_add(1, Ordering::Relaxed);
                run_pages += 1;
                linear_cursor += 1;
            }

            // Flush trailing run.
            if run_pages > 0 {
                let run_len = run_pages * page_size;
                let data = unsafe {
                    std::slice::from_raw_parts(pages[run_start].ptr, run_len)
                };
                mem_file
                    .write_all_at(data, pages[run_start].file_offset)
                    .map_err(|err| {
                        CreateSnapshotError::MemoryBackingFile("write", err)
                    })?;
                for i in run_start..run_start + run_pages {
                    page_state::mark_scan_done(&page_states[i]);
                }
            }

            // wp_resolve the entire batch range in contiguous link ranges.
            let mut wp_start = batch_start;
            while wp_start < batch_end {
                let link_idx = pages[wp_start].link_index;
                let mut wp_end = wp_start + 1;
                while wp_end < batch_end
                    && pages[wp_end].link_index == link_idx
                    && pages[wp_end].ptr
                        == unsafe { pages[wp_start].ptr.add((wp_end - wp_start) * page_size) }
                {
                    wp_end += 1;
                }
                let range_len = (wp_end - wp_start) * page_size;
                if let Err(err) = bpf_fault_wp_resolve(
                    guard.links[link_idx].as_raw_fd(),
                    pages[wp_start].ptr as u64,
                    range_len as u64,
                ) {
                    warn!(
                        "Live-BPF: wp_resolve failed at 0x{:x}: {err}",
                        pages[wp_start].ptr as u64
                    );
                }
                wp_start = wp_end;
            }

            // Check for ring buffer overflow periodically.
            let drops = read_bpf_drop_counter(&bpf_obj);
            if drops > 0 {
                return Err(CreateSnapshotError::BpfRingBufOverflow(format!(
                    "ring buffer dropped {drops} pre-image(s) during linear scan \
                     (cursor {linear_cursor}/{total_pages}) — snapshot is inconsistent",
                )));
            }
        }
    }

    let _ = stop_tx;
    let ringbuf_pages_saved = 0usize;

    // One final check after the drain thread exits: any remaining events
    // that arrived between our wp_resolve and the drain thread's final
    // drain would have been picked up by the final for_each above.  But
    // verify saved_count matches.
    let final_saved = saved_count.load(Ordering::Relaxed);
    if final_saved < total_pages {
        // Some pages may have been removed by balloon (MADV_DONTNEED).
        // The file is pre-zeroed (fallocate), so those regions are correct.
        info!(
            "Live-BPF: {final_saved}/{total_pages} pages saved \
             ({} pages zeroed by balloon)",
            total_pages - final_saved
        );
    }

    let _ = saved_count;

    // === Phase 4: FINALIZE ===
    let t_stream_total = t_stream_start.elapsed();

    // Check BPF drop counter: ring buffer overflows mean some pre-images
    // were lost and those pages were saved with post-write content by the
    // linear scan.  The snapshot is inconsistent and must not be used.
    let drop_count = read_bpf_drop_counter(&bpf_obj);
    if drop_count > 0 {
        return Err(CreateSnapshotError::BpfRingBufOverflow(format!(
            "ring buffer dropped {drop_count} pre-image(s) during snapshot — \
             snapshot is inconsistent. Consider increasing ring buffer size \
             or reducing LINEAR_BATCH.",
        )));
    }

    info!(
        "Live-BPF snapshot: Phase 3 (stream) took {} us, {} pages total \
         ({} ring-buffer, {} linear-scan, {} ring-drops)",
        t_stream_total.as_micros(),
        total_pages,
        ringbuf_pages_saved,
        total_pages - ringbuf_pages_saved,
        drop_count,
    );
    info!("Live-BPF snapshot: Phase 4 - Finalize");
    let t_finalize_start = std::time::Instant::now();

    guard.vmm.kick_devices();
    guard.devices_kicked = true;
    guard.links.clear();

    snapshot_state_to_file(&microvm_state, &params.snapshot_path)?;

    mem_file
        .sync_all()
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("sync_all", err))?;

    let t_total = t_start.elapsed();
    info!(
        "Live-BPF snapshot: Phase 4 (finalize) took {} us",
        t_finalize_start.elapsed().as_micros()
    );
    info!(
        "Live-BPF snapshot: complete in {} us (freeze/downtime={} us)",
        t_total.as_micros(),
        t_freeze_total.as_micros(),
    );
    Ok(())
}
