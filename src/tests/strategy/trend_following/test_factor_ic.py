"""NEX-33: 6-1-2 factor IC / quintile analysis (strategy.trend_following.factor_ic)."""
import dataclasses
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import numpy as np
import polars as pl

from strategy.trend_following import factor_ic as fic
from strategy.trend_following import run as run_mod
from strategy.trend_following.config import DEV_END, IC_VERDICT_HORIZON, StrategyParams
from strategy.trend_following.dataset import build_dataset
from strategy.trend_following.snapshot import save_snapshot
from tests.strategy.trend_following.helpers import business_days

N_NAMES = 30
START = date(2021, 1, 4)


def _frames(dates, seed=0, tail_noise_from: date | None = None):
    """Random walks with big overnight gaps (open[t+1] != close[t]) and big intraday
    moves (close[t+1] != open[t+1]), so a different base price changes the return."""
    rng = np.random.default_rng(seed)
    n = len(dates)
    gap = rng.normal(0, 0.03, (n, N_NAMES))
    intra = rng.normal(0, 0.02, (n, N_NAMES))
    if tail_noise_from is not None:                   # scramble everything from that date on
        k = next(i for i, d in enumerate(dates) if d >= tail_noise_from)
        rng2 = np.random.default_rng(seed + 99)
        gap[k:] = rng2.normal(0, 0.2, (n - k, N_NAMES))
        intra[k:] = rng2.normal(0, 0.2, (n - k, N_NAMES))
    frames = {}
    for j in range(N_NAMES):
        o = np.empty(n)
        c = np.empty(n)
        prev = 1_000_000.0
        for t in range(n):
            o[t] = prev * np.exp(gap[t, j])
            c[t] = o[t] * np.exp(intra[t, j])
            prev = c[t]
        h = np.maximum(o, c) * 1.001
        lo = np.minimum(o, c) * 0.999
        frames[f"S{j:02d}"] = pl.DataFrame({"Date": dates, "Open": o, "High": h, "Low": lo, "Close": c,
                                            "Volume": np.full(n, 2e6)})
    return frames


def _dataset(n_days=1250, seed=0, tail_noise_from=None):
    dates = business_days(date(2020, 1, 2), n_days)
    frames = _frames(dates, seed, tail_noise_from)
    index = 2000.0 * np.cumprod(np.full(len(dates), 1.0005))
    return build_dataset(dates, index, frames, START)


def _patched(factor_of_book):
    """compute_scores replacement: real scores, but every liquid name qualifies and the
    five factor percentiles and the total are replaced by factor_of_book(book)."""
    real = fic.compute_scores

    def fake(book, params):
        sb = real(book, params)
        v = factor_of_book(book)
        return dataclasses.replace(sb, liquid=np.ones_like(sb.liquid), gate=np.ones_like(sb.gate),
                                   pct={f: v for f in fic.FACTORS}, total=v)
    return fake


def _fwd_open_manual(book, h):
    """close[t+h] / open[t+1] - 1, written out without the module."""
    T = book.T
    out = np.full((T, book.N), np.nan)
    for t in range(T - h):
        out[t] = book.close[t + h] / book.open[t + 1] - 1.0
    return out


class TestForwardReturnBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _dataset(n_days=400)

    def test_forward_return_is_next_open_to_horizon_close(self):
        b = self.ds.stocks
        for h in (5, 10, 20):
            np.testing.assert_allclose(fic.forward_returns(b, h), _fwd_open_manual(b, h), equal_nan=True)

    def test_close_of_signal_day_or_next_close_as_base_gives_different_returns(self):
        b = self.ds.stocks
        h = 5
        good = fic.forward_returns(b, h)
        from_t_close = np.full_like(good, np.nan)
        from_t_close[:b.T - h] = b.close[h:] / b.close[:b.T - h] - 1.0
        from_t1_close = np.full_like(good, np.nan)
        from_t1_close[:b.T - h] = b.close[h:] / b.close[1:b.T - h + 1] - 1.0
        for wrong in (from_t_close, from_t1_close):
            self.assertGreater(np.nanmax(np.abs(good - wrong)), 0.01)

    def test_window_past_the_end_is_nan(self):
        r = fic.forward_returns(self.ds.stocks, 20)
        self.assertTrue(np.isnan(r[-20:]).all())
        self.assertTrue(np.isfinite(r[-21]).all())


class TestRankIC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = _dataset()

    def _mean_ic(self, factor_of_book, h):
        with mock.patch.object(fic, "compute_scores", _patched(factor_of_book)):
            res = fic.run_factor_ic(self.ds)
        return res.ic[(fic.ALL_LABEL, h, "r3")].mean, res

    def test_perfect_factor_gives_ic_one(self):
        for h in (5, 10, 20):
            ic, _ = self._mean_ic(lambda b, h=h: _fwd_open_manual(b, h), h)
            self.assertAlmostEqual(ic, 1.0, places=6)

    def test_inverted_factor_gives_ic_minus_one(self):
        for h in (5, 20):
            ic, _ = self._mean_ic(lambda b, h=h: -_fwd_open_manual(b, h), h)
            self.assertAlmostEqual(ic, -1.0, places=6)

    def test_random_factor_gives_ic_near_zero(self):
        ic, res = self._mean_ic(lambda b: np.random.default_rng(5).normal(size=(b.T, b.N)), 20)
        self.assertLess(abs(ic), 0.05)
        self.assertGreater(res.ic[(fic.ALL_LABEL, 20, "r3")].n, 500)

    def test_factor_built_on_wrong_base_is_not_perfect(self):
        """A factor equal to the return from the signal-day close (or the next close) is not the
        t+1-open return the IC is measured against, so the IC must fall clearly below 1."""
        def from_t_close(b, h):
            out = np.full((b.T, b.N), np.nan)
            out[:b.T - h] = b.close[h:] / b.close[:b.T - h] - 1.0
            return out

        def from_t1_close(b, h):
            out = np.full((b.T, b.N), np.nan)
            out[:b.T - h] = b.close[h:] / b.close[1:b.T - h + 1] - 1.0
            return out

        for h in (5, 20):
            for make in (from_t_close, from_t1_close):
                ic, _ = self._mean_ic(lambda b, h=h, make=make: make(b, h), h)
                self.assertLess(ic, 0.99)
        ic, _ = self._mean_ic(lambda b: from_t_close(b, 5), 5)
        self.assertLess(ic, 0.95)

    def test_stats_fields(self):
        s = fic.ic_stats(np.array([0.1, 0.3, -0.1, np.nan]))
        self.assertEqual(s.n, 3)
        self.assertAlmostEqual(s.mean, 0.1)
        self.assertAlmostEqual(s.std, np.std([0.1, 0.3, -0.1], ddof=1))
        self.assertAlmostEqual(s.ir, s.mean / s.std)
        self.assertAlmostEqual(s.pos_ratio, 2 / 3)

    def test_periods_use_only_their_years(self):
        _, res = self._mean_ic(lambda b: _fwd_open_manual(b, 20), 20)
        n_all = res.ic[(fic.ALL_LABEL, 20, "r3")].n
        n_parts = sum(res.ic[(label, 20, "r3")].n for label in ("2021-22", "2023-24"))
        self.assertEqual(n_all, n_parts)
        self.assertGreater(res.ic[("2021-22", 20, "r3")].n, 400)


class TestQuintiles(unittest.TestCase):
    def test_monotone_total_gives_positive_spread(self):
        ds = _dataset(n_days=900)
        with mock.patch.object(fic, "compute_scores", _patched(lambda b: _fwd_open_manual(b, 20))):
            res = fic.run_factor_ic(ds)
        q = res.quintiles[(fic.ALL_LABEL, IC_VERDICT_HORIZON)]
        self.assertTrue(q.monotone)
        self.assertGreater(q.spread, 0)
        self.assertEqual(list(q.means), sorted(q.means))
        self.assertTrue(res.quintiles_gated[(fic.ALL_LABEL, IC_VERDICT_HORIZON)].monotone)

    def test_reversed_total_is_not_monotone_increasing(self):
        ds = _dataset(n_days=900)
        with mock.patch.object(fic, "compute_scores", _patched(lambda b: -_fwd_open_manual(b, 20))):
            q = fic.run_factor_ic(ds).quintiles[(fic.ALL_LABEL, IC_VERDICT_HORIZON)]
        self.assertFalse(q.monotone)
        self.assertLess(q.spread, 0)

    def test_too_few_names_gives_no_quintiles(self):
        score = np.random.default_rng(0).normal(size=(50, 10))
        self.assertTrue(np.isnan(fic.quintile_returns(score, score, np.ones_like(score, dtype=bool))).all())


class TestVerdict(unittest.TestCase):
    def test_table(self):
        j = fic.judge
        # R3 hypothesis is "-": positive IC in both periods is the opposite direction -> review
        v = j("r3", {"2021-22": 0.03, "2023-24": 0.01})
        self.assertEqual((v.status, v.action), (fic.STATUS_REVIEW, fic.HYPOTHESES["r3"][1]))
        # signs differ -> unstable, whichever way they differ
        self.assertEqual(j("r3", {"2021-22": 0.03, "2023-24": -0.01}).status, fic.STATUS_UNSTABLE)
        self.assertEqual(j("ma50_div", {"2021-22": -0.03, "2023-24": 0.02}).status, fic.STATUS_UNSTABLE)
        # as hypothesised in both -> keep
        self.assertEqual(j("r3", {"2021-22": -0.03, "2023-24": -0.01}).status, fic.STATUS_KEEP)
        self.assertEqual(j("range52", {"2021-22": 0.03, "2023-24": 0.01}).status, fic.STATUS_KEEP)
        # + hypothesis, negative in both -> review with the table action
        v = j("range52", {"2021-22": -0.02, "2023-24": -0.04})
        self.assertEqual((v.status, v.action), (fic.STATUS_REVIEW, "`high52_prox`로 교체 검토"))
        # MA20Div hypothesis is "-" (overheat)
        self.assertEqual(j("ma20_div", {"2021-22": 0.02, "2023-24": 0.02}).status, fic.STATUS_REVIEW)
        self.assertEqual(j("ma20_div", {"2021-22": -0.02, "2023-24": -0.02}).status, fic.STATUS_KEEP)
        # missing or zero mean -> cannot judge
        self.assertEqual(j("r10", {"2021-22": float("nan"), "2023-24": -0.02}).status, fic.STATUS_NA)
        self.assertEqual(j("r10", {"2021-22": 0.0, "2023-24": -0.02}).status, fic.STATUS_NA)

    def test_verdict_uses_the_fixed_horizon_only(self):
        ds = _dataset(n_days=1250)

        def factor(b):          # perfect at 5 days, anti-predictive at 20 days is not constructible
            return _fwd_open_manual(b, 5)                # so just check the plumbing: same keys, h = 20
        with mock.patch.object(fic, "compute_scores", _patched(factor)):
            res = fic.run_factor_ic(ds)
        for v in res.verdicts:
            for label, val in v.period_ic.items():
                self.assertEqual(val, res.ic[(label, IC_VERDICT_HORIZON, v.factor)].mean)

    def test_hypothesis_table_matches_spec(self):
        self.assertEqual({f: s for f, (s, _) in fic.HYPOTHESES.items()},
                         {"ma50_div": 1, "range52": 1, "ma20_div": -1, "r10": -1, "r3": -1})
        self.assertEqual(fic.FACTORS, ("r3", "r10", "range52", "ma20_div", "ma50_div"))

    def test_module_never_changes_parameters(self):
        p = StrategyParams()
        fic.run_factor_ic(_dataset(n_days=600), p)
        self.assertEqual(p, StrategyParams())


class TestHoldoutGuard(unittest.TestCase):
    def test_rows_after_dev_end_are_not_used(self):
        clean = _dataset(n_days=1400, seed=3)
        dirty = _dataset(n_days=1400, seed=3, tail_noise_from=date(2025, 1, 1))
        self.assertGreater(clean.dates[-1], DEV_END)              # the datasets really reach into 2025
        a, b = fic.run_factor_ic(clean), fic.run_factor_ic(dirty)
        self.assertEqual(a.info["dropped_after_dev_end"], b.info["dropped_after_dev_end"])
        self.assertGreater(a.info["dropped_after_dev_end"], 0)
        self.assertLessEqual(date.fromisoformat(a.info["last_date"]), DEV_END)
        self.assertEqual(a.ic, b.ic)
        self.assertEqual(a.quintiles, b.quintiles)
        np.testing.assert_array_equal(a.corr_before, b.corr_before)
        np.testing.assert_array_equal(a.corr_after, b.corr_after)

    def test_cut_book_and_forward_window(self):
        ds = _dataset(n_days=1400)
        book, dropped = fic.cut_book(ds.stocks)
        self.assertLessEqual(book.dates[-1], DEV_END)
        self.assertEqual(book.T + dropped, ds.stocks.T)
        # the last dev sessions have no forward return: nothing is borrowed from 2025
        self.assertTrue(np.isnan(fic.forward_returns(book, 5)[-5:]).all())


class TestCorrelation(unittest.TestCase):
    def test_row_rank_corr_known_values(self):
        x = np.tile(np.arange(12, dtype=float), (3, 1))
        self.assertTrue(np.allclose(fic.row_rank_corr(x, x), 1.0))
        self.assertTrue(np.allclose(fic.row_rank_corr(x, -x), -1.0))
        self.assertTrue(np.allclose(fic.row_rank_corr(x, x ** 3), 1.0))     # rank based, not linear
        self.assertTrue(np.isnan(fic.row_rank_corr(x[:, :5], x[:, :5])).all())     # below MIN_NAMES

    def test_flagged_pairs_in_report(self):
        ds = _dataset(n_days=900)
        res = fic.run_factor_ic(ds)
        # R10 and R3 share their window; MA50Div and Range52 are both price levels: some pair may exceed 0.8
        self.assertEqual(res.corr_before.shape, (5, 5))
        np.testing.assert_allclose(res.corr_after, res.corr_after.T, equal_nan=True)
        for a, b, _, r in res.flagged_pairs():
            self.assertGreater(abs(r), fic.CORR_FLAG)
        # range52 and ma50_div carry no normalisation: identical before and after
        i, k = fic.FACTORS.index("range52"), fic.FACTORS.index("ma50_div")
        self.assertAlmostEqual(res.corr_before[i, k], res.corr_after[i, k], places=9)


class TestReportAndRun(unittest.TestCase):
    def test_markdown_sections(self):
        res = fic.run_factor_ic(_dataset(n_days=900))
        md = res.to_markdown(["- 헤더 줄"])
        for s in ("- 헤더 줄", "상관행렬", "Rank IC", "5분위", "판정 표", "판정 horizon은 20일", "R10", "MA20Div"):
            self.assertIn(s, md)

    def test_headless_factor_ic_report(self):
        ds = _dataset(n_days=900)
        with tempfile.TemporaryDirectory() as tmp:
            man = save_snapshot(ds, Path(tmp) / "snap")
            out = Path(tmp) / "out"
            self.assertEqual(run_mod.main(["--snapshot", str(Path(tmp) / "snap"), "--out", str(out), "--factor-ic"]), 0)
            files = list(out.glob("factor_ic_*.md"))
            self.assertEqual(len(files), 1)
            md = files[0].read_text(encoding="utf-8")
        self.assertIn(man["sha256"], md)
        self.assertRegex(md, r"대상 커밋: `[0-9a-f]{40}(\+dirty)?`")
        self.assertIn("생존편향", md)
        self.assertIn("스펙 버전", md)
        self.assertIn("판정 표", md)

    def test_sparse_universe_renders_without_error(self):
        from tests.strategy.trend_following.helpers import synthetic_dataset
        md = fic.run_factor_ic(synthetic_dataset()).to_markdown()      # 4 names: below every minimum
        self.assertIn(fic.STATUS_NA, md)


if __name__ == "__main__":
    unittest.main()
