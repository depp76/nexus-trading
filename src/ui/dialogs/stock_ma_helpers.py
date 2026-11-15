"""ui/dialogs/stock_ma_helpers.py — pure helpers for the MA chart dialog
(ui/dialogs/stock_ma.py): vectorised volume-bar polygons and the compact
number format of the end labels and axis ticks.

Moved out of ui/dialogs/stock_ma.py (2026-11-15, code review #5).
"""
import numpy as np


def _make_bars(x, y_bottom, y_top, w):
    """Vectorised (N, 4, 2) rectangle polygons for volume bars; NaN rows dropped."""
    x = np.asarray(x, dtype=float)
    y_bottom = np.asarray(y_bottom, dtype=float)
    y_top = np.asarray(y_top, dtype=float)
    valid = ~(np.isnan(y_bottom) | np.isnan(y_top))
    x, y_bottom, y_top = x[valid], y_bottom[valid], y_top[valid]
    if len(x) == 0:
        return []
    w2 = w / 2
    polys = np.empty((len(x), 4, 2))
    polys[:, 0] = np.stack([x - w2, y_bottom], axis=1)
    polys[:, 1] = np.stack([x - w2, y_top], axis=1)
    polys[:, 2] = np.stack([x + w2, y_top], axis=1)
    polys[:, 3] = np.stack([x + w2, y_bottom], axis=1)
    return polys


def _compact(v: float) -> str:
    """1,849,000 -> '1.85M', 71,200 -> '71.2k' -- for the narrow end labels."""
    a = abs(v)
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e3:
        return f"{v / 1e3:.1f}k"
    return f"{v:,.2f}" if a < 100 else f"{v:,.0f}"
