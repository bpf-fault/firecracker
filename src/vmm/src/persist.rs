// Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! Defines state structures for saving/restoring a Firecracker microVM.

use std::collections::BTreeMap;
use std::fmt::Debug;
use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::mem::forget;
use std::os::unix::fs::FileExt;
use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::{Arc, Mutex};

use semver::Version;
use serde::{Deserialize, Serialize};
use userfaultfd::{FeatureFlags, Uffd, UffdBuilder};
use vmm_sys_util::sock_ctrl_msg::ScmSocket;

#[cfg(target_arch = "aarch64")]
use crate::arch::aarch64::vcpu::get_manufacturer_id_from_host;
use crate::builder::{self, BuildMicrovmFromSnapshotError};
use crate::cpu_config::templates::StaticCpuTemplate;
#[cfg(target_arch = "x86_64")]
use crate::cpu_config::x86_64::cpuid::CpuidTrait;
#[cfg(target_arch = "x86_64")]
use crate::cpu_config::x86_64::cpuid::common::get_vendor_id_from_host;
use crate::device_manager::{DevicePersistError, DevicesState};
use crate::logger::{info, warn};
use crate::resources::VmResources;
use crate::seccomp::BpfThreadMap;
use crate::snapshot::Snapshot;
use crate::utils::u64_to_usize;
use crate::vmm_config::boot_source::BootSourceConfig;
use crate::vmm_config::instance_info::InstanceInfo;
use crate::vmm_config::machine_config::{HugePageConfig, MachineConfigError, MachineConfigUpdate};
use crate::vmm_config::snapshot::{CreateSnapshotParams, LoadSnapshotParams, MemBackendType};
use crate::vstate::kvm::KvmState;
use crate::vstate::memory::{
    self, GuestMemory, GuestMemoryExtension, GuestMemoryState, GuestRegionMmap, GuestRegionType,
    MemoryError,
};
use crate::vstate::vcpu::{VcpuSendEventError, VcpuState};
use crate::vstate::vm::{VmError, VmState};
use crate::{EventManager, Vmm, VmmError, vstate};

/// Holds information related to the VM that is not part of VmState.
#[derive(Clone, Debug, Default, Deserialize, PartialEq, Eq, Serialize)]
pub struct VmInfo {
    /// Guest memory size.
    pub mem_size_mib: u64,
    /// smt information
    pub smt: bool,
    /// CPU template type
    pub cpu_template: StaticCpuTemplate,
    /// Boot source information.
    pub boot_source: BootSourceConfig,
    /// Huge page configuration
    pub huge_pages: HugePageConfig,
}

impl From<&VmResources> for VmInfo {
    fn from(value: &VmResources) -> Self {
        Self {
            mem_size_mib: value.machine_config.mem_size_mib as u64,
            smt: value.machine_config.smt,
            cpu_template: StaticCpuTemplate::from(&value.machine_config.cpu_template),
            boot_source: value.boot_source.config.clone(),
            huge_pages: value.machine_config.huge_pages,
        }
    }
}

/// Contains the necessary state for saving/restoring a microVM.
#[derive(Debug, Default, Serialize, Deserialize)]
pub struct MicrovmState {
    /// Miscellaneous VM info.
    pub vm_info: VmInfo,
    /// KVM KVM state.
    pub kvm_state: KvmState,
    /// VM KVM state.
    pub vm_state: VmState,
    /// Vcpu states.
    pub vcpu_states: Vec<VcpuState>,
    /// Device states.
    pub device_states: DevicesState,
}

/// This describes the mapping between Firecracker base virtual address and
/// offset in the buffer or file backend for a guest memory region. It is used
/// to tell an external process/thread where to populate the guest memory data
/// for this range.
///
/// E.g. Guest memory contents for a region of `size` bytes can be found in the
/// backend at `offset` bytes from the beginning, and should be copied/populated
/// into `base_host_address`.
#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct GuestRegionUffdMapping {
    /// Base host virtual address where the guest memory contents for this
    /// region should be copied/populated.
    pub base_host_virt_addr: u64,
    /// Region size.
    pub size: usize,
    /// Offset in the backend file/buffer where the region contents are.
    pub offset: u64,
    /// The configured page size for this memory region.
    pub page_size: usize,
    /// The configured page size **in bytes** for this memory region. The name is
    /// wrong but cannot be changed due to being API, so this field is deprecated,
    /// to be removed in 2.0.
    #[deprecated]
    pub page_size_kib: usize,
}

/// Errors related to saving and restoring Microvm state.
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum MicrovmStateError {
    /// Operation not allowed: {0}
    NotAllowed(String),
    /// Cannot restore devices: {0}
    RestoreDevices(#[from] DevicePersistError),
    /// Cannot save Vcpu state: {0}
    SaveVcpuState(vstate::vcpu::VcpuError),
    /// Cannot save Vm state: {0}
    SaveVmState(vstate::vm::ArchVmError),
    /// Cannot signal Vcpu: {0}
    SignalVcpu(VcpuSendEventError),
    /// Vcpu is in unexpected state.
    UnexpectedVcpuResponse,
}

/// Errors associated with creating a snapshot.
#[rustfmt::skip]
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum CreateSnapshotError {
    /// Cannot get dirty bitmap: {0}
    DirtyBitmap(#[from] VmError),
    /// Cannot write memory file: {0}
    Memory(#[from] MemoryError),
    /// Cannot perform {0} on the memory backing file: {1}
    MemoryBackingFile(&'static str, io::Error),
    /// Cannot save the microVM state: {0}
    MicrovmState(MicrovmStateError),
    /// Cannot serialize the microVM state: {0}
    SerializeMicrovmState(#[from] crate::snapshot::SnapshotError),
    /// Cannot perform {0} on the snapshot backing file: {1}
    SnapshotBackingFile(&'static str, io::Error),
    /// Failed to create userfaultfd (live snapshots require Linux >= 5.7 with WP support): {0}
    UffdCreate(userfaultfd::Error),
    /// Failed to register memory with userfaultfd: {0}
    UffdRegister(userfaultfd::Error),
    /// Failed to write-protect memory via userfaultfd: {0}
    UffdWriteProtect(userfaultfd::Error),
    /// Failed to read userfaultfd event: {0}
    UffdReadEvent(userfaultfd::Error),
    /// VMM error during live snapshot: {0}
    VmmError(VmmError),
}

/// Snapshot version
pub const SNAPSHOT_VERSION: Version = Version::new(8, 0, 0);

/// Creates a Microvm snapshot.
pub fn create_snapshot(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<(), CreateSnapshotError> {
    let microvm_state = vmm
        .save_state(vm_info)
        .map_err(CreateSnapshotError::MicrovmState)?;

    snapshot_state_to_file(&microvm_state, &params.snapshot_path)?;

    vmm.vm
        .snapshot_memory_to_file(&params.mem_file_path, params.snapshot_type)?;

    // We need to mark queues as dirty again for all activated devices. The reason we
    // do it here is that we don't mark pages as dirty during runtime
    // for queue objects.
    vmm.device_manager
        .mark_virtio_queue_memory_dirty(vmm.vm.guest_memory());

    Ok(())
}

/// Information about a registered UFFD memory region, used for cleanup.
struct UffdRegion {
    ptr: *mut libc::c_void,
    len: usize,
}

/// RAII guard that ensures the VM is left in a consistent running state
/// even if the live snapshot fails partway through.
struct LiveSnapshotGuard<'a> {
    vmm: &'a mut Vmm,
    uffd: Option<Uffd>,
    regions: Vec<UffdRegion>,
    paused: bool,
    devices_kicked: bool,
}

impl<'a> LiveSnapshotGuard<'a> {
    fn new(vmm: &'a mut Vmm) -> Self {
        Self {
            vmm,
            uffd: None,
            regions: Vec::new(),
            paused: false,
            devices_kicked: false,
        }
    }
}

impl Drop for LiveSnapshotGuard<'_> {
    fn drop(&mut self) {
        // Remove write-protection and unregister all regions from UFFD.
        if let Some(ref uffd) = self.uffd {
            for region in &self.regions {
                if let Err(err) = uffd.remove_write_protection(region.ptr, region.len, true) {
                    warn!(
                        "LiveSnapshotGuard: failed to remove write-protection \
                         at {:?} len {}: {}",
                        region.ptr, region.len, err
                    );
                }
            }
            for region in &self.regions {
                if let Err(err) = uffd.unregister(region.ptr, region.len) {
                    warn!(
                        "LiveSnapshotGuard: failed to unregister UFFD region \
                         at {:?} len {}: {}",
                        region.ptr, region.len, err
                    );
                }
            }
        }
        // Resume VM if still paused.
        if self.paused
            && let Err(err) = self.vmm.resume_vcpus_only()
        {
            warn!("LiveSnapshotGuard: failed to resume vCPUs: {}", err);
        }
        // Kick devices if not yet done.
        if !self.devices_kicked {
            self.vmm.kick_devices();
        }
    }
}

/// Registers a memory range with the UFFD in write-protect mode.
fn uffd_register_wp(
    uffd: &Uffd,
    start: *mut libc::c_void,
    len: usize,
) -> Result<(), userfaultfd::Error> {
    use userfaultfd::RegisterMode;
    // Register with both MISSING and WP modes so that WP faults are reported.
    let mode = RegisterMode::MISSING | RegisterMode::WRITE_PROTECT;
    uffd.register_with_mode(start, len, mode)?;
    Ok(())
}

/// Page tracking entry for the live snapshot streaming phase.
struct PageEntry {
    /// Host pointer to the start of this page.
    ptr: *const u8,
    /// File offset where this page should be written.
    file_offset: u64,
    /// Size of this page.
    size: usize,
    /// Whether this page has been saved to the output file.
    saved: bool,
}

/// Creates a live snapshot of the microVM.
///
/// The VM is paused only briefly to save device/vCPU state and enable
/// write-protection. Memory is then streamed to the output file while
/// the VM continues running. Writes to not-yet-saved pages are trapped
/// via userfaultfd write-protect and saved before unblocking the vCPU.
pub fn create_live_snapshot(
    vmm: &mut Vmm,
    vm_info: &VmInfo,
    params: &CreateSnapshotParams,
) -> Result<(), CreateSnapshotError> {
    let page_size = vm_info.huge_pages.page_size();
    let t_start = std::time::Instant::now();

    // === Phase 1: PREPARE (VM still running) ===
    info!("Live snapshot: Phase 1 - Prepare");

    // Populate all RAM pages so PTEs exist (UFFDIO_WRITEPROTECT skips missing PTEs).
    let t_populate_start = std::time::Instant::now();
    vmm.vm.guest_memory().populate_pages(page_size);
    info!(
        "Live snapshot: populate_pages took {} us",
        t_populate_start.elapsed().as_micros()
    );

    // Open/truncate memory output file.
    let mut mem_file = OpenOptions::new()
        .create(true)
        .write(true)
        .read(true)
        .truncate(true)
        .open(&params.mem_file_path)
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("open", err))?;

    // Calculate total guest memory size for the file.
    let total_mem_size: u64 = vmm
        .vm
        .guest_memory()
        .iter()
        .flat_map(|region| region.slots())
        .map(|(slot, _)| slot.slice.len() as u64)
        .sum();
    mem_file
        .set_len(total_mem_size)
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("set_len", err))?;

    // Create UFFD with WP support.
    let uffd = UffdBuilder::new()
        .require_features(FeatureFlags::PAGEFAULT_FLAG_WP | FeatureFlags::EVENT_REMOVE)
        .close_on_exec(true)
        .non_blocking(true)
        .user_mode_only(false)
        .create()
        .map_err(CreateSnapshotError::UffdCreate)?;

    // Set up RAII guard for cleanup. From this point on, all access to vmm goes
    // through guard.vmm (which holds the mutable borrow).
    let mut guard = LiveSnapshotGuard::new(vmm);
    guard.uffd = Some(uffd);

    // === Phase 2: FREEZE (brief pause) ===
    let t_phase1 = t_start.elapsed();
    info!(
        "Live snapshot: Phase 1 took {} us",
        t_phase1.as_micros()
    );
    info!("Live snapshot: Phase 2 - Freeze");
    let t_freeze_start = std::time::Instant::now();

    guard
        .vmm
        .pause_vm()
        .map_err(CreateSnapshotError::VmmError)?;
    guard.paused = true;
    let t_after_pause = t_freeze_start.elapsed();

    // Save device + vCPU + KVM state.
    let microvm_state = guard
        .vmm
        .save_state(vm_info)
        .map_err(CreateSnapshotError::MicrovmState)?;
    let t_after_save_state = t_freeze_start.elapsed();

    // Register all plugged slots with UFFD in WP mode and enable write-protection.
    let uffd_ref = guard.uffd.as_ref().expect("uffd was just created");

    // guest_memory() returns a &GuestMemoryMmap (not an Arc), so the borrow cannot
    // be held across mutable borrows of guard.vmm (pause/resume). We re-bind it in
    // each phase where it's needed; the underlying data is the same throughout.
    {
        let guest_memory = guard.vmm.vm.guest_memory();
        for region in guest_memory.iter() {
            for slot in region.plugged_slots() {
                let ptr = slot.slice.ptr_guard().as_ptr() as *mut libc::c_void;
                let len = slot.slice.len();
                uffd_register_wp(uffd_ref, ptr, len)
                    .map_err(CreateSnapshotError::UffdRegister)?;
                uffd_ref
                    .write_protect(ptr, len)
                    .map_err(CreateSnapshotError::UffdWriteProtect)?;
                guard.regions.push(UffdRegion { ptr, len });
            }
        }
    }
    let t_after_wp = t_freeze_start.elapsed();

    // Resume vCPUs (without kicking devices to avoid deadlock).
    guard
        .vmm
        .resume_vcpus_only()
        .map_err(CreateSnapshotError::VmmError)?;
    guard.paused = false;
    let t_freeze_total = t_freeze_start.elapsed();
    info!(
        "Live snapshot: Phase 2 (freeze) took {} us \
         (pause={} us, save_state={} us, wp_enable={} us, resume={} us)",
        t_freeze_total.as_micros(),
        t_after_pause.as_micros(),
        (t_after_save_state - t_after_pause).as_micros(),
        (t_after_wp - t_after_save_state).as_micros(),
        (t_freeze_total - t_after_wp).as_micros(),
    );

    // === Phase 3: STREAM RAM (VM running) ===
    info!("Live snapshot: Phase 3 - Stream RAM");
    let t_stream_start = std::time::Instant::now();
    let mut fault_pages_saved = 0usize;

    // Build page tracking table.
    let mut pages: Vec<PageEntry> = Vec::new();
    let mut file_offset: u64 = 0;
    let guest_memory = guard.vmm.vm.guest_memory();
    for region in guest_memory.iter() {
        for (slot, plugged) in region.slots() {
            let slot_len = slot.slice.len();
            if plugged {
                let base_ptr = slot.slice.ptr_guard().as_ptr();
                for off in (0..slot_len).step_by(page_size) {
                    let actual_size = std::cmp::min(page_size, slot_len - off);
                    pages.push(PageEntry {
                        // SAFETY: base_ptr + off is within the guest memory slot.
                        ptr: unsafe { base_ptr.add(off) },
                        file_offset: file_offset + off as u64,
                        size: actual_size,
                        saved: false,
                    });
                }
            }
            file_offset += slot_len as u64;
        }
    }

    let total_pages = pages.len();
    let mut saved_count = 0usize;
    let mut linear_cursor = 0usize;

    // Build index for O(log n) page fault lookup instead of O(n) linear scan.
    let page_index: BTreeMap<usize, usize> = pages
        .iter()
        .enumerate()
        .map(|(i, p)| (p.ptr as usize, i))
        .collect();

    while saved_count < total_pages {
        // Non-blocking check for WP faults.
        let uffd_ref = guard.uffd.as_ref().expect("uffd exists");
        match uffd_ref.read_event() {
            Ok(Some(userfaultfd::Event::Pagefault { addr, .. })) => {
                // Find and save the faulted page using BTreeMap for O(log n) lookup.
                let fault_addr = addr as usize;
                if let Some((&page_start, &idx)) =
                    page_index.range(..=fault_addr).next_back()
                {
                    let page = &mut pages[idx];
                    if !page.saved && fault_addr < page_start + page.size {
                        save_page(&mut mem_file, page)?;
                        page.saved = true;
                        saved_count += 1;
                        fault_pages_saved += 1;

                        // Remove write-protection (unblocks the faulting vCPU).
                        let page_ptr = page.ptr as *mut libc::c_void;
                        uffd_ref
                            .remove_write_protection(page_ptr, page.size, true)
                            .map_err(CreateSnapshotError::UffdWriteProtect)?;
                    }
                }
            }
            Ok(Some(userfaultfd::Event::Remove { start, end })) => {
                // Balloon removed pages via madvise(MADV_DONTNEED).
                // Use BTreeMap range query to find only affected pages.
                let start_addr = start as usize;
                let end_addr = end as usize;
                for (&page_start, &idx) in page_index.range(start_addr..end_addr) {
                    let page = &mut pages[idx];
                    if !page.saved && page_start >= start_addr && page_start < end_addr {
                        // File is sparse — unwritten regions are already zero.
                        page.saved = true;
                        saved_count += 1;
                    }
                }
            }
            Ok(None) => {
                // No fault pending — save the next unsaved page from the linear scan.
                while linear_cursor < total_pages && pages[linear_cursor].saved {
                    linear_cursor += 1;
                }

                if linear_cursor < total_pages {
                    let page = &mut pages[linear_cursor];
                    save_page(&mut mem_file, page)?;
                    page.saved = true;
                    saved_count += 1;

                    // Remove write-protection for this page.
                    let page_ptr = page.ptr as *mut libc::c_void;
                    uffd_ref
                        .remove_write_protection(page_ptr, page.size, true)
                        .map_err(CreateSnapshotError::UffdWriteProtect)?;

                    linear_cursor += 1;
                }
            }
            Ok(Some(_)) => {
                // Other events (Fork, Remap, Unmap) — ignore.
            }
            Err(err) => {
                return Err(CreateSnapshotError::UffdReadEvent(err));
            }
        }
    }

    // === Phase 4: FINALIZE ===
    let t_stream_total = t_stream_start.elapsed();
    info!(
        "Live snapshot: Phase 3 (stream) took {} us, {} pages total \
         ({} fault-driven, {} linear-scan)",
        t_stream_total.as_micros(),
        total_pages,
        fault_pages_saved,
        total_pages - fault_pages_saved,
    );
    info!("Live snapshot: Phase 4 - Finalize");
    let t_finalize_start = std::time::Instant::now();

    // Kick devices (deferred from resume).
    //
    // Note: unlike create_snapshot(), we do NOT call mark_virtio_queue_memory_dirty() here.
    // Full/diff snapshots need it because they only dump dirty pages and might miss queue
    // memory that was modified outside the dirty-tracking path. Live snapshots dump ALL
    // guest memory pages, so virtio queue contents are captured as part of the streaming
    // loop. On restore, the canonical device/queue state comes from the snapshot file
    // (saved during Phase 2), not from the memory file, so any queue-page writes that
    // occurred between Phase 2 and Phase 3 page save are harmless.
    guard.vmm.kick_devices();
    guard.devices_kicked = true;

    // Unregister all memory from UFFD and drop it.
    if let Some(ref uffd) = guard.uffd {
        for region in &guard.regions {
            let _ = uffd.unregister(region.ptr, region.len);
        }
    }
    guard.regions.clear();
    guard.uffd = None;

    // Write device state to snapshot file.
    snapshot_state_to_file(&microvm_state, &params.snapshot_path)?;

    // Sync memory file.
    mem_file
        .sync_all()
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("sync_all", err))?;

    let t_total = t_start.elapsed();
    info!(
        "Live snapshot: Phase 4 (finalize) took {} us",
        t_finalize_start.elapsed().as_micros()
    );
    info!(
        "Live snapshot: complete in {} us (freeze/downtime={} us)",
        t_total.as_micros(),
        t_freeze_total.as_micros(),
    );
    Ok(())
}

/// Saves a single page to the memory file at the correct offset.
fn save_page(file: &File, page: &PageEntry) -> Result<(), CreateSnapshotError> {
    // SAFETY: page.ptr points to valid guest memory of page.size bytes.
    let data = unsafe { std::slice::from_raw_parts(page.ptr, page.size) };
    file.write_all_at(data, page.file_offset)
        .map_err(|err| CreateSnapshotError::MemoryBackingFile("write", err))?;
    Ok(())
}

fn snapshot_state_to_file(
    microvm_state: &MicrovmState,
    snapshot_path: &Path,
) -> Result<(), CreateSnapshotError> {
    use self::CreateSnapshotError::*;
    let mut snapshot_file = OpenOptions::new()
        .create(true)
        .write(true)
        .truncate(true)
        .open(snapshot_path)
        .map_err(|err| SnapshotBackingFile("open", err))?;

    let snapshot = Snapshot::new(microvm_state);
    snapshot.save(&mut snapshot_file)?;
    snapshot_file
        .flush()
        .map_err(|err| SnapshotBackingFile("flush", err))?;
    snapshot_file
        .sync_all()
        .map_err(|err| SnapshotBackingFile("sync_all", err))
}

/// Validates that snapshot CPU vendor matches the host CPU vendor.
///
/// # Errors
///
/// When:
/// - Failed to read host vendor.
/// - Failed to read snapshot vendor.
#[cfg(target_arch = "x86_64")]
pub fn validate_cpu_vendor(microvm_state: &MicrovmState) {
    let host_vendor_id = get_vendor_id_from_host();
    let snapshot_vendor_id = microvm_state.vcpu_states[0].cpuid.vendor_id();
    match (host_vendor_id, snapshot_vendor_id) {
        (Ok(host_id), Some(snapshot_id)) => {
            info!("Host CPU vendor ID: {host_id:?}");
            info!("Snapshot CPU vendor ID: {snapshot_id:?}");
            if host_id != snapshot_id {
                warn!("Host CPU vendor ID differs from the snapshotted one",);
            }
        }
        (Ok(host_id), None) => {
            info!("Host CPU vendor ID: {host_id:?}");
            warn!("Snapshot CPU vendor ID: couldn't get from the snapshot");
        }
        (Err(_), Some(snapshot_id)) => {
            warn!("Host CPU vendor ID: couldn't get from the host");
            info!("Snapshot CPU vendor ID: {snapshot_id:?}");
        }
        (Err(_), None) => {
            warn!("Host CPU vendor ID: couldn't get from the host");
            warn!("Snapshot CPU vendor ID: couldn't get from the snapshot");
        }
    }
}

/// Validate that Snapshot Manufacturer ID matches
/// the one from the Host
///
/// The manufacturer ID for the Snapshot is taken from each VCPU state.
/// # Errors
///
/// When:
/// - Failed to read host vendor.
/// - Failed to read snapshot vendor.
#[cfg(target_arch = "aarch64")]
pub fn validate_cpu_manufacturer_id(microvm_state: &MicrovmState) {
    let host_cpu_id = get_manufacturer_id_from_host();
    let snapshot_cpu_id = microvm_state.vcpu_states[0].regs.manifacturer_id();
    match (host_cpu_id, snapshot_cpu_id) {
        (Some(host_id), Some(snapshot_id)) => {
            info!("Host CPU manufacturer ID: {host_id:?}");
            info!("Snapshot CPU manufacturer ID: {snapshot_id:?}");
            if host_id != snapshot_id {
                warn!("Host CPU manufacturer ID differs from the snapshotted one",);
            }
        }
        (Some(host_id), None) => {
            info!("Host CPU manufacturer ID: {host_id:?}");
            warn!("Snapshot CPU manufacturer ID: couldn't get from the snapshot");
        }
        (None, Some(snapshot_id)) => {
            warn!("Host CPU manufacturer ID: couldn't get from the host");
            info!("Snapshot CPU manufacturer ID: {snapshot_id:?}");
        }
        (None, None) => {
            warn!("Host CPU manufacturer ID: couldn't get from the host");
            warn!("Snapshot CPU manufacturer ID: couldn't get from the snapshot");
        }
    }
}
/// Error type for [`snapshot_state_sanity_check`].
#[derive(Debug, thiserror::Error, displaydoc::Display, PartialEq, Eq)]
pub enum SnapShotStateSanityCheckError {
    /// No memory region defined.
    NoMemory,
    /// No DRAM memory region defined.
    NoDramMemory,
    /// DRAM memory has more than a single slot.
    DramMemoryTooManySlots,
    /// DRAM memory is unplugged.
    DramMemoryUnplugged,
}

/// Performs sanity checks against the state file and returns specific errors.
pub fn snapshot_state_sanity_check(
    microvm_state: &MicrovmState,
) -> Result<(), SnapShotStateSanityCheckError> {
    // Check that the snapshot contains at least 1 mem region, that at least one is Dram,
    // and that Dram region contains a single plugged slot.
    // Upper bound check will be done when creating guest memory by comparing against
    // KVM max supported value kvm_context.max_memslots().
    let regions = &microvm_state.vm_state.memory.regions;

    if regions.is_empty() {
        return Err(SnapShotStateSanityCheckError::NoMemory);
    }

    if !regions
        .iter()
        .any(|r| r.region_type == GuestRegionType::Dram)
    {
        return Err(SnapShotStateSanityCheckError::NoDramMemory);
    }

    for dram_region in regions
        .iter()
        .filter(|r| r.region_type == GuestRegionType::Dram)
    {
        if dram_region.plugged.len() != 1 {
            return Err(SnapShotStateSanityCheckError::DramMemoryTooManySlots);
        }

        if !dram_region.plugged[0] {
            return Err(SnapShotStateSanityCheckError::DramMemoryUnplugged);
        }
    }

    #[cfg(target_arch = "x86_64")]
    validate_cpu_vendor(microvm_state);
    #[cfg(target_arch = "aarch64")]
    validate_cpu_manufacturer_id(microvm_state);

    Ok(())
}

/// Error type for [`restore_from_snapshot`].
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum RestoreFromSnapshotError {
    /// Failed to get snapshot state from file: {0}
    File(#[from] SnapshotStateFromFileError),
    /// Invalid snapshot state: {0}
    Invalid(#[from] SnapShotStateSanityCheckError),
    /// Failed to load guest memory: {0}
    GuestMemory(#[from] RestoreFromSnapshotGuestMemoryError),
    /// Failed to build microVM from snapshot: {0}
    Build(#[from] BuildMicrovmFromSnapshotError),
}
/// Sub-Error type for [`restore_from_snapshot`] to contain either [`GuestMemoryFromFileError`] or
/// [`GuestMemoryFromUffdError`] within [`RestoreFromSnapshotError`].
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum RestoreFromSnapshotGuestMemoryError {
    /// Error creating guest memory from file: {0}
    File(#[from] GuestMemoryFromFileError),
    /// Error creating guest memory from uffd: {0}
    Uffd(#[from] GuestMemoryFromUffdError),
}

/// Loads a Microvm snapshot producing a 'paused' Microvm.
pub fn restore_from_snapshot(
    instance_info: &InstanceInfo,
    event_manager: &mut EventManager,
    seccomp_filters: &BpfThreadMap,
    params: &LoadSnapshotParams,
    vm_resources: &mut VmResources,
) -> Result<Arc<Mutex<Vmm>>, RestoreFromSnapshotError> {
    let mut microvm_state = snapshot_state_from_file(&params.snapshot_path)?;
    for entry in &params.network_overrides {
        microvm_state
            .device_states
            .mmio_state
            .net_devices
            .iter_mut()
            .map(|device| &mut device.device_state)
            .chain(
                microvm_state
                    .device_states
                    .pci_state
                    .net_devices
                    .iter_mut()
                    .map(|device| &mut device.device_state),
            )
            .find(|x| x.id == entry.iface_id)
            .map(|device_state| device_state.tap_if_name.clone_from(&entry.host_dev_name))
            .ok_or(SnapshotStateFromFileError::UnknownNetworkDevice)?;
    }
    let track_dirty_pages = params.track_dirty_pages;

    let vcpu_count = microvm_state
        .vcpu_states
        .len()
        .try_into()
        .map_err(|_| MachineConfigError::InvalidVcpuCount)
        .map_err(BuildMicrovmFromSnapshotError::VmUpdateConfig)?;

    vm_resources
        .update_machine_config(&MachineConfigUpdate {
            vcpu_count: Some(vcpu_count),
            mem_size_mib: Some(u64_to_usize(microvm_state.vm_info.mem_size_mib)),
            smt: Some(microvm_state.vm_info.smt),
            cpu_template: Some(microvm_state.vm_info.cpu_template),
            track_dirty_pages: Some(track_dirty_pages),
            huge_pages: Some(microvm_state.vm_info.huge_pages),
            #[cfg(feature = "gdb")]
            gdb_socket_path: None,
        })
        .map_err(BuildMicrovmFromSnapshotError::VmUpdateConfig)?;

    // Some sanity checks before building the microvm.
    snapshot_state_sanity_check(&microvm_state)?;

    let mem_backend_path = &params.mem_backend.backend_path;
    let mem_state = &microvm_state.vm_state.memory;

    let (guest_memory, uffd) = match params.mem_backend.backend_type {
        MemBackendType::File => {
            if vm_resources.machine_config.huge_pages.is_hugetlbfs() {
                return Err(RestoreFromSnapshotGuestMemoryError::File(
                    GuestMemoryFromFileError::HugetlbfsSnapshot,
                )
                .into());
            }
            (
                guest_memory_from_file(mem_backend_path, mem_state, track_dirty_pages)
                    .map_err(RestoreFromSnapshotGuestMemoryError::File)?,
                None,
            )
        }
        MemBackendType::Uffd => guest_memory_from_uffd(
            mem_backend_path,
            mem_state,
            track_dirty_pages,
            vm_resources.machine_config.huge_pages,
        )
        .map_err(RestoreFromSnapshotGuestMemoryError::Uffd)?,
    };
    builder::build_microvm_from_snapshot(
        instance_info,
        event_manager,
        microvm_state,
        guest_memory,
        uffd,
        seccomp_filters,
        vm_resources,
    )
    .map_err(RestoreFromSnapshotError::Build)
}

/// Error type for [`snapshot_state_from_file`]
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum SnapshotStateFromFileError {
    /// Failed to open snapshot file: {0}
    Open(#[from] std::io::Error),
    /// Failed to load snapshot state from file: {0}
    Load(#[from] crate::snapshot::SnapshotError),
    /// Unknown Network Device.
    UnknownNetworkDevice,
}

fn snapshot_state_from_file(
    snapshot_path: &Path,
) -> Result<MicrovmState, SnapshotStateFromFileError> {
    let mut snapshot_reader = File::open(snapshot_path)?;
    let snapshot = Snapshot::load(&mut snapshot_reader)?;

    Ok(snapshot.data)
}

/// Error type for [`guest_memory_from_file`].
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum GuestMemoryFromFileError {
    /// Failed to load guest memory: {0}
    File(#[from] std::io::Error),
    /// Failed to restore guest memory: {0}
    Restore(#[from] MemoryError),
    /// Cannot restore hugetlbfs backed snapshot by mapping the memory file. Please use uffd.
    HugetlbfsSnapshot,
}

fn guest_memory_from_file(
    mem_file_path: &Path,
    mem_state: &GuestMemoryState,
    track_dirty_pages: bool,
) -> Result<Vec<GuestRegionMmap>, GuestMemoryFromFileError> {
    let mem_file = File::open(mem_file_path)?;
    let guest_mem = memory::snapshot_file(mem_file, mem_state.regions(), track_dirty_pages)?;
    Ok(guest_mem)
}

/// Error type for [`guest_memory_from_uffd`]
#[derive(Debug, thiserror::Error, displaydoc::Display)]
pub enum GuestMemoryFromUffdError {
    /// Failed to restore guest memory: {0}
    Restore(#[from] MemoryError),
    /// Failed to create UFFD object: {0}
    Create(userfaultfd::Error),
    /// Failed to register memory address range with the userfaultfd object: {0}
    Register(userfaultfd::Error),
    /// Failed to connect to UDS Unix stream: {0}
    Connect(#[from] std::io::Error),
    /// Failed to send file descriptor: {0}
    Send(#[from] vmm_sys_util::errno::Error),
}

fn guest_memory_from_uffd(
    mem_uds_path: &Path,
    mem_state: &GuestMemoryState,
    track_dirty_pages: bool,
    huge_pages: HugePageConfig,
) -> Result<(Vec<GuestRegionMmap>, Option<Uffd>), GuestMemoryFromUffdError> {
    let (guest_memory, backend_mappings) =
        create_guest_memory(mem_state, track_dirty_pages, huge_pages)?;

    let mut uffd_builder = UffdBuilder::new();

    // We only make use of this if balloon devices are present, but we can enable it unconditionally
    // because the only place the kernel checks this is in a hook from madvise, e.g. it doesn't
    // actively change the behavior of UFFD, only passively. Without balloon devices
    // we never call madvise anyway, so no need to put this into a conditional.
    uffd_builder.require_features(FeatureFlags::EVENT_REMOVE);

    let uffd = uffd_builder
        .close_on_exec(true)
        .non_blocking(true)
        .user_mode_only(false)
        .create()
        .map_err(GuestMemoryFromUffdError::Create)?;

    for mem_region in guest_memory.iter() {
        uffd.register(mem_region.as_ptr().cast(), mem_region.size() as _)
            .map_err(GuestMemoryFromUffdError::Register)?;
    }

    send_uffd_handshake(mem_uds_path, &backend_mappings, &uffd)?;

    Ok((guest_memory, Some(uffd)))
}

fn create_guest_memory(
    mem_state: &GuestMemoryState,
    track_dirty_pages: bool,
    huge_pages: HugePageConfig,
) -> Result<(Vec<GuestRegionMmap>, Vec<GuestRegionUffdMapping>), GuestMemoryFromUffdError> {
    let guest_memory = memory::anonymous(mem_state.regions(), track_dirty_pages, huge_pages)?;
    let mut backend_mappings = Vec::with_capacity(guest_memory.len());
    let mut offset = 0;
    for mem_region in guest_memory.iter() {
        #[allow(deprecated)]
        backend_mappings.push(GuestRegionUffdMapping {
            base_host_virt_addr: mem_region.as_ptr() as u64,
            size: mem_region.size(),
            offset,
            page_size: huge_pages.page_size(),
            page_size_kib: huge_pages.page_size(),
        });
        offset += mem_region.size() as u64;
    }

    Ok((guest_memory, backend_mappings))
}

fn send_uffd_handshake(
    mem_uds_path: &Path,
    backend_mappings: &[GuestRegionUffdMapping],
    uffd: &impl AsRawFd,
) -> Result<(), GuestMemoryFromUffdError> {
    // This is safe to unwrap() because we control the contents of the vector
    // (i.e GuestRegionUffdMapping entries).
    let backend_mappings = serde_json::to_string(backend_mappings).unwrap();

    let socket = UnixStream::connect(mem_uds_path)?;
    socket.send_with_fd(
        backend_mappings.as_bytes(),
        // In the happy case we can close the fd since the other process has it open and is
        // using it to serve us pages.
        //
        // The problem is that if other process crashes/exits, firecracker guest memory
        // will simply revert to anon-mem behavior which would lead to silent errors and
        // undefined behavior.
        //
        // To tackle this scenario, the page fault handler can notify Firecracker of any
        // crashes/exits. There is no need for Firecracker to explicitly send its process ID.
        // The external process can obtain Firecracker's PID by calling `getsockopt` with
        // `libc::SO_PEERCRED` option like so:
        //
        // let mut val = libc::ucred { pid: 0, gid: 0, uid: 0 };
        // let mut ucred_size: u32 = mem::size_of::<libc::ucred>() as u32;
        // libc::getsockopt(
        //      socket.as_raw_fd(),
        //      libc::SOL_SOCKET,
        //      libc::SO_PEERCRED,
        //      &mut val as *mut _ as *mut _,
        //      &mut ucred_size as *mut libc::socklen_t,
        // );
        //
        // Per this linux man page: https://man7.org/linux/man-pages/man7/unix.7.html,
        // `SO_PEERCRED` returns the credentials (PID, UID and GID) of the peer process
        // connected to this socket. The returned credentials are those that were in effect
        // at the time of the `connect` call.
        //
        // Moreover, Firecracker holds a copy of the UFFD fd as well, so that even if the
        // page fault handler process does not tear down Firecracker when necessary, the
        // uffd will still be alive but with no one to serve faults, leading to guest freeze.
        uffd.as_raw_fd(),
    )?;

    // We prevent Rust from closing the socket file descriptor to avoid a potential race condition
    // between the mappings message and the connection shutdown. If the latter arrives at the UFFD
    // handler first, the handler never sees the mappings.
    forget(socket);

    Ok(())
}

#[cfg(test)]
mod tests {
    use std::os::unix::net::UnixListener;

    use vmm_sys_util::tempfile::TempFile;

    use super::*;
    use crate::Vmm;
    #[cfg(target_arch = "x86_64")]
    use crate::builder::tests::insert_vmclock_device;
    #[cfg(target_arch = "x86_64")]
    use crate::builder::tests::insert_vmgenid_device;
    use crate::builder::tests::{
        CustomBlockConfig, default_kernel_cmdline, default_vmm, insert_balloon_device,
        insert_block_devices, insert_net_device, insert_vsock_device,
    };
    #[cfg(target_arch = "aarch64")]
    use crate::construct_kvm_mpidrs;
    use crate::devices::virtio::block::CacheType;
    use crate::snapshot::Persist;
    use crate::vmm_config::balloon::BalloonDeviceConfig;
    use crate::vmm_config::net::NetworkInterfaceConfig;
    use crate::vmm_config::vsock::tests::default_config;
    use crate::vstate::memory::{GuestMemoryRegionState, GuestRegionType};

    fn default_vmm_with_devices() -> Vmm {
        let mut event_manager = EventManager::new().expect("Cannot create EventManager");
        let mut vmm = default_vmm();
        let mut cmdline = default_kernel_cmdline();

        // Add a balloon device.
        let balloon_config = BalloonDeviceConfig {
            amount_mib: 0,
            deflate_on_oom: false,
            stats_polling_interval_s: 0,
            free_page_hinting: false,
            free_page_reporting: false,
        };
        insert_balloon_device(&mut vmm, &mut cmdline, &mut event_manager, balloon_config);

        // Add a block device.
        let drive_id = String::from("root");
        let block_configs = vec![CustomBlockConfig::new(
            drive_id,
            true,
            None,
            true,
            CacheType::Unsafe,
        )];
        insert_block_devices(&mut vmm, &mut cmdline, &mut event_manager, block_configs);

        // Add net device.
        let network_interface = NetworkInterfaceConfig {
            iface_id: String::from("netif"),
            host_dev_name: String::from("hostname"),
            guest_mac: None,
            rx_rate_limiter: None,
            tx_rate_limiter: None,
        };
        insert_net_device(
            &mut vmm,
            &mut cmdline,
            &mut event_manager,
            network_interface,
        );

        // Add vsock device.
        let mut tmp_sock_file = TempFile::new().unwrap();
        tmp_sock_file.remove().unwrap();
        let vsock_config = default_config(&tmp_sock_file);

        insert_vsock_device(&mut vmm, &mut cmdline, &mut event_manager, vsock_config);

        #[cfg(target_arch = "x86_64")]
        insert_vmgenid_device(&mut vmm);
        #[cfg(target_arch = "x86_64")]
        insert_vmclock_device(&mut vmm);

        vmm
    }

    #[test]
    fn test_microvm_state_snapshot() {
        let vmm = default_vmm_with_devices();
        let states = vmm.device_manager.save();

        // Only checking that all devices are saved, actual device state
        // is tested by that device's tests.
        assert_eq!(states.mmio_state.block_devices.len(), 1);
        assert_eq!(states.mmio_state.net_devices.len(), 1);
        assert!(states.mmio_state.vsock_device.is_some());
        assert!(states.mmio_state.balloon_device.is_some());

        let vcpu_states = vec![VcpuState::default()];
        #[cfg(target_arch = "aarch64")]
        let mpidrs = construct_kvm_mpidrs(&vcpu_states);
        let microvm_state = MicrovmState {
            device_states: states,
            vcpu_states,
            kvm_state: Default::default(),
            vm_info: VmInfo {
                mem_size_mib: 1u64,
                ..Default::default()
            },
            #[cfg(target_arch = "aarch64")]
            vm_state: vmm.vm.save_state(&mpidrs).unwrap(),
            #[cfg(target_arch = "x86_64")]
            vm_state: vmm.vm.save_state().unwrap(),
        };

        let mut buf = vec![0; 10000];
        Snapshot::new(&microvm_state)
            .save(&mut buf.as_mut_slice())
            .unwrap();

        let restored_microvm_state: MicrovmState = Snapshot::load_without_crc_check(buf.as_slice())
            .unwrap()
            .data;

        assert_eq!(restored_microvm_state.vm_info, microvm_state.vm_info);
        assert_eq!(
            restored_microvm_state.device_states.mmio_state,
            microvm_state.device_states.mmio_state
        )
    }

    #[test]
    fn test_create_guest_memory() {
        let mem_state = GuestMemoryState {
            regions: vec![GuestMemoryRegionState {
                base_address: 0,
                size: 0x20000,
                region_type: GuestRegionType::Dram,
                plugged: vec![true],
            }],
        };

        let (_, uffd_regions) =
            create_guest_memory(&mem_state, false, HugePageConfig::None).unwrap();

        assert_eq!(uffd_regions.len(), 1);
        assert_eq!(uffd_regions[0].size, 0x20000);
        assert_eq!(uffd_regions[0].offset, 0);
        assert_eq!(uffd_regions[0].page_size, HugePageConfig::None.page_size());
    }

    #[test]
    fn test_send_uffd_handshake() {
        #[allow(deprecated)]
        let uffd_regions = vec![
            GuestRegionUffdMapping {
                base_host_virt_addr: 0,
                size: 0x100000,
                offset: 0,
                page_size: HugePageConfig::None.page_size(),
                page_size_kib: HugePageConfig::None.page_size(),
            },
            GuestRegionUffdMapping {
                base_host_virt_addr: 0x100000,
                size: 0x200000,
                offset: 0,
                page_size: HugePageConfig::Hugetlbfs2M.page_size(),
                page_size_kib: HugePageConfig::Hugetlbfs2M.page_size(),
            },
        ];

        let uds_path = TempFile::new().unwrap();
        let uds_path = uds_path.as_path();
        std::fs::remove_file(uds_path).unwrap();

        let listener = UnixListener::bind(uds_path).expect("Cannot bind to socket path");

        send_uffd_handshake(uds_path, &uffd_regions, &std::io::stdin()).unwrap();

        let (stream, _) = listener.accept().expect("Cannot listen on UDS socket");

        let mut message_buf = vec![0u8; 1024];
        let (bytes_read, _) = stream
            .recv_with_fd(&mut message_buf[..])
            .expect("Cannot recv_with_fd");
        message_buf.resize(bytes_read, 0);

        let deserialized: Vec<GuestRegionUffdMapping> =
            serde_json::from_slice(&message_buf).unwrap();

        assert_eq!(uffd_regions, deserialized);
    }
}
