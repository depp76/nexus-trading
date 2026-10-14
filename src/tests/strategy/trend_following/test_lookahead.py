"""NEX-31 (checklist G3, A1, A3, A4): look-ahead regression tests on random synthetic paths.

  A3  every indicator / signal / score at row t is identical when the data is cut at t and
      recomputed, and when everything after t is overwritten with garbage;
  A4  names that only exist after t do not move any percentile, total or Top/Bottom list at t;
  A1  in every variant (A0-A5, B) a decision from the close of t fills at t+1 and at that open,
      and a fill never depends on its own session's high / low / close.

Fills are observed through the engine's two execution methods, so what is checked is what the
ledger booked. The flow-timing and zero-volume cases already live in test_nex28_flow_timing.py
and test_nex16_fixes.py and are not repeated here.
"""
import copy
import dataclasses
import unittest
from datetime import date
from unittest.mock import patch

import numpy as np
import polars as pl

from strategy.trend_following import VARIANTS, StrategyParams, compute_features
from strategy.trend_following.backtest import Engine, run_backtest
from strategy.trend_following.costs import CostModel
from strategy.trend_following.dataset import PriceBook, build_dataset
from strategy.trend_following.scoring import ScoreBook, compute_scores, weekly_recommendation
from tests.strategy.trend_following.helpers import business_days, synthetic_dataset

N_DAYS = 430
DATES = business_days(date(2020, 1, 2), N_DAYS)
CUTS = (130, 215, 262, 301, 380, 429)       # row indices; 215+ are past the 201-row 52-week minimum
SEEDS = (1, 7, 23)
SCALAR_FIELDS = {"has_flows", "flow_coverage"}


# ---------------------------------------------------------------------------
# Random world
# ---------------------------------------------------------------------------
def _frame(rng, dates, rows, p0, mu, sigma, vol0=1e6):
    """OHLCV frame of random-walk closes with gap opens, restricted to the session indices `rows`."""
    n = len(dates)
    close = p0 * np.cumprod(1.0 + rng.normal(mu, sigma, n))
    open_ = np.concatenate([[p0], close[:-1]]) * (1.0 + rng.normal(0.0, 0.004, n))
    high = np.maximum(open_, close) * (1.0 + np.abs(rng.normal(0.0, 0.006, n)))
    low = np.minimum(open_, close) * (1.0 - np.abs(rng.normal(0.0, 0.006, n)))
    vol = vol0 * rng.lognormal(0.0, 0.5, n)
    flows = (rng.normal(0.0, 1.0, n) * vol * 0.05, rng.normal(0.0, 1.0, n) * vol * 0.05, rng.normal(0.0, 1.0, n) * vol * 0.1)
    rows = np.asarray(rows)
    price = pl.DataFrame({"Date": [dates[i] for i in rows], "Open": open_[rows], "High": high[rows], "Low": low[rows],
                          "Close": close[rows], "Volume": vol[rows]})
    flow = pl.DataFrame({"Date": [dates[i] for i in rows], "Close": close[rows], "Foreigner": flows[0][rows],
                         "Institution": flows[1][rows], "Retail": flows[2][rows]})
    return price, flow


class World:
    """Frames of `n_names` random names (plus optional late-listed ones) on the module calendar."""

    def __init__(self, seed: int, n_names: int = 9, late: dict[str, int] | None = None, n_days: int = N_DAYS):
        rng = np.random.default_rng(seed)
        self.dates = DATES[:n_days]
        self.index_close = 2000.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.01, n_days))
        self.frames: dict[str, pl.DataFrame] = {}
        self.flows: dict[str, pl.DataFrame] = {}
        for k in range(n_names):
            price, flow = _frame(rng, self.dates, range(n_days), p0=rng.uniform(20_000, 90_000),
                                 mu=rng.uniform(0.0005, 0.003), sigma=rng.uniform(0.012, 0.022))
            self.frames[f"N{k}"], self.flows[f"N{k}"] = price, flow
        for name, first in (late or {}).items():       # names that only exist from session `first` on
            price, flow = _frame(rng, self.dates, range(first, n_days), p0=rng.uniform(20_000, 90_000),
                                 mu=0.004, sigma=0.03, vol0=2e6)
            self.frames[name], self.flows[name] = price, flow

    def build(self, last: int | None = None, drop=(), start_idx: int = 1):
        """Dataset over sessions 0..last (all when None) without the names in `drop`."""
        cutoff = None if last is None else self.dates[last]
        keep = [k for k in self.frames if k not in drop]
        return build_dataset(
            self.dates, self.index_close.copy(), {k: self.frames[k] for k in keep}, self.dates[start_idx],
            markets={k: "KOSPI" for k in keep}, names={k: k for k in keep},
            flow_frames={k: self.flows[k] for k in keep}, cutoff=cutoff)


def _eq(a, b) -> bool:
    a, b = np.asarray(a), np.asarray(b)
    if a.dtype == bool or b.dtype == bool:
        return np.array_equal(a, b)
    return a.shape == b.shape and bool(np.allclose(a, b, rtol=1e-12, atol=0.0, equal_nan=True))


def _first_diff(a, b) -> str:
    a, b = np.asarray(a), np.asarray(b)
    if a.shape != b.shape:
        return f"shape {a.shape} vs {b.shape}"
    bad = np.argwhere(~np.isclose(a.astype(float), b.astype(float), rtol=1e-12, atol=0.0, equal_nan=True))
    return f"first mismatch at {tuple(bad[0])}: {a[tuple(bad[0])]} vs {b[tuple(bad[0])]}" if len(bad) else "equal"


def _feature_arrays(feats):
    return {f.name: getattr(feats, f.name) for f in dataclasses.fields(feats) if f.name not in SCALAR_FIELDS}


def _score_arrays(sb: ScoreBook) -> dict:
    out = {f.name: getattr(sb, f.name) for f in dataclasses.fields(sb)
           if f.name not in ("dates", "tickers", "pct")}
    out.update({f"pct_{k}": v for k, v in sb.pct.items()})
    return out


def _slice_book(book: PriceBook, t: int) -> PriceBook:
    return PriceBook(dates=list(book.dates[:t + 1]), tickers=list(book.tickers), open=book.open[:t + 1],
                     high=book.high[:t + 1], low=book.low[:t + 1], close=book.close[:t + 1],
                     volume=book.volume[:t + 1], markets=dict(book.markets), names=dict(book.names))


def _slice_scores(sb: ScoreBook, t: int, cols: list[int] | None = None) -> ScoreBook:
    """Rows 0..t (and only the columns `cols`) of a ScoreBook, as if computed on data ending at t."""
    c = slice(None) if cols is None else cols
    kw = {}
    for f in dataclasses.fields(sb):
        v = getattr(sb, f.name)
        if f.name == "dates":
            kw[f.name] = list(v[:t + 1])
        elif f.name == "tickers":
            kw[f.name] = list(v) if cols is None else [v[j] for j in cols]
        elif f.name == "pct":
            kw[f.name] = {k: a[:t + 1][:, c] for k, a in v.items()}
        else:
            kw[f.name] = v[:t + 1][:, c]
    return ScoreBook(**kw)


def _garble_after(ds, t: int, seed: int) -> None:
    """Overwrite every input row after t with garbage (prices, volumes, index, flows)."""
    rng = np.random.default_rng(seed)
    b = ds.stocks
    for arr in (b.open, b.high, b.low, b.close):
        arr[t + 1:] = rng.uniform(1.0, 1e6, arr[t + 1:].shape)
    b.volume[t + 1:] = rng.uniform(0.0, 1e9, b.volume[t + 1:].shape)
    ds.index_close[t + 1:] = rng.uniform(100.0, 9000.0, ds.index_close[t + 1:].shape)
    ds.flow_fi[t + 1:] = rng.normal(0.0, 1e12, ds.flow_fi[t + 1:].shape)


# ---------------------------------------------------------------------------
# A3: truncate-and-recompute
# ---------------------------------------------------------------------------
class TestTruncatedRecomputeFeatures(unittest.TestCase):
    def _check(self, params: StrategyParams, upto=lambda t, ds: t):
        for seed in SEEDS:
            world = World(seed)
            full = _feature_arrays(compute_features(world.build(), params))
            for t in CUTS:
                cut = _feature_arrays(compute_features(world.build(last=t), params))
                n = upto(t, world)
                for name, arr in full.items():
                    a = arr[:n + 1]
                    b = cut[name][:n + 1]
                    self.assertTrue(_eq(a, b), f"{name} seed={seed} cut={t}: {_first_diff(a, b)}")

    def test_signals_at_t_equal_the_truncated_recompute(self):
        # MA20/60 trend, Donchian 20/10, ATR20, 20-day ADV, flow strength, RS, 52-week proximity, L1 regime
        self._check(StrategyParams())

    def test_inputs_cover_each_named_indicator(self):
        feats = _feature_arrays(compute_features(World(1).build(), StrategyParams()))
        for name in ("trend_ok", "breakout", "exit_channel", "atr", "adv", "ma_short", "high52_prox",
                     "regime_on", "flow_strength", "flow_ok", "flow_exit", "rs", "vol_ok", "liquid", "traded"):
            self.assertIn(name, feats)
        arr = feats["high52_prox"][-1]
        self.assertTrue(np.isfinite(arr).all())            # the 52-week window is warm at the last row
        self.assertTrue(feats["regime_on"].any() and not feats["regime_on"].all())
        self.assertTrue(feats["breakout"].any() and feats["exit_channel"].any())

    def test_a_later_weekly_check_weekday_only_differs_in_the_unfinished_week(self):
        # For weekday 4 the fallback "last trading day of the week" needs to know the week is over, so the
        # last (unfinished) week of a truncated calendar may flag a day the full data does not.
        def last_week_start(t, world):
            wk = world.dates[t].isocalendar()[:2]
            return max(i for i in range(t + 1) if world.dates[i].isocalendar()[:2] != wk)
        self._check(StrategyParams(check_weekday=4), upto=last_week_start)

    def test_a_perturbed_future_does_not_change_the_past(self):
        params = StrategyParams()
        world = World(5)
        base = _feature_arrays(compute_features(world.build(), params))
        for t in CUTS[:-1]:
            ds = world.build()
            _garble_after(ds, t, seed=t)
            alt = _feature_arrays(compute_features(ds, params))
            for name, arr in base.items():
                self.assertTrue(_eq(arr[:t + 1], alt[name][:t + 1]), f"{name} cut={t}")
            self.assertFalse(_eq(base["atr"][t + 1:], alt["atr"][t + 1:]))      # the garbage really was applied


class TestTruncatedRecomputeScores(unittest.TestCase):
    def test_scores_at_t_equal_the_truncated_recompute(self):
        params = StrategyParams()
        for seed in SEEDS:
            world = World(seed)
            full = _score_arrays(compute_scores(world.build().stocks, params))
            for t in CUTS:
                cut = _score_arrays(compute_scores(world.build(last=t).stocks, params))
                for name, arr in full.items():
                    a, b = arr[:t + 1], cut[name]
                    self.assertTrue(_eq(a, b), f"{name} seed={seed} cut={t}: {_first_diff(a, b)}")

    def test_scores_cover_the_five_indicators_gate_and_total(self):
        sb = compute_scores(World(1).build().stocks, StrategyParams())
        arrays = _score_arrays(sb)
        for name in ("ma50_div", "range52", "ma20_div", "r10", "r3", "gate", "overheated", "total", "total_rank",
                     "pct_ma50_div", "pct_range52", "pct_ma20_div", "pct_r10", "pct_r3"):
            self.assertIn(name, arrays)
        self.assertTrue(sb.gate[-1].any() and sb.liquid[-1].any())        # the comparison is not vacuous
        self.assertTrue(np.isfinite(sb.total[-1]).any())

    def test_a_perturbed_future_does_not_change_past_scores(self):
        params = StrategyParams()
        world = World(11)
        base = _score_arrays(compute_scores(world.build().stocks, params))
        for t in CUTS[1:-1]:
            ds = world.build()
            _garble_after(ds, t, seed=t)
            alt = _score_arrays(compute_scores(ds.stocks, params))
            for name, arr in base.items():
                self.assertTrue(_eq(arr[:t + 1], alt[name][:t + 1]), f"{name} cut={t}")


# ---------------------------------------------------------------------------
# A4: names that only exist after t
# ---------------------------------------------------------------------------
class TestFutureNamesAreInvisible(unittest.TestCase):
    LATE_AT = 300

    def _worlds(self, seed):
        late = {"F1": self.LATE_AT + 1, "F2": self.LATE_AT + 40, "F3": self.LATE_AT + 1}
        with_future = World(seed, late=late)
        base = World(seed)                     # same seed: the first nine names are the same paths
        for k in base.frames:
            self.assertTrue(base.frames[k].equals(with_future.frames[k]))
        return base, with_future, list(late)

    def test_percentiles_totals_and_lists_before_the_listing_are_unchanged(self):
        params = StrategyParams()
        for seed in SEEDS:
            base, with_future, late = self._worlds(seed)
            sb_base = compute_scores(base.build().stocks, params)
            ds = with_future.build()
            sb_fut = compute_scores(ds.stocks, params)
            self.assertEqual(sb_fut.tickers[-3:], late)
            keep = list(range(len(sb_base.tickers)))
            for t in (150, 230, 262, self.LATE_AT):
                a = _score_arrays(sb_base)
                b = _score_arrays(_slice_scores(sb_fut, sb_fut.T - 1, keep))
                for name in a:
                    self.assertTrue(_eq(a[name][:t + 1], b[name][:t + 1]), f"{name} seed={seed} t={t}")
                # the late names themselves are not scored before they exist
                for name in ("total", "total_rank", "ma50_div", "range52"):
                    self.assertTrue(np.isnan(getattr(sb_fut, name)[:self.LATE_AT + 1, len(keep):]).all(), name)

    def test_top_and_bottom_lists_at_t_ignore_future_names(self):
        params = StrategyParams()
        for seed in SEEDS:
            base, with_future, late = self._worlds(seed)
            book_base = base.build().stocks
            book_fut = with_future.build().stocks
            sb_base = compute_scores(book_base, params)
            sb_fut = compute_scores(book_fut, params)
            nonempty = 0
            for t in (230, 262, 280, self.LATE_AT):
                rec_base = weekly_recommendation(_slice_scores(sb_base, t), _slice_book(book_base, t), params)
                rec_fut = weekly_recommendation(_slice_scores(sb_fut, t), _slice_book(book_fut, t), params)
                for side in ("top", "bottom"):
                    got = [(s.ticker, s.week_avg, s.latest_total) for s in getattr(rec_fut, side)]
                    want = [(s.ticker, s.week_avg, s.latest_total) for s in getattr(rec_base, side)]
                    self.assertEqual(got, want, f"{side} seed={seed} t={t}")
                self.assertEqual((rec_fut.n_liquid, rec_fut.n_gated), (rec_base.n_liquid, rec_base.n_gated))
                nonempty += len(rec_base.top) + len(rec_base.bottom)
                self.assertTrue(rec_base.bottom, f"vacuous: empty Bottom list seed={seed} t={t}")
            self.assertGreater(nonempty, 0)

    def test_a_name_that_is_listed_later_changes_the_lists_only_from_its_listing_on(self):
        # Control: the same comparison does detect a name that exists at t (so the equalities above can fail).
        params = StrategyParams()
        base, with_future, _ = self._worlds(2)
        sb_base = compute_scores(base.build().stocks, params)
        sb_fut = compute_scores(with_future.build().stocks, params)
        keep = list(range(sb_base.N))
        t = self.LATE_AT + 60                  # F1, F2, F3 all exist by now
        a, b = sb_base.total_rank[:t + 1], _slice_scores(sb_fut, sb_fut.T - 1, keep).total_rank[:t + 1]
        self.assertFalse(_eq(a, b))
        self.assertTrue(_eq(a[:self.LATE_AT + 1], b[:self.LATE_AT + 1]))


# ---------------------------------------------------------------------------
# A1: price-signal fills happen at t+1 open, in every variant
# ---------------------------------------------------------------------------
def run_recorded(ds, params: StrategyParams, variant, cm: CostModel | None = None, feats=None):
    """(result, features, fills) with fills = [(side, session index, ticker, qty, price, reason)]."""
    cm = cm or CostModel()
    feats = feats or compute_features(ds, params)
    fills = []
    buy, sell = Engine._execute_buy, Engine._execute_sell

    def rec_buy(self, t, ticker, qty, price, reason, stop_offset):
        fills.append(("buy", t, ticker, qty, price, reason))
        return buy(self, t, ticker, qty, price, reason, stop_offset)

    def rec_sell(self, t, pos, qty, price, reason):
        fills.append(("sell", t, pos.ticker, qty, price, reason))
        return sell(self, t, pos, qty, price, reason)

    with patch.object(Engine, "_execute_buy", rec_buy), patch.object(Engine, "_execute_sell", rec_sell):
        res = run_backtest(ds, feats, params, variant, cm)
    return res, feats, fills


def _synthetic_worlds():
    return {"synthetic": synthetic_dataset(with_flows=True), "random": World(3).build(start_idx=262)}


class TestFillsAreNextOpen(unittest.TestCase):
    def _entry_signal(self, feats, v, s: int, j: int) -> bool:
        if v.mode == "high52":
            return bool(feats.check_day[s] and feats.regime_on[s] and feats.trend_ok[s, j]
                        and feats.liquid[s, j] and feats.traded[s, j])
        ok = feats.trend_ok[s, j] and feats.breakout[s, j] and feats.liquid[s, j] and feats.traded[s, j]
        if v.regime:
            ok = ok and feats.regime_on[s]
        if v.volume:
            ok = ok and feats.vol_ok[s, j]
        if v.flow:
            ok = ok and feats.flow_ok[s, j]
        return bool(ok)

    def _exit_signal(self, feats, ds, p, v, fills_before, s: int, j: int, reason: str, ticker: str) -> bool:
        if reason == "channel":
            return bool(feats.exit_channel[s, j])
        if reason == "flow":
            return bool(feats.flow_exit[s, j])
        if reason in ("rank_out", "weight_trim"):
            return bool(feats.check_day[s])
        if reason == "stop":
            entry = [f for f in fills_before if f[0] == "buy" and f[2] == ticker][-1]
            stop = entry[4] - p.stop_atr_multiple * feats.atr[entry[1] - 1, j]
            return bool(ds.stocks.close[s, j] < stop)
        raise AssertionError(f"unexpected exit reason {reason}")

    def test_every_variant_fills_signals_at_the_next_session_open(self):
        p, cm = StrategyParams(), CostModel()
        for world_name, ds in _synthetic_worlds().items():
            b = ds.stocks
            for vid, v in VARIANTS.items():
                with self.subTest(world=world_name, variant=vid):
                    res, feats, fills = run_recorded(ds, p, v, cm)
                    self.assertTrue(any(f[0] == "buy" for f in fills), "no entries: the test would be vacuous")
                    kinds = set()
                    for i, (side, t, tk, qty, price, reason) in enumerate(fills):
                        j = b.col(tk)
                        d, market = b.dates[t], b.market_of(tk)
                        kinds.add((side, reason))
                        if reason == "year_end_trim":
                            self.assertEqual(price, cm.fill_price(d, market, b.close[t, j], "sell"))
                            self.assertNotEqual(b.dates[t + 1].year, d.year)
                            continue
                        if reason == "delisted":
                            continue
                        self.assertGreater(t - 1, ds.start_idx - 1)                      # decided inside the window
                        self.assertEqual(price, cm.fill_price(d, market, b.open[t, j], side), f"{fills[i]}")
                        if side == "buy":
                            self.assertEqual(reason, "entry")
                            self.assertTrue(self._entry_signal(feats, v, t - 1, j), f"{fills[i]}")
                        else:
                            self.assertTrue(self._exit_signal(feats, ds, p, v, fills[:i], t - 1, j, reason, tk),
                                            f"{fills[i]}")
                    # the ledger and the fills agree
                    for tr in res.trades:
                        if tr.reason not in ("year_end_trim", "delisted"):
                            self.assertIn(tr.exit_date, b.dates)
                    self.assertTrue(res.trades)

    def test_exit_kinds_are_exercised(self):
        # The per-variant loop above is only meaningful if channel / flow / stop-or-rank exits actually occur.
        p = StrategyParams()
        ds = synthetic_dataset(with_flows=True)
        seen = set()
        for vid in ("A0", "A5", "B"):
            seen |= {f[5] for f in run_recorded(ds, p, VARIANTS[vid])[2]}
        self.assertTrue({"entry", "channel", "flow"} <= seen, seen)

    def test_a_fill_does_not_depend_on_its_own_session_high_low_close(self):
        p = StrategyParams()
        for vid in ("A0", "A5", "B"):
            ds = synthetic_dataset(with_flows=True)
            feats = compute_features(ds, p)
            _, _, base = run_recorded(ds, p, VARIANTS[vid], feats=feats)
            t0 = next(f[1] for f in base if f[5] == "entry") - 1          # decision session of the first entry
            alt = copy.deepcopy(ds)
            rng = np.random.default_rng(0)
            b = alt.stocks
            for arr in (b.high, b.low, b.close):
                arr[t0 + 1:] *= rng.uniform(0.5, 2.0, arr[t0 + 1:].shape)
            _, _, got = run_recorded(alt, p, VARIANTS[vid], feats=feats_like(alt, p, feats, t0))
            first = [f for f in base if f[1] <= t0 + 1]
            self.assertTrue(first)
            self.assertEqual(first, [f for f in got if f[1] <= t0 + 1], vid)

    def test_overwriting_everything_after_t_leaves_fills_up_to_t_unchanged(self):
        p = StrategyParams()
        for vid, v in VARIANTS.items():
            ds = synthetic_dataset(with_flows=True)
            _, _, base = run_recorded(ds, p, v)
            t = ds.start_idx + 200
            alt = copy.deepcopy(ds)
            _garble_after(alt, t, seed=9)
            _, _, got = run_recorded(alt, p, v)
            want = [f for f in base if f[1] <= t]
            self.assertTrue(want, vid)
            self.assertEqual(want, [f for f in got if f[1] <= t], vid)


def feats_like(ds, p, base_feats, t0):
    """Features of the altered dataset, kept equal to the baseline's up to the decision session t0.

    Rows after t0 are recomputed on the altered prices (they only affect decisions made after the t0+1 fill),
    rows up to t0 are asserted identical so the comparison isolates the fill itself."""
    new = compute_features(ds, p)
    for f in dataclasses.fields(new):
        if f.name in SCALAR_FIELDS:
            continue
        a, b = getattr(base_feats, f.name), getattr(new, f.name)
        if not _eq(a[:t0 + 1], b[:t0 + 1]):
            raise AssertionError(f"{f.name} changed at or before the decision session")
    return new


if __name__ == "__main__":
    unittest.main()
