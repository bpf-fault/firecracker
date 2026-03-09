# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Synthetic memory-write workload helpers for the snapshot live experiment."""

import re
import time

from ..constants import WORKLOAD_PARAMS


def _start_workload(vm, workload):
    """Start a controlled-rate memory write workload inside the guest.

    Returns the measured baseline write rate in MiB/s, or 0.0 for idle.
    """
    if workload == "idle":
        return 0.0

    bs, count, sleep_s = WORKLOAD_PARAMS[workload]

    # Calibrate: run one burst and measure throughput.
    total_bytes = bs * count
    _, stdout, _ = vm.ssh.check_output(
        f"dd if=/dev/urandom of=/tmp/calibrate bs={bs} count={count} 2>&1 "
        "| tail -1",
        timeout=30,
    )
    # Parse dd output for throughput (e.g. "... 1048576 bytes ... copied, 0.123 s, 8.1 MB/s")
    baseline_mibs = 0.0
    m = re.search(r"([\d.]+)\s+s,", stdout)
    if m:
        elapsed = float(m.group(1))
        if elapsed > 0:
            baseline_mibs = (total_bytes / (1024 * 1024)) / elapsed

    # Clean up calibration file.
    vm.ssh.check_output("rm -f /tmp/calibrate")

    # Start the continuous workload in the background.
    vm.ssh.check_output(
        f"nohup sh -c '"
        f"while true; do "
        f"  dd if=/dev/urandom of=/tmp/workload bs={bs} count={count} 2>/dev/null; "
        f"  sleep {sleep_s}; "
        f"done' </dev/null >/dev/null 2>&1 &"
    )

    # Let the workload stabilise.
    time.sleep(2)
    return baseline_mibs


def _measure_workload_throughput(vm, workload):
    """Measure current write throughput inside the guest.

    Runs a single timed burst matching the workload parameters.
    Returns throughput in MiB/s, or 0.0 for idle.
    """
    if workload == "idle":
        return 0.0

    bs, count, _ = WORKLOAD_PARAMS[workload]
    total_bytes = bs * count

    _, stdout, _ = vm.ssh.check_output(
        f"dd if=/dev/urandom of=/tmp/measure bs={bs} count={count} 2>&1 "
        "| tail -1",
        timeout=30,
    )
    vm.ssh.check_output("rm -f /tmp/measure")

    m = re.search(r"([\d.]+)\s+s,", stdout)
    if m:
        elapsed = float(m.group(1))
        if elapsed > 0:
            return (total_bytes / (1024 * 1024)) / elapsed
    return 0.0


def _stop_workload(vm):
    """Kill any background dd/sh workload processes in the guest."""
    vm.ssh.check_output("pkill -f 'dd if=/dev/urandom' 2>/dev/null || true")
    vm.ssh.check_output("pkill -f 'of=/tmp/workload' 2>/dev/null || true")
