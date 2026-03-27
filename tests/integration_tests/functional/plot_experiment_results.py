#!/usr/bin/env python3
# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Generate plots from live snapshot experiment results CSV.

Usage:
    python3 plot_experiment_results.py [path/to/experiment_results.csv]
    python3 plot_experiment_results.py results.csv --only timeline
    python3 plot_experiment_results.py results.csv --timeline redis_light 512

Produces PNG files in the same directory as the CSV.
Requires: matplotlib (pip install matplotlib)
"""

import argparse
import csv
import os
import sys

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker

from analysis.io import (
    DEFAULT_CSV, MEM_SIZES, WORKLOADS, APP_MEM_SIZES, APP_WORKLOADS,
    STREAM_KERNELS, load_csv, group_rows,
)
from analysis.stats import avg

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WORKLOAD_COLORS = {
    "idle": "#2196F3",
    "light": "#4CAF50",
    "medium": "#FF9800",
    "heavy": "#F44336",
}
FULL_COLOR = "#9E9E9E"
LIVE_COLOR = "#2196F3"

# Application workloads (reduced matrix per design doc §6.2)
APP_WORKLOAD_COLORS = {
    "redis_light":       "#B3E5FC",
    "redis_mixed":       "#0288D1",
    "redis_heavy":       "#01579B",
    "memcached_light":   "#C8E6C9",
    "memcached_heavy":   "#2E7D32",
    "stream":            "#FF6F00",
}


def _savefig(fig, outdir, name):
    path = os.path.join(outdir, name)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved: {path}")


def _rolling_mean(xs, ys, window_s=0.5):
    """Return smoothed_ys using a time-based centred rolling window."""
    xs = np.array(xs, dtype=float)
    ys = np.array(ys, dtype=float)
    out = np.empty_like(ys)
    half = window_s / 2
    for i, t in enumerate(xs):
        mask = (xs >= t - half) & (xs <= t + half)
        out[i] = ys[mask].mean() if mask.any() else ys[i]
    return xs, out


def _smooth_with_gaps(xs, ys, window_s=0.5, gap_thresh_s=1.0):
    """Compute rolling mean on ok-sample arrays, inserting NaN at time gaps.

    matplotlib treats NaN in plot() as a line break, so the smoothed curve
    will not interpolate across pause windows where no latency data exists.
    """
    if not xs:
        return [], []
    _, ys_s = _rolling_mean(xs, ys, window_s=window_s)
    out_xs, out_ys = [xs[0]], [ys_s[0]]
    for i in range(1, len(xs)):
        if xs[i] - xs[i - 1] > gap_thresh_s:
            out_xs.append(float("nan"))
            out_ys.append(float("nan"))
        out_xs.append(float(xs[i]))
        out_ys.append(float(ys_s[i]))
    return out_xs, out_ys


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

    mem_offsets = {m: (-len(APP_MEM_SIZES) + 1 + 2 * i) for i, m in enumerate(APP_MEM_SIZES)}
    full_offsets = {m: (-len(APP_MEM_SIZES) + 2 + 2 * i) for i, m in enumerate(APP_MEM_SIZES)}

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
    # Three windows × N memory sizes = 3N bar clusters per workload.
    _n = len(APP_MEM_SIZES)
    _mem_offsets_lat = {
        m: [(-_n * 1.5 + 3 * i + j) for j in range(3)]
        for i, m in enumerate(APP_MEM_SIZES)
    }
    mem_palettes = {
        APP_MEM_SIZES[0]: ["#90CAF9", "#EF9A9A", "#A5D6A7"],
        APP_MEM_SIZES[1]: ["#1565C0", "#B71C1C", "#2E7D32"],
        APP_MEM_SIZES[2] if _n > 2 else APP_MEM_SIZES[0]: ["#0D47A1", "#880E4F", "#1B5E20"],
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

        for mem, offsets in [(m, _mem_offsets_lat[m]) for m in APP_MEM_SIZES]:
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
    # Three windows × N memory sizes = 3N bar positions per kernel.
    _n = len(APP_MEM_SIZES)
    palettes = {
        APP_MEM_SIZES[0]: ["#A5D6A7", "#FFCC80", "#90CAF9"],
        APP_MEM_SIZES[1]: ["#1B5E20", "#E65100", "#0D47A1"],
        APP_MEM_SIZES[2] if _n > 2 else APP_MEM_SIZES[0]: ["#004D40", "#BF360C", "#0A237F"],
    }
    _stream_offsets = {
        m: [(-_n * 1.5 + 3 * i + j) for j in range(3)]
        for i, m in enumerate(APP_MEM_SIZES)
    }

    for mem, offsets in [(m, _stream_offsets[m]) for m in APP_MEM_SIZES]:
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
    # Full and live × N memory sizes = 2N bar positions per workload.
    _full_colors = ["#BDBDBD", "#757575", "#424242"]
    _live_colors = ["#90CAF9", "#1565C0", "#0D47A1"]
    _n = len(APP_MEM_SIZES)
    config = []
    for _i, _m in enumerate(APP_MEM_SIZES):
        _base = (-_n + 2 * _i)
        config.append(("full", _m, _base - 0.5, _full_colors[_i]))
        config.append(("live", _m, _base + 0.5, _live_colors[_i]))

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
# Plot 16: Throughput-over-time timeline (reconstructed schematic)
# ---------------------------------------------------------------------------


def _load_timeseries_for_plot(grouped, outdir, workload, mem, mode):
    """Return (ts_rows, timing_anchors) for the first available iteration, or (None, None).

    Looks for a ``timeseries_file`` field in the grouped rows and reads the
    corresponding CSV.  ``timing_anchors`` is a dict with keys
    ``ts_snap_start_s``, ``ts_snap_end_s``, ``ts_freeze_start_s``,
    ``ts_freeze_end_s``.
    """
    for row in grouped.get((mem, workload, mode), []):
        ts_file = row.get("timeseries_file", "")
        if not ts_file:
            continue
        path = os.path.join(outdir, ts_file)
        if not os.path.isfile(path):
            continue
        ts_rows = []
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    ts_rows.append({
                        "t_rel_s":   float(r["t_rel_s"]),
                        "throughput": float(r["throughput"]),
                        "avg_ms":    float(r.get("avg_ms",  0) or 0),
                        "p50_ms":    float(r.get("p50_ms",  0) or 0),
                        "p99_ms":    float(r.get("p99_ms",  0) or 0),
                        "p999_ms":   float(r.get("p999_ms", 0) or 0),
                        "failed":    int(r.get("failed",    0) or 0),
                    })
                except (KeyError, ValueError):
                    pass
        if not ts_rows:
            continue
        anchors = {k: float(row.get(k, 0) or 0) for k in
                   ["ts_snap_start_s", "ts_snap_end_s",
                    "ts_freeze_start_s", "ts_freeze_end_s"]}
        return ts_rows, anchors
    return None, None


def _plot_timeline_one_config(grouped, outdir, workload, mem):
    """Render and save a 2×2-panel timeline PNG for one (workload, mem) config.

    Top row: throughput (ops/s) — scatter + smoothed line.
    Bottom row: p99 / p99.9 latency over time.
    Left column: full snapshot.  Right column: live snapshot.

    Returns True if anything was plotted, False if data was missing.
    """
    color = "#2196F3"
    fig, axes = plt.subplots(2, 2, figsize=(18, 10))
    ax_thr_full, ax_thr_live = axes[0]
    ax_lat_full, ax_lat_live = axes[1]
    has_real_data = False
    plotted = False

    def _shade_region(ax_thr, ax_lat, x0, x1):
        """Shade a pause/freeze region on both the throughput and latency axes."""
        for ax in (ax_thr, ax_lat):
            ax.axvspan(x0, x1, alpha=0.15, color="red", hatch="//", label="_nolegend_")

    def _vlines(ax_thr, ax_lat, x0, x1):
        for ax in (ax_thr, ax_lat):
            ax.axvline(x0, color=color, linestyle="--", linewidth=1, alpha=0.6)
            ax.axvline(x1, color=color, linestyle="--", linewidth=1, alpha=0.6)

    # ------------------------------------------------------------------ #
    # Full snapshot panel
    # ------------------------------------------------------------------ #
    full_rows = grouped.get((mem, workload, "full"), [])
    if full_rows:
        ts_rows, anchors = _load_timeseries_for_plot(grouped, outdir, workload, mem, "full")

        if ts_rows and anchors:
            has_real_data = True
            plotted = True
            ok_rows = [r for r in ts_rows if not r["failed"]]
            xs_ok  = [r["t_rel_s"]   for r in ok_rows]
            ys_ok  = [r["throughput"] for r in ok_rows]
            p99s   = [r["p99_ms"]    for r in ok_rows]
            p999s  = [r["p999_ms"]   for r in ok_rows]
            failed_xs = [r["t_rel_s"]   for r in ts_rows if r["failed"]]
            failed_ys = [r["throughput"] for r in ts_rows if r["failed"]]
            xs_all = [r["t_rel_s"]   for r in ts_rows]
            ys_all = [0.0 if r["failed"] else r["throughput"] for r in ts_rows]

            ax_thr_full.scatter(xs_ok, ys_ok, s=4, color=color, alpha=0.4, label=f"{mem} MiB raw")
            if xs_all:
                _, ys_s = _rolling_mean(xs_all, ys_all, window_s=0.5)
                ax_thr_full.plot(xs_all, ys_s, color=color, linewidth=2, label="smoothed")
            if failed_xs:
                ax_thr_full.scatter(failed_xs, failed_ys, s=40, color="red",
                                    marker="x", linewidths=1.5, zorder=5,
                                    label="connection failed")

            if xs_ok:
                ax_lat_full.scatter(xs_ok, p99s,  s=4, color="orange", alpha=0.4, label="p99 raw")
                ax_lat_full.scatter(xs_ok, p999s, s=4, color="red",    alpha=0.4, label="p99.9 raw")
                p99s_sx,  p99s_sy  = _smooth_with_gaps(xs_ok, p99s,  window_s=0.5)
                p999s_sx, p999s_sy = _smooth_with_gaps(xs_ok, p999s, window_s=0.5)
                ax_lat_full.plot(p99s_sx,  p99s_sy,  color="orange", linewidth=2, label="p99")
                ax_lat_full.plot(p999s_sx, p999s_sy, color="red",    linewidth=2, label="p99.9")

            snap_start = anchors["ts_snap_start_s"]
            snap_end   = anchors["ts_snap_end_s"]
            _vlines(ax_thr_full, ax_lat_full, snap_start, snap_end)
            _shade_region(ax_thr_full, ax_lat_full, snap_start, snap_end)
        else:
            b_ops   = avg([r.get("app_baseline_ops", 0) for r in full_rows])
            p_ops   = avg([r.get("post_snap_ops",    0) for r in full_rows])
            snap_ms = avg([r.get("full_total_ms",    0) for r in full_rows])

            if b_ops > 0 and p_ops > 0 and snap_ms > 0:
                plotted = True
                t_base  = 50000.0 / b_ops
                snap_s  = snap_ms / 1000.0
                t_post  = 50000.0 / p_ops

                segments = [
                    (0,               t_base,              b_ops, "baseline"),
                    (t_base,          t_base + snap_s,     0,     "paused"),
                    (t_base + snap_s, t_base + snap_s + t_post, p_ops, "post-snap"),
                ]
                xs, ys = [], []
                for t0, t1, ops, _ in segments:
                    xs += [t0, t1]
                    ys += [ops, ops]
                ax_thr_full.plot(xs, ys, color=color, linewidth=2, label=f"{mem} MiB")
                ax_thr_full.axvline(t_base,          color=color, linestyle="--",
                                    linewidth=1, alpha=0.6)
                ax_thr_full.axvline(t_base + snap_s, color=color, linestyle="--",
                                    linewidth=1, alpha=0.6)
                ax_thr_full.axvspan(t_base, t_base + snap_s, alpha=0.15, color="red",
                                    hatch="//", label="_nolegend_")

    # ------------------------------------------------------------------ #
    # Live snapshot panel
    # ------------------------------------------------------------------ #
    live_rows = grouped.get((mem, workload, "live"), [])
    if live_rows:
        ts_rows, anchors = _load_timeseries_for_plot(grouped, outdir, workload, mem, "live")

        if ts_rows and anchors:
            has_real_data = True
            plotted = True
            ok_rows = [r for r in ts_rows if not r["failed"]]
            xs_ok  = [r["t_rel_s"]   for r in ok_rows]
            ys_ok  = [r["throughput"] for r in ok_rows]
            p99s   = [r["p99_ms"]    for r in ok_rows]
            p999s  = [r["p999_ms"]   for r in ok_rows]
            failed_xs = [r["t_rel_s"]   for r in ts_rows if r["failed"]]
            failed_ys = [r["throughput"] for r in ts_rows if r["failed"]]
            xs_all = [r["t_rel_s"]   for r in ts_rows]
            ys_all = [0.0 if r["failed"] else r["throughput"] for r in ts_rows]

            ax_thr_live.scatter(xs_ok, ys_ok, s=4, color=color, alpha=0.4, label=f"{mem} MiB raw")
            if xs_all:
                _, ys_s = _rolling_mean(xs_all, ys_all, window_s=0.5)
                ax_thr_live.plot(xs_all, ys_s, color=color, linewidth=2, label="smoothed")
            if failed_xs:
                ax_thr_live.scatter(failed_xs, failed_ys, s=40, color="red",
                                    marker="x", linewidths=1.5, zorder=5,
                                    label="connection failed")

            if xs_ok:
                ax_lat_live.scatter(xs_ok, p99s,  s=4, color="orange", alpha=0.4, label="p99 raw")
                ax_lat_live.scatter(xs_ok, p999s, s=4, color="red",    alpha=0.4, label="p99.9 raw")
                p99s_sx,  p99s_sy  = _smooth_with_gaps(xs_ok, p99s,  window_s=0.5)
                p999s_sx, p999s_sy = _smooth_with_gaps(xs_ok, p999s, window_s=0.5)
                ax_lat_live.plot(p99s_sx,  p99s_sy,  color="orange", linewidth=2, label="p99")
                ax_lat_live.plot(p999s_sx, p999s_sy, color="red",    linewidth=2, label="p99.9")

            snap_start   = anchors["ts_snap_start_s"]
            snap_end     = anchors["ts_snap_end_s"]
            freeze_start = anchors["ts_freeze_start_s"]
            freeze_end   = anchors["ts_freeze_end_s"]
            _vlines(ax_thr_live, ax_lat_live, snap_start, snap_end)
            _shade_region(ax_thr_live, ax_lat_live, freeze_start, freeze_end)
        else:
            b_ops    = avg([r.get("app_baseline_ops", 0) for r in live_rows])
            d_ops    = avg([r.get("app_during_ops",   0) for r in live_rows])
            p_ops    = avg([r.get("post_snap_ops",    0) for r in live_rows])
            snap_us  = avg([r.get("total_us",         0) for r in live_rows])
            ph1_us   = avg([r.get("phase1_us",        0) for r in live_rows])
            down_us  = avg([r.get("downtime_us",      0) for r in live_rows])

            if b_ops > 0 and p_ops > 0 and snap_us > 0:
                plotted = True
                t_base   = 50000.0 / b_ops
                snap_s   = snap_us  / 1e6
                ph1_s    = ph1_us   / 1e6
                freeze_s = down_us  / 1e6
                t_post   = 50000.0 / p_ops

                segments = [
                    (0,                         t_base,                b_ops, "baseline"),
                    (t_base,                    t_base + ph1_s,        d_ops, "during (pre-freeze)"),
                    (t_base + ph1_s,            t_base + ph1_s + freeze_s, 0, "frozen"),
                    (t_base + ph1_s + freeze_s, t_base + snap_s,       d_ops, "during (post-freeze)"),
                    (t_base + snap_s,           t_base + snap_s + t_post, p_ops, "post-snap"),
                ]
                xs, ys = [], []
                for t0, t1, ops, _ in segments:
                    xs += [t0, t1]
                    ys += [ops, ops]
                ax_thr_live.plot(xs, ys, color=color, linewidth=2, label=f"{mem} MiB")
                ax_thr_live.axvline(t_base,          color=color, linestyle="--",
                                    linewidth=1, alpha=0.6)
                ax_thr_live.axvline(t_base + snap_s, color=color, linestyle="--",
                                    linewidth=1, alpha=0.6)
                ax_thr_live.axvspan(t_base + ph1_s, t_base + ph1_s + freeze_s,
                                    alpha=0.15, color="red", hatch="//", label="_nolegend_")

    if not plotted:
        plt.close(fig)
        return False

    subtitle_note = (
        "Real per-~100ms samples"
        if has_real_data
        else "Reconstructed from per-window averages (x-axis is approximate)"
    )
    fig.suptitle(
        f"{workload} / {mem} MiB: Throughput Timeline Through Snapshot\n{subtitle_note}",
        fontsize=13, fontweight="bold",
    )

    for ax, title in [
        (ax_thr_full, "Full Snapshot — Throughput (VM fully paused)"),
        (ax_thr_live, "Live Snapshot — Throughput (brief freeze only)"),
    ]:
        ax.set_xlabel("Time (s)", fontsize=11)
        ax.set_ylabel("Throughput (ops/s)", fontsize=11)
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0)

    for ax, title in [
        (ax_lat_full, "Full Snapshot — Latency"),
        (ax_lat_live, "Live Snapshot — Latency"),
    ]:
        ax.set_xlabel("Time (s)", fontsize=11)
        ax.set_ylabel("Latency (ms)  [gaps = failed]", fontsize=11)
        ax.set_title(title, fontsize=12, fontweight="bold")
        ax.legend(fontsize=9)
        ax.grid(True, alpha=0.3)
        ax.set_ylim(bottom=0)

    fig.tight_layout()

    timelines_dir = os.path.join(outdir, "timelines")
    os.makedirs(timelines_dir, exist_ok=True)
    fname = f"timeline_{workload}_{mem}mib.png"
    _savefig(fig, timelines_dir, fname)
    return True


def plot_throughput_timeline(grouped, outdir, configs=None):
    """Throughput timeline of ops/sec through a snapshot cycle.

    Generates one 2-panel PNG per (workload, mem_size_mib) config in a
    ``timelines/`` subdirectory.  When real timeseries data is available it
    is plotted as a scatter with red × markers for failed samples; otherwise
    a step-function is reconstructed from per-window averages.

    ``configs`` is an optional list of ``(workload, mem_size_mib)`` tuples.
    When None, all configs that have any data in ``grouped`` are auto-detected.
    """
    if configs is None:
        # Auto-detect all (workload, mem) pairs that have any row data.
        seen = set()
        for (mem, wl, _mode) in grouped:
            seen.add((wl, mem))
        # Keep only redis workloads (timeseries sampler is redis-only).
        configs = sorted(
            (wl, mem) for (wl, mem) in seen if wl.startswith("redis_")
        )

    if not configs:
        print("  Skipping plot 16: no redis workload data in CSV")
        return

    count = 0
    for workload, mem in configs:
        if _plot_timeline_one_config(grouped, outdir, workload, mem):
            count += 1

    if count == 0:
        print("  Skipping plot 16: no plottable data found for requested configs")


# ---------------------------------------------------------------------------
# New research plots (17–21) and summary table
# ---------------------------------------------------------------------------

LIVE_BPF_COLOR = "#4CAF50"   # green


def _bar_mean_std(rows, field, scale=1.0):
    """Return (mean, std) scaled by ``scale``, or (None, None) if no data."""
    vals = []
    for r in rows:
        v = r.get(field, "")
        if v != "" and v is not None:
            try:
                vals.append(float(v) * scale)
            except (ValueError, TypeError):
                pass
    if not vals:
        return None, None
    arr = np.array(vals)
    return float(arr.mean()), float(arr.std(ddof=0) if len(arr) > 1 else 0.0)


def plot_downtime_full_vs_live(grouped, outdir):
    """Plot 17 — Grouped bar chart: downtime (ms) for full / live / live_bpf, log scale."""
    fig, ax = plt.subplots(figsize=(10, 6))

    x_labels = [str(m) for m in MEM_SIZES]
    n = len(MEM_SIZES)
    bar_w = 0.25
    offsets = [-bar_w, 0, bar_w]
    modes = [
        ("full",     FULL_COLOR,     "Full (sync)",    "full_total_ms",  1.0),
        ("live",     LIVE_COLOR,     "Live UFFD",      "downtime_us",    1e-3),
        ("live_bpf", LIVE_BPF_COLOR, "Live eBPF",      "downtime_us",    1e-3),
    ]

    for idx, (mode, color, label, field, scale) in enumerate(modes):
        means, errs = [], []
        for mem in MEM_SIZES:
            rows = grouped.get((mem, "idle", mode), [])
            m, s = _bar_mean_std(rows, field, scale)
            means.append(m)
            errs.append(s)

        xs = np.arange(n) + offsets[idx]
        valid = [(i, m, e) for i, (m, e) in enumerate(zip(means, errs)) if m is not None]
        if not valid:
            continue
        xi = np.array([v[0] for v in valid]) + offsets[idx]
        ym = np.array([v[1] for v in valid])
        ye = np.array([v[2] for v in valid])
        ax.bar(xi, ym, width=bar_w, color=color, label=label,
               yerr=ye, capsize=3, error_kw={"elinewidth": 1})

    ax.set_yscale("log")
    ax.set_xticks(np.arange(n))
    ax.set_xticklabels([f"{m} MiB" for m in MEM_SIZES])
    ax.set_xlabel("Guest Memory Size", fontsize=12)
    ax.set_ylabel("Downtime (ms) — log scale", fontsize=12)
    ax.set_title("Snapshot Downtime: Full vs Live vs Live-eBPF (idle workload)", fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    _savefig(fig, outdir, "17_downtime_full_vs_live.png")


def plot_total_time_full_vs_live(grouped, outdir):
    """Plot 18 — Grouped bar chart: total snapshot time (ms), linear scale."""
    fig, ax = plt.subplots(figsize=(10, 6))

    n = len(MEM_SIZES)
    bar_w = 0.25
    offsets = [-bar_w, 0, bar_w]
    modes = [
        ("full",     FULL_COLOR,     "Full (sync)",   "full_total_ms", 1.0),
        ("live",     LIVE_COLOR,     "Live UFFD",     "total_us",      1e-3),
        ("live_bpf", LIVE_BPF_COLOR, "Live eBPF",     "total_us",      1e-3),
    ]

    for idx, (mode, color, label, field, scale) in enumerate(modes):
        valid = []
        for i, mem in enumerate(MEM_SIZES):
            rows = grouped.get((mem, "idle", mode), [])
            m, s = _bar_mean_std(rows, field, scale)
            if m is not None:
                valid.append((i, m, s))
        if not valid:
            continue
        xi = np.array([v[0] for v in valid]) + offsets[idx]
        ym = np.array([v[1] for v in valid])
        ye = np.array([v[2] for v in valid])
        ax.bar(xi, ym, width=bar_w, color=color, label=label,
               yerr=ye, capsize=3, error_kw={"elinewidth": 1})

    ax.set_xticks(np.arange(n))
    ax.set_xticklabels([f"{m} MiB" for m in MEM_SIZES])
    ax.set_xlabel("Guest Memory Size", fontsize=12)
    ax.set_ylabel("Total Snapshot Time (ms)", fontsize=12)
    ax.set_title("Total Snapshot Time: Full vs Live vs Live-eBPF (idle workload)", fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    _savefig(fig, outdir, "18_total_time_full_vs_live.png")


def plot_snapshot_timeseries_overlay(grouped, outdir):
    """Plot 19 — Per-(workload, mem, mode) timeseries overlay with dual y-axis."""
    overlay_dir = os.path.join(outdir, "timeseries_overlay")
    os.makedirs(overlay_dir, exist_ok=True)

    # Collect all (workload, mem, mode) combos with timeseries data.
    combos = set()
    for (mem, wl, mode) in grouped:
        if wl.startswith("redis_"):
            combos.add((wl, mem, mode))

    count = 0
    for workload, mem, mode in sorted(combos):
        ts_rows, anchors = _load_timeseries_for_plot(grouped, outdir, workload, mem, mode)
        if not ts_rows or not anchors:
            continue

        fig, ax_thr = plt.subplots(figsize=(12, 5))
        ax_lat = ax_thr.twinx()

        ok_rows = [r for r in ts_rows if not r["failed"]]
        xs_ok  = [r["t_rel_s"]   for r in ok_rows]
        ys_ok  = [r["throughput"] for r in ok_rows]
        p99s   = [r["p99_ms"]    for r in ok_rows]
        p999s  = [r["p999_ms"]   for r in ok_rows]
        failed_xs = [r["t_rel_s"] for r in ts_rows if r["failed"]]

        xs_all = [r["t_rel_s"]   for r in ts_rows]
        ys_all = [0.0 if r["failed"] else r["throughput"] for r in ts_rows]

        # Throughput (left axis) — scatter + smoothed
        ax_thr.scatter(xs_ok, ys_ok, s=4, color="steelblue", alpha=0.3, label="throughput raw")
        if xs_all:
            _, ys_s = _rolling_mean(xs_all, ys_all, window_s=0.5)
            ax_thr.plot(xs_all, ys_s, color="steelblue", linewidth=2, label="throughput")
        if failed_xs:
            ax_thr.scatter(failed_xs, [0] * len(failed_xs), s=40, color="red",
                           marker="x", linewidths=1.5, zorder=5, label="failed")

        # Latency (right axis) — smoothed with gaps
        if xs_ok:
            p99s_sx, p99s_sy   = _smooth_with_gaps(xs_ok, p99s,  window_s=0.5)
            p999s_sx, p999s_sy = _smooth_with_gaps(xs_ok, p999s, window_s=0.5)
            ax_lat.plot(p99s_sx,  p99s_sy,  color="orange", linewidth=2, label="p99")
            ax_lat.plot(p999s_sx, p999s_sy, color="red",    linewidth=2, label="p99.9")

        # Vertical lines for snap start/end
        snap_start   = anchors["ts_snap_start_s"]
        snap_end     = anchors["ts_snap_end_s"]
        freeze_start = anchors["ts_freeze_start_s"]
        freeze_end   = anchors["ts_freeze_end_s"]

        trans = ax_thr.get_xaxis_transform()
        for x, lbl in [(snap_start, "snap start"), (snap_end, "snap end")]:
            ax_thr.axvline(x, color="gray", linestyle="--", linewidth=1, alpha=0.8)
            ax_thr.text(x, 1.02, lbl, transform=trans, ha="center",
                        fontsize=8, color="gray")

        # Red-hatched freeze region
        ax_thr.axvspan(freeze_start, freeze_end, alpha=0.15, color="red",
                       hatch="//", label="_nolegend_")

        ax_thr.set_xlabel("Time (s)", fontsize=11)
        ax_thr.set_ylabel("Throughput (ops/s)", fontsize=11, color="steelblue")
        ax_lat.set_ylabel("Latency (ms)", fontsize=11, color="darkorange")
        ax_thr.set_ylim(bottom=0)
        ax_lat.set_ylim(bottom=0)

        # Combined legend
        lines_thr, labels_thr = ax_thr.get_legend_handles_labels()
        lines_lat, labels_lat = ax_lat.get_legend_handles_labels()
        ax_thr.legend(lines_thr + lines_lat, labels_thr + labels_lat,
                      fontsize=8, loc="upper left")

        mode_label = {"full": "Full", "live": "Live UFFD", "live_bpf": "Live eBPF"}.get(mode, mode)
        ax_thr.set_title(
            f"{workload} / {mem} MiB — {mode_label} snapshot timeseries",
            fontsize=12, fontweight="bold",
        )
        ax_thr.grid(True, alpha=0.3)
        fig.tight_layout()

        fname = f"ts_overlay_{workload}_{mem}mib_{mode}.png"
        _savefig(fig, overlay_dir, fname)
        count += 1

    if count == 0:
        print("  Skipping plot 19: no timeseries data found")


def plot_avg_throughput_by_config(grouped, outdir):
    """Plot 20 — Grouped bar chart: throughput (ops/s) during snapshot per (workload, mem)."""
    app_redis_workloads = [w for w in APP_WORKLOADS if w.startswith("redis_")]
    combos = [(wl, mem) for wl in app_redis_workloads for mem in APP_MEM_SIZES
              if any(grouped.get((mem, wl, m)) for m in ("full", "live", "live_bpf"))]
    if not combos:
        print("  Skipping plot 20: no app workload data")
        return

    n = len(combos)
    bar_w = 0.2
    modes_cfg = [
        ("baseline", "steelblue", "Baseline",    "app_baseline_ops", 1.0),
        ("full",     FULL_COLOR,  "Sync / Full", "app_during_ops",   1.0),
        ("live",     LIVE_COLOR,  "Async UFFD",  "app_during_ops",   1.0),
        ("live_bpf", LIVE_BPF_COLOR, "Async eBPF", "app_during_ops", 1.0),
    ]
    offsets = np.array([-1.5, -0.5, 0.5, 1.5]) * bar_w

    fig, ax = plt.subplots(figsize=(max(10, n * 1.5), 6))

    for idx, (mode, color, label, field, scale) in enumerate(modes_cfg):
        valid = []
        for i, (wl, mem) in enumerate(combos):
            if mode == "baseline":
                # Baseline is the same regardless of snapshot mode; use live rows first.
                rows = (grouped.get((mem, wl, "live"), []) or
                        grouped.get((mem, wl, "full"), []) or
                        grouped.get((mem, wl, "live_bpf"), []))
                src_field = "app_baseline_ops"
            else:
                rows = grouped.get((mem, wl, mode), [])
                src_field = field
            m, s = _bar_mean_std(rows, src_field, scale)
            if m is not None:
                valid.append((i, m, s))
        if not valid:
            continue
        xi = np.array([v[0] for v in valid]) + offsets[idx]
        ym = np.array([v[1] for v in valid])
        ye = np.array([v[2] for v in valid])
        ax.bar(xi, ym, width=bar_w, color=color, label=label,
               yerr=ye, capsize=3, error_kw={"elinewidth": 1})

    ax.set_xticks(np.arange(n))
    ax.set_xticklabels([f"{wl}\n{mem} MiB" for wl, mem in combos], fontsize=8)
    ax.set_xlabel("(Workload, Memory)", fontsize=12)
    ax.set_ylabel("Throughput (ops/s)", fontsize=12)
    ax.set_title("Average Throughput During Snapshot Window", fontsize=13)
    ax.legend(fontsize=10)
    ax.grid(True, axis="y", alpha=0.3)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    _savefig(fig, outdir, "20_avg_throughput_by_config.png")


def plot_avg_latency_by_config(grouped, outdir):
    """Plot 21 — Grouped bar chart: avg latency (µs) during snapshot per (workload, mem)."""
    app_redis_workloads = [w for w in APP_WORKLOADS if w.startswith("redis_")]
    combos = [(wl, mem) for wl in app_redis_workloads for mem in APP_MEM_SIZES
              if any(grouped.get((mem, wl, m)) for m in ("live", "live_bpf"))]
    if not combos:
        print("  Skipping plot 21: no live app workload data")
        return

    n = len(combos)
    bar_w = 0.25
    modes_cfg = [
        ("baseline", "steelblue", "Baseline",   "app_baseline_avg_us"),
        ("live",     LIVE_COLOR,  "Async UFFD", "app_during_avg_us"),
        ("live_bpf", LIVE_BPF_COLOR, "Async eBPF", "app_during_avg_us"),
    ]
    offsets = np.array([-1.0, 0.0, 1.0]) * bar_w

    fig, (ax_avg, ax_p99) = plt.subplots(1, 2, figsize=(max(14, n * 2), 6))

    for ax, lat_field_suffix, title_suffix in [
        (ax_avg, "avg_us", "Avg Latency"),
        (ax_p99, "p99_us", "p99 Latency"),
    ]:
        for idx, (mode, color, label, _) in enumerate(modes_cfg):
            valid = []
            for i, (wl, mem) in enumerate(combos):
                if mode == "baseline":
                    rows = (grouped.get((mem, wl, "live"), []) or
                            grouped.get((mem, wl, "live_bpf"), []))
                    src_field = f"app_baseline_{lat_field_suffix}"
                else:
                    rows = grouped.get((mem, wl, mode), [])
                    src_field = f"app_during_{lat_field_suffix}"
                m, s = _bar_mean_std(rows, src_field)
                if m is not None:
                    valid.append((i, m, s))
            if not valid:
                continue
            xi = np.array([v[0] for v in valid]) + offsets[idx]
            ym = np.array([v[1] for v in valid])
            ye = np.array([v[2] for v in valid])
            ax.bar(xi, ym, width=bar_w, color=color, label=label,
                   yerr=ye, capsize=3, error_kw={"elinewidth": 1})

        ax.set_xticks(np.arange(n))
        ax.set_xticklabels([f"{wl}\n{mem} MiB" for wl, mem in combos], fontsize=8)
        ax.set_xlabel("(Workload, Memory)", fontsize=12)
        ax.set_ylabel("Latency (µs)", fontsize=12)
        ax.set_title(f"{title_suffix} During Snapshot Window", fontsize=12)
        ax.legend(fontsize=10)
        ax.grid(True, axis="y", alpha=0.3)
        ax.set_ylim(bottom=0)

    fig.suptitle("Application Latency During Snapshot", fontsize=13, fontweight="bold")
    fig.tight_layout()
    _savefig(fig, outdir, "21_avg_latency_by_config.png")


def generate_summary_table(grouped, outdir):
    """Write summary_table.md — redis_light focused table of key metrics per (mem, mode)."""
    REPRESENTATIVE_WORKLOAD = "redis_light"
    entries = []
    for (mem, wl, mode) in sorted(grouped.keys()):
        if wl != REPRESENTATIVE_WORKLOAD:
            continue
        rows = grouped[(mem, wl, mode)]
        if not any(r.get("service_interruption_ms") for r in rows):
            continue

        n = len(rows)
        failed_m, _   = _bar_mean_std(rows, "timeseries_failed_samples")
        fault_m, _    = _bar_mean_std(rows, "fault_pages")
        interr_m, _   = _bar_mean_std(rows, "service_interruption_ms")
        observed_outage_ms = failed_m * 100 if failed_m is not None else None

        entries.append({
            "mem_mib": mem,
            "mode": mode,
            "iterations": n,
            "observed_outage_ms":      f"{observed_outage_ms:.0f}" if observed_outage_ms is not None else "",
            "conn_windows_lost":       f"{failed_m:.1f}"   if failed_m  is not None else "",
            "fault_pages":             f"{fault_m:.0f}"    if fault_m   is not None else "",
            "service_interruption_ms": f"{interr_m:.2f}"   if interr_m  is not None else "",
        })

    if not entries:
        print("  Skipping summary table: no redis_light data")
        return

    # Sort by mem → mode.
    mode_order = {"full": 0, "live": 1, "live_bpf": 2}
    entries.sort(key=lambda e: (e["mem_mib"], mode_order.get(e["mode"], 99)))

    lines = [
        f"## Summary: {REPRESENTATIVE_WORKLOAD} workload",
        "| mem (MiB) | mode | iterations | observed_outage_ms | conn_windows_lost | fault_pages | service_interruption_ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        lines.append(
            f"| {e['mem_mib']} | {e['mode']} | {e['iterations']} "
            f"| {e['observed_outage_ms']} | {e['conn_windows_lost']} "
            f"| {e['fault_pages']} | {e['service_interruption_ms']} |"
        )

    path = os.path.join(outdir, "summary_table.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"  Saved: {path}")


# ---------------------------------------------------------------------------
# Plot 22: Service interruption vs memory size (redis_light)
# ---------------------------------------------------------------------------


def plot_service_interruption_vs_mem(grouped, outdir):
    """Plot 22 — Service interruption (ms) vs memory size for redis_light, log scale.

    Shows the key result: live/live_bpf reduce client-visible outage from
    seconds (full) to ~60-200 ms (freeze only), and the advantage grows with
    memory size.
    """
    workload = "redis_light"
    modes = [
        ("full",     FULL_COLOR,     "Full (sync)"),
        ("live",     LIVE_COLOR,     "Live UFFD"),
        ("live_bpf", LIVE_BPF_COLOR, "Live eBPF"),
    ]
    fig, ax = plt.subplots(figsize=(9, 6))
    for mode, color, label in modes:
        xs, ys, errs = [], [], []
        for mem in APP_MEM_SIZES:
            rows = grouped.get((mem, workload, mode), [])
            m, s = _bar_mean_std(rows, "service_interruption_ms")
            if m is not None:
                xs.append(mem)
                ys.append(m)
                errs.append(s)
        if xs:
            ax.errorbar(xs, ys, yerr=errs, marker="o", linewidth=2,
                        markersize=7, capsize=4, color=color, label=label)
    ax.set_yscale("log")
    ax.set_xlabel("Guest Memory Size (MiB)", fontsize=12)
    ax.set_ylabel("Service Interruption (ms) — log scale", fontsize=12)
    ax.set_title(
        f"Client-Visible Service Interruption: Full vs Live vs Live-eBPF\n"
        f"({workload} workload, 10 iterations × 2 kernels)",
        fontsize=12,
    )
    ax.set_xticks(APP_MEM_SIZES)
    ax.legend(fontsize=11)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    _savefig(fig, outdir, "22_service_interruption_vs_mem.png")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Generate plots from live snapshot experiment results CSV."
    )
    parser.add_argument("csv", nargs="?", default=DEFAULT_CSV, metavar="CSV_PATH")
    parser.add_argument(
        "--only", choices=["all", "timeline", "new"], default="all",
        help="Run only the specified plot group (default: all)",
    )
    parser.add_argument(
        "--timeline", nargs=2, metavar=("WORKLOAD", "MEM_SIZE"),
        help="Restrict timeline to one (workload, mem_size_mib) config; implies --only timeline",
    )
    args = parser.parse_args()

    csv_path = args.csv
    if not os.path.isfile(csv_path):
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    outdir = os.path.dirname(os.path.abspath(csv_path))
    rows = load_csv(csv_path)
    grouped = group_rows(rows)

    print(f"Generating plots from {csv_path}")
    print(f"Output directory: {outdir}")
    print()

    run_timelines_only = (args.only == "timeline") or (args.timeline is not None)
    run_new_only = args.only == "new"
    timeline_configs = (
        [(args.timeline[0], int(args.timeline[1]))] if args.timeline else None
    )

    if not run_timelines_only and not run_new_only:
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

    if not run_timelines_only:
        plot_downtime_full_vs_live(grouped, outdir)
        plot_total_time_full_vs_live(grouped, outdir)
        plot_snapshot_timeseries_overlay(grouped, outdir)
        plot_avg_throughput_by_config(grouped, outdir)
        plot_avg_latency_by_config(grouped, outdir)
        plot_service_interruption_vs_mem(grouped, outdir)
        generate_summary_table(grouped, outdir)

    if not run_new_only:
        plot_throughput_timeline(grouped, outdir, configs=timeline_configs)

    print()
    if run_timelines_only:
        print("Done! Timeline plots saved to timelines/ subdirectory.")
    elif run_new_only:
        print(f"Done! New research plots (17–22) + table saved to {outdir}/")
    else:
        print(f"Done! All plots saved to {outdir}/")


if __name__ == "__main__":
    main()
