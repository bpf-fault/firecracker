#!/usr/bin/env python3
# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate plots from live snapshot experiment results CSV.

Usage:
    python3 plot_experiment_results.py [path/to/experiment_results.csv]

Produces PNG files in the same directory as the CSV.
Requires: matplotlib (pip install matplotlib)
"""

import csv
import os
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MEM_SIZES = [256, 512, 1024, 2048, 4096]
WORKLOADS = ["idle", "light", "medium", "heavy"]
WORKLOAD_COLORS = {
    "idle": "#2196F3",
    "light": "#4CAF50",
    "medium": "#FF9800",
    "heavy": "#F44336",
}
FULL_COLOR = "#9E9E9E"
LIVE_COLOR = "#2196F3"


def _repo_root():
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


DEFAULT_CSV = os.path.join(_repo_root(), "test_results", "experiment_results.csv")


def load_and_group(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    grouped = defaultdict(list)
    for r in rows:
        key = (int(r["mem_size_mib"]), r["workload"], r["snapshot_mode"])
        grouped[key].append(r)
    return grouped


def avg(vals):
    vals = [float(v) for v in vals if v]
    return sum(vals) / len(vals) if vals else 0


def _savefig(fig, outdir, name):
    path = os.path.join(outdir, name)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


# ---------------------------------------------------------------------------
# Plot 1: Downtime vs Memory Size
# ---------------------------------------------------------------------------


def plot_downtime_vs_mem(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    # Full snapshot (single line — workload doesn't affect it since VM is paused)
    full_dts = [avg([r["full_total_ms"] for r in grouped[(m, "idle", "full")]]) for m in MEM_SIZES]
    ax.plot(MEM_SIZES, full_dts, "s--", color=FULL_COLOR, linewidth=2, markersize=8, label="Full (= downtime)")

    # Live snapshot per workload
    for wl in WORKLOADS:
        dts = [avg([r["downtime_us"] for r in grouped[(m, wl, "live")]]) / 1000 for m in MEM_SIZES]
        ax.plot(MEM_SIZES, dts, "o-", color=WORKLOAD_COLORS[wl], linewidth=2, markersize=7, label=f"Live ({wl})")

    ax.set_xlabel("VM Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Downtime (ms)", fontsize=12)
    ax.set_title("Snapshot Downtime vs VM Memory Size", fontsize=14, fontweight="bold")
    ax.legend(fontsize=10)
    ax.set_xticks(MEM_SIZES)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "01_downtime_vs_mem.png")


# ---------------------------------------------------------------------------
# Plot 2: Wall-Clock Time vs Memory Size
# ---------------------------------------------------------------------------


def plot_wallclock_vs_mem(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    full_wcs = [avg([r["full_total_ms"] for r in grouped[(m, "idle", "full")]]) for m in MEM_SIZES]
    ax.plot(MEM_SIZES, full_wcs, "s--", color=FULL_COLOR, linewidth=2, markersize=8, label="Full")

    for wl in WORKLOADS:
        wcs = [avg([r["total_us"] for r in grouped[(m, wl, "live")]]) / 1000 for m in MEM_SIZES]
        ax.plot(MEM_SIZES, wcs, "o-", color=WORKLOAD_COLORS[wl], linewidth=2, markersize=7, label=f"Live ({wl})")

    ax.set_xlabel("VM Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Wall-Clock Time (ms)", fontsize=12)
    ax.set_title("Total Snapshot Time vs VM Memory Size", fontsize=14, fontweight="bold")
    ax.legend(fontsize=10)
    ax.set_xticks(MEM_SIZES)
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "02_wallclock_vs_mem.png")


# ---------------------------------------------------------------------------
# Plot 3: Downtime Speedup vs Memory Size
# ---------------------------------------------------------------------------


def plot_speedup_vs_mem(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    for wl in WORKLOADS:
        speedups = []
        for m in MEM_SIZES:
            full_dt = avg([r["full_total_ms"] for r in grouped[(m, wl, "full")]])
            live_dt = avg([r["downtime_us"] for r in grouped[(m, wl, "live")]]) / 1000
            speedups.append(full_dt / live_dt if live_dt > 0 else 0)
        ax.plot(MEM_SIZES, speedups, "o-", color=WORKLOAD_COLORS[wl], linewidth=2, markersize=7, label=f"{wl}")

    ax.set_xlabel("VM Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Downtime Speedup (Full / Live)", fontsize=12)
    ax.set_title("Live Snapshot Downtime Speedup", fontsize=14, fontweight="bold")
    ax.legend(title="Workload", fontsize=10)
    ax.set_xticks(MEM_SIZES)
    ax.grid(True, alpha=0.3)
    ax.axhline(y=1, color="gray", linestyle=":", alpha=0.5)
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "03_speedup_vs_mem.png")


# ---------------------------------------------------------------------------
# Plot 4: Streaming Throughput vs Workload
# ---------------------------------------------------------------------------


def plot_throughput_vs_workload(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    x = range(len(WORKLOADS))
    width = 0.15
    offsets = [-2, -1, 0, 1, 2]
    mem_colors = ["#E3F2FD", "#90CAF9", "#42A5F5", "#1E88E5", "#0D47A1"]

    for i, mem in enumerate(MEM_SIZES):
        tps = [avg([r["throughput_mibs"] for r in grouped[(mem, wl, "live")]]) for wl in WORKLOADS]
        bars = ax.bar([xi + offsets[i] * width for xi in x], tps, width,
                      label=f"{mem} MiB", color=mem_colors[i], edgecolor="white")

    ax.set_xlabel("Workload Intensity", fontsize=12)
    ax.set_ylabel("Streaming Throughput (MiB/s)", fontsize=12)
    ax.set_title("Live Snapshot Streaming Throughput by Workload", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([w.capitalize() for w in WORKLOADS])
    ax.legend(title="VM Memory", fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "04_throughput_vs_workload.png")


# ---------------------------------------------------------------------------
# Plot 5: Fault Page Fraction vs Workload
# ---------------------------------------------------------------------------


def plot_faults_vs_workload(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    x = range(len(WORKLOADS))
    width = 0.15
    offsets = [-2, -1, 0, 1, 2]
    mem_colors = ["#E8F5E9", "#A5D6A7", "#66BB6A", "#2E7D32", "#1B5E20"]

    for i, mem in enumerate(MEM_SIZES):
        faults = [avg([r["fault_fraction_pct"] for r in grouped[(mem, wl, "live")]]) for wl in WORKLOADS]
        ax.bar([xi + offsets[i] * width for xi in x], faults, width,
               label=f"{mem} MiB", color=mem_colors[i], edgecolor="white")

    ax.set_xlabel("Workload Intensity", fontsize=12)
    ax.set_ylabel("Fault-Driven Pages (%)", fontsize=12)
    ax.set_title("Fault-Driven Page Fraction by Workload", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([w.capitalize() for w in WORKLOADS])
    ax.legend(title="VM Memory", fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "05_faults_vs_workload.png")


# ---------------------------------------------------------------------------
# Plot 6: Phase Breakdown Stacked Bar
# ---------------------------------------------------------------------------


def plot_phase_breakdown(grouped, outdir):
    fig, ax = plt.subplots(figsize=(14, 7))

    labels = []
    p1_vals, p2_vals, p3_vals, p4_vals = [], [], [], []

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            rr = grouped.get((mem, wl, "live"), [])
            if not rr:
                continue
            labels.append(f"{mem}\n{wl}")
            p1_vals.append(avg([r["phase1_us"] for r in rr]) / 1000)
            p2_vals.append(avg([r["freeze_us"] for r in rr]) / 1000)
            p3_vals.append(avg([r["stream_us"] for r in rr]) / 1000)
            p4_vals.append(avg([r["finalize_us"] for r in rr]) / 1000)

    x = range(len(labels))
    ax.bar(x, p1_vals, label="Phase 1 (prepare)", color="#BBDEFB")
    ax.bar(x, p2_vals, bottom=p1_vals, label="Phase 2 (freeze = downtime)", color="#F44336")
    bottoms_3 = [a + b for a, b in zip(p1_vals, p2_vals)]
    ax.bar(x, p3_vals, bottom=bottoms_3, label="Phase 3 (stream)", color="#64B5F6")
    bottoms_4 = [a + b for a, b in zip(bottoms_3, p3_vals)]
    ax.bar(x, p4_vals, bottom=bottoms_4, label="Phase 4 (finalize)", color="#E0E0E0")

    ax.set_xlabel("Configuration (Memory / Workload)", fontsize=11)
    ax.set_ylabel("Time (ms)", fontsize=12)
    ax.set_title("Live Snapshot Phase Breakdown", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=8)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis="y")

    _savefig(fig, outdir, "06_phase_breakdown.png")


# ---------------------------------------------------------------------------
# Plot 7: Freeze (Downtime) Breakdown
# ---------------------------------------------------------------------------


def plot_freeze_breakdown(grouped, outdir):
    fig, ax = plt.subplots(figsize=(10, 6))

    labels = []
    pause_vals, save_vals, wp_vals, resume_vals = [], [], [], []

    for mem in MEM_SIZES:
        rr = grouped.get((mem, "idle", "live"), [])
        if not rr:
            continue
        labels.append(f"{mem}")
        pause_vals.append(avg([r["pause_us"] for r in rr]) / 1000)
        save_vals.append(avg([r["save_state_us"] for r in rr]) / 1000)
        wp_vals.append(avg([r["wp_enable_us"] for r in rr]) / 1000)
        resume_vals.append(avg([r["resume_us"] for r in rr]) / 1000)

    x = range(len(labels))
    width = 0.5
    ax.bar(x, pause_vals, width, label="pause", color="#FFF9C4")
    ax.bar(x, save_vals, width, bottom=pause_vals, label="save_state", color="#FFE082")
    bottoms_wp = [a + b for a, b in zip(pause_vals, save_vals)]
    ax.bar(x, wp_vals, width, bottom=bottoms_wp, label="wp_enable", color="#F44336")
    bottoms_r = [a + b for a, b in zip(bottoms_wp, wp_vals)]
    ax.bar(x, resume_vals, width, bottom=bottoms_r, label="resume", color="#C8E6C9")

    ax.set_xlabel("VM Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Freeze Duration (ms)", fontsize=12)
    ax.set_title("Phase 2 (Freeze/Downtime) Breakdown — Idle Workload", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels(labels)
    ax.legend(fontsize=10)
    ax.grid(True, alpha=0.3, axis="y")

    _savefig(fig, outdir, "07_freeze_breakdown.png")


# ---------------------------------------------------------------------------
# Plot 8: Side-by-side downtime vs wall-clock (grouped bar)
# ---------------------------------------------------------------------------


def plot_downtime_vs_wallclock(grouped, outdir):
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6))

    x = range(len(MEM_SIZES))
    width = 0.35

    # Idle workload
    full_dt = [avg([r["full_total_ms"] for r in grouped[(m, "idle", "full")]]) for m in MEM_SIZES]
    live_dt = [avg([r["downtime_us"] for r in grouped[(m, "idle", "live")]]) / 1000 for m in MEM_SIZES]
    live_wc = [avg([r["total_us"] for r in grouped[(m, "idle", "live")]]) / 1000 for m in MEM_SIZES]

    bars1 = ax1.bar([xi - width / 2 for xi in x], full_dt, width, label="Full (DT = wall-clock)", color=FULL_COLOR)
    bars2 = ax1.bar([xi + width / 2 for xi in x], live_dt, width, label="Live (downtime)", color="#F44336")
    ax1.bar([xi + width / 2 for xi in x], [w - d for w, d in zip(live_wc, live_dt)],
            width, bottom=live_dt, label="Live (running during stream)", color="#90CAF9", alpha=0.7)

    ax1.set_title("Idle Workload", fontsize=13, fontweight="bold")
    ax1.set_xlabel("VM Memory (MiB)", fontsize=11)
    ax1.set_ylabel("Time (ms)", fontsize=11)
    ax1.set_xticks(x)
    ax1.set_xticklabels(MEM_SIZES)
    ax1.legend(fontsize=9)
    ax1.grid(True, alpha=0.3, axis="y")

    # Heavy workload
    full_dt_h = [avg([r["full_total_ms"] for r in grouped[(m, "heavy", "full")]]) for m in MEM_SIZES]
    live_dt_h = [avg([r["downtime_us"] for r in grouped[(m, "heavy", "live")]]) / 1000 for m in MEM_SIZES]
    live_wc_h = [avg([r["total_us"] for r in grouped[(m, "heavy", "live")]]) / 1000 for m in MEM_SIZES]

    ax2.bar([xi - width / 2 for xi in x], full_dt_h, width, label="Full (DT = wall-clock)", color=FULL_COLOR)
    ax2.bar([xi + width / 2 for xi in x], live_dt_h, width, label="Live (downtime)", color="#F44336")
    ax2.bar([xi + width / 2 for xi in x], [w - d for w, d in zip(live_wc_h, live_dt_h)],
            width, bottom=live_dt_h, label="Live (running during stream)", color="#90CAF9", alpha=0.7)

    ax2.set_title("Heavy Workload (~128 MiB/s)", fontsize=13, fontweight="bold")
    ax2.set_xlabel("VM Memory (MiB)", fontsize=11)
    ax2.set_xticks(x)
    ax2.set_xticklabels(MEM_SIZES)
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3, axis="y")

    fig.suptitle("Full vs Live Snapshot: Downtime (red) vs Total Time", fontsize=14, fontweight="bold", y=1.02)
    fig.tight_layout()
    _savefig(fig, outdir, "08_downtime_vs_wallclock.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV

    if not os.path.isfile(csv_path):
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    outdir = os.path.dirname(os.path.abspath(csv_path))
    grouped = load_and_group(csv_path)

    print(f"Generating plots from {csv_path}")
    print(f"Output directory: {outdir}")
    print()

    plot_downtime_vs_mem(grouped, outdir)
    plot_wallclock_vs_mem(grouped, outdir)
    plot_speedup_vs_mem(grouped, outdir)
    plot_throughput_vs_workload(grouped, outdir)
    plot_faults_vs_workload(grouped, outdir)
    plot_phase_breakdown(grouped, outdir)
    plot_freeze_breakdown(grouped, outdir)
    plot_downtime_vs_wallclock(grouped, outdir)

    print()
    print(f"Done! {8} plots saved to {outdir}/")


if __name__ == "__main__":
    main()
