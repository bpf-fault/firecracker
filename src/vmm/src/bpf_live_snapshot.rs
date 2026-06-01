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

use std::fs::{File, OpenOptions};
use std::os::fd::AsFd;
use std::os::unix::fs::FileExt;
use std::os::unix::io::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use libbpf_rs::MapCore;
use vm_memory::GuestMemory;

use crate::Vmm;
use crate::logger::{info, warn};
use crate::persist::{CreateSnapshotError, MicrovmState, VmInfo, snapshot_state_to_file};
use crate::snapshot_worker::{SnapshotTimings, StreamingTask};
use crate::vmm_config::snapshot::CreateSnapshotParams;

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

// ── BPF constants (fault_ops-specific, not handled by libbpf) ───────────────

/// BPF commands for fault_ops link management.
const BPF_LINK_CREATE: u32 = 28;
const BPF_LINK_FAULT_OPS_CMD: u32 = 38;

/// BPF attach type for fault_ops.
const BPF_FAULT_OPS: u32 = 58;

/// bpf_fault WP enable flag (BPF_LINK_FAULT_OPS_CMD flags).
const BPF_FAULT_WP_ENABLE: u32 = 1;
/// bpf_fault register-region flag (BPF_LINK_FAULT_OPS_CMD flags).
const BPF_FAULT_REGISTER: u32 = 2;
/// bpf_fault unregister-region flag (BPF_LINK_FAULT_OPS_CMD flags).
const BPF_FAULT_UNREGISTER: u32 = 4;
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

// ── Slot range tracking ─────────────────────────────────────────────────────

/// Tracks a contiguous range of pages within a single guest memory slot,
/// mapping ring-buffer fault addresses to page indices and BPF link fds.
struct SlotRange {
    base_addr: usize,
    page_count: usize,
    page_index_start: usize,
    link_index: usize,
}

// ── RAII guard ──────────────────────────────────────────────────────────────

/// RAII guard for bpf_fault live snapshot.
struct BpfLiveSnapshotGuard<'a> {
    vmm: &'a mut Vmm,
    links: Vec<BpfFaultLink>,
    paused: bool,
    devices_kicked: bool,
    /// Set by `into_streaming_state` to suppress Drop cleanup after handoff.
    handed_off: bool,
}

impl<'a> BpfLiveSnapshotGuard<'a> {
    fn new(vmm: &'a mut Vmm) -> Self {
        Self {
            vmm,
            links: Vec::new(),
            paused: false,
            devices_kicked: false,
            handed_off: false,
        }
    }

    /// Extracts the BPF links for handoff to the streaming worker thread.
    ///
    /// After this call, the guard's Drop will not attempt any cleanup.
    /// The caller is responsible for dropping the links (which closes BPF fds
    /// and removes WP) and calling `kick_devices()` after streaming completes.
    fn into_streaming_state(mut self) -> Vec<BpfFaultLink> {
        assert!(!self.paused, "must resume vCPUs before handoff");
        let links = std::mem::take(&mut self.links);
        self.handed_off = true;
        links
    }
}

impl Drop for BpfLiveSnapshotGuard<'_> {
    fn drop(&mut self) {
        if self.handed_off {
            return;
        }
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

// SAFETY: `BpfFaultObject` is transferred to the snapshot worker thread after
// Phase 2. After handoff, only the worker thread accesses it (for reading the
// drop counter via `read_bpf_drop_counter`). All underlying libbpf operations
// are kernel syscalls on file descriptors, which are thread-safe.
unsafe impl Send for BpfFaultObject {}

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

/// Removes a memory region from an existing bpf_fault link (BPF_FAULT_UNREGISTER).
///
/// Called per-slot at the end of Phase 3 so that `links.clear()` in Phase 4
/// finds no VMAs left to walk, eliminating the `mmap_write_lock` stall.
fn bpf_fault_unregister_region(link_fd: RawFd, addr: u64, len: u64) -> Result<(), String> {
    let mut attr = [0u8; 32];
    attr[0..4].copy_from_slice(&link_fd.cast_unsigned().to_ne_bytes());
    attr[4..8].copy_from_slice(&BPF_FAULT_UNREGISTER.to_ne_bytes());
    attr[8..16].copy_from_slice(&addr.to_ne_bytes());
    attr[16..24].copy_from_slice(&len.to_ne_bytes());

    // SAFETY: attr is a valid BPF_LINK_FAULT_OPS_CMD attribute.
    unsafe { sys_bpf(BPF_LINK_FAULT_OPS_CMD, attr.as_mut_ptr(), 32)? };
    Ok(())
}

/// Adds a memory region to an existing bpf_fault link (BPF_FAULT_REGISTER).
///
/// After the first region is registered via `bpf_create_fault_link`, subsequent
/// regions are added with this call rather than creating a new link each time.
fn bpf_fault_register_region(link_fd: RawFd, addr: u64, len: u64) -> Result<(), String> {
    let mut attr = [0u8; 32];
    attr[0..4].copy_from_slice(&link_fd.cast_unsigned().to_ne_bytes());
    attr[4..8].copy_from_slice(&BPF_FAULT_REGISTER.to_ne_bytes());
    attr[8..16].copy_from_slice(&addr.to_ne_bytes());
    attr[16..24].copy_from_slice(&len.to_ne_bytes());

    // SAFETY: attr is a valid BPF_LINK_FAULT_OPS_CMD attribute.
    unsafe { sys_bpf(BPF_LINK_FAULT_OPS_CMD, attr.as_mut_ptr(), 32)? };
    Ok(())
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

// ── Streaming task (runs on worker thread) ──────────────────────────────────

/// Streaming timeout: abort if no progress is made within this duration.
const STREAM_TIMEOUT: Duration = Duration::from_secs(300);

/// All state needed for Phase 3 (RAM streaming) + Phase 4 (finalize, minus
/// kick_devices). Transferred to the snapshot worker thread.
pub(crate) struct BpfStreamingTask {
    mem_file: File,
    pages: Vec<PageEntry>,
    saved_bitmap: Vec<u64>,
    slot_ranges: Vec<SlotRange>,
    ring_consumer: RingBufConsumer,
    bpf_obj: BpfFaultObject,
    links: Vec<BpfFaultLink>,
    page_size: usize,
    total_pages: usize,
    microvm_state: MicrovmState,
    snapshot_path: PathBuf,
    t_start: Instant,
    t_freeze_total: Duration,
}

// SAFETY: All fields are either inherently Send (File, Vec, PathBuf, etc.) or
// have been individually reviewed:
// - PageEntry contains *const u8 pointing to stable guest memory mappings
//   that remain valid for the VM's lifetime (backed by Arc<Vm>).
// - BpfFaultObject: see its unsafe impl Send above.
// - RingBufConsumer: already has unsafe impl Send.
// - BpfFaultLink wraps OwnedFd, which is Send.
unsafe impl Send for BpfStreamingTask {}

impl StreamingTask for BpfStreamingTask {
    fn run(mut self: Box<Self>) -> Result<SnapshotTimings, CreateSnapshotError> {
        // === Phase 3: STREAM RAM (VM running, vCPUs never blocked) ===
        info!("Live-BPF snapshot: Phase 3 - Stream RAM (worker thread)");
        let t_stream_start = Instant::now();
        let mut ringbuf_pages_saved = 0usize;
        let mut saved_count = 0usize;
        let mut linear_cursor = 0usize;

        let page_size = self.page_size;
        let total_pages = self.total_pages;

        #[inline(always)]
        fn bitmap_test(bitmap: &[u64], idx: usize) -> bool {
            bitmap[idx / 64] & (1u64 << (idx % 64)) != 0
        }

        #[inline(always)]
        fn bitmap_set(bitmap: &mut [u64], idx: usize) {
            bitmap[idx / 64] |= 1u64 << (idx % 64);
        }

        #[inline]
        fn addr_to_page_index(
            ranges: &[SlotRange],
            addr: usize,
            page_size: usize,
        ) -> Option<usize> {
            for r in ranges {
                let end = r.base_addr + r.page_count * page_size;
                if addr >= r.base_addr && addr < end {
                    return Some(r.page_index_start + (addr - r.base_addr) / page_size);
                }
            }
            None
        }

        let mut last_progress = Instant::now();
        const LINEAR_BATCH: usize = 4096;

        /// Writes a contiguous run of pages to the mem file and immediately
        /// resolves write-protection for that run.
        #[inline]
        fn flush_run(
            mem_file: &File,
            pages: &[PageEntry],
            links: &[BpfFaultLink],
            run_start: usize,
            run_pages: usize,
            page_size: usize,
        ) -> Result<(), CreateSnapshotError> {
            let run_len = run_pages * page_size;
            // SAFETY: pages within a run are contiguous in host memory.
            let data =
                unsafe { std::slice::from_raw_parts(pages[run_start].ptr, run_len) };
            mem_file
                .write_all_at(data, pages[run_start].file_offset)
                .map_err(|err| CreateSnapshotError::MemoryBackingFile("write", err))?;

            // Resolve WP immediately for this run so future guest writes
            // to these pages don't trigger unnecessary BPF faults.
            let link_idx = pages[run_start].link_index;
            if let Err(err) = bpf_fault_wp_resolve(
                links[link_idx].as_raw_fd(),
                pages[run_start].ptr as u64,
                run_len as u64,
            ) {
                warn!(
                    "Live-BPF: wp_resolve failed at 0x{:x}: {err}",
                    pages[run_start].ptr as u64
                );
            }
            Ok(())
        }

        // ── Phase 3a: Linear scan with drain-before-scan ─────────────────
        //
        // Before each batch, drain the ring buffer so that BPF-captured
        // pre-images are saved and marked in the bitmap.  The linear scan
        // then skips those pages, eliminating double writes.  After saving
        // each contiguous run of pages, wp_resolve is called immediately
        // (per-run) instead of per-batch, minimising the window in which
        // saved-but-still-WP pages can generate unnecessary BPF faults.
        {
            let mut batch_start = linear_cursor;
            while linear_cursor < total_pages {
                let batch_end = std::cmp::min(batch_start + LINEAR_BATCH, total_pages);

                // Drain ring buffer BEFORE scanning this batch.
                // Pre-images from BPF faults that fired before (or during the
                // previous batch) are written to disk and marked in the bitmap
                // so the linear scan below can skip them.
                self.ring_consumer
                    .for_each(|addr, data| {
                        if let Some(idx) =
                            addr_to_page_index(&self.slot_ranges, addr as usize, page_size)
                        {
                            let pg = &self.pages[idx];
                            self.mem_file.write_all_at(data, pg.file_offset)?;
                            if !bitmap_test(&self.saved_bitmap, idx) {
                                bitmap_set(&mut self.saved_bitmap, idx);
                                saved_count += 1;
                            }
                            ringbuf_pages_saved += 1;
                        }
                        Ok(())
                    })
                    .map_err(|err| CreateSnapshotError::MemoryBackingFile("write", err))?;

                let drops = read_bpf_drop_counter(&self.bpf_obj);
                if drops > 0 {
                    return Err(CreateSnapshotError::BpfRingBufOverflow(format!(
                        "ring buffer dropped {drops} pre-image(s) during linear scan \
                         (batch {batch_start}/{total_pages}) — snapshot is inconsistent",
                    )));
                }

                // Linear scan this batch with per-run wp_resolve.
                let mut run_start = linear_cursor;
                let mut run_pages = 0usize;

                while linear_cursor < batch_end {
                    if bitmap_test(&self.saved_bitmap, linear_cursor) {
                        // Already saved (by ring buffer drain above). Flush
                        // the current run before skipping.
                        if run_pages > 0 {
                            flush_run(
                                &self.mem_file,
                                &self.pages,
                                &self.links,
                                run_start,
                                run_pages,
                                page_size,
                            )?;
                            run_pages = 0;
                        }
                        linear_cursor += 1;
                        continue;
                    }

                    // Break the run if the next page is not contiguous.
                    if run_pages > 0
                        && self.pages[linear_cursor].ptr
                            != unsafe { self.pages[run_start].ptr.add(run_pages * page_size) }
                    {
                        flush_run(
                            &self.mem_file,
                            &self.pages,
                            &self.links,
                            run_start,
                            run_pages,
                            page_size,
                        )?;
                        run_pages = 0;
                    }

                    if run_pages == 0 {
                        run_start = linear_cursor;
                    }
                    bitmap_set(&mut self.saved_bitmap, linear_cursor);
                    saved_count += 1;
                    run_pages += 1;
                    linear_cursor += 1;
                }

                // Flush the last run of this batch.
                if run_pages > 0 {
                    flush_run(
                        &self.mem_file,
                        &self.pages,
                        &self.links,
                        run_start,
                        run_pages,
                        page_size,
                    )?;
                }

                batch_start = batch_end;
            }
        }

        // ── Phase 3b: Final ring buffer drain ──────────────────────────────
        loop {
            let prev = saved_count;
            self.ring_consumer
                .for_each(|addr, data| {
                    if let Some(idx) =
                        addr_to_page_index(&self.slot_ranges, addr as usize, page_size)
                    {
                        let page = &self.pages[idx];
                        self.mem_file.write_all_at(data, page.file_offset)?;
                        if !bitmap_test(&self.saved_bitmap, idx) {
                            bitmap_set(&mut self.saved_bitmap, idx);
                            saved_count += 1;
                        }
                        ringbuf_pages_saved += 1;
                    }
                    Ok(())
                })
                .map_err(|err| CreateSnapshotError::MemoryBackingFile("write", err))?;

            if saved_count >= total_pages {
                break;
            }
            if saved_count > prev {
                last_progress = Instant::now();
            } else {
                std::thread::yield_now();
                if last_progress.elapsed() > STREAM_TIMEOUT {
                    return Err(CreateSnapshotError::BpfLoad(format!(
                        "Live-BPF streaming timeout: no progress for {}s \
                         ({saved_count}/{total_pages} pages saved)",
                        STREAM_TIMEOUT.as_secs()
                    )));
                }
            }
        }

        // === Phase 4: FINALIZE (minus kick_devices — done by event manager) ===
        let t_stream_total = t_stream_start.elapsed();

        let drop_count = read_bpf_drop_counter(&self.bpf_obj);
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
        info!("Live-BPF snapshot: Phase 4 - Finalize (worker)");
        let t_finalize_start = Instant::now();

        // Unregister all WP-protected regions before dropping the link fd.
        //
        // Each BPF_FAULT_UNREGISTER call holds mmap_write_lock(mm) for the whole
        // range it covers while it resolves the uffd-wp PTEs and clears the
        // VMA's bpf_fault flags. For a multi-GiB guest a single full-region call
        // holds the write lock for ~100-200 ms. That window blocks every other
        // mmap_lock reader in the process — in particular the ordinary COW write
        // faults the running guest keeps taking on still-zero-page-backed RAM
        // (memory is faulted in read-only via MADV_POPULATE_READ at boot, so the
        // guest's first write to each page COW-faults). The result is a sharp
        // throughput dip at the end of the snapshot.
        //
        // Unregistering in small chunks keeps each individual call's write-lock
        // hold short and, crucially, *releases* the lock between calls so those
        // COW faults can interleave. Each chunk is still a complete, atomic
        // per-range teardown (resolve + flag clear under the write lock), so this
        // is exactly as safe as unregistering disjoint ranges — only finer
        // grained. The cumulative work (and total teardown time) is essentially
        // unchanged, but the per-fault stall drops from ~200 ms to ~1 ms.
        //
        // Dropping the link fd afterwards (`bpf_fault_release_all`) then finds no
        // registered VMAs left and is a no-op.
        const UNREG_CHUNK_BYTES: u64 = 2 * 1024 * 1024;
        let t_unreg_start = Instant::now();
        if let Some(link) = self.links.first() {
            let link_fd = link.as_raw_fd();
            for sr in &self.slot_ranges {
                let addr = sr.base_addr as u64;
                let len = (sr.page_count * page_size) as u64;
                let mut off = 0u64;
                while off < len {
                    let clen = std::cmp::min(UNREG_CHUNK_BYTES, len - off);
                    if let Err(err) = bpf_fault_unregister_region(link_fd, addr + off, clen) {
                        warn!(
                            "Live-BPF: unregister_region failed at 0x{:x}: {err}",
                            addr + off
                        );
                    }
                    off += clen;
                }
            }
        }
        let t_unreg = t_unreg_start.elapsed();

        // Drop link fd — all VMAs already unregistered above.
        let t_clear_start = Instant::now();
        self.links.clear();
        let t_clear = t_clear_start.elapsed();

        let t_savestate_start = Instant::now();
        snapshot_state_to_file(&self.microvm_state, &self.snapshot_path)?;
        let t_savestate = t_savestate_start.elapsed();

        let t_sync_start = Instant::now();
        self.mem_file
            .sync_all()
            .map_err(|err| CreateSnapshotError::MemoryBackingFile("sync_all", err))?;
        let t_sync = t_sync_start.elapsed();
        info!(
            "Live-BPF snapshot: Phase 4 breakdown — unregister={} us, link_drop={} us, \
             save_state={} us, sync_all={} us",
            t_unreg.as_micros(),
            t_clear.as_micros(),
            t_savestate.as_micros(),
            t_sync.as_micros(),
        );

        let t_total = self.t_start.elapsed();
        let finalize_us = t_finalize_start.elapsed().as_micros();
        info!("Live-BPF snapshot: Phase 4 (finalize) took {} us", finalize_us);
        info!(
            "Live-BPF snapshot: complete in {} us (freeze/downtime={} us)",
            t_total.as_micros(),
            self.t_freeze_total.as_micros(),
        );

        Ok(SnapshotTimings {
            total_us: t_total.as_micros(),
            freeze_us: self.t_freeze_total.as_micros(),
            stream_us: t_stream_total.as_micros(),
            finalize_us,
            total_pages,
            detail: format!(
                "ring-buffer={}, linear-scan={}, ring-drops={}",
                ringbuf_pages_saved,
                total_pages - ringbuf_pages_saved,
                drop_count
            ),
        })
    }
}

// ── Main entry points ───────────────────────────────────────────────────────

/// Result of Phase 1 BPF preparation (no Vmm lock required).
pub(crate) struct Phase1BpfResult {
    /// Pre-allocated memory backing file.
    pub mem_file: File,
    /// Total guest memory size in bytes.
    pub total_mem_size: u64,
    /// Loaded BPF object (struct_ops + ring buffer fds).
    pub bpf_obj: BpfFaultObject,
    /// Ring buffer size in bytes.
    pub ring_buf_size: usize,
}

/// Phase 1: Prepare file and BPF object (VM still running, no Vmm lock needed).
///
/// Computes total memory size, loads the BPF ELF (expensive verifier pass),
/// creates the memory backing file, and pre-allocates its blocks.  Takes only
/// `&Vm` (via `Arc<Vm>`) so it can run on the worker thread without holding
/// the Vmm mutex.
pub(crate) fn phase1_prepare_bpf(
    vm: &crate::vstate::vm::Vm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<Phase1BpfResult, CreateSnapshotError> {
    if vm_info.huge_pages.is_hugetlbfs() {
        return Err(CreateSnapshotError::BpfLoad(
            "LiveBpf snapshots do not support huge pages; the BPF program \
             captures 4 KiB pre-images only"
                .to_string(),
        ));
    }

    info!("Live-BPF snapshot: Phase 1 - Prepare");

    let total_mem_size: u64 = vm
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

    // Pre-allocate the snapshot file's backing pages.
    {
        // SAFETY: mem_file is a valid open fd.
        let ret = unsafe {
            libc::fallocate(mem_file.as_raw_fd(), 0, 0, total_mem_size as libc::off_t)
        };
        if ret != 0 {
            info!(
                "Live-BPF snapshot: fallocate failed ({}), continuing without pre-alloc",
                std::io::Error::last_os_error()
            );
        }
    }

    Ok(Phase1BpfResult {
        mem_file,
        total_mem_size,
        bpf_obj,
        ring_buf_size,
    })
}

/// Phase 2: Freeze VM, enable BPF WP, resume, and build the streaming task.
///
/// Requires `&mut Vmm` (caller holds the Vmm mutex).  The guard ensures the
/// VM is resumed and devices kicked on error.
pub(crate) fn phase2_freeze_bpf(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    phase1: Phase1BpfResult,
    params: &CreateSnapshotParams,
    t_start: Instant,
) -> Result<BpfStreamingTask, CreateSnapshotError> {
    let page_size = vm_info.huge_pages.page_size();

    let mut guard = BpfLiveSnapshotGuard::new(vmm);

    // === Phase 2: FREEZE (brief pause) ===
    info!("Live-BPF snapshot: Phase 2 - Freeze");
    let t_freeze_start = Instant::now();

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
    //
    // A single link is created for the first slot; all subsequent slots are
    // added to the same link via BPF_FAULT_REGISTER.  This mirrors the uffd
    // path (one fd, N register calls) and avoids creating N kernel link objects.
    let mut slot_ranges: Vec<SlotRange> = Vec::new();
    let mut page_index = 0usize;
    {
        let guest_memory = guard.vmm.vm.guest_memory();
        for region in guest_memory.iter() {
            for slot in region.plugged_slots() {
                let ptr = slot.slice.ptr_guard().as_ptr() as u64;
                let len = slot.slice.len() as u64;

                let link_fd = if guard.links.is_empty() {
                    let fd = bpf_create_fault_link(
                        phase1.bpf_obj.struct_ops_map_fd,
                        ptr,
                        len,
                        BPF_FAULT_FLAG_WP,
                    )
                    .map_err(CreateSnapshotError::BpfAttach)?;
                    // SAFETY: bpf_create_fault_link returned a valid link fd.
                    guard.links.push(unsafe { BpfFaultLink::from_raw_fd(fd) });
                    fd
                } else {
                    let fd = guard.links[0].as_raw_fd();
                    bpf_fault_register_region(fd, ptr, len)
                        .map_err(CreateSnapshotError::BpfAttach)?;
                    fd
                };

                bpf_fault_wp_enable(link_fd, ptr, len)
                    .map_err(CreateSnapshotError::BpfWriteProtect)?;

                let n_pages = (len as usize + page_size - 1) / page_size;
                slot_ranges.push(SlotRange {
                    base_addr: ptr as usize,
                    page_count: n_pages,
                    page_index_start: page_index,
                    link_index: 0,
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

    // Build the page table for streaming.
    let total_page_estimate: usize = slot_ranges.iter().map(|sr| sr.page_count).sum();
    let mut pages: Vec<PageEntry> = Vec::with_capacity(total_page_estimate);
    let mut file_offset: u64 = 0;
    let mut slot_range_cursor = 0usize;
    {
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
    }

    let total_pages = pages.len();
    let bitmap_words = (total_pages + 63) / 64;
    let saved_bitmap = vec![0u64; bitmap_words];

    let ring_consumer = RingBufConsumer::new(phase1.bpf_obj.ring_buf_fd, phase1.ring_buf_size)
        .map_err(CreateSnapshotError::BpfRingBuf)?;

    // Extract links from guard — this consumes the guard and releases &mut Vmm.
    let links = guard.into_streaming_state();

    let snapshot_path = params.snapshot_path.clone();

    Ok(BpfStreamingTask {
        mem_file: phase1.mem_file,
        pages,
        saved_bitmap,
        slot_ranges,
        ring_consumer,
        bpf_obj: phase1.bpf_obj,
        links,
        page_size,
        total_pages,
        microvm_state,
        snapshot_path,
        t_start,
        t_freeze_total,
    })
}

/// Prepares a live BPF snapshot (Phase 1 + Phase 2) and returns a streaming
/// task.  Used by the synchronous fallback path when no worker is available.
pub(crate) fn prepare_live_bpf_snapshot(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<BpfStreamingTask, CreateSnapshotError> {
    let t_start = Instant::now();
    let phase1 = phase1_prepare_bpf(&vmm.vm, vm_info, params)?;
    let t_phase1 = t_start.elapsed();
    info!("Live-BPF snapshot: Phase 1 took {} us", t_phase1.as_micros());
    phase2_freeze_bpf(vmm, vm_info, phase1, params, t_start)
}

/// Creates a live snapshot using the bpf_fault kernel interface (synchronous).
///
/// This is a convenience wrapper that calls [`prepare_live_bpf_snapshot`]
/// followed by running the streaming task synchronously on the current thread.
/// For background streaming (to keep device I/O alive), use
/// `prepare_live_bpf_snapshot` and submit the task to the snapshot worker.
pub fn create_live_bpf_snapshot(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<(), CreateSnapshotError> {
    let task = prepare_live_bpf_snapshot(vmm, vm_info, params)?;
    let _timings = Box::new(task).run()?;
    vmm.kick_devices();
    Ok(())
}
