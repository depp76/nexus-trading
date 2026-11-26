"""The MA chart's 재무 tab (docs/dart_db_plan_2026-11-26.md 13, stage 5;
docs/ui.md 8.34): the pure view model and the tab built on a small dart.db."""
import datetime as dt
import os
import tempfile
import time
import unittest

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QApplication

from data.dart import financials as fin
from data.dart import query
from data.dart import segments as seg
from data.dart.store import connect
from tests.test_dart import SAMSUNG, _is_rows
from ui import dart_financials_model as fm

_APP = QApplication.instance() or QApplication([])


def _pump(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def _db(path):
    """Samsung 2024 Q1..Q4 and 2025 Q1..Q3 (Q4 2024 derived), segments for the 2024 annual report
    (external revenue) and 2025 Q3 (gross, with an elimination)."""
    conn = connect(path)
    conn.execute("INSERT INTO company(corp_code, stock_code, corp_name, market, fiscal_month) VALUES "
                 "(?, '005930', '삼성전자', 'KOSPI', 12)", (SAMSUNG,))
    q = [("11013", 2024, "100", "10", "8", "7", None, "20240515000001"),
         ("11012", 2024, "110", "-5", "-4", "-4", {"rev": "210", "op": "5", "ni": "4", "own": "3"}, "20240814000001"),
         ("11014", 2024, "120", "12", "9", "8", {"rev": "330", "op": "17", "ni": "13", "own": "11"}, "20241114000001"),
         ("11011", 2024, "460", "30", "20", "18", None, "20250310000001"),
         ("11013", 2025, "130", "13", "10", "9", None, "20250515000001"),
         ("11012", 2025, "140", "14", "11", "10", {"rev": "270", "op": "27", "ni": "21", "own": "19"}, "20250814000001"),
         ("11014", 2025, "150", "15", "12", "11", {"rev": "420", "op": "42", "ni": "33", "own": "30"}, "20251114000001")]
    for reprt, fy, rev, op, ni, own, add, rc in q:
        fin.store_period(conn, SAMSUNG, fy, reprt, "CFS", _is_rows(rev, op, ni, own, rc, add=add))
    conn.execute("UPDATE filing SET is_correction = 1 WHERE rcept_no = '20250814000001'")
    for fy, fq, rc, items, adj in ((2024, 4, "20250310000001", [("DX 부문", 300.0), ("DS 부문", 160.0)], []),
                                   (2025, 3, "20251114000001", [("DX 부문", 100.0), ("DS 부문", 60.0)],
                                    [("부문간 내부거래 제거", -10.0)])):
        c = seg.Candidate("xbrl", items, adj)
        seg.store_segments(conn, SAMSUNG, fy, fq, rc, "2025-01-01", c, "q" if fq != 4 else "ytd", 0.0, "ok", "xbrl")
        seg.record_fetch(conn, rc, "ok", "xbrl")
    conn.commit()
    conn.close()


class TestModel(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "dart.db")
        _db(self.db)
        self.data = query.company_financials("005930", db_path=self.db)

    def test_quarters_and_kpis(self):
        qs = fm.quarter_rows(self.data)
        self.assertEqual([(q["fiscal_year"], q["fiscal_q"]) for q in qs][-3:], [(2025, 1), (2025, 2), (2025, 3)])
        q4 = next(q for q in qs if (q["fiscal_year"], q["fiscal_q"]) == (2024, 4))
        self.assertEqual((q4["revenue"], q4["derived"]), (130.0, True))        # 460 - 330
        h1 = next(q for q in qs if (q["fiscal_year"], q["fiscal_q"]) == (2025, 2))
        self.assertTrue(h1["corrected"])
        k = fm.kpis(qs)
        self.assertEqual(k["quarter"], (2025, 3))
        self.assertAlmostEqual(k["last"]["revenue_yoy"], 150 / 120 - 1)
        self.assertEqual(k["ttm"], 130 + 130 + 140 + 150)
        self.assertIsNone(k["ttm_growth"])                 # 7 quarters: no previous four to compare
        self.assertAlmostEqual(k["margin_change"], 15 / 150 - 12 / 120)

    def test_annual_rows(self):
        rows = fm.annual_rows(fm.quarter_rows(self.data))
        y24, y25 = rows
        self.assertEqual((y24["fiscal_year"], y24["partial_q"], y24["revenue"]), (2024, None, 460.0))
        self.assertEqual((y25["partial_q"], y25["revenue"]), (3, 420.0))
        self.assertAlmostEqual(y25["revenue_yoy"], 420 / 330 - 1)               # vs the same cumulative

    def test_segment_view(self):
        sv = fm.segment_view(self.data)
        self.assertEqual([c["label"] for c in sv["columns"]], ["2024", "2025 3Q"])
        self.assertEqual([c["gross"] for c in sv["columns"]], [False, True])
        dx = next(s for s in sv["segments"] if s["code"] == "DX")
        self.assertEqual(dx["values"], [300.0, 100.0])
        self.assertEqual(dx["yoy"], [None, None])          # an annual vs a quarter, and external vs gross
        self.assertAlmostEqual(dx["shares"][0], 300 / 460)

    def test_comparable_years_and_cagr(self):
        def col(fy, gross):
            return [{"fiscal_year": fy, "fiscal_q": 4, "segment_raw": "A", "segment_code": "A", "revenue": 100.0 * (fy - 2020),
                     "basis": "ytd", "check_status": "ok", "source": "x"},
                    *([{"fiscal_year": fy, "fiscal_q": 4, "segment_raw": "제거", "segment_code": "_adj",
                        "revenue": -50.0, "basis": "ytd", "check_status": "ok", "source": "x"}] if gross else [])]
        data = {"segments": col(2021, False) + col(2022, False) + col(2023, True) + col(2024, True)}
        a = fm.segment_view(data)["segments"][0]
        self.assertEqual([None if g is None else round(g, 3) for g in a["yoy"]], [None, 1.0, None, 0.333])
        self.assertAlmostEqual(a["cagr"], 400 / 300 - 1)                   # the newest run on one basis

    def test_units(self):
        self.assertEqual(fm.unit_of("KRW")[:2], (1e8, "억원"))
        self.assertEqual(fm.unit_of("USD")[:2], (1e6, "백만 USD"))


def _q(fy, fq, rev, currency="KRW", fs_div="CFS", op=None):
    return {"fiscal_year": fy, "fiscal_q": fq, "revenue": rev, "op_income": op, "op_margin": None,
            "currency": currency, "fs_div": fs_div, "ytd": {"revenue": rev, "op_income": op, "net_income": None},
            "rcept_dt": "2026-01-01", "corrected": False}


class TestReviewCodex2Model(unittest.TestCase):
    """review_codex.md, second round: the tab's own sums and comparisons."""

    def test_ttm_needs_four_consecutive_quarters(self):
        qs = [_q(2024, 4, 100.0), _q(2025, 1, 100.0), _q(2025, 3, 100.0), _q(2025, 4, 100.0)]   # 2025 Q2 missing
        self.assertIsNone(fm.kpis(qs)["ttm"])                                     # was 400
        qs.insert(2, _q(2025, 2, 100.0))
        self.assertEqual(fm.kpis(qs)["ttm"], 400.0)
        self.assertIsNone(fm.kpis(qs)["ttm_growth"])                              # no four quarters before

    def test_ttm_and_annual_change_stay_on_one_currency_and_statement(self):
        qs = [_q(2025, q, 100.0) for q in (1, 2, 3)] + [_q(2025, 4, 100.0, currency="USD")]
        self.assertIsNone(fm.kpis(qs)["ttm"])
        a = fm.annual_rows([_q(2024, 4, 100.0), _q(2025, 4, 200.0, currency="USD")])
        self.assertEqual((a[1]["revenue_yoy"], a[1]["basis_changed"]), (None, True))      # was +100 %
        b = fm.annual_rows([_q(2024, 4, 100.0, fs_div="OFS"), _q(2025, 4, 150.0)])
        self.assertEqual((b[1]["revenue_yoy"], b[1]["basis_changed"]), (None, True))
        ok = fm.annual_rows([_q(2024, 4, 100.0), _q(2025, 4, 150.0)])
        self.assertAlmostEqual(ok[1]["revenue_yoy"], 0.5)

    def test_segment_yoy_needs_the_year_before(self):
        def col(fy, v):
            return {"fiscal_year": fy, "fiscal_q": 4, "segment_raw": "A", "segment_code": "A", "revenue": v,
                    "basis": "ytd", "check_status": "ok", "source": "x"}
        a = fm.segment_view({"segments": [col(2023, 100.0), col(2025, 150.0)]})["segments"][0]
        self.assertEqual(a["yoy"], [None, None])                                  # was +50 % over two years
        self.assertIsNone(a["cagr"])
        b = fm.segment_view({"segments": [col(2023, 100.0), col(2024, 120.0), col(2025, 150.0)]})["segments"][0]
        self.assertAlmostEqual(b["yoy"][2], 0.25)


class TestTab(unittest.TestCase):
    def setUp(self):
        import paths
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = os.path.join(self._tmp.name, "dart.db")
        _db(self.db)
        self._old = paths.DART_DB_FILE
        paths.DART_DB_FILE = self.db
        self.addCleanup(setattr, paths, "DART_DB_FILE", self._old)

    def _tab(self, code="005930"):
        from ui.dialogs.stock_financials import StockFinancialsTab
        t = StockFinancialsTab(code, "x", "KOSPI")
        self.addCleanup(t.deleteLater)
        self.addCleanup(t.shutdown)
        t.ensure_loaded()
        self.assertTrue(_pump(lambda: t._thread is not None and not t._thread.isRunning()))
        _pump(lambda: False, 0.2)
        return t

    def test_tab_shows_kpis_results_and_segments(self):
        t = self._tab()
        self.assertEqual([c.value.text() for c in t.kpi_cards], ["0", "0 (10.0%)", "0", "0"])   # 억원 of tiny values
        self.assertIn("2025 3Q", t.kpi_cards[0].label.text())
        # Transposed: items down the side (the vertical header, frozen while
        # scrolling sideways), periods across (oldest first, opened at the newest).
        tb = t._table
        self.assertEqual(t._table_title.text(), "실적")
        # Chart and table share one card, so one 분기 | 연간 switch drives both.
        self.assertIs(t._canvas.parentWidget(), t._table.parentWidget())
        self.assertEqual(tb.rowCount(), 9)
        self.assertEqual(tb.columnCount(), 7)
        self.assertTrue(tb.verticalHeader().isVisibleTo(tb))
        self.assertEqual([tb.verticalHeaderItem(i).text().strip() for i in range(9)],
                         ["매출", "YoY", "영업이익", "YoY", "이익률", "지배순이익", "YoY", "비고", "접수일"])
        self.assertEqual(tb.horizontalHeaderItem(6).text(), "2025 3Q")
        self.assertEqual(tb.horizontalHeaderItem(3).text(), "2024 4Q†")
        self.assertIn("ⓡ", tb.item(7, 5).text())
        self.assertTrue(tb.item(3, 6).text().endswith("%") or tb.item(3, 6).text() == "—")   # op income YoY
        # Qt's sideways bar is off; ours mirrors it and starts after the frozen item names.
        self.assertEqual(tb.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        t.resize(500, 900)
        t.show()
        self.addCleanup(t.close)
        QApplication.processEvents()
        self.assertEqual(t._hbar_lead.width(), tb.frameWidth() + tb.verticalHeader().width())
        inner = tb.horizontalScrollBar()
        self.assertGreater(inner.maximum(), 0)
        self.assertEqual(inner.value(), inner.maximum())                    # opened at the newest
        t.results_hbar.triggerAction(t.results_hbar.SliderAction.SliderToMinimum)
        QApplication.processEvents()
        self.assertEqual(inner.value(), 0)
        self.assertFalse(t._hbar_at_end)
        # Marker: hovering a bar shows the band, the margin dot and the value
        # box, and selects the same period's column in the table.
        class _Ev:
            def __init__(self, ax, x):
                self.inaxes, self.xdata = ax, x
        t._on_chart_motion(_Ev(t._marker_ax, 6.1))                          # the newest bar, 2025 3Q
        self.assertTrue(t._marker_band.get_visible())
        self.assertTrue(t._marker_box.get_visible())
        self.assertTrue(t._marker_box.get_text().startswith("2025 3Q"))
        self.assertIn("영업이익률", t._marker_box.get_text())
        self.assertEqual({i.column() for i in tb.selectedIndexes()}, {6})
        self.assertEqual(inner.value(), 0)                                  # selecting does not scroll
        t._on_chart_motion(_Ev(t._marker_ax, 5.5))                          # between two bars
        self.assertFalse(t._marker_box.get_visible())
        self.assertFalse(tb.selectedIndexes())
        t.set_mode("y")
        self.assertEqual(tb.columnCount(), 2)
        self.assertEqual(tb.horizontalHeaderItem(1).text(), "2025 3Q누적")
        self.assertTrue(t.mode_buttons["y"].isChecked())
        self.assertEqual(t.segment_table.rowCount(), 3)                       # DX, DS, check
        self.assertEqual(t.segment_table.horizontalHeaderItem(2).text(), "2025 3Q*")

    def test_unknown_stock_shows_a_message(self):
        t = self._tab("999999")
        self.assertIn("DART 재무 데이터가 아직 없습니다", t._scroll.widget().text())

    def test_only_korean_stocks_get_the_tab(self):
        import polars as pl
        from ui.dialogs.stock_ma import StockMaDialog
        df = pl.DataFrame({"Date": [dt.date(2025, 1, 2)], "Close": [1.0]})
        kr = StockMaDialog("005930", "삼성전자", "KOSPI", df)
        us = StockMaDialog("AAPL", "Apple", "NASDAQ 150", df)
        self.addCleanup(kr.close)
        self.addCleanup(us.close)
        self.assertEqual([kr.tabs.tabText(i) for i in range(kr.tabs.count())], ["차트", "기업 정보"])
        bar = kr.tabs.tabBar()
        self.assertEqual(bar.tabSizeHint(0).width(), bar.tabSizeHint(1).width())   # equal tab widths
        self.assertIsNone(us.tabs)
        kr.tabs.setCurrentIndex(1)
        self.assertTrue(_pump(lambda: kr.financials_tab._data is not None))


if __name__ == "__main__":
    unittest.main()
