#!/usr/bin/env python3
# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Analyze and print tables from live snapshot experiment results CSV.

Usage:
    python3 analyze_experiment_results.py [path/to/experiment_results.csv]

If no path is given, defaults to test_results/experiment_results.csv
relative to the repo root.
"""

import os
import sys
from analysis.io import (
    DEFAULT_CSV, MEM_SIZES, WORKLOADS, APP_MEM_SIZES, APP_WORKLOADS,
    STREAM_KERNELS, load_csv, group_rows,
)
from analysis.stats import avg, stdev


# ---------------------------------------------------------------------------
# Table printers
# ---------------------------------------------------------------------------


def print_full_table(grouped):
    """Print the complete results table with all metrics."""
    hdr = (
        f"{'Mem':>5}  {'Workload':>8}  {'Mode':>5}  {'Downtime(ms)':>13}  "
        f"{'Wall-clock(ms)':>15}  {'Throughput':>12}  {'Fault%':>7}  {'Degrad%':>8}"
    )
    sep = "=" * len(hdr)

    print(sep)
    print(hdr)
    print(sep)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            for mode in ["full", "live"]:
                key = (mem, wl, mode)
                rr = grouped.get(key, [])
                if not rr:
                    continue

                if mode == "full":
                    dt = avg([r["full_total_ms"] for r in rr])
                    wc = dt
                    tp = avg([r["full_throughput_mibs"] for r in rr])
                    ff = ""
                    dg = ""
                else:
                    dt = avg([r["downtime_us"] for r in rr]) / 1000
                    wc = avg([r["total_us"] for r in rr]) / 1000
                    tp = avg([r["throughput_mibs"] for r in rr])
                    ff = f"{avg([r['fault_fraction_pct'] for r in rr]):.2f}"
                    dg_val = avg([r["workload_degradation_pct"] for r in rr])
                    dg = f"{dg_val:.1f}" if wl != "idle" else ""

                print(
                    f"{mem:>5}  {wl:>8}  {mode:>5}  {dt:>12.1f}  "
                    f"{wc:>14.1f}  {tp:>10.0f}  {ff:>7}  {dg:>8}"
                )

            # Speedup line
            full_rr = grouped.get((mem, wl, "full"), [])
            live_rr = grouped.get((mem, wl, "live"), [])
            if full_rr and live_rr:
                full_dt = avg([r["full_total_ms"] for r in full_rr])
                live_dt = avg([r["downtime_us"] for r in live_rr]) / 1000
                speedup = full_dt / live_dt if live_dt > 0 else float("inf")
                print(
                    f"{'':>5}  {'':>8}  {'':>5}  "
                    f"{'--- speedup:':>12}  {speedup:>7.1f}x"
                )
        print("-" * len(hdr))


def print_downtime_table(grouped):
    """Print downtime comparison: Full vs Live across memory sizes."""
    print()
    print("DOWNTIME COMPARISON (ms)")
    print("=" * 80)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Full DT':>10}  "
        f"{'Live DT':>10}  {'Speedup':>8}  {'Live WP(ms)':>12}"
    )
    print("-" * 80)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            full_rr = grouped.get((mem, wl, "full"), [])
            live_rr = grouped.get((mem, wl, "live"), [])
            if not full_rr or not live_rr:
                continue

            full_dt = avg([r["full_total_ms"] for r in full_rr])
            live_dt = avg([r["downtime_us"] for r in live_rr]) / 1000
            wp = avg([r["wp_enable_us"] for r in live_rr]) / 1000
            speedup = full_dt / live_dt if live_dt > 0 else 0

            print(
                f"{mem:>10}  {wl:>8}  {full_dt:>9.1f}  "
                f"{live_dt:>9.1f}  {speedup:>7.1f}x  {wp:>11.1f}"
            )
        if mem < MEM_SIZES[-1]:
            print()


def print_throughput_table(grouped):
    """Print streaming throughput comparison."""
    print()
    print("STREAMING THROUGHPUT (MiB/s)")
    print("=" * 70)
    print(f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Full':>10}  {'Live':>10}  {'Ratio':>8}")
    print("-" * 70)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            full_rr = grouped.get((mem, wl, "full"), [])
            live_rr = grouped.get((mem, wl, "live"), [])
            if not full_rr or not live_rr:
                continue

            full_tp = avg([r["full_throughput_mibs"] for r in full_rr])
            live_tp = avg([r["throughput_mibs"] for r in live_rr])
            ratio = live_tp / full_tp if full_tp > 0 else 0

            print(
                f"{mem:>10}  {wl:>8}  {full_tp:>9.0f}  "
                f"{live_tp:>9.0f}  {ratio:>7.1%}"
            )
        if mem < MEM_SIZES[-1]:
            print()


def print_fault_table(grouped):
    """Print fault-driven page statistics."""
    print()
    print("FAULT-DRIVEN PAGES (live snapshot only)")
    print("=" * 80)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Total Pages':>12}  "
        f"{'Fault Pages':>12}  {'Fault %':>8}  {'Linear Pages':>13}"
    )
    print("-" * 80)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            rr = grouped.get((mem, wl, "live"), [])
            if not rr:
                continue

            total = avg([r["total_pages"] for r in rr])
            fault = avg([r["fault_pages"] for r in rr])
            linear = avg([r["linear_pages"] for r in rr])
            pct = avg([r["fault_fraction_pct"] for r in rr])

            print(
                f"{mem:>10}  {wl:>8}  {total:>11.0f}  "
                f"{fault:>11.0f}  {pct:>7.2f}  {linear:>12.0f}"
            )
        if mem < MEM_SIZES[-1]:
            print()


def print_phase_breakdown(grouped):
    """Print live snapshot phase timing breakdown."""
    print()
    print("LIVE SNAPSHOT PHASE BREAKDOWN (ms)")
    print("=" * 100)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Ph1(prep)':>10}  "
        f"{'Ph2(freeze)':>12}  {'Ph3(stream)':>12}  {'Ph4(final)':>11}  {'Total':>10}"
    )
    print("-" * 100)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            rr = grouped.get((mem, wl, "live"), [])
            if not rr:
                continue

            p1 = avg([r["phase1_us"] for r in rr]) / 1000
            p2 = avg([r["freeze_us"] for r in rr]) / 1000
            p3 = avg([r["stream_us"] for r in rr]) / 1000
            p4 = avg([r["finalize_us"] for r in rr]) / 1000
            total = avg([r["total_us"] for r in rr]) / 1000

            print(
                f"{mem:>10}  {wl:>8}  {p1:>9.1f}  "
                f"{p2:>11.1f}  {p3:>11.1f}  {p4:>10.1f}  {total:>9.1f}"
            )
        if mem < MEM_SIZES[-1]:
            print()


def print_freeze_breakdown(grouped):
    """Print Phase 2 (freeze/downtime) sub-timing breakdown."""
    print()
    print("PHASE 2 (FREEZE) BREAKDOWN (ms)")
    print("=" * 90)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'pause':>8}  "
        f"{'save_state':>11}  {'wp_enable':>10}  {'resume':>8}  {'Total':>8}"
    )
    print("-" * 90)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            rr = grouped.get((mem, wl, "live"), [])
            if not rr:
                continue

            pause = avg([r["pause_us"] for r in rr]) / 1000
            save = avg([r["save_state_us"] for r in rr]) / 1000
            wp = avg([r["wp_enable_us"] for r in rr]) / 1000
            resume = avg([r["resume_us"] for r in rr]) / 1000
            total = avg([r["freeze_us"] for r in rr]) / 1000

            print(
                f"{mem:>10}  {wl:>8}  {pause:>7.2f}  "
                f"{save:>10.2f}  {wp:>9.1f}  {resume:>7.2f}  {total:>7.1f}"
            )
        if mem < MEM_SIZES[-1]:
            print()


def print_restore_table(grouped):
    """Print snapshot restore timing."""
    print()
    print("RESTORE TIMING (ms)")
    print("=" * 70)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Mode':>5}  "
        f"{'API(ms)':>10}  {'SSH ready(ms)':>14}"
    )
    print("-" * 70)

    for mem in MEM_SIZES:
        for wl in WORKLOADS:
            for mode in ["full", "live"]:
                rr = grouped.get((mem, wl, mode), [])
                if not rr:
                    continue

                api = avg([r["restore_api_ms"] for r in rr])
                ssh = avg([r["ssh_ready_ms"] for r in rr])

                print(
                    f"{mem:>10}  {wl:>8}  {mode:>5}  "
                    f"{api:>9.1f}  {ssh:>13.1f}"
                )
        if mem < MEM_SIZES[-1]:
            print()


def print_host_resources(grouped):
    """Print host resource usage."""
    print()
    print("HOST RESOURCE USAGE")
    print("=" * 80)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>8}  {'Mode':>5}  "
        f"{'RSS pre(KiB)':>13}  {'RSS peak(KiB)':>14}  {'MemFile(MiB)':>13}"
    )
    print("-" * 80)

    for mem in MEM_SIZES:
        for wl in ["idle", "heavy"]:
            for mode in ["full", "live"]:
                rr = grouped.get((mem, wl, mode), [])
                if not rr:
                    continue

                pre = avg([r["rss_pre_kib"] for r in rr])
                peak = avg([r["rss_peak_kib"] for r in rr])
                memf = avg([r["mem_file_bytes"] for r in rr]) / (1024 * 1024)

                print(
                    f"{mem:>10}  {wl:>8}  {mode:>5}  "
                    f"{pre:>12.0f}  {peak:>13.0f}  {memf:>12.0f}"
                )
        if mem < MEM_SIZES[-1]:
            print()


def print_key_findings(grouped):
    """Print key findings summary."""
    print()
    print("=" * 60)
    print("KEY FINDINGS")
    print("=" * 60)

    print()
    print("Downtime Speedup (Full / Live) by memory size (idle):")
    for mem in MEM_SIZES:
        full_rr = grouped.get((mem, "idle", "full"), [])
        live_rr = grouped.get((mem, "idle", "live"), [])
        if not full_rr or not live_rr:
            continue
        full_dt = avg([r["full_total_ms"] for r in full_rr])
        live_dt = avg([r["downtime_us"] for r in live_rr]) / 1000
        speedup = full_dt / live_dt if live_dt > 0 else 0
        print(f"  {mem:>4} MiB: {full_dt:>7.0f} ms -> {live_dt:>5.0f} ms = {speedup:.1f}x")

    print()
    print("Fault-driven pages by workload (4096 MiB):")
    for wl in WORKLOADS:
        rr = grouped.get((4096, wl, "live"), [])
        if not rr:
            continue
        fp = avg([r["fault_pages"] for r in rr])
        ff = avg([r["fault_fraction_pct"] for r in rr])
        print(f"  {wl:>8}: {fp:>6.0f} pages ({ff:.2f}%)")

    print()
    print("Live snapshot streaming throughput by workload (4096 MiB):")
    for wl in WORKLOADS:
        rr = grouped.get((4096, wl, "live"), [])
        if not rr:
            continue
        tp = avg([r["throughput_mibs"] for r in rr])
        print(f"  {wl:>8}: {tp:.0f} MiB/s")

    print()
    print("wp_enable dominates downtime (% of Phase 2):")
    for mem in MEM_SIZES:
        rr = grouped.get((mem, "idle", "live"), [])
        if not rr:
            continue
        wp = avg([r["wp_enable_us"] for r in rr])
        freeze = avg([r["freeze_us"] for r in rr])
        pct = wp / freeze * 100 if freeze > 0 else 0
        print(f"  {mem:>4} MiB: wp_enable = {wp/1000:.1f} ms ({pct:.0f}% of freeze)")


def print_app_ops_table(grouped):
    """Print ops/sec for Redis and Memcached: baseline / during / post per mode."""
    has_data = any(k[1] in APP_WORKLOADS for k in grouped)
    if not has_data:
        print("\n[No app workload data in CSV — skipping app ops table]")
        return

    print()
    print("APPLICATION OPS/SEC — BASELINE / DURING / POST SNAPSHOT")
    print("=" * 100)
    print(
        f"{'Workload':>18}  {'Mem':>5}  {'Mode':>5}  "
        f"{'Baseline':>10}  {'During':>10}  {'Post':>10}  {'Degrad%':>8}"
    )
    print("-" * 100)

    for wl in APP_WORKLOADS:
        for mem in APP_MEM_SIZES:
            for mode in ["full", "live"]:
                rr = grouped.get((mem, wl, mode), [])
                if not rr:
                    continue
                b_ops  = avg([r.get("app_baseline_ops", 0) for r in rr])
                d_ops  = avg([r.get("app_during_ops",   0) for r in rr])
                p_ops  = avg([r.get("post_snap_ops",    0) for r in rr])
                degrad = avg([r.get("app_ops_degradation_pct", 0) for r in rr])
                print(
                    f"{wl:>18}  {mem:>5}  {mode:>5}  "
                    f"{b_ops:>10.0f}  {d_ops:>10.0f}  {p_ops:>10.0f}  {degrad:>7.1f}"
                )
        print()


def print_app_latency_table(grouped):
    """Print avg and p99 latency for Redis/Memcached: baseline / during / post."""
    has_data = any(k[1] in APP_WORKLOADS for k in grouped)
    if not has_data:
        print("\n[No app workload data in CSV — skipping app latency table]")
        return

    print()
    print("APPLICATION LATENCY (µs) — AVG and P99 PER WINDOW")
    print("=" * 120)
    print(
        f"{'Workload':>18}  {'Mem':>5}  {'Mode':>5}  "
        f"{'Avg-Base':>10}  {'Avg-Dur':>9}  {'Avg-Post':>9}  "
        f"{'p99-Base':>10}  {'p99-Dur':>9}  {'p99-Post':>9}"
    )
    print("-" * 120)

    for wl in APP_WORKLOADS:
        for mem in APP_MEM_SIZES:
            for mode in ["full", "live"]:
                rr = grouped.get((mem, wl, mode), [])
                if not rr:
                    continue
                avg_b  = avg([r.get("app_baseline_avg_us", 0) for r in rr])
                avg_d  = avg([r.get("app_during_avg_us",  0) for r in rr])
                avg_p  = avg([r.get("post_snap_avg_us",   0) for r in rr])
                p99_b  = avg([r.get("app_baseline_p99_us", 0) for r in rr])
                p99_d  = avg([r.get("app_during_p99_us",  0) for r in rr])
                p99_p  = avg([r.get("post_snap_p99_us",   0) for r in rr])
                print(
                    f"{wl:>18}  {mem:>5}  {mode:>5}  "
                    f"{avg_b:>10.0f}  {avg_d:>9.0f}  {avg_p:>9.0f}  "
                    f"{p99_b:>10.0f}  {p99_d:>9.0f}  {p99_p:>9.0f}"
                )
        print()


def print_stream_table(grouped):
    """Print STREAM Triad bandwidth: baseline / during / post for both modes."""
    has_data = any(k[1] == "stream" for k in grouped)
    if not has_data:
        print("\n[No STREAM workload data in CSV — skipping STREAM table]")
        return

    print()
    print("STREAM BENCHMARK — TRIAD BANDWIDTH (MiB/s): BASELINE / DURING / POST")
    print("=" * 90)
    print(
        f"{'Mem':>5}  {'Mode':>5}  "
        f"{'Base-Copy':>10}  {'Base-Triad':>11}  "
        f"{'Dur-Triad':>10}  {'Post-Triad':>11}  "
        f"{'Degrad%':>8}  {'OvrlMean':>9}  {'OvrlStd':>8}"
    )
    print("-" * 90)

    for mem in APP_MEM_SIZES:
        for mode in ["full", "live"]:
            rr = grouped.get((mem, "stream", mode), [])
            if not rr:
                continue
            b_copy  = avg([r.get("stream_baseline_copy_mibs",  0) for r in rr])
            b_triad = avg([r.get("stream_baseline_triad_mibs", 0) for r in rr])
            d_triad = avg([r.get("stream_during_triad_mibs",   0) for r in rr])
            p_triad = avg([r.get("stream_post_triad_mibs",     0) for r in rr])
            degrad  = avg([r.get("stream_triad_degradation_pct", 0) for r in rr])
            ov_mean = avg([r.get("overall_triad_mean_mibs",    0) for r in rr])
            ov_std  = avg([r.get("overall_triad_stddev_mibs",  0) for r in rr])
            print(
                f"{mem:>5}  {mode:>5}  "
                f"{b_copy:>10.0f}  {b_triad:>11.0f}  "
                f"{d_triad:>10.0f}  {p_triad:>11.0f}  "
                f"{degrad:>7.1f}  {ov_mean:>9.0f}  {ov_std:>8.0f}"
            )
        if mem < APP_MEM_SIZES[-1]:
            print()


def corrected_overall_avg(grouped, mem, wl, mode, metric_base, post_field):
    """Recompute overall latency/ops excluding the zero-during window for full snapshot.

    For full snapshot, the VM was paused so there are no real during-window
    measurements. Averaging in 0.0 from the paused window artificially lowers
    the mean. Instead, average only the baseline and post windows.
    """
    rr = grouped.get((mem, wl, mode), [])
    if not rr:
        return 0.0, 0.0
    b_vals = [float(r.get(f"app_baseline_{metric_base}", 0) or 0) for r in rr]
    p_vals = [float(r.get(post_field, 0) or 0) for r in rr]
    b = sum(b_vals) / len(b_vals) if b_vals else 0.0
    p = sum(p_vals) / len(p_vals) if p_vals else 0.0
    if mode == "full":
        corrected = (b + p) / 2
    else:
        d_vals = [float(r.get(f"app_during_{metric_base}", 0) or 0) for r in rr]
        d = sum(d_vals) / len(d_vals) if d_vals else 0.0
        corrected = (b + d + p) / 3
    return corrected


def print_overall_stats_table(grouped):
    """Print overall run aggregates (mean ± stddev across pre/during/post windows).

    For full snapshot, the during-window value is excluded from the average
    because the VM was paused and served no requests during that window.
    Averaging in 0 would artificially lower the full snapshot mean.
    """
    # Show synthetic workloads (throughput) and app workloads (ops + latency).
    print()
    print("OVERALL RUN STATISTICS — MEAN ± STDDEV ACROSS PRE/DURING/POST WINDOWS")
    print("  NOTE: full snapshot during-window excluded from averages (VM was paused)")

    # Synthetic: throughput
    syn_data = any(
        grouped.get((m, wl, mode), [])
        for m in MEM_SIZES for wl in WORKLOADS if wl != "idle"
        for mode in ["full", "live"]
    )
    if syn_data:
        print()
        print("  Synthetic workloads — overall throughput (MiB/s):")
        print(
            f"  {'Mem':>5}  {'Workload':>8}  {'Mode':>5}  "
            f"{'Mean':>9}  {'Stddev':>8}"
        )
        print("  " + "-" * 50)
        for mem in MEM_SIZES:
            for wl in [w for w in WORKLOADS if w != "idle"]:
                for mode in ["full", "live"]:
                    rr = grouped.get((mem, wl, mode), [])
                    if not rr:
                        continue
                    m_val = avg([r.get("overall_throughput_mean_mibs",   0) for r in rr])
                    s_val = avg([r.get("overall_throughput_stddev_mibs", 0) for r in rr])
                    print(f"  {mem:>5}  {wl:>8}  {mode:>5}  {m_val:>9.1f}  {s_val:>8.1f}")
            print()

    # App workloads: ops + latency
    has_app = any(k[1] in APP_WORKLOADS for k in grouped)
    if has_app:
        print()
        print("  Application workloads — overall ops/sec and latency (µs):")
        print("  (corrected: full snapshot excludes zero-during window)")
        print(
            f"  {'Workload':>18}  {'Mem':>5}  {'Mode':>5}  "
            f"{'AvgLat(corr)':>13}  {'p99(corr)':>10}"
        )
        print("  " + "-" * 70)
        for wl in APP_WORKLOADS:
            for mem in APP_MEM_SIZES:
                for mode in ["full", "live"]:
                    rr = grouped.get((mem, wl, mode), [])
                    if not rr:
                        continue
                    lat_m = corrected_overall_avg(grouped, mem, wl, mode, "avg_us", "post_snap_avg_us")
                    p99_m = corrected_overall_avg(grouped, mem, wl, mode, "p99_us", "post_snap_p99_us")
                    print(
                        f"  {wl:>18}  {mem:>5}  {mode:>5}  "
                        f"{lat_m:>13.0f}  {p99_m:>10.0f}"
                    )
            print()


def print_service_interruption_table(grouped):
    """Print service interruption duration: time server was fully unresponsive.

    For full snapshot this equals the total snapshot time (VM was paused).
    For live snapshot this equals the freeze/downtime window only.
    """
    all_workloads = [wl for wl in WORKLOADS if wl != "idle"] + APP_WORKLOADS + ["stream"]
    all_mems = MEM_SIZES + [m for m in APP_MEM_SIZES if m not in MEM_SIZES]

    has_data = False
    for wl in all_workloads:
        for mem in all_mems:
            if grouped.get((mem, wl, "full"), []) or grouped.get((mem, wl, "live"), []):
                has_data = True
                break

    if not has_data:
        print("\n[No workload data for service interruption table]")
        return

    print()
    print("SERVICE INTERRUPTION (ms) — time server was fully unresponsive")
    print("=" * 70)
    print(
        f"{'Mem(MiB)':>10}  {'Workload':>18}  {'Full (ms)':>10}  "
        f"{'Live (ms)':>10}  {'Ratio':>7}"
    )
    print("-" * 70)

    for wl in all_workloads:
        for mem in all_mems:
            full_rr = grouped.get((mem, wl, "full"), [])
            live_rr = grouped.get((mem, wl, "live"), [])
            if not full_rr and not live_rr:
                continue

            # Full: use service_interruption_ms if present, else fall back to full_total_ms.
            if full_rr:
                si_vals = [r.get("service_interruption_ms") for r in full_rr
                           if r.get("service_interruption_ms")]
                if si_vals:
                    full_ms = avg(si_vals)
                else:
                    full_ms = avg([r["full_total_ms"] for r in full_rr])
            else:
                full_ms = 0.0

            # Live: use service_interruption_ms if present, else fall back to downtime_us.
            if live_rr:
                si_vals = [r.get("service_interruption_ms") for r in live_rr
                           if r.get("service_interruption_ms")]
                if si_vals:
                    live_ms = avg(si_vals)
                else:
                    live_ms = avg([r["downtime_us"] for r in live_rr]) / 1000
            else:
                live_ms = 0.0

            ratio = full_ms / live_ms if live_ms > 0 else float("inf")
            ratio_str = f"{ratio:.1f}x" if ratio != float("inf") else "inf"

            print(
                f"{mem:>10}  {wl:>18}  {full_ms:>9.1f}  "
                f"{live_ms:>9.1f}  {ratio_str:>7}"
            )
        print()


def main():
    csv_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV

    if not os.path.isfile(csv_path):
        print(f"Error: CSV file not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    rows = load_csv(csv_path)
    grouped = group_rows(rows)

    n_rows = len(rows)
    n_configs = len(grouped)
    print(f"Loaded {n_rows} rows across {n_configs} configurations from {csv_path}")
    print()

    print_full_table(grouped)
    print_downtime_table(grouped)
    print_throughput_table(grouped)
    print_fault_table(grouped)
    print_phase_breakdown(grouped)
    print_freeze_breakdown(grouped)
    print_restore_table(grouped)
    print_host_resources(grouped)
    print_key_findings(grouped)
    print_app_ops_table(grouped)
    print_app_latency_table(grouped)
    print_stream_table(grouped)
    print_overall_stats_table(grouped)
    print_service_interruption_table(grouped)


if __name__ == "__main__":
    main()
