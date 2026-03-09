# Copyright 2025 Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared statistical helpers for experiment analysis scripts."""


def avg(vals):
    """Mean of a list of numbers; empty/blank values are ignored."""
    vals = [float(v) for v in vals if v]
    return sum(vals) / len(vals) if vals else 0.0


def stdev(vals):
    """Sample standard deviation; returns 0 for < 2 values."""
    vals = [float(v) for v in vals if v]
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return (sum((x - m) ** 2 for x in vals) / (len(vals) - 1)) ** 0.5


def linear_regression(xs, ys):
    """Least-squares linear fit; returns (slope, intercept, r_squared)."""
    n = len(xs)
    if n < 2:
        return 0.0, 0.0, 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    ss_xy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    ss_xx = sum((x - mx) ** 2 for x in xs)
    if ss_xx == 0:
        return 0.0, my, 0.0
    slope = ss_xy / ss_xx
    intercept = my - slope * mx
    y_pred = [slope * x + intercept for x in xs]
    ss_res = sum((y - yp) ** 2 for y, yp in zip(ys, y_pred))
    ss_tot = sum((y - my) ** 2 for y in ys)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return slope, intercept, r2


def cv_pct(vals):
    """Coefficient of variation as a percentage."""
    vals = [float(v) for v in vals if v]
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    if m == 0:
        return 0.0
    sd = stdev([str(v) for v in vals])
    return sd / m * 100.0
