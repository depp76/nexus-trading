"""NEX-46: C1~C3 scoring policy (trend_following.md 2-5 (4)(5), 6-2, 9-16).

The decision rules are exercised on hand-made ScoreBooks (so a rank of 0.6 or 0.4 is exact, not a
by-product of a price path) over a real PriceBook and the real Engine; the scoring arithmetic itself
is covered by test_scoring.py and is not repeated."""
import dataclasses
import unittest
from datetime import date
from types import SimpleNamespace

import numpy as np

from strategy.trend_following import VARIANTS, StrategyParams, compute_features
from strategy.trend_following.backtest import Engine, Position, run_backtest
from strategy.trend_following.costs import CostModel, TradeCost
from strategy.trend_following.dataset import PriceBook
from strategy.trend_following.research import ResearchRequest, run_research
from strategy.trend_following.scoring import ScoreBook, compute_scores
from strategy.trend_following.scoring_policy import REASON_MA50, REASON_RANK, REASON_STOP, ScoringPolicy
from tests.strategy.trend_following.helpers import business_days, make_frame, path, synthetic_dataset
from tests.strategy.trend_following.test_scoring import score_book

T = 40
NAMES = ("AAA", "BBB", "CCC")
CHECK = 9                      # a weekly check session (index); 10..14 are ordinary sessions


def _book(closes: dict[str, np.ndarray] | None = None) -> PriceBook:
    dates = business_days(date(2021, 1, 4), T)
    closes = closes or {k: path(10_000.0, [(T, 0.01)]) for k in NAMES}
    frames = {k: make_frame(dates, v) for k, v in closes.items()}
    return PriceBook.from_frames(frames, dates, {k: "KOSPI" for k in frames}, {k: k for k in frames})


def _scores(book: PriceBook, rank=None, gate=None, ma50=None, overheated=None, total=None) -> ScoreBook:
    """A ScoreBook with every name passing the gate at rank 0.9 unless a cell is overridden: dict[(t, col)] -> value."""
    shape = (book.T, book.N)

    def arr(default, over, dtype=float):
        a = np.full(shape, default, dtype=dtype)
        for (t, j), v in (over or {}).items():
            a[t, j] = v
        return a

    sb_gate = arr(True, gate, bool)
    return ScoreBook(
        dates=list(book.dates), tickers=list(book.tickers),
        ma50_div=arr(0.1, ma50), range52=np.full(shape, 0.9), ma20_div=np.full(shape, 0.02),
        r10=np.full(shape, 0.0), r3=np.full(shape, 0.0), atr_pct=np.full(shape, 0.02),
        liquid=np.ones(shape, bool), overheated=arr(False, overheated, bool), gate=sb_gate,
        trend_score=np.full(shape, 1.0), timing_score=np.full(shape, -0.5),
        total=arr(0.5, total), total_rank=arr(0.9, rank),
    )


def _feats(book: PriceBook, regime_on=True, check=(CHECK,)) -> SimpleNamespace:
    check_day = np.zeros(book.T, bool)
    check_day[list(check)] = True
    return SimpleNamespace(
        check_day=check_day, regime_on=np.full(book.T, regime_on), traded=np.ones((book.T, book.N), bool),
        atr=np.abs(book.close) * 0.01, adv=None,
        exit_channel=np.ones((book.T, book.N), bool),          # Donchian-10 fires every session: must be ignored
    )


def _engine(book: PriceBook, p: StrategyParams) -> Engine:
    return Engine(book, np.zeros(book.T), p, CostModel(), 0, "test")


def _hold(eng: Engine, book: PriceBook, ticker: str, t: int, stop_gap: float = 0.1) -> None:
    j = book.col(ticker)
    px = float(book.close[t, j])
    eng.positions[ticker] = Position(ticker=ticker, col=j, qty=100, entry_price=px, entry_idx=0,
                                     stop_price=px * (1.0 - stop_gap), entry_costs=TradeCost(), last_close=px)


class TestWeeklyBuy(unittest.TestCase):
    def setUp(self):
        self.p = StrategyParams()
        self.book = _book()
        self.j = {k: self.book.col(k) for k in NAMES}

    def _decide(self, sb, feats=None, params=None, hold=(), t=CHECK):
        p = params or self.p
        eng = _engine(self.book, p)
        for tk in hold:
            _hold(eng, self.book, tk, t)
        pol = ScoringPolicy(feats or _feats(self.book), p, VARIANTS["C1"], sb)
        return pol.decide(t, eng)

    def test_buys_gated_names_at_or_above_the_buy_threshold_only(self):
        j = self.j
        sb = _scores(self.book, rank={(CHECK, j["AAA"]): 0.85, (CHECK, j["BBB"]): 0.79, (CHECK, j["CCC"]): 0.95},
                     gate={(CHECK, j["CCC"]): False})
        orders = self._decide(sb)
        self.assertEqual([(o.ticker, o.side, o.reason) for o in orders], [("AAA", "buy", "entry")])

    def test_threshold_is_inclusive(self):
        sb = _scores(self.book, rank={(CHECK, j): 0.80 for j in self.j.values()})
        self.assertEqual({o.ticker for o in self._decide(sb)}, set(NAMES))

    def test_fills_only_the_free_slots_highest_total_first(self):
        j = self.j
        sb = _scores(self.book, total={(CHECK, j["AAA"]): 1.0, (CHECK, j["BBB"]): 1.5, (CHECK, j["CCC"]): 1.2})
        orders = self._decide(sb, params=dataclasses.replace(self.p, max_positions=2))
        self.assertEqual([o.ticker for o in orders], ["BBB", "CCC"])
        # a holding occupies one of the two slots
        orders = self._decide(sb, params=dataclasses.replace(self.p, max_positions=2), hold=("AAA",))
        self.assertEqual([o.ticker for o in orders if o.side == "buy"], ["BBB"])

    def test_sizing_is_the_2_4_rule(self):
        sb = _scores(self.book)
        feats = _feats(self.book)
        o = next(o for o in self._decide(sb, feats) if o.ticker == "AAA")
        j, close = self.j["AAA"], self.book.close[CHECK, self.j["AAA"]]
        equity = self.p.seed_krw
        risk_qty = int(equity * self.p.risk_per_trade // (self.p.atr_multiple * feats.atr[CHECK, j]))
        cap_qty = int(equity * self.p.max_weight // close)
        self.assertEqual(o.qty, min(risk_qty, cap_qty))
        self.assertEqual(o.weight_cap, self.p.max_weight)
        self.assertAlmostEqual(o.stop_offset, self.p.stop_atr_multiple * feats.atr[CHECK, j])

    def test_no_buy_off_the_weekly_check_day(self):
        self.assertEqual(self._decide(_scores(self.book), t=CHECK + 1), [])

    def test_no_new_buys_in_risk_off(self):
        sb = _scores(self.book)
        self.assertEqual(self._decide(sb, _feats(self.book, regime_on=False)), [])

    def test_risk_off_still_runs_the_exits(self):
        sb = _scores(self.book, ma50={(CHECK, self.j["AAA"]): -0.01})
        orders = self._decide(sb, _feats(self.book, regime_on=False), hold=("AAA",))
        self.assertEqual([(o.ticker, o.reason) for o in orders], [("AAA", REASON_MA50)])

    def test_a_held_name_is_not_bought_again(self):
        orders = self._decide(_scores(self.book), hold=("AAA",))
        self.assertNotIn("AAA", [o.ticker for o in orders])


class TestSells(unittest.TestCase):
    def setUp(self):
        self.p = StrategyParams()
        self.book = _book()
        self.j = {k: self.book.col(k) for k in NAMES}

    def _decide(self, sb, t, hold=("AAA",), feats=None, book=None):
        book = book or self.book
        eng = _engine(book, self.p)
        for tk in hold:
            _hold(eng, book, tk, t)
        return ScoringPolicy(feats or _feats(book), self.p, VARIANTS["C1"], sb).decide(t, eng)

    def _sells(self, orders):
        return [(o.ticker, o.reason) for o in orders if o.side == "sell"]

    def test_hysteresis_rank_0_6_is_held_and_0_4_is_sold(self):
        j = self.j
        sb = _scores(self.book, rank={(CHECK, j["AAA"]): 0.6, (CHECK, j["BBB"]): 0.4})
        self.assertEqual(self._sells(self._decide(sb, CHECK, hold=("AAA", "BBB"))), [("BBB", REASON_RANK)])

    def test_sell_threshold_is_exclusive(self):
        sb = _scores(self.book, rank={(CHECK, self.j["AAA"]): 0.50})
        self.assertEqual(self._sells(self._decide(sb, CHECK)), [])

    def test_a_held_name_between_the_thresholds_is_neither_bought_nor_sold(self):
        sb = _scores(self.book, rank={(CHECK, self.j["AAA"]): 0.6})
        orders = self._decide(sb, CHECK)
        self.assertNotIn("AAA", [o.ticker for o in orders])

    def test_relative_sell_is_weekly_only(self):
        sb = _scores(self.book, rank={(CHECK + 1, self.j["AAA"]): 0.1})
        self.assertEqual(self._sells(self._decide(sb, CHECK + 1)), [])

    def test_overheated_holding_is_not_sold(self):
        j = self.j["AAA"]
        # overheated => gate False => no rank; a sub-0.5 rank could not exist either
        sb = _scores(self.book, overheated={(CHECK, j): True}, gate={(CHECK, j): False}, rank={(CHECK, j): np.nan})
        self.assertEqual(self._sells(self._decide(sb, CHECK)), [])

    def test_gate_failure_other_than_ma50_does_not_sell_9_16_decision_1(self):
        j = self.j["AAA"]
        sb = _scores(self.book, gate={(CHECK, j): False}, rank={(CHECK, j): np.nan})   # e.g. Range52 < 0.70
        self.assertEqual(self._sells(self._decide(sb, CHECK)), [])

    def test_ma50_exit_is_daily_and_ignores_rank(self):
        j = self.j["AAA"]
        for t in (CHECK, CHECK + 2):           # check day and an ordinary day
            sb = _scores(self.book, ma50={(t, j): -0.001}, rank={(t, j): 0.99})
            self.assertEqual(self._sells(self._decide(sb, t)), [("AAA", REASON_MA50)])

    def test_atr_stop(self):
        closes = {k: path(10_000.0, [(T, 0.0)]) for k in NAMES}
        closes["AAA"] = path(10_000.0, [(CHECK + 1, 0.0), (T - CHECK - 1, -0.05)])
        book = _book(closes)
        eng = _engine(book, self.p)
        t = CHECK + 2
        j = book.col("AAA")
        eng.positions["AAA"] = Position("AAA", j, 100, 10_000.0, 0, 10_000.0 - 2 * 200.0, TradeCost(), 10_000.0)
        self.assertLess(book.close[t, j], 9_600.0)
        orders = ScoringPolicy(_feats(book), self.p, VARIANTS["C1"], _scores(book)).decide(t, eng)
        self.assertEqual(self._sells(orders), [("AAA", REASON_STOP)])
        # and not while the close is above the stop
        orders = ScoringPolicy(_feats(book), self.p, VARIANTS["C1"], _scores(book)).decide(CHECK, eng)
        self.assertEqual(self._sells(orders), [])

    def test_donchian_10_exit_is_not_used(self):
        feats = _feats(self.book)
        self.assertTrue(feats.exit_channel[CHECK + 2, self.j["AAA"]])
        self.assertEqual(self._sells(self._decide(_scores(self.book), CHECK + 2, feats=feats)), [])

    def test_excess_weight_is_trimmed_on_the_check_day_not_sold_whole(self):
        eng = _engine(self.book, self.p)
        _hold(eng, self.book, "AAA", CHECK)
        eng.positions["AAA"].qty = 150          # 150 x ~11k = 1.65M... pushed over 20% by a small book
        eng.cash = 0.0
        eng.equity_now = eng.positions_value()
        orders = ScoringPolicy(_feats(self.book), self.p, VARIANTS["C1"], _scores(self.book)).decide(CHECK, eng)
        trims = [o for o in orders if o.reason == "weight_trim"]
        self.assertEqual([o.ticker for o in trims], ["AAA"])
        self.assertLess(trims[0].qty, 150)


class TestEngineFills(unittest.TestCase):
    """The orders reach the engine and fill at the next session's open, not the signal day's close."""

    def setUp(self):
        self.p = StrategyParams()
        self.book = _book()
        self.cm = CostModel()

    def _run(self, sb):
        eng = _engine(self.book, self.p)
        res = eng.run(ScoringPolicy(_feats(self.book), self.p, VARIANTS["C1"], sb))
        return res

    def test_entry_then_ma50_exit_fill_at_the_next_open(self):
        j = self.book.col("AAA")
        s = CHECK + 3                       # session whose close drops below the 50-day average
        only_aaa = {(CHECK, self.book.col(k)): 0.1 for k in ("BBB", "CCC")}
        sb = _scores(self.book, rank={(CHECK, self.book.col(k)): 0.1 for k in ("BBB", "CCC")},
                     gate={(t, self.book.col(k)): False for t in range(T) for k in ("BBB", "CCC")},
                     ma50={(t, j): -0.01 for t in range(s, T)})
        self.assertTrue(only_aaa)
        res = self._run(sb)
        trade = next(tr for tr in res.trades if tr.ticker == "AAA")
        self.assertEqual(trade.reason, REASON_MA50)
        self.assertEqual(trade.entry_date, self.book.dates[CHECK + 1])
        self.assertEqual(trade.exit_date, self.book.dates[s + 1])
        self.assertEqual(trade.exit_price, self.cm.fill_price(self.book.dates[s + 1], "KOSPI",
                                                              self.book.open[s + 1, j], "sell"))
        self.assertNotEqual(trade.exit_price, self.book.close[s, j])

    def test_no_entry_while_risk_off(self):
        eng = _engine(self.book, self.p)
        res = eng.run(ScoringPolicy(_feats(self.book, regime_on=False), self.p, VARIANTS["C1"], _scores(self.book)))
        self.assertEqual(res.trades, [])
        self.assertEqual(float(res.n_positions.max()), 0.0)

    def test_score_and_feature_sessions_must_match(self):
        with self.assertRaises(ValueError):
            ScoringPolicy(_feats(self.book), self.p, VARIANTS["C1"], _scores(_book_short()))


def _book_short() -> PriceBook:
    dates = business_days(date(2021, 1, 4), T - 5)
    frames = {k: make_frame(dates, path(10_000.0, [(T - 5, 0.01)])) for k in NAMES}
    return PriceBook.from_frames(frames, dates, {k: "KOSPI" for k in frames}, {k: k for k in frames})


class TestVariants(unittest.TestCase):
    def test_c_rows_are_score_mode_without_flows_or_breakout_filters(self):
        for vid in ("C1", "C2", "C3"):
            v = VARIANTS[vid]
            self.assertEqual(v.mode, "score")
            self.assertTrue(v.regime)
            self.assertFalse(v.needs_flows or v.volume or v.flow or v.flow_exit)
            self.assertTrue(v.label.startswith(vid))

    def test_overrides_are_exactly_the_6_2_differences(self):
        p = StrategyParams()
        self.assertEqual(VARIANTS["C1"].apply(p), p)
        self.assertEqual(VARIANTS["C2"].apply(p), dataclasses.replace(p, score_vol_normalize=False))
        self.assertEqual(VARIANTS["C3"].apply(p), dataclasses.replace(p, score_range_mode="high52_prox"))
        self.assertTrue(p.score_vol_normalize)
        self.assertEqual(p.score_range_mode, "range")

    def test_c2_and_c3_really_score_differently(self):
        book, _ = score_book()
        p = StrategyParams()
        sbs = {vid: compute_scores(book, VARIANTS[vid].apply(p)) for vid in ("C1", "C2", "C3")}
        t = book.T - 1
        # C3: close / high252 instead of the 52-week range position
        self.assertFalse(np.allclose(sbs["C1"].range52[t], sbs["C3"].range52[t], equal_nan=True))
        self.assertTrue(np.allclose(sbs["C1"].ma50_div, sbs["C3"].ma50_div, equal_nan=True))

        # C2: LOWV rose less in raw terms (+2.1% vs +3.0% over 3 sessions) but with a far smaller ATR%, so its
        # normalised R3 is the larger one; the raw R3 order is the opposite
        dates = business_days(date(2021, 1, 4), 60)
        frames = {
            "LOWV": make_frame(dates, path(10_000.0, [(57, 0.0), (3, 0.007)]), spread=0.002),
            "HIGHV": make_frame(dates, path(10_000.0, [(57, 0.0), (3, 0.01)]), spread=0.03),
            "MID": make_frame(dates, path(10_000.0, [(60, 0.0)]), spread=0.01),
        }
        vbook = PriceBook.from_frames(frames, dates, {k: "KOSPI" for k in frames}, {k: k for k in frames})
        c1, c2 = (compute_scores(vbook, VARIANTS[v].apply(p)) for v in ("C1", "C2"))
        lo, hi = vbook.col("LOWV"), vbook.col("HIGHV")
        self.assertGreater(c1.pct["r3"][-1, lo], c1.pct["r3"][-1, hi])
        self.assertLess(c2.pct["r3"][-1, lo], c2.pct["r3"][-1, hi])
        np.testing.assert_array_equal(c1.r3, c2.r3)        # the displayed indicator is raw in both

    def test_the_policy_gets_the_overridden_scores(self):
        ds = synthetic_dataset()
        p = StrategyParams()
        feats = compute_features(ds, p)
        res = {vid: run_backtest(ds, feats, p, VARIANTS[vid], CostModel()) for vid in ("C1", "C2", "C3")}
        self.assertTrue(res["C1"].params["score_vol_normalize"])
        self.assertFalse(res["C2"].params["score_vol_normalize"])
        self.assertEqual(res["C3"].params["score_range_mode"], "high52_prox")
        # a precomputed ScoreBook is used as is
        sb = compute_scores(ds.stocks, VARIANTS["C3"].apply(p))
        again = run_backtest(ds, feats, p, VARIANTS["C3"], CostModel(), scores=sb)
        np.testing.assert_array_equal(again.nav, res["C3"].nav)


class TestMatrixReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds = synthetic_dataset()
        req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A1", "C1", "C2", "C3"), cost_multipliers=(1.0,),
                              run_event_study=False)
        cls.result = run_research(ds, req)
        cls.md = cls.result.to_markdown()

    def test_c_rows_are_in_the_matrix_and_the_report(self):
        self.assertEqual({r.id for r in self.result.runs_at(1.0, "strategy")}, {"A1", "C1", "C2", "C3"})
        for vid in ("| C1 |", "| C2 |", "| C3 |"):
            self.assertIn(vid, self.md)
        self.assertIn("- C1: ", self.md)

    def test_turnover_and_holding_period_are_reported_for_the_c_rows_only(self):
        head = "## 6-2 C-series turnover and holding period (1x)"
        self.assertIn(head, self.md)
        section = self.md.split(head)[1].split("\n## ")[0]
        self.assertIn("Turnover (round trips/yr)", section)
        self.assertIn("Avg hold (d)", section)
        for vid in ("| C1 |", "| C2 |", "| C3 |"):
            self.assertIn(vid, section)
        self.assertNotIn("| A1 |", section)

    def test_no_c_section_without_c_rows(self):
        req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A1",), cost_multipliers=(1.0,),
                              run_event_study=False)
        self.assertNotIn("C-series turnover", run_research(synthetic_dataset(), req).to_markdown())


if __name__ == "__main__":
    unittest.main()
