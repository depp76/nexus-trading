"""The MA chart's 재무 tab (docs/dart_db_plan_2026-11-26.md 13, stage 5;
docs/ui.md 8.34): the pure view model and the tab built on a small dart.db."""
import datetime as dt
import os
import tempfile
import time
import unittest

from PyQt6.QtCore import QCoreApplication, Qt
from PyQt6.QtWidgets import QApplication, QLabel

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
        self.assertEqual([c["label"] for c in sv["columns"]], ["2024", "2025 3Q누적"])
        self.assertEqual([c["gross"] for c in sv["columns"]], [False, True])
        dx = next(s for s in sv["segments"] if s["code"] == "DX")
        # The year in progress is its cumulative: a 3-month Q3 without Q1 / Q2 gives none.
        self.assertEqual(dx["values"], [300.0, None])
        self.assertEqual(dx["yoy"], [None, None])
        self.assertAlmostEqual(dx["shares"][0], 300 / 460)

    def test_columns_follow_the_results_periods(self):
        """One 실적 card: the segment columns are the results' periods, slot
        for slot; a period without segment data is an empty column."""
        keys = [(2024, 3), (2024, 4), (2025, 1), (2025, 2), (2025, 3)]
        sv = fm.segment_view(self.data, mode="q", keys=keys)
        self.assertEqual([c["label"] for c in sv["columns"]], ["2024 3Q", "2024 4Q", "2025 1Q", "2025 2Q", "2025 3Q"])
        dx = next(s for s in sv["segments"] if s["code"] == "DX")
        self.assertEqual(dx["values"], [None, None, None, None, 100.0])
        self.assertTrue(sv["has_values"])
        ann = fm.segment_view(self.data, keys=[(2023, 4), (2024, 4), (2025, 3)])
        self.assertEqual([c["label"] for c in ann["columns"]], ["2023", "2024", "2025 3Q누적"])
        self.assertEqual([c["partial"] for c in ann["columns"]], [False, False, True])

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


def _seg_row(fy, fq, raw, rev, source="biz_product", basis="ytd", level="product"):
    return {"fiscal_year": fy, "fiscal_q": fq, "segment_raw": raw, "segment_code": raw, "revenue": rev,
            "basis": basis, "check_status": "ok", "source": source, "level": level}


class TestQuarterlySegments(unittest.TestCase):
    """User direction 2026-11-27: quarterly amounts from the business section's
    cumulative figures, their share of revenue and YoY."""

    def _data(self):
        ytd = {(2024, 1): (50, 30), (2024, 2): (110, 70), (2024, 3): (180, 100),
               (2025, 1): (60, 40), (2025, 2): (130, 90), (2025, 3): (200, 150)}
        segs = [_seg_row(fy, fq, n, float(v)) for (fy, fq), vals in ytd.items() for n, v in zip("AB", vals)]
        revenue = [{"fiscal_year": fy, "fiscal_q": fq, "metric": "revenue", "value_q": 100.0, "value_ytd": 100.0 * fq,
                    "currency": "KRW"} for fy, fq in ytd]
        return {"segments": segs, "rows": revenue}

    def test_quarters_are_cumulatives_differenced(self):
        sv = fm.segment_view(self._data(), mode="q")
        self.assertEqual([c["label"] for c in sv["columns"]][-3:], ["2025 1Q", "2025 2Q", "2025 3Q"])
        a = next(s for s in sv["segments"] if s["code"] == "A")
        self.assertEqual(a["values"][-3:], [60.0, 70.0, 70.0])                # 60, 130 - 60, 200 - 130
        self.assertAlmostEqual(a["yoy"][-2], 70 / 60 - 1)                      # vs 2024 Q2 = 110 - 50
        self.assertAlmostEqual(a["shares"][-1], 0.70)                          # of the quarter's revenue (100)
        self.assertIsNone(a["cagr"])

    def test_a_missing_quarter_leaves_the_next_one_unsplit(self):
        data = self._data()
        data["segments"] = [r for r in data["segments"] if (r["fiscal_year"], r["fiscal_q"]) != (2025, 2)]
        sv = fm.segment_view(data, mode="q")
        self.assertEqual(sv["columns"][-1]["label"], "2025 1Q")                # 2025 Q3 has no Q2 to subtract
        a = next(s for s in sv["segments"] if s["code"] == "A")
        self.assertEqual(a["values"][-1], 60.0)

    def test_one_breakdown_for_every_column(self):
        """Products where the business section has them, else segments; a
        year without the chosen breakdown is not filled from the other one."""
        data = self._data()
        data["segments"] += [_seg_row(2024, 3, "X", 280.0, "xbrl", level="segment"),
                             _seg_row(2025, 3, "X", 350.0, "xbrl", level="segment"),
                             _seg_row(2023, 4, "X", 300.0, "xbrl", level="segment")]
        self.assertEqual(fm.segment_levels(data), ["product", "segment"])
        prod = fm.segment_view(data)
        self.assertEqual((prod["level"], [c["fiscal_year"] for c in prod["columns"]]), ("product", [2024, 2025]))
        seg_ = fm.segment_view(data, level="segment")
        self.assertEqual([s["code"] for s in seg_["segments"]], ["X"])
        self.assertEqual([c["fiscal_year"] for c in seg_["columns"]], [2023, 2024, 2025])

    def test_annual_view_marks_the_year_in_progress(self):
        sv = fm.segment_view(self._data())
        self.assertEqual([(c["label"], c["partial"]) for c in sv["columns"]],
                         [("2024 3Q누적", True), ("2025 3Q누적", True)])
        a = next(s for s in sv["segments"] if s["code"] == "A")
        self.assertAlmostEqual(a["shares"][-1], 200 / 300)                    # of the cumulative revenue

    def test_long_product_lists_are_shortened(self):
        self.assertEqual(fm._display_name("설화수,헤라,아이오페,라네즈 등"), "설화수,헤라 등")
        self.assertEqual(fm._display_name("TV,모니터등"), "TV,모니터등")


class TestReviewCodex5Model(unittest.TestCase):
    """review_codex 5 P1: an amount that failed the check against revenue is
    shown as disclosed and enters no calculation."""

    def _row(self, fy, fq, rev, status="ok", basis="ytd"):
        return {**_seg_row(fy, fq, "A", rev, source="html_segment", basis=basis, level="segment"),
                "check_status": status}

    def test_unverified_quarter_is_not_subtracted(self):
        data = {"segments": [self._row(2025, 1, 80.0, "sum_mismatch", None), self._row(2025, 2, 100.0)]}
        keys = [(2025, 1), (2025, 2)]
        q = fm.segment_view(data, mode="q", keys=keys)
        self.assertEqual(q["segments"][0]["values"], [80.0, None])           # Q1 as disclosed, Q2 alone unknown
        self.assertEqual([c["verified"] for c in q["columns"]], [False, True])
        y = fm.segment_view(data, keys=keys)
        self.assertEqual(y["segments"][0]["values"], [80.0, 100.0])          # the half-year cumulative shows
        # Verified on both sides: the split works as before.
        ok = {"segments": [self._row(2025, 1, 80.0), self._row(2025, 2, 100.0)]}
        self.assertEqual(fm.segment_view(ok, mode="q", keys=keys)["segments"][0]["values"], [80.0, 20.0])
        # A 3-month figure is not added to an unverified cumulative either.
        q_basis = {"segments": [self._row(2025, 1, 80.0, "sum_mismatch", None), self._row(2025, 2, 20.0, basis="q")]}
        self.assertEqual(fm.segment_view(q_basis, keys=keys)["segments"][0]["values"], [80.0, None])
        self.assertEqual(fm.segment_view(q_basis, mode="q", keys=keys)["segments"][0]["values"], [80.0, 20.0])
        # Later in the year an amount of unknown basis is no quarter amount.
        late = {"segments": [self._row(2025, 1, 80.0), self._row(2025, 2, 100.0, "sum_mismatch", None)]}
        self.assertEqual(fm.segment_view(late, mode="q", keys=keys)["segments"][0]["values"], [80.0, None])

    def test_unverified_periods_give_no_growth(self):
        data = {"segments": [self._row(2023, 4, 100.0), self._row(2024, 4, 150.0, "sum_mismatch", None),
                             self._row(2025, 4, 200.0)]}
        a = fm.segment_view(data)["segments"][0]
        self.assertEqual(a["values"], [100.0, 150.0, 200.0])
        self.assertEqual(a["yoy"], [None, None, None])                       # neither from nor against 2024
        self.assertIsNone(a["cagr"])                                         # the run of verified years is one year
        quarters = {"segments": [self._row(2024, 1, 50.0, "sum_mismatch", None), self._row(2025, 1, 80.0)]}
        q = fm.segment_view(quarters, mode="q", keys=[(2024, 1), (2025, 1)])["segments"][0]
        self.assertEqual((q["values"], q["yoy"]), ([50.0, 80.0], [None, None]))
        both = {"segments": [self._row(2024, 1, 50.0), self._row(2025, 1, 80.0)]}
        self.assertAlmostEqual(fm.segment_view(both, mode="q", keys=[(2024, 1), (2025, 1)])["segments"][0]["yoy"][1], 0.6)


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
        self.assertEqual(t._results_scroll.lead.width(), tb.frameWidth() + tb.verticalHeader().width())
        inner = tb.horizontalScrollBar()
        self.assertGreater(inner.maximum(), 0)
        self.assertEqual(inner.value(), inner.maximum())                    # opened at the newest
        t.results_hbar.triggerAction(t.results_hbar.SliderAction.SliderToMinimum)
        QApplication.processEvents()
        self.assertEqual(inner.value(), 0)
        self.assertFalse(t._results_scroll.at_end)
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
        # One 실적 card (user direction 2026-11-27): the segment section sits
        # under the results in the same card, on the results' periods slot for
        # slot; items down the frozen side, CAGR last in the annual view.
        st = t.segment_table
        self.assertEqual(t._seg_title.text(), "사업부별")
        self.assertIs(t._seg_canvas.parentWidget(), t._canvas.parentWidget())  # one card
        self.assertEqual(st.rowCount(), 3)                                    # DX, DS, check
        self.assertEqual([st.verticalHeaderItem(i).text() for i in range(3)], ["DX 부문", "DS 부문", "검산"])
        self.assertTrue(st.verticalHeader().isVisibleTo(st))
        self.assertEqual([st.horizontalHeaderItem(j).text() for j in range(3)], ["2024", "2025 3Q누적*", "연평균"])
        dx = st.item(0, 0).text()                                             # 2024: amount, share of revenue
        self.assertTrue(dx.endswith("65%"), dx)                               # 300 / 460
        self.assertEqual(st.horizontalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.assertEqual(t._seg_marker_ax.get_xlim(), t._results_ax.get_xlim())
        self.assertEqual(t._seg_marker_ax.get_position().bounds, t._results_ax.get_position().bounds)
        t._on_seg_motion(_Ev(t._seg_marker_ax, 1.0))                          # hover 2025 (cumulative)
        self.assertTrue(t._seg_marker_box.get_visible())
        self.assertTrue(t._seg_marker_box.get_text().startswith("2025 3Q누적*"))
        self.assertEqual({i.column() for i in st.selectedIndexes()}, {1})
        self.assertEqual({i.column() for i in tb.selectedIndexes()}, {1})      # the results too
        self.assertTrue(t._marker_band.get_visible())
        t._on_seg_motion(_Ev(t._seg_marker_ax, 0.5))
        self.assertFalse(t._seg_marker_box.get_visible())
        self.assertFalse(t._marker_box.get_visible())
        # The same 분기 | 연간 switch: the results' seven quarters, segment data in 2025 Q3 only.
        t.set_mode("q")
        self.assertEqual(t.segment_data["mode"], "q")
        self.assertEqual(st.columnCount(), tb.columnCount())                  # no CAGR
        self.assertEqual(st.horizontalHeaderItem(6).text(), "2025 3Q*")         # gross (elimination)
        self.assertEqual(st.item(0, 0).text(), "—")
        t._on_chart_motion(_Ev(t._marker_ax, 6.0))                            # hover on the results chart
        self.assertEqual({i.column() for i in st.selectedIndexes()}, {6})
        self.assertEqual(t.level_buttons, {})                                 # segments only: no switch

    def test_products_first_with_a_switch_to_segments(self):
        conn = connect(self.db)
        c = seg.Candidate("biz_product", [("메모리", 300.0), ("스마트폰", 150.0)])
        seg.store_segments(conn, SAMSUNG, 2024, 4, "20250310000001", "2025-03-10", c, "ytd", 10.0, "ok",
                           "biz_product", level="product")
        conn.commit()
        conn.close()
        t = self._tab()
        self.assertEqual(set(t.level_buttons), {"product", "segment"})
        self.assertTrue(t.segment_no_data)                                    # products: annual only here
        t.set_mode("y")
        self.assertFalse(t.segment_no_data)
        self.assertTrue(t.level_buttons["product"].isChecked())
        st = t.segment_table
        self.assertEqual({st.verticalHeaderItem(i).text() for i in range(2)}, {"메모리", "스마트폰"})
        t.set_segment_level("segment")
        self.assertEqual({st.verticalHeaderItem(i).text() for i in range(2)}, {"DX 부문", "DS 부문"})

    def test_switching_views_moves_no_chart_or_table(self):
        """User direction 2026-11-27: 분기 | 연간 and 제품별 | 부문별 change the
        content, never where the charts and tables sit; no caption or notes
        under the segment section."""
        conn = connect(self.db)
        c = seg.Candidate("biz_product", [("아주 긴 제품 이름이 들어간 품목", 300.0), ("스마트폰", 150.0)])
        seg.store_segments(conn, SAMSUNG, 2024, 4, "20250310000001", "2025-03-10", c, "ytd", 10.0, "ok",
                           "biz_product", level="product")
        conn.commit()
        conn.close()
        t = self._tab()
        t.resize(1400, 1200)
        t.show()
        self.addCleanup(t.close)
        QApplication.processEvents()

        def where():
            QApplication.processEvents()
            out = []
            for w in (t._canvas, t._table, t._seg_canvas, t.segment_table):
                out.append(w.mapTo(t, w.rect().topLeft()))
                out.append(w.size())
            for tbl in (t._table, t.segment_table):
                out.append(tbl.verticalHeader().width())
                out.append(tbl.columnWidth(0))
            out.append(t._results_ax.get_position().bounds)
            out.append(t._seg_marker_ax.get_position().bounds)
            return out

        first = where()
        for step in (lambda: t.set_mode("y"), lambda: t.set_segment_level("segment"), lambda: t.set_mode("q"),
                     lambda: t.set_segment_level("product")):
            step()
            self.assertEqual(where(), first)
        # The two charts' plot areas coincide.
        self.assertEqual(t._results_ax.get_position().bounds, t._seg_marker_ax.get_position().bounds)
        labels = [w.text() for w in t._seg_canvas.parentWidget().findChildren(QLabel)]
        self.assertFalse([x for x in labels if "누적 차이" in x or "검산" in x])

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
