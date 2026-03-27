// Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! Pre-spawned worker thread for live snapshot execution.
//!
//! The worker thread is spawned **before** seccomp filters are applied (the VMM
//! thread's filter does not allow `clone`/`clone3`). At boot it runs
//! [`populate_pages`](crate::vstate::memory::GuestMemory::populate_pages) to
//! fault in guest memory PTEs (replacing the old `fc_populate` thread), then
//! enters a receive loop waiting for [`SnapshotJob`] submissions.
//!
//! When a job arrives the worker executes all four snapshot phases:
//!   - **Phase 1** — file creation, fallocate, BPF/UFFD setup (no Vmm lock)
//!   - **Phase 2** — pause VM, save state, enable WP, resume (brief Vmm lock)
//!   - **Phase 3** — stream RAM to disk (no Vmm lock)
//!   - **Phase 4** — finalize (no Vmm lock)
//!
//! This keeps the VMM event-loop thread free to process device I/O throughout.

use std::os::unix::io::AsRawFd;
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::Instant;

use vmm_sys_util::eventfd::EventFd;

use crate::vstate::memory::GuestMemoryExtension;

use crate::Vmm;
use crate::logger::{error, info};
use crate::persist::{
    CreateSnapshotError, VmInfo, phase1_prepare_uffd, phase2_freeze_uffd,
};
use crate::bpf_live_snapshot::{phase1_prepare_bpf, phase2_freeze_bpf};
use crate::vmm_config::snapshot::{CreateSnapshotParams, SnapshotType};

/// Trait for snapshot streaming tasks that run on the worker thread.
///
/// Both BPF and UFFD streaming tasks implement this trait.
pub trait StreamingTask: Send {
    /// Execute the streaming phase (Phase 3 + Phase 4 minus kick_devices).
    fn run(self: Box<Self>) -> Result<SnapshotTimings, CreateSnapshotError>;
}

/// Timing and statistics collected during snapshot streaming.
#[derive(Debug)]
pub struct SnapshotTimings {
    /// Total wall-clock time of the snapshot (from Phase 1 start).
    pub total_us: u128,
    /// Time spent in Phase 2 (freeze/downtime).
    pub freeze_us: u128,
    /// Time spent in Phase 3 (streaming RAM).
    pub stream_us: u128,
    /// Time spent in Phase 4 (finalize).
    pub finalize_us: u128,
    /// Total pages saved.
    pub total_pages: usize,
    /// Method-specific detail string for logging.
    pub detail: String,
}

/// A full snapshot job submitted from the VMM thread.
///
/// Contains everything the worker needs to execute Phases 1–4 independently.
pub struct SnapshotJob {
    /// Handle to the VMM (locked briefly for Phase 2 only).
    pub vmm: Arc<Mutex<Vmm>>,
    /// VM configuration info (huge pages, memory size, etc.).
    pub vm_info: VmInfo,
    /// Snapshot paths and type.
    pub params: CreateSnapshotParams,
}

/// Message sent from the VMM thread to the worker thread.
enum WorkerMessage {
    /// Execute a full snapshot job (Phases 1–4).
    ExecuteSnapshot(SnapshotJob),
    /// Execute a pre-built streaming task (legacy synchronous fallback).
    Execute(Box<dyn StreamingTask>),
    /// Shut down the worker thread.
    Shutdown,
}

/// Handle to the pre-spawned snapshot worker thread.
///
/// Created before seccomp filters are applied. Lives for the duration of the
/// VMM process.
pub struct SnapshotWorkerHandle {
    /// Send jobs/tasks to the worker.
    task_tx: mpsc::SyncSender<WorkerMessage>,
    /// Receive results from the worker.
    result_rx: mpsc::Receiver<Result<SnapshotTimings, CreateSnapshotError>>,
    /// EventFd signaled by the worker when a task completes.
    /// The event manager clones this fd (via `dup()`) and registers it with epoll.
    pub completion_fd: EventFd,
}

impl std::fmt::Debug for SnapshotWorkerHandle {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SnapshotWorkerHandle")
            .field("completion_fd", &self.completion_fd.as_raw_fd())
            .finish()
    }
}

impl SnapshotWorkerHandle {
    /// Spawns the worker thread.
    ///
    /// Must be called **before** seccomp filters are applied to the VMM thread.
    ///
    /// If `populate_args` is `Some((vm, page_size))`, the worker thread runs
    /// `populate_pages` before entering the receive loop (replacing the old
    /// `fc_populate` thread).  Pass `None` for the snapshot-restore path where
    /// page population is not needed.
    pub fn spawn(
        populate_args: Option<(Arc<crate::vstate::vm::Vm>, usize)>,
    ) -> (Self, JoinHandle<()>) {
        let (task_tx, task_rx) = mpsc::sync_channel::<WorkerMessage>(1);
        let (result_tx, result_rx) = mpsc::sync_channel(1);
        let completion_fd = EventFd::new(libc::EFD_NONBLOCK).expect("Failed to create EventFd");
        // Clone the EventFd for the worker thread AND for the event manager
        // BEFORE seccomp is applied (try_clone uses fcntl(F_DUPFD_CLOEXEC)
        // which the VMM seccomp filter does not allow).
        let worker_completion_fd = completion_fd
            .try_clone()
            .expect("Failed to clone EventFd for worker thread");

        let join_handle = std::thread::Builder::new()
            .name("fc_snap_work".to_owned())
            .spawn(move || {
                // Run populate_pages before entering the task loop so that
                // all guest memory PTEs exist by the time the first snapshot
                // job arrives.
                if let Some((vm, page_size)) = populate_args {
                    info!("Snapshot worker: populating guest memory pages");
                    let t0 = Instant::now();
                    vm.guest_memory().populate_pages(page_size);
                    info!(
                        "Snapshot worker: populate_pages took {} us",
                        t0.elapsed().as_micros()
                    );
                }
                worker_loop(task_rx, result_tx, worker_completion_fd);
            })
            .expect("Failed to spawn snapshot worker thread");

        let handle = Self {
            task_tx,
            result_rx,
            completion_fd,
        };
        (handle, join_handle)
    }

    /// Submits a full snapshot job to the worker thread.
    ///
    /// The worker executes all four phases.  The VMM thread can return to its
    /// event loop immediately after this call.
    pub fn submit_snapshot_job(
        &self,
        job: SnapshotJob,
    ) -> Result<(), CreateSnapshotError> {
        self.task_tx
            .send(WorkerMessage::ExecuteSnapshot(job))
            .map_err(|_| {
                CreateSnapshotError::WorkerUnavailable(
                    "Snapshot worker thread is not running".to_string(),
                )
            })
    }

    /// Submits a pre-built streaming task to the worker thread (legacy path).
    pub fn submit(
        &self,
        task: Box<dyn StreamingTask>,
    ) -> Result<(), CreateSnapshotError> {
        self.task_tx
            .send(WorkerMessage::Execute(task))
            .map_err(|_| {
                CreateSnapshotError::WorkerUnavailable(
                    "Snapshot worker thread is not running".to_string(),
                )
            })
    }

    /// Collects the result after the completion EventFd fires.
    ///
    /// This is non-blocking (the result is already available because the
    /// EventFd was signaled).
    pub fn collect_result(&self) -> Result<SnapshotTimings, CreateSnapshotError> {
        self.result_rx.try_recv().map_err(|_| {
            CreateSnapshotError::WorkerUnavailable(
                "Failed to receive result from snapshot worker thread".to_string(),
            )
        })?
    }
}

impl Drop for SnapshotWorkerHandle {
    fn drop(&mut self) {
        let _ = self.task_tx.send(WorkerMessage::Shutdown);
    }
}

/// Executes a full snapshot job (Phases 1–4) on the worker thread.
fn execute_full_snapshot(job: SnapshotJob) -> Result<SnapshotTimings, CreateSnapshotError> {
    let t_start = Instant::now();

    // Get Arc<Vm> with a brief lock — no heavy work under the mutex.
    let vm = job.vmm.lock().unwrap().vm.clone();

    let snapshot_type = job.params.snapshot_type;

    match snapshot_type {
        SnapshotType::Live => {
            // Phase 1: file + UFFD (no Vmm lock).
            let phase1 = phase1_prepare_uffd(&vm, &job.params)?;
            let t_phase1 = t_start.elapsed();
            info!("Live snapshot: Phase 1 took {} us", t_phase1.as_micros());

            // Phase 2: freeze (brief Vmm lock).
            let task = {
                let mut locked = job.vmm.lock().unwrap();
                phase2_freeze_uffd(&mut locked, &job.vm_info, phase1, &job.params, t_start)?
            };

            // Phase 3 + 4: stream + finalize (no Vmm lock).
            Box::new(task).run()
        }
        SnapshotType::LiveBpf => {
            // Phase 1: file + BPF load (no Vmm lock).
            let phase1 = phase1_prepare_bpf(&vm, &job.vm_info, &job.params)?;
            let t_phase1 = t_start.elapsed();
            info!("Live-BPF snapshot: Phase 1 took {} us", t_phase1.as_micros());

            // Phase 2: freeze (brief Vmm lock).
            let task = {
                let mut locked = job.vmm.lock().unwrap();
                phase2_freeze_bpf(&mut locked, &job.vm_info, phase1, &job.params, t_start)?
            };

            // Phase 3 + 4: stream + finalize (no Vmm lock).
            Box::new(task).run()
        }
        _ => Err(CreateSnapshotError::WorkerUnavailable(format!(
            "Unsupported snapshot type for worker: {snapshot_type:?}"
        ))),
    }
}

/// The worker thread's main loop.
///
/// Blocks on `task_rx.recv()` until a job/task arrives, executes it, sends the
/// result back, and signals the completion EventFd.
fn worker_loop(
    task_rx: mpsc::Receiver<WorkerMessage>,
    result_tx: mpsc::SyncSender<Result<SnapshotTimings, CreateSnapshotError>>,
    completion_fd: EventFd,
) {
    loop {
        match task_rx.recv() {
            Ok(WorkerMessage::ExecuteSnapshot(job)) => {
                info!("Snapshot worker: starting full snapshot job");
                let result = execute_full_snapshot(job);
                match &result {
                    Ok(timings) => {
                        info!(
                            "Snapshot worker: snapshot complete, {} pages in {} us",
                            timings.total_pages, timings.stream_us
                        );
                    }
                    Err(err) => {
                        error!("Snapshot worker: snapshot failed: {}", err);
                    }
                }
                let _ = result_tx.send(result);
                if let Err(err) = completion_fd.write(1) {
                    error!("Snapshot worker: failed to signal completion: {}", err);
                }
            }
            Ok(WorkerMessage::Execute(task)) => {
                info!("Snapshot worker: starting streaming task");
                let result = task.run();
                match &result {
                    Ok(timings) => {
                        info!(
                            "Snapshot worker: streaming complete, {} pages in {} us",
                            timings.total_pages, timings.stream_us
                        );
                    }
                    Err(err) => {
                        error!("Snapshot worker: streaming failed: {}", err);
                    }
                }
                let _ = result_tx.send(result);
                if let Err(err) = completion_fd.write(1) {
                    error!("Snapshot worker: failed to signal completion: {}", err);
                }
            }
            Ok(WorkerMessage::Shutdown) | Err(_) => {
                info!("Snapshot worker: shutting down");
                break;
            }
        }
    }
}
