"""universe_signals.pick_signals: the Universe "전략 신호" lists come from the
backtest's own signal code (2026-11-16). Synthetic paths (helpers), no network."""
import unittest
from dataclasses import replace

import numpy as np

from strategy.trend_following.config import StrategyParams, VARIANTS
from strategy.trend_following.dataset import PriceBook
from strategy.trend_following.scoring import compute_scores, weekly_recommendation
from strategy.trend_following.signals import compute_features
from strategy.trend_following.universe_signals import pick_signals
from tests.strategy.trend_following.helpers import TOP, synthetic_dataset
from tests.strategy.trend_following.test_scoring import score_book


def _cut(book: PriceBook, t: int) -> PriceBook:
    """`book` through session t (inclusive)."""
    return replace(book, dates=book.dates[:t + 1], open=book.open[:t + 1], high=book.high[:t + 1],
                   low=book.low[:t + 1], close=book.close[:t + 1], volume=book.volume[:t + 1])


class TestBreakoutAndHigh52(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def _picks(self, vid, t=None, flows=True):
        book, idx, fi = self.ds.stocks, self.ds.index_close, self.ds.flow_fi
        if t is not None:
            book, idx, fi = _cut(book, t), idx[:t + 1], fi[:t + 1]
        return pick_signals(vid, book, idx, fi if flows else None)

    def test_breakout_top_is_todays_entry_signal_and_not_padded(self):
        p = self._picks("A0")
        self.assertEqual([s.ticker for s in p.top], ["UP"])          # the only breakout today
        self.assertEqual((p.top_kind, p.bottom_kind, p.n_top_candidates), ("entry", "exit", 1))
        self.assertTrue(p.top[0].value_label.startswith("RS +"))
        # V filter: UP has its volume spike only every 7th session
        f = compute_features(self.ds, StrategyParams())
        t = self.ds.stocks.T - 1
        expect = ["UP"] if f.vol_ok[t, self.ds.stocks.col("UP")] else []
        self.assertEqual([s.ticker for s in self._picks("A2").top], expect)

    def test_flow_rows_need_flows(self):
        self.assertEqual([s.ticker for s in self._picks("A3").top], ["UP"])
        self.assertEqual(self._picks("A3", flows=False).top, [])     # no flows -> the F filter never passes

    def test_bottom_is_the_trend_break_exit(self):
        t = TOP + 3                                                  # SPIKE falls 2%/day after TOP
        p = self._picks("A0", t=t)
        f = compute_features(self.ds, StrategyParams())
        exits = {tk for j, tk in enumerate(self.ds.stocks.tickers)
                 if f.exit_channel[t, j] and f.liquid[t, j] and f.traded[t, j]}
        self.assertIn("SPIKE", exits)
        self.assertEqual({s.ticker for s in p.bottom}, exits)
        rs = [s.value for s in p.bottom]
        self.assertEqual(rs, sorted(rs))                             # weakest relative strength first

    def test_a5_adds_the_flow_exit(self):
        a0, a5 = self._picks("A0"), self._picks("A5")
        self.assertNotIn("DRIFT", [s.ticker for s in a0.bottom])
        self.assertIn("DRIFT", [s.ticker for s in a5.bottom])        # flows negative + close < MA20

    def test_b_ranks_trend_names_by_52w_high_proximity(self):
        p = self._picks("B")
        self.assertEqual([s.ticker for s in p.top], ["UP"])
        self.assertEqual(p.top_kind, "high52")
        self.assertIn("52주 고가의", p.top[0].value_label)
        self.assertTrue(p.uses_regime)
        self.assertTrue(p.regime_on)


class TestScoreRows(unittest.TestCase):
    def test_c_rows_are_the_weekly_recommendation_with_their_overrides(self):
        book, _dates = score_book()
        for vid in ("C1", "C2", "C3"):
            p = VARIANTS[vid].apply(StrategyParams())
            rec = weekly_recommendation(compute_scores(book, p), book, p)
            picks = pick_signals(vid, book, None)
            self.assertEqual([s.ticker for s in picks.top], [s.ticker for s in rec.top], vid)
            self.assertEqual([s.ticker for s in picks.bottom], [s.ticker for s in rec.bottom], vid)
            self.assertIsNotNone(picks.recommendation)
            self.assertEqual(picks.top_kind, "score")
            self.assertIsNone(picks.regime_on)                       # no index closes given

    def test_n_caps_both_lists(self):
        book, _dates = score_book()
        picks = pick_signals("C1", book, None, n=1)
        self.assertLessEqual(len(picks.top), 1)
        self.assertLessEqual(len(picks.bottom), 1)


class TestEveryVariantRuns(unittest.TestCase):
    def test_all_rows(self):
        ds = synthetic_dataset(with_flows=True)
        for vid in VARIANTS:
            p = pick_signals(vid, ds.stocks, ds.index_close, ds.flow_fi)
            self.assertEqual(p.variant_id, vid)
            self.assertLessEqual(len(p.top), 10)
            self.assertTrue(all(np.isfinite(s.close) for s in p.top + p.bottom))


if __name__ == "__main__":
    unittest.main()
