"""strategy/trend_following/event_study.py — 6-1 pre-validation.

Collects every Donchian-20 breakout in the liquid universe and compares the
forward 5 / 20 / 60-session returns (from the next open, i.e. where the
strategy would have been filled) across the conditions the strategy proposes
to filter on: volume confirmation, foreign+institution net buying vs selling
over the prior 20 sessions, and breakouts carried by retail alone. Each row
also carries the KOSPI-adjusted excess return so a bull-market drift does not
masquerade as a filter effect.
"""
from __future__ import annotations

import numpy as np

from strategy.trend_following.config import StrategyParams
from strategy.trend_following.signals import Features, rolling

HORIZONS = (5, 20, 60)
RETAIL_GROUP = "R: retail-only breakout (5d: FI <= 0 & retail > 0)"


def _stats(rows: np.ndarray, excess: np.ndarray, horizons) -> dict:
    out: dict = {"n": int(rows.shape[0])}
    for k, h in enumerate(horizons):
        r = rows[:, k]
        e = excess[:, k]
        good = np.isfinite(r)
        rr, ee = r[good], e[good]
        out[f"mean_{h}"] = float(np.mean(rr)) if rr.size else float("nan")
        out[f"median_{h}"] = float(np.median(rr)) if rr.size else float("nan")
        out[f"hit_{h}"] = float(np.mean(rr > 0)) if rr.size else float("nan")
        out[f"excess_{h}"] = float(np.mean(ee)) if ee.size else float("nan")
    return out


def run_event_study(ds, feats: Features, params: StrategyParams, horizons=HORIZONS,
                    require_trend: bool = False) -> list[dict]:
    """One row per condition group: n, mean/median/hit-rate/excess per horizon."""
    b = ds.stocks
    T = b.T
    t0 = ds.start_idx
    max_h = max(horizons)
    idx = np.asarray(ds.index_close, dtype=float)

    event_mask = feats.breakout & feats.liquid
    if require_trend:
        event_mask &= feats.trend_ok
    ts, js = np.nonzero(event_mask)
    keep = (ts >= t0) & (ts + max_h <= T - 1)
    ts, js = ts[keep], js[keep]
    if ts.size == 0:
        return [{"group": "all", "n": 0}]

    base = b.open[ts + 1, js]
    valid = np.isfinite(base) & (base > 0)
    ts, js, base = ts[valid], js[valid], base[valid]
    if ts.size == 0:
        return [{"group": "all", "n": 0}]

    rets = np.full((ts.size, len(horizons)), np.nan)
    excess = np.full((ts.size, len(horizons)), np.nan)
    for k, h in enumerate(horizons):
        end_px = b.close[ts + h, js]
        rets[:, k] = end_px / base - 1.0
        mkt = idx[ts + h] / idx[ts + 1] - 1.0
        excess[:, k] = rets[:, k] - mkt

    groups: list[tuple[str, np.ndarray]] = [("all breakouts", np.ones(ts.size, dtype=bool))]
    vol_ok = feats.vol_ok[ts, js]
    groups.append(("V: volume >= 1.5x avg", vol_ok))
    groups.append(("V: volume < 1.5x avg", ~vol_ok))

    if feats.has_flows and ds.flow_fi is not None:
        fs = feats.flow_strength[ts, js]
        known = np.isfinite(fs)
        groups.append(("F: FI net buy (20d)", known & (fs > 0)))
        groups.append(("F: FI net sell (20d)", known & (fs <= 0)))
        # Spec 6-1 "retail-only breakout": foreigner+institution 5d net <= 0 and
        # individual 5d net > 0, using the measured flow_retail (NEX-23 onward;
        # NEX-27). Same convention as the signals: flows through the event day t
        # (known after the close), fill at t+1 open. A day with a missing value on
        # either side is excluded, never zero-filled.
        fi5 = rolling(ds.flow_fi, params.flow_short_window, "sum")[ts, js]
        known5 = np.isfinite(fi5)
        retail_group = np.zeros(ts.size, dtype=bool)
        if ds.flow_retail is not None:
            rt5 = rolling(ds.flow_retail, params.flow_short_window, "sum")[ts, js]
            retail_group = known5 & np.isfinite(rt5) & (fi5 <= 0) & (rt5 > 0)
        groups.append((RETAIL_GROUP, retail_group))
        groups.append(("FI-backed breakout (5d: FI > 0)", known5 & (fi5 > 0)))

    rows = []
    for name, m in groups:
        row = {"group": name}
        row.update(_stats(rets[m], excess[m], horizons))
        rows.append(row)
    return rows
