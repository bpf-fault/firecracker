# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""STREAM benchmark helpers for the snapshot live experiment."""

import re


def _parse_stream_output(output):
    """Parse STREAM benchmark output into a dict of MiB/s values.

    Converts MB/s → MiB/s (×1e6 / 1024²).
    Returns {"copy": X, "scale": X, "add": X, "triad": X}.
    """
    results = {}
    mb_to_mib = 1e6 / (1024 * 1024)
    for kernel in ("Copy", "Scale", "Add", "Triad"):
        m = re.search(rf"{kernel}:\s+([\d.]+)", output, re.IGNORECASE)
        if m:
            results[kernel.lower()] = float(m.group(1)) * mb_to_mib
    return results


def _run_stream_benchmark(vm):
    """Execute /usr/local/bin/stream and return (copy, scale, add, triad) in MiB/s."""
    _, out, _ = vm.ssh.check_output("/usr/local/bin/stream", timeout=120)
    r = _parse_stream_output(out)
    return r.get("copy", 0.0), r.get("scale", 0.0), r.get("add", 0.0), r.get("triad", 0.0)


def _start_stream_during_burst(vm):
    """Launch a STREAM benchmark burst in the background.

    Writes output to /tmp/stream_during.log and touches /tmp/stream_during.done.
    """
    vm.ssh.check_output(
        "nohup sh -c '"
        "/usr/local/bin/stream > /tmp/stream_during.log 2>&1; "
        "touch /tmp/stream_during.done' "
        "</dev/null >/dev/null 2>&1 &"
    )
