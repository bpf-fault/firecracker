// Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

//! Build script for the vmm crate.
//!
//! Compiles the BPF program `resources/bpf/snapshot_fault_ops.bpf.c` into
//! `resources/bpf/snapshot_fault_ops.bpf.o` so that it can be embedded via
//! `include_bytes!` at compile time. The compilation requires `bpftool` (for
//! generating `vmlinux.h` from kernel BTF) and `clang` (for compiling the BPF
//! C source). If either tool is missing or the kernel BTF is unavailable, the
//! build script prints a cargo warning and falls back to any pre-existing
//! `.bpf.o` file.

use std::path::{Path, PathBuf};
use std::process::Command;

/// Workspace root, two levels up from `src/vmm/`.
fn workspace_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .canonicalize()
        .expect("failed to canonicalize workspace root")
}

/// Returns the BPF target architecture define based on the cargo build target.
fn bpf_target_arch() -> &'static str {
    let target_arch =
        std::env::var("CARGO_CFG_TARGET_ARCH").unwrap_or_else(|_| String::from("x86_64"));
    match target_arch.as_str() {
        "x86_64" => "__TARGET_ARCH_x86",
        "aarch64" => "__TARGET_ARCH_aarch64",
        other => {
            println!(
                "cargo::warning=Unsupported target architecture for BPF compilation: {other}"
            );
            "__TARGET_ARCH_x86"
        }
    }
}

/// Check whether a command exists on `$PATH`.
fn has_command(name: &str) -> bool {
    Command::new("which")
        .arg(name)
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

fn main() {
    let root = workspace_root();
    let bpf_dir = root.join("resources").join("bpf");
    let bpf_src = bpf_dir.join("snapshot_fault_ops.bpf.c");
    let bpf_obj = bpf_dir.join("snapshot_fault_ops.bpf.o");
    let vmlinux_h = bpf_dir.join("vmlinux.h");
    let kernel_btf = Path::new("/sys/kernel/btf/vmlinux");

    // Tell cargo to rerun this build script when the BPF source changes.
    println!("cargo::rerun-if-changed={}", bpf_src.display());
    // Also rerun if kernel BTF changes (e.g. kernel upgrade).
    println!("cargo::rerun-if-changed={}", kernel_btf.display());

    // ── Check prerequisites ──────────────────────────────────────────────

    // Skipping compilation is only worth a warning when there is no
    // pre-existing object to fall back to (the include_bytes! below
    // would then fail).
    if !has_command("bpftool") {
        if !bpf_obj.exists() {
            println!("cargo::warning=bpftool not found and no pre-existing .bpf.o; BPF snapshot support cannot build");
        }
        return;
    }

    if !has_command("clang") {
        if !bpf_obj.exists() {
            println!("cargo::warning=clang not found and no pre-existing .bpf.o; BPF snapshot support cannot build");
        }
        return;
    }

    // ── Step 1: Generate vmlinux.h from kernel BTF ───────────────────────

    if kernel_btf.exists() {
        let output = Command::new("bpftool")
            .args(["btf", "dump", "file"])
            .arg(kernel_btf)
            .args(["format", "c"])
            .output()
            .expect("failed to execute bpftool");

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr);
            println!(
                "cargo::warning=bpftool btf dump failed: {stderr}; skipping BPF compilation"
            );
            return;
        }

        std::fs::write(&vmlinux_h, &output.stdout).expect("failed to write vmlinux.h");
    } else {
        // No kernel BTF available. Check if vmlinux.h already exists (e.g.
        // pre-generated). If not, we cannot compile the BPF program.
        if !vmlinux_h.exists() {
            println!(
                "cargo::warning=Kernel BTF not available at {path} and no pre-existing vmlinux.h; \
                 skipping BPF compilation",
                path = kernel_btf.display()
            );
            return;
        }
        println!(
            "cargo::warning=Kernel BTF not available; using pre-existing vmlinux.h for BPF \
             compilation"
        );
    }

    // ── Step 2: Compile the BPF program with clang ───────────────────────

    let arch_define = bpf_target_arch();

    let status = Command::new("clang")
        .args(["-O2", "-g", "-target", "bpf"])
        .arg(format!("-D{arch_define}"))
        .arg("-I/usr/local/include")
        .arg(format!("-I{}", bpf_dir.display()))
        .arg("-c")
        .arg(&bpf_src)
        .arg("-o")
        .arg(&bpf_obj)
        .status()
        .expect("failed to execute clang");

    if !status.success() {
        // If clang failed but a pre-existing .bpf.o exists, warn but don't
        // fail the build.
        if bpf_obj.exists() {
            println!(
                "cargo::warning=clang compilation of BPF program failed; using pre-existing .bpf.o"
            );
        } else {
            panic!(
                "clang compilation of BPF program failed and no pre-existing .bpf.o found at {}",
                bpf_obj.display()
            );
        }
    }
}
