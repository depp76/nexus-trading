"""signal_provider (trend following's strategy.signal_api provider): the
Universe "전략 신호" lists and the MA chart "전략 점검" come from the
backtest's own signal code (2026-11-16). Synthetic paths (helpers), no network."""
import unittest
from dataclasses import replace

import numpy as np

from strategy.trend_following.config import StrategyParams, VARIANTS
from strategy.trend_following.dataset import PriceBook
from strategy.trend_following.scoring import compute_scores, weekly_recommendation
from strategy.trend_following.signals import compute_features
from strategy.trend_following.signal_provider import check_stock, pick_signals
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
        self.assertEqual(p.n_top_candidates, 1)
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
        # No liquidity / traded filter on exits (review 2026-11-20 H14), like the backtest.
        exits = {tk for j, tk in enumerate(self.ds.stocks.tickers) if f.exit_channel[t, j]}
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
            self.assertIsNotNone(picks.detail)
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


class TestStockCheck(unittest.TestCase):
    """check_stock: one stock's conditions under a variant (MA chart "전략 점검")."""

    @classmethod
    def setUpClass(cls):
        cls.ds = synthetic_dataset(with_flows=True)

    def _check(self, vid, ticker, t=None, picks=None):
        book, idx, fi = self.ds.stocks, self.ds.index_close, self.ds.flow_fi
        if t is not None:
            book, idx, fi = _cut(book, t), idx[:t + 1], fi[:t + 1]
        return check_stock(vid, ticker, book, idx, fi, picks=picks)

    def test_entry_matches_the_universe_list(self):
        c = self._check("A0", "UP")
        self.assertEqual(c.verdict, "entry")
        self.assertTrue(all(r.ok for r in c.entry if r.ok is not None))
        self.assertEqual([r.ok for r in c.exit], [False])
        p = pick_signals("A0", self.ds.stocks, self.ds.index_close, self.ds.flow_fi)
        for tk in self.ds.stocks.tickers:                           # same answer as the Top list, name by name
            self.assertEqual(self._check("A0", tk).verdict == "entry", tk in {s.ticker for s in p.top}, tk)

    def test_exit_and_flow_rows(self):
        c = self._check("A0", "SPIKE", t=TOP + 3)
        self.assertEqual(c.verdict, "exit")
        self.assertTrue(c.exit[0].ok)
        a5 = self._check("A5", "DRIFT")
        self.assertEqual(len(a5.exit), 2)                            # + the FX flow exit
        self.assertTrue(a5.exit[1].ok)
        labels = [r.label for r in self._check("A4", "UP").entry]
        self.assertTrue(any("거래량" in s for s in labels) and any("수급" in s for s in labels))
        self.assertTrue(any("시장 조건" in s for s in labels))
        self.assertFalse(any("시장 조건" in r.label for r in self._check("A0", "UP").entry))   # A0 has no L1

    def test_b_rank_needs_a_universe_run(self):
        alone = self._check("B", "UP")
        self.assertIn("순위는 유니버스 계산", alone.verdict_text)
        picks = pick_signals("B", self.ds.stocks, self.ds.index_close, self.ds.flow_fi)
        ranked = self._check("B", "UP", picks=picks)
        self.assertEqual(ranked.verdict, "hold")
        self.assertIn("상위 1", ranked.verdict_text)
        self.assertTrue(any(r.value.endswith("%") for r in ranked.entry))

    def test_c_rows_need_a_universe_run(self):
        book, _dates = score_book()
        self.assertEqual(check_stock("C1", "UP", book, None).verdict, "na")
        picks = pick_signals("C1", book, None)
        top = picks.top[0].ticker
        c = check_stock("C1", top, book, None, picks=picks)
        self.assertEqual(c.verdict, "entry")
        self.assertIn("상위 1", c.verdict_text)
        self.assertEqual(len(c.entry), 4)                            # the four gate rows
        self.assertTrue(all(r.ok is not None for r in c.entry))
        s = picks.detail.scored[top]
        self.assertEqual(c.score, f"{s.week_avg:+.2f}")
        self.assertEqual([m.label for m in c.metrics], ["MA60Div", "Range52", "R3"])   # MA{score_ma_long}Div
        self.assertEqual(c.metrics[0].value, f"{s.pct_ma50:.2f}")
        none = check_stock("C1", top, book, None)                   # no run: same rows, "-" values
        self.assertEqual((len(none.entry), none.score), (4, "-"))
        self.assertEqual({m.value for m in none.metrics}, {"-"})
        self.assertIsNone(none.entry[0].ok)
        unscored = check_stock("C1", "NOPE", book, None, picks=picks)
        self.assertEqual(unscored.verdict_text, "채점 제외")


class TestSignalApi(unittest.TestCase):
    def test_registry_offers_every_variant_once(self):
        from strategy import signal_api
        self.assertEqual(signal_api.variants(), list(VARIANTS))
        self.assertEqual(signal_api.variants("KOSDAQ"), list(VARIANTS))
        self.assertEqual(signal_api.variants("NASDAQ 150"), [])          # KR-only strategy
        self.assertEqual(signal_api.default_variant(), "C1")
        self.assertEqual(signal_api.title("A0"), "돌파만")
        self.assertTrue(signal_api.needs_universe("C2"))
        self.assertFalse(signal_api.needs_universe("B"))
        self.assertEqual(signal_api.labels("A0").bottom_tag, "이탈")
        self.assertEqual(signal_api.labels("C1").bottom_tag, "하위")
        self.assertEqual(signal_api.labels("B").top_what, "52주 고가 근접")

    def test_registry_is_the_one_strategy_list(self):
        from strategy.registry import STRATEGIES
        from ui.strategy_tab import STRATEGY_TABS
        self.assertEqual([t for t, _ in STRATEGY_TABS], [e.title() for e in STRATEGIES if e.page])

    def test_universe_rows_follow_the_variant_markets(self):
        from strategy import signal_api
        rows = [{"ticker": "A", "market": "KOSPI"}, {"ticker": "B", "market": "NASDAQ 150"},
                {"ticker": "KS11", "market": "KOSPI", "is_index": True}]
        self.assertEqual([r["ticker"] for r in signal_api.universe_rows(rows, "A0")], ["A"])

    def test_runs_go_through_the_provider(self):
        from unittest.mock import patch
        from strategy import signal_api
        from strategy.common.inputs import DailyInputs
        ds = synthetic_dataset(with_flows=True)
        inp = DailyInputs(ds.stocks, ds.index_close, "KS11", ds.flow_fi)
        with patch("strategy.trend_following.signal_provider.load_daily_inputs", return_value=inp) as load:
            picks = signal_api.run_picks([{"ticker": "UP", "market": "KOSPI"}], "A5")
            check = signal_api.run_check({"ticker": "DRIFT", "market": "KOSPI"}, "A5")
        self.assertIs(load.call_args_list[0].args[2], True)              # A5 needs flows
        self.assertEqual(picks.variant_id, "A5")
        self.assertEqual(check.verdict, "exit")
        with self.assertRaises(ValueError):
            signal_api.run_picks([{"ticker": "AAPL", "market": "NASDAQ 150"}], "A0")
        with patch("strategy.trend_following.signal_provider.load_daily_inputs") as load:
            c = signal_api.run_check({"ticker": "X", "market": "KOSPI"}, "C1")   # C needs no prices
        load.assert_not_called()
        self.assertEqual(c.verdict, "na")


class TestSignalCache(unittest.TestCase):
    def test_runs_last_the_day(self):
        from datetime import date
        from strategy.signal_api import SignalCache
        book, _ = score_book()
        picks = pick_signals("C1", book, None)
        day = [date(2026, 11, 16)]
        cache = SignalCache(today=lambda: day[0])
        cache.put(picks)
        self.assertIs(cache.get("C1"), picks)
        day[0] = date(2026, 11, 17)
        self.assertIsNone(cache.get("C1"))
        day[0] = date(2026, 11, 17)
        cache.put(picks)
        other = pick_signals("C2", book, None)
        cache.put(other)
        cache.clear(keep=picks)
        self.assertIs(cache.get("C1"), picks)
        self.assertNotIn("C2", cache)


class TestLayoutTemplates(unittest.TestCase):
    """layout_templates: the largest shape of each variant's check, with the
    same rows a real check has (the MA chart block keeps one size)."""

    def test_templates_have_the_real_rows(self):
        from strategy.trend_following.signal_provider import layout_templates
        ds = synthetic_dataset(with_flows=True)
        book, _ = score_book()
        picks = pick_signals("C1", book, None)
        for vid in VARIANTS:
            temps = layout_templates(vid)
            self.assertTrue(temps, vid)
            if VARIANTS[vid].mode == "score":
                real = check_stock(vid, picks.top[0].ticker, book, None, picks=picks if vid == "C1" else None)
            else:
                real = check_stock(vid, "UP", ds.stocks, ds.index_close, ds.flow_fi)
            t = temps[0]
            self.assertEqual([r.label for r in t.entry], [r.label for r in real.entry], vid)
            self.assertEqual([r.label for r in t.exit], [r.label for r in real.exit], vid)
            self.assertEqual([m.label for m in t.metrics], [m.label for m in real.metrics], vid)

if __name__ == "__main__":
    unittest.main()
