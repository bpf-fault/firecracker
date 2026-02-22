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

# Application workloads (reduced matrix per design doc §6.2)
APP_MEM_SIZES = [512, 2048]
APP_WORKLOADS = [
    "redis_light", "redis_mixed", "redis_heavy",
    "memcached_light", "memcached_heavy",
]
STREAM_KERNELS = ["copy", "scale", "add", "triad"]
APP_WORKLOAD_COLORS = {
    "redis_light":       "#B3E5FC",
    "redis_mixed":       "#0288D1",
    "redis_heavy":       "#01579B",
    "memcached_light":   "#C8E6C9",
    "memcached_heavy":   "#2E7D32",
    "stream":            "#FF6F00",
}


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
# Plot 9: Application ops/sec degradation (live vs full)
# ---------------------------------------------------------------------------


def plot_app_ops_degradation(grouped, outdir):
    """Grouped bar chart of ops/sec degradation for Redis and Memcached workloads."""
    # Check whether any app workload data exists.
    if not any(k[1] in APP_WORKLOADS for k in grouped):
        print("  Skipping plot 9: no app workload data in CSV")
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    n_wl = len(APP_WORKLOADS)
    n_mem = len(APP_MEM_SIZES)
    total_bars = n_mem * 2 + 1  # mem sizes × (full + live) + gap
    width = 0.15
    x = range(n_wl)

    mem_offsets = {
        512:  -1.5,
        2048: -0.5,
    }
    # Full snapshot is always 100 % degradation (VM paused).
    full_offsets = {
        512:  0.5,
        2048: 1.5,
    }

    for i, mem in enumerate(APP_MEM_SIZES):
        live_vals = []
        full_vals = []
        for wl in APP_WORKLOADS:
            live_rows = grouped.get((mem, wl, "live"), [])
            live_vals.append(avg([r.get("app_ops_degradation_pct", 0) for r in live_rows]))
            full_vals.append(100.0)  # full snapshot always 100 % degradation

        color = APP_WORKLOAD_COLORS.get(APP_WORKLOADS[0], "#888888")
        live_color = list(APP_WORKLOAD_COLORS.values())[i * 2]
        ax.bar(
            [xi + mem_offsets[mem] * width for xi in x],
            live_vals, width,
            label=f"Live {mem} MiB",
            color=live_color,
            edgecolor="white",
        )
        ax.bar(
            [xi + full_offsets[mem] * width for xi in x],
            full_vals, width,
            label=f"Full {mem} MiB (always 100%)",
            color=FULL_COLOR,
            alpha=0.5,
            edgecolor="white",
        )

    ax.set_xlabel("Application Workload", fontsize=12)
    ax.set_ylabel("Ops/sec Degradation During Snapshot (%)", fontsize=12)
    ax.set_title("Application Ops/sec Degradation: Full vs Live Snapshot", fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([w.replace("_", "\n") for w in APP_WORKLOADS], fontsize=9)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(0, 110)

    _savefig(fig, outdir, "09_app_ops_degradation.png")


# ---------------------------------------------------------------------------
# Plot 10: Application tail latency (baseline vs during snapshot)
# ---------------------------------------------------------------------------


def plot_app_tail_latency(grouped, outdir):
    """Three-bar grouped chart: baseline / during / post p99 and avg latency."""
    if not any(k[1] in APP_WORKLOADS for k in grouped):
        print("  Skipping plot 10: no app workload data in CSV")
        return

    fig, axes = plt.subplots(1, 2, figsize=(18, 6))

    x = range(len(APP_WORKLOADS))
    width = 0.12
    # Three windows × two memory sizes = 6 bar clusters per workload.
    # Offsets: base-512, dur-512, post-512, base-2048, dur-2048, post-2048
    offsets_512  = [-2.5, -1.5, -0.5]
    offsets_2048 = [ 0.5,  1.5,  2.5]
    mem_palettes = {
        512:  ["#90CAF9", "#EF9A9A", "#A5D6A7"],   # blue / red / green
        2048: ["#1565C0", "#B71C1C", "#2E7D32"],
    }
    window_labels = ["Baseline", "During snap", "Post-snap"]

    for ax, metric, ylabel, title_suffix in [
        (axes[0], "app_{}_p99_us",   "p99 Latency (µs)",     "p99"),
        (axes[1], "app_{}_avg_us",   "Avg Latency (µs)",     "Avg"),
    ]:
        # Override field names for post-snap (different prefix).
        def _get(rows, window, m):
            if window == "baseline":
                return avg([r.get(f"app_baseline_{m}", 0) for r in rows])
            if window == "during":
                return avg([r.get(f"app_during_{m}", 0) for r in rows])
            # post
            field = "post_snap_p99_us" if "p99" in m else "post_snap_avg_us"
            return avg([r.get(field, 0) for r in rows])

        for mem, offsets in [(512, offsets_512), (2048, offsets_2048)]:
            palette = mem_palettes[mem]
            for j, (window, label) in enumerate(
                [("baseline", "Baseline"), ("during", "During"), ("post", "Post-snap")]
            ):
                vals = [_get(grouped.get((mem, wl, "live"), []), window,
                             "p99_us" if "p99" in metric else "avg_us")
                        for wl in APP_WORKLOADS]
                ax.bar(
                    [xi + offsets[j] * width for xi in x], vals, width,
                    label=f"{label} {mem} MiB", color=palette[j], edgecolor="white",
                )

        ax.set_xlabel("Application Workload", fontsize=11)
        ax.set_ylabel(ylabel, fontsize=11)
        ax.set_title(f"{title_suffix} Latency — Baseline / During / Post (Live snapshot)",
                     fontsize=12, fontweight="bold")
        ax.set_xticks(x)
        ax.set_xticklabels([w.replace("_", "\n") for w in APP_WORKLOADS], fontsize=8)
        ax.legend(fontsize=8, ncol=2)
        ax.grid(True, alpha=0.3, axis="y")
        ax.set_ylim(bottom=0)

    fig.tight_layout()
    _savefig(fig, outdir, "10_app_tail_latency.png")


# ---------------------------------------------------------------------------
# Plot 11: STREAM memory bandwidth (baseline vs during snapshot)
# ---------------------------------------------------------------------------


def plot_stream_bandwidth(grouped, outdir):
    """Three-bar grouped chart of STREAM kernel bandwidth: baseline/during/post."""
    if not any(k[1] == "stream" for k in grouped):
        print("  Skipping plot 11: no STREAM workload data in CSV")
        return

    fig, ax = plt.subplots(figsize=(14, 6))

    n_kernels = len(STREAM_KERNELS)
    x = range(n_kernels)
    width = 0.12
    # Three windows × two memory sizes = 6 bar positions per kernel.
    palettes = {
        512:  ["#A5D6A7", "#FFCC80", "#90CAF9"],   # green / orange / blue
        2048: ["#1B5E20", "#E65100", "#0D47A1"],
    }
    offsets_512  = [-2.5, -1.5, -0.5]
    offsets_2048 = [ 0.5,  1.5,  2.5]

    for mem, offsets in [(512, offsets_512), (2048, offsets_2048)]:
        live_rows = grouped.get((mem, "stream", "live"), [])
        windows = [
            ("baseline", f"Baseline {mem} MiB"),
            ("during",   f"During snap {mem} MiB"),
            ("post",     f"Post-snap {mem} MiB"),
        ]
        for j, (window, label) in enumerate(windows):
            if window == "post":
                vals = [avg([r.get(f"stream_post_{k}_mibs", 0) for r in live_rows])
                        for k in STREAM_KERNELS]
            else:
                vals = [avg([r.get(f"stream_{window}_{k}_mibs", 0) for r in live_rows])
                        for k in STREAM_KERNELS]
            ax.bar(
                [xi + offsets[j] * width for xi in x], vals, width,
                label=label, color=palettes[mem][j], edgecolor="white",
            )

        # Annotate overall Triad mean ± stddev above the Triad group.
        triad_idx = STREAM_KERNELS.index("triad")
        ov_mean = avg([r.get("overall_triad_mean_mibs",   0) for r in live_rows])
        ov_std  = avg([r.get("overall_triad_stddev_mibs", 0) for r in live_rows])
        if ov_mean > 0:
            ax.annotate(
                f"Overall\n{ov_mean:.0f}±{ov_std:.0f}",
                xy=(triad_idx + offsets_2048[1] * width if mem == 2048
                    else triad_idx + offsets_512[1] * width, ov_mean),
                xytext=(0, 18), textcoords="offset points",
                ha="center", fontsize=7, color=palettes[mem][1],
            )

    ax.set_xlabel("STREAM Kernel", fontsize=12)
    ax.set_ylabel("Bandwidth (MiB/s)", fontsize=12)
    ax.set_title("STREAM Benchmark Bandwidth: Baseline / During / Post Snapshot",
                 fontsize=14, fontweight="bold")
    ax.set_xticks(x)
    ax.set_xticklabels([k.capitalize() for k in STREAM_KERNELS])
    ax.legend(fontsize=9, ncol=2)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "11_stream_bandwidth.png")


# ---------------------------------------------------------------------------
# Plot 12: Fault fraction comparison — synthetic, app, and STREAM workloads
# ---------------------------------------------------------------------------


def plot_fault_fraction_comparison(grouped, outdir):
    """Bar chart comparing fault_fraction_pct across all workload types and memory sizes."""
    all_workloads = WORKLOADS + APP_WORKLOADS + ["stream"]
    all_mem = MEM_SIZES  # synthetic uses full matrix; app uses subset

    # Collect only combinations that have data.
    data_by_wl = {}
    for wl in all_workloads:
        per_mem = {}
        for mem in all_mem:
            rows = grouped.get((mem, wl, "live"), [])
            vals = [float(r.get("fault_fraction_pct", 0)) for r in rows if r.get("fault_fraction_pct")]
            if vals:
                per_mem[mem] = sum(vals) / len(vals)
        if per_mem:
            data_by_wl[wl] = per_mem

    if not data_by_wl:
        print("  Skipping plot 12: no fault_fraction_pct data in CSV")
        return

    # Build plot: one group per workload, bars per memory size.
    present_wls = list(data_by_wl.keys())
    present_mems = sorted({m for d in data_by_wl.values() for m in d})
    n_wl = len(present_wls)
    n_mem = len(present_mems)
    width = 0.8 / n_mem
    x = range(n_wl)

    mem_palette = ["#E3F2FD", "#90CAF9", "#42A5F5", "#1E88E5", "#0D47A1"]
    fig, ax = plt.subplots(figsize=(max(12, n_wl * 1.5), 6))

    for j, mem in enumerate(present_mems):
        vals = [data_by_wl[wl].get(mem, 0) for wl in present_wls]
        offset = (j - n_mem / 2 + 0.5) * width
        color = mem_palette[j % len(mem_palette)]
        ax.bar(
            [xi + offset for xi in x], vals, width,
            label=f"{mem} MiB", color=color, edgecolor="white",
        )

    ax.set_xlabel("Workload", fontsize=12)
    ax.set_ylabel("Fault-Driven Page Fraction (%)", fontsize=12)
    ax.set_title(
        "Fault-Driven Page Fraction: Synthetic vs Application vs STREAM Workloads",
        fontsize=13, fontweight="bold",
    )
    ax.set_xticks(x)
    ax.set_xticklabels([w.replace("_", "\n") for w in present_wls], fontsize=8)
    ax.legend(title="VM Memory", fontsize=9)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "12_fault_fraction_all.png")


# ---------------------------------------------------------------------------
# Plot 13: Overall avg latency with error bars — full vs live per workload
# ---------------------------------------------------------------------------


def _corrected_overall_lat(grouped, mem, wl, mode, baseline_field, during_field, post_field):
    """Recompute overall latency excluding the zero-during window for full snapshot.

    For full snapshot the VM was paused, so there are no real during-window
    measurements. We average only baseline and post to avoid pulling the mean
    down with a fake zero.
    """
    rr = grouped.get((mem, wl, mode), [])
    if not rr:
        return 0.0
    b = avg([r.get(baseline_field, 0) for r in rr])
    p = avg([r.get(post_field, 0) for r in rr])
    if mode == "full":
        return (b + p) / 2
    d = avg([r.get(during_field, 0) for r in rr])
    return (b + d + p) / 3


def plot_overall_avg_latency(grouped, outdir):
    """Bar chart of corrected overall latency for app workloads (full vs live).

    For full snapshot the during-window is excluded because the VM was paused
    and served no requests — including 0 would artificially lower the mean.
    """
    if not any(k[1] in APP_WORKLOADS for k in grouped):
        print("  Skipping plot 13: no app workload data in CSV")
        return

    fig, axes = plt.subplots(1, 2, figsize=(16, 6), sharey=False)

    x = range(len(APP_WORKLOADS))
    width = 0.18
    # Full and live × two memory sizes = 4 bar positions per workload.
    config = [
        ("full", 512,  -1.5, "#BDBDBD"),
        ("live", 512,  -0.5, "#90CAF9"),
        ("full", 2048,  0.5, "#757575"),
        ("live", 2048,  1.5, "#1565C0"),
    ]

    for ax, baseline_f, during_f, post_f, ylabel, title in [
        (axes[0], "app_baseline_avg_us", "app_during_avg_us", "post_snap_avg_us",
         "Overall Avg Latency (µs)", "Average Latency"),
        (axes[1], "app_baseline_p99_us", "app_during_p99_us", "post_snap_p99_us",
         "Overall p99 Latency (µs)", "p99 Latency"),
    ]:
        for mode, mem, offset, color in config:
            means = [
                _corrected_overall_lat(grouped, mem, wl, mode, baseline_f, during_f, post_f)
                for wl in APP_WORKLOADS
            ]
            ax.bar(
                [xi + offset * width for xi in x], means, width,
                label=f"{mode.capitalize()} {mem} MiB",
                color=color, edgecolor="white",
            )

        ax.set_xlabel("Application Workload", fontsize=11)
        ax.set_ylabel(ylabel, fontsize=11)
        ax.set_title(
            f"Overall Run {title}\n"
            "(during window excluded for full — VM was paused)",
            fontsize=11, fontweight="bold",
        )
        ax.set_xticks(x)
        ax.set_xticklabels([w.replace("_", "\n") for w in APP_WORKLOADS], fontsize=8)
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3, axis="y")
        ax.set_ylim(bottom=0)

    fig.tight_layout()
    _savefig(fig, outdir, "13_overall_avg_latency.png")


# ---------------------------------------------------------------------------
# Plot 14: Three-window throughput recovery — synthetic workloads
# ---------------------------------------------------------------------------


def plot_three_window_throughput(grouped, outdir):
    """Baseline / during / post throughput for synthetic dd workloads by memory size."""
    syn_workloads = [w for w in WORKLOADS if w != "idle"]
    has_data = any(
        grouped.get((m, wl, mode), [])
        for m in MEM_SIZES for wl in syn_workloads for mode in ["full", "live"]
    )
    if not has_data:
        print("  Skipping plot 14: no synthetic workload throughput data in CSV")
        return

    fig, axes = plt.subplots(1, len(syn_workloads), figsize=(5 * len(syn_workloads), 6),
                             sharey=False)
    if len(syn_workloads) == 1:
        axes = [axes]

    x = range(len(MEM_SIZES))
    width = 0.22
    # baseline / during / post for live; baseline / post for full (during=0).
    palette_live = ["#42A5F5", "#EF5350", "#66BB6A"]   # blue / red / green
    palette_full = ["#BDBDBD", "#E0E0E0", "#9E9E9E"]

    for ax, wl in zip(axes, syn_workloads):
        for mode, palette, offsets in [
            ("live", palette_live, [-1.0, 0.0,  1.0]),
            ("full", palette_full, [-1.5, None, 1.5]),
        ]:
            windows = [
                ("workload_baseline_mibs", "Baseline"),
                ("workload_during_mibs",   "During"),
                ("post_snap_throughput_mibs", "Post-snap"),
            ]
            for j, (field, label) in enumerate(windows):
                if mode == "full" and label == "During":
                    continue   # full snapshot: VM was paused, skip
                offset = offsets[j]
                vals = [avg([r.get(field, 0) for r in grouped.get((m, wl, mode), [])])
                        for m in MEM_SIZES]
                ax.bar(
                    [xi + offset * width for xi in x], vals, width,
                    label=f"{label} ({mode})",
                    color=palette[j], edgecolor="white",
                )

        ax.set_title(f"{wl.capitalize()} workload", fontsize=12, fontweight="bold")
        ax.set_xlabel("VM Memory (MiB)", fontsize=11)
        ax.set_ylabel("Write Throughput (MiB/s)", fontsize=11)
        ax.set_xticks(x)
        ax.set_xticklabels(MEM_SIZES)
        ax.legend(fontsize=8, ncol=2)
        ax.grid(True, alpha=0.3, axis="y")
        ax.set_ylim(bottom=0)

    fig.suptitle("Synthetic Workload Throughput: Baseline / During / Post Snapshot",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    _savefig(fig, outdir, "14_three_window_throughput.png")


# ---------------------------------------------------------------------------
# Plot 15: Service interruption — full vs live
# ---------------------------------------------------------------------------


def plot_service_interruption(grouped, outdir):
    """Bar chart: service interruption (ms) for full vs live snapshot.

    Full snapshot: server is completely unresponsive for the entire snapshot
    duration (VM paused). Live snapshot: server is only unresponsive for the
    brief freeze/downtime window.
    """
    all_workloads = [wl for wl in WORKLOADS if wl != "idle"] + APP_WORKLOADS
    all_mems = sorted({m for (m, wl, mode) in grouped
                       if wl in all_workloads})
    if not all_mems:
        print("  Skipping plot 15: no workload data for service interruption")
        return

    # Collect per-memory-size averages across workloads.
    full_ms_by_mem = []
    live_ms_by_mem = []
    valid_mems = []

    for mem in sorted(set(MEM_SIZES + APP_MEM_SIZES)):
        full_vals = []
        live_vals = []
        for wl in all_workloads:
            full_rr = grouped.get((mem, wl, "full"), [])
            live_rr = grouped.get((mem, wl, "live"), [])
            if full_rr:
                si = [r.get("service_interruption_ms") for r in full_rr
                      if r.get("service_interruption_ms")]
                if si:
                    full_vals.append(avg(si))
                else:
                    full_vals.append(avg([r["full_total_ms"] for r in full_rr]))
            if live_rr:
                si = [r.get("service_interruption_ms") for r in live_rr
                      if r.get("service_interruption_ms")]
                if si:
                    live_vals.append(avg(si))
                else:
                    live_vals.append(avg([r["downtime_us"] for r in live_rr]) / 1000)
        if full_vals or live_vals:
            full_ms_by_mem.append(sum(full_vals) / len(full_vals) if full_vals else 0)
            live_ms_by_mem.append(sum(live_vals) / len(live_vals) if live_vals else 0)
            valid_mems.append(mem)

    if not valid_mems:
        print("  Skipping plot 15: insufficient data")
        return

    fig, ax = plt.subplots(figsize=(10, 6))
    x = range(len(valid_mems))
    width = 0.35

    ax.bar([xi - width / 2 for xi in x], full_ms_by_mem, width,
           label="Full snapshot", color=FULL_COLOR, edgecolor="white")
    ax.bar([xi + width / 2 for xi in x], live_ms_by_mem, width,
           label="Live snapshot (downtime only)", color=LIVE_COLOR, edgecolor="white")

    ax.set_xlabel("VM Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Service Interruption (ms)", fontsize=12)
    ax.set_title(
        "Service Interruption: Full vs Live Snapshot\n"
        "(time server was fully unresponsive)",
        fontsize=13, fontweight="bold",
    )
    ax.set_xticks(x)
    ax.set_xticklabels(valid_mems)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3, axis="y")
    ax.set_ylim(bottom=0)

    _savefig(fig, outdir, "15_service_interruption.png")


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
    plot_app_ops_degradation(grouped, outdir)
    plot_app_tail_latency(grouped, outdir)
    plot_stream_bandwidth(grouped, outdir)
    plot_fault_fraction_comparison(grouped, outdir)
    plot_overall_avg_latency(grouped, outdir)
    plot_three_window_throughput(grouped, outdir)
    plot_service_interruption(grouped, outdir)

    print()
    print(f"Done! {15} plots saved to {outdir}/")


if __name__ == "__main__":
    main()
