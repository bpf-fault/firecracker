# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Metric parsing and statistics helpers for the snapshot live experiment."""

import math
import re
import statistics
from pathlib import Path


def _compute_overall_stats(values):
    """Compute (mean, stddev, min, max) from a list of finite floats.

    Filters out None, NaN, and non-finite values before computing. Returns
    (0.0, 0.0, 0.0, 0.0) if fewer than one valid value remains; stddev is
    0.0 when fewer than two valid values are present.
    """
    valid = [v for v in values if v is not None and math.isfinite(v)]
    if not valid:
        return 0.0, 0.0, 0.0, 0.0
    mean = statistics.mean(valid)
    stddev = statistics.stdev(valid) if len(valid) >= 2 else 0.0
    return mean, stddev, min(valid), max(valid)


def _parse_stream_log_all_runs(log_text):
    """Parse all completed STREAM benchmark runs from a cumulative log.

    Each run prints Copy/Scale/Add/Triad lines. Returns a list of dicts
    with keys "copy", "scale", "add", "triad" in MiB/s (converted from
    the MB/s values STREAM reports).
    """
    runs = []
    mb_to_mib = 1e6 / (1024 * 1024)
    # Split log into individual invocation blocks on the STREAM header line.
    blocks = re.split(r"-------------------------------------------------------------", log_text)
    for block in blocks:
        result = {}
        for kernel in ("Copy", "Scale", "Add", "Triad"):
            m = re.search(rf"^{kernel}:\s+([\d.]+)", block, re.MULTILINE | re.IGNORECASE)
            if m:
                result[kernel.lower()] = float(m.group(1)) * mb_to_mib
        if len(result) == 4:
            runs.append(result)
    return runs


def _parse_live_snapshot_log(log_data):
    """Extract timing metrics from Firecracker live-snapshot log lines.

    Returns a dict with timing values in microseconds.
    """
    metrics = {}

    m = re.search(r"populate_pages took (\d+) us", log_data)
    if m:
        metrics["populate_pages_us"] = int(m.group(1))

    m = re.search(r"Phase 1 took (\d+) us", log_data)
    if m:
        metrics["phase1_us"] = int(m.group(1))

    m = re.search(
        r"Phase 2 \(freeze\) took (\d+) us "
        r"\(pause=(\d+) us, save_state=(\d+) us, "
        r"wp_enable=(\d+) us, resume=(\d+) us\)",
        log_data,
    )
    if m:
        metrics["freeze_us"] = int(m.group(1))
        metrics["pause_us"] = int(m.group(2))
        metrics["save_state_us"] = int(m.group(3))
        metrics["wp_enable_us"] = int(m.group(4))
        metrics["resume_us"] = int(m.group(5))

    m = re.search(
        r"Phase 3 \(stream\) took (\d+) us, (\d+) pages total "
        r"\((\d+) (?:fault-driven|ring-buffer), (\d+) linear-scan(?:, \d+ ring-drops)?\)",
        log_data,
    )
    if m:
        metrics["stream_us"] = int(m.group(1))
        metrics["total_pages"] = int(m.group(2))
        metrics["fault_pages"] = int(m.group(3))
        metrics["linear_pages"] = int(m.group(4))

    m = re.search(r"Phase 4 \(finalize\) took (\d+) us", log_data)
    if m:
        metrics["finalize_us"] = int(m.group(1))

    m = re.search(
        r"(?:Live snapshot|Live-BPF snapshot): complete in (\d+) us \(freeze/downtime=(\d+) us\)",
        log_data,
    )
    if m:
        metrics["total_us"] = int(m.group(1))
        metrics["downtime_us"] = int(m.group(2))

    return metrics


def _get_rss_kib(pid):
    """Read current RSS from /proc/<pid>/status in KiB."""
    try:
        status = Path(f"/proc/{pid}/status").read_text("utf-8")
        m = re.search(r"VmRSS:\s+(\d+)\s+kB", status)
        if m:
            return int(m.group(1))
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0


def _get_peak_rss_kib(pid):
    """Read peak RSS (VmHWM) from /proc/<pid>/status in KiB."""
    try:
        status = Path(f"/proc/{pid}/status").read_text("utf-8")
        m = re.search(r"VmHWM:\s+(\d+)\s+kB", status)
        if m:
            return int(m.group(1))
    except (FileNotFoundError, ProcessLookupError):
        pass
    return 0
