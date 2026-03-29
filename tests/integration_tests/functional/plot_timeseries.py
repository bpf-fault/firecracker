#!/usr/bin/env python3
"""Plot throughput and latency timeseries from a single CSV file.

Produces the same visual style as the timeline panels in plot_experiment_results.py:
scatter of raw samples, rolling-mean smoothed line, and snapshot/freeze shading.

Usage:
    python3 plot_timeseries.py <timeseries.csv> [options]

Options:
    --results <experiment_results.csv>   Source snapshot/freeze marker times.
                                         Auto-detected from timeseries filename if omitted.
    --out <output.png>                   Output path (default: same dir/name as input, .png)
    --title <string>                     Custom plot title

Example:
    python3 tests/integration_tests/functional/plot_timeseries.py \\
        test_results/timeseries/redis_light_8192mib_live_iter00.csv
"""

import argparse
import csv
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


COLOR = "#2196F3"


# ---------------------------------------------------------------------------
# Helpers copied from plot_experiment_results.py to keep identical style
# ---------------------------------------------------------------------------

def _rolling_mean(xs, ys, window_s=0.5):
    xs = np.array(xs, dtype=float)
    ys = np.array(ys, dtype=float)
    out = np.empty_like(ys)
    half = window_s / 2
    for i, t in enumerate(xs):
        mask = (xs >= t - half) & (xs <= t + half)
        out[i] = ys[mask].mean() if mask.any() else ys[i]
    return xs, out


def _smooth_with_gaps(xs, ys, window_s=0.5, gap_thresh_s=1.0):
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
# Data loading
# ---------------------------------------------------------------------------

def load_timeseries(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append({
                    "t_rel_s":    float(r["t_rel_s"]),
                    "throughput": float(r["throughput"]),
                    "avg_ms":     float(r.get("avg_ms",  0) or 0),
                    "p50_ms":     float(r.get("p50_ms",  0) or 0),
                    "p99_ms":     float(r.get("p99_ms",  0) or 0),
                    "p999_ms":    float(r.get("p999_ms", 0) or 0),
                    "failed":     int(r.get("failed",    0) or 0),
                })
            except (KeyError, ValueError):
                pass
    return rows


def find_anchors(ts_path, results_csv):
    ts_basename = os.path.basename(ts_path)
    if not results_csv or not os.path.exists(results_csv):
        return None
    with open(results_csv, newline="") as f:
        for row in csv.DictReader(f):
            if os.path.basename(row.get("timeseries_file", "")) == ts_basename:
                try:
                    return {k: float(row[k]) for k in
                            ("ts_snap_start_s", "ts_snap_end_s",
                             "ts_freeze_start_s", "ts_freeze_end_s")}
                except (KeyError, ValueError):
                    return None
    return None


def auto_results_csv(ts_path):
    ts_dir = os.path.dirname(os.path.abspath(ts_path))
    candidate = os.path.join(os.path.dirname(ts_dir), "experiment_results.csv")
    return candidate if os.path.exists(candidate) else None


def infer_mode(ts_path):
    """Infer snapshot mode from the filename (live_bpf, live, full)."""
    name = os.path.basename(ts_path)
    if "live_bpf" in name:
        return "live_bpf"
    if "live" in name:
        return "live"
    if "full" in name:
        return "full"
    return "live"


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------

def plot(ts_path, results_csv=None, out_path=None, title=None):
    rows = load_timeseries(ts_path)
    if not rows:
        print(f"ERROR: no data in {ts_path}", file=sys.stderr)
        sys.exit(1)

    if results_csv is None:
        results_csv = auto_results_csv(ts_path)

    anchors = find_anchors(ts_path, results_csv)
    mode    = infer_mode(ts_path)

    ok_rows   = [r for r in rows if not r["failed"]]
    xs_ok     = [r["t_rel_s"]    for r in ok_rows]
    ys_ok     = [r["throughput"] for r in ok_rows]
    p99s      = [r["p99_ms"]     for r in ok_rows]
    p999s     = [r["p999_ms"]    for r in ok_rows]
    xs_all    = [r["t_rel_s"]    for r in rows]
    ys_all    = [0.0 if r["failed"] else r["throughput"] for r in rows]
    failed_xs = [r["t_rel_s"]    for r in rows if r["failed"]]
    failed_ys = [r["throughput"] for r in rows if r["failed"]]

    fig, (ax_thr, ax_lat) = plt.subplots(2, 1, figsize=(12, 8), sharex=True)

    # ── Throughput ──────────────────────────────────────────────────────────
    ax_thr.scatter(xs_ok, ys_ok, s=4, color=COLOR, alpha=0.4, label="raw samples")
    if xs_all:
        _, ys_s = _rolling_mean(xs_all, ys_all, window_s=0.5)
        ax_thr.plot(xs_all, ys_s, color=COLOR, linewidth=2, label="smoothed")
    if failed_xs:
        ax_thr.scatter(failed_xs, failed_ys, s=40, color="red",
                       marker="x", linewidths=1.5, zorder=5, label="connection failed")
    ax_thr.set_ylabel("Throughput (ops/s)", fontsize=11)
    ax_thr.legend(fontsize=9)
    ax_thr.grid(True, alpha=0.3)
    ax_thr.set_ylim(bottom=0)

    # ── Latency ─────────────────────────────────────────────────────────────
    ax_lat.scatter(xs_ok, p99s,  s=4, color="orange", alpha=0.4, label="p99 raw")
    ax_lat.scatter(xs_ok, p999s, s=4, color="red",    alpha=0.4, label="p99.9 raw")
    if xs_ok:
        p99s_sx,  p99s_sy  = _smooth_with_gaps(xs_ok, p99s,  window_s=0.5)
        p999s_sx, p999s_sy = _smooth_with_gaps(xs_ok, p999s, window_s=0.5)
        ax_lat.plot(p99s_sx,  p99s_sy,  color="orange", linewidth=2, label="p99")
        ax_lat.plot(p999s_sx, p999s_sy, color="red",    linewidth=2, label="p99.9")
    ax_lat.set_ylabel("Latency (ms)  [gaps = failed]", fontsize=11)
    ax_lat.set_xlabel("Time (s)", fontsize=11)
    ax_lat.legend(fontsize=9)
    ax_lat.grid(True, alpha=0.3)
    ax_lat.set_ylim(bottom=0)

    # ── Snapshot / freeze markers ────────────────────────────────────────────
    if anchors:
        snap_s   = anchors["ts_snap_start_s"]
        snap_e   = anchors["ts_snap_end_s"]
        freeze_s = anchors.get("ts_freeze_start_s", 0)
        freeze_e = anchors.get("ts_freeze_end_s",   0)

        for ax in (ax_thr, ax_lat):
            ax.axvline(snap_s, color=COLOR, linestyle="--", linewidth=1, alpha=0.6)
            ax.axvline(snap_e, color=COLOR, linestyle="--", linewidth=1, alpha=0.6)

        if mode == "full":
            # Full snapshot: entire window is the pause
            for ax in (ax_thr, ax_lat):
                ax.axvspan(snap_s, snap_e, alpha=0.15, color="red",
                           hatch="//", label="_nolegend_")
        else:
            # Live/BPF: only the brief freeze is the real downtime
            if freeze_s and freeze_e and freeze_e > freeze_s:
                for ax in (ax_thr, ax_lat):
                    ax.axvspan(freeze_s, freeze_e, alpha=0.15, color="red",
                               hatch="//", label="_nolegend_")

    # ── Title ────────────────────────────────────────────────────────────────
    if title is None:
        title = os.path.basename(ts_path)
    fig.suptitle(title, fontsize=13, fontweight="bold")

    fig.tight_layout()

    if out_path is None:
        out_path = os.path.splitext(ts_path)[0] + ".png"

    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Saved: {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Plot a single timeseries CSV.")
    ap.add_argument("csv", help="Path to timeseries CSV")
    ap.add_argument("--results", metavar="CSV",
                    help="experiment_results.csv for snapshot markers (auto-detected if omitted)")
    ap.add_argument("--out", metavar="PNG", help="Output PNG path")
    ap.add_argument("--title", help="Plot title")
    args = ap.parse_args()

    plot(args.csv, results_csv=args.results, out_path=args.out, title=args.title)


if __name__ == "__main__":
    main()
