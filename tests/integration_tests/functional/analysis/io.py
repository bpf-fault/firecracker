# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared I/O helpers and data-schema constants for experiment analysis scripts."""

import csv
import os
from collections import defaultdict


def _repo_root():
    return os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    )


DEFAULT_CSV = os.path.join(_repo_root(), "test_results", "experiment_results.csv")

MEM_SIZES = [256, 512, 1024, 2048, 4096]
WORKLOADS = ["idle", "light", "medium", "heavy"]
APP_MEM_SIZES = [2048, 4096, 8192]
APP_WORKLOADS = [
    "redis_light", "redis_mixed", "redis_heavy",
    "memcached_light", "memcached_heavy",
]
STREAM_KERNELS = ["copy", "scale", "add", "triad"]


def load_csv(path):
    """Load CSV rows as dicts."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def group_rows(rows):
    """Group rows by (mem_size_mib, workload, snapshot_mode)."""
    grouped = defaultdict(list)
    for r in rows:
        key = (int(r["mem_size_mib"]), r["workload"], r["snapshot_mode"])
        grouped[key].append(r)
    return grouped
