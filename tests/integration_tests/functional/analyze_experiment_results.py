#!/usr/bin/env python3
# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Analyze and print tables from live snapshot experiment results CSV.

Usage:
    python3 analyze_experiment_results.py [path/to/experiment_results.csv]

If no path is given, defaults to test_results/experiment_results.csv
relative to the repo root.
"""

import csv
import os
import sys
from collections import defaultdict


def _repo_root():
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )


DEFAULT_CSV = os.path.join(_repo_root(), "test_results", "experiment_results.csv")

MEM_SIZES = [256, 512, 1024, 2048, 4096]
WORKLOADS = ["idle", "light", "medium", "heavy"]


def load_csv(path):
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def group_rows(rows):
    """Group rows by (mem_size_mib, workload, snapshot_mode), averaging across
    PCI modes and iterations."""
    grouped = defaultdict(list)
    for r in rows:
        key = (int(r["mem_size_mib"]), r["workload"], r["snapshot_mode"])
        grouped[key].append(r)
    return grouped


def avg(vals):
    vals = [float(v) for v in vals if v]
    return sum(vals) / len(vals) if vals else 0


def stdev(vals):
    vals = [float(v) for v in vals if v]
    if len(vals) < 2:
        return 0
    m = sum(vals) / len(vals)
    return (sum((x - m) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5


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


if __name__ == "__main__":
    main()
