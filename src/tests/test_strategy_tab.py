"""ui/strategy_tab.py + ui/trend_following_tab.py built headlessly: request
building from the form, the worker wiring, result population, error path."""
import unittest
from datetime import date
from unittest.mock import patch

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QDialog
from PyQt6.QtGui import QPixmap

app = QApplication.instance() or QApplication([])

from strategy.trend_following.research import ResearchRequest, run_research  # noqa: E402
from tests.strategy.trend_following.helpers import synthetic_dataset  # noqa: E402
from ui.strategy_tab import StrategyTab  # noqa: E402
from ui.trend_following_tab import TrendFollowingTab  # noqa: E402


class _FakeThread:
    instances = []

    def __init__(self, request):
        self.request = request
        self.started = False
        self.cancelled = False
        self.progress = _Sig()
        self.finished = _Sig()
        _FakeThread.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def isRunning(self):
        return False

    def isFinished(self):
        return not self.started

    def blockSignals(self, _b):
        pass


class _Sig:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *a):
        for s in self.slots:
            s(*a)


class TestTrendFollowingTab(unittest.TestCase):
    def test_request_reflects_the_form(self):
        tab = TrendFollowingTab()
        tab.universe_spin.setValue(120)
        tab.liquidity_spin.setValue(50)
        tab.positions_spin.setValue(8)
        tab.weekday_combo.setCurrentIndex(2)
        tab.cost_combo.setCurrentIndex(1)
        tab.flows_cb.setChecked(True)
        tab.set_variant_choice([vid for vid in tab._selected_variants() if vid != "A3"])
        tab.event_cb.setChecked(False)
        req = tab.build_request()
        self.assertEqual(req.params.universe_size, 120)
        self.assertEqual(req.params.min_avg_trading_value, 50e8)
        self.assertEqual(req.params.max_positions, 8)
        self.assertEqual(req.params.check_weekday, 2)
        self.assertEqual(req.cost_multipliers, (1.0,))
        self.assertNotIn("A3", req.variant_ids)
        self.assertIn("B", req.variant_ids)
        self.assertTrue(req.include_flows)
        self.assertFalse(req.run_event_study)
        self.assertEqual(req.start, date(2021, 1, 1))

    def test_flow_variants_follow_the_flows_checkbox(self):
        """review_codex.md S1: without investor flows A3-A5 cannot run, so they
        are left out of the request and the label says why; ticking flows
        restores them, and a picker choice made with flows off keeps them."""
        from strategy.trend_following import VARIANTS
        flow_ids = [vid for vid, v in VARIANTS.items() if v.needs_flows]
        self.assertTrue(flow_ids)
        tab = TrendFollowingTab()
        self.assertFalse(tab.flows_cb.isChecked())
        req = tab.build_request()
        self.assertFalse(set(flow_ids) & set(req.variant_ids))
        self.assertIn("A0", req.variant_ids)
        self.assertIn("need Investor flows", tab.variants_lbl.text())

        tab.flows_cb.setChecked(True)
        self.assertEqual(tab._selected_variants(), tuple(VARIANTS))
        self.assertEqual(tab.variants_lbl.text(), f"All {len(VARIANTS)} variants")
        tab.set_variant_choice([v for v in VARIANTS if v != flow_ids[0]])
        tab.flows_cb.setChecked(False)
        tab.set_variant_choice(["A0", "B"])    # picker with flows off: flow rows untouched
        tab.flows_cb.setChecked(True)
        self.assertEqual(set(tab._selected_variants()), {"A0", "B"} | set(flow_ids[1:]))

    def test_variants_button_opens_the_picker_and_applies_its_choice(self):
        tab = TrendFollowingTab()
        seen = {}

        class _Dlg:
            def __init__(self, selected, flows_on, params=None, parent=None):
                seen.update(selected=tuple(selected), flows_on=flows_on, params=params)

            def exec(self):
                return QDialog.DialogCode.Accepted

            def selected_ids(self):
                return ("A1", "C1")

        tab.positions_spin.setValue(8)
        before = tab._selected_variants()
        with patch("ui.trend_following_tab.VariantPickerDialog", _Dlg):
            tab._on_variants_clicked()
        self.assertEqual(seen["selected"], before)
        self.assertFalse(seen["flows_on"])
        self.assertEqual(seen["params"].max_positions, 8)     # descriptions quote the form's numbers
        self.assertEqual(tab._selected_variants(), ("A1", "C1"))
        self.assertIn("A1, C1", tab.variants_lbl.text())

    def test_skipped_variants_and_warnings_are_shown_without_saving(self):
        """review_codex.md S1/S3: a run that could not load flows names the
        skipped variants and shows every warning in the tab."""
        ds = synthetic_dataset()             # no flows
        req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0", "A3"), cost_multipliers=(1.0,),
                              include_flows=True, run_event_study=False)
        result = run_research(ds, req)
        self.assertEqual(result.skipped, {"A3": "no investor-flow data"})
        self.assertNotIn("A3", {r.id for r in result.runs})
        self.assertIn("A3 (no investor-flow data)", result.to_markdown())
        from ui.colors import WARN
        from ui.trend_following_tab import TAB_WARNINGS
        tab = TrendFollowingTab()
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), "Warnings (0)")
        tab._on_research_finished(result, "")
        n = len(result.warnings) + 1
        # ui_redesign_2026-11 P1-1 item 5: the toggle panel became a result tab.
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), f"Warnings ({n})")
        self.assertEqual(tab.result_tabs.tabBar().tabTextColor(TAB_WARNINGS).name(), WARN)
        self.assertIn("A3 not run", tab.warn_lbl.text())
        self.assertIn("skipped A3", tab.status_lbl.text())

    def test_tables_copy_selected_rows(self):
        """review_codex.md S4: every result table gets the app-wide Ctrl+C."""
        tab = TrendFollowingTab()
        for tbl in (tab.table, tab.event_table, tab.compare_table, tab.monthly_table, tab.trades_table):
            self.assertTrue(hasattr(tbl, "_table_keys"))

    def test_run_tooltip_lists_every_variant(self):
        from strategy.trend_following import VARIANTS
        tab = TrendFollowingTab()
        for vid in VARIANTS:
            self.assertIn(vid, tab.run_btn.toolTip())

    def test_run_starts_a_tracked_worker_and_disables_the_button(self):
        tab = TrendFollowingTab()
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab._on_run_clicked()
        self.assertEqual(len(_FakeThread.instances), 1)
        worker = _FakeThread.instances[0]
        self.assertTrue(worker.started)
        self.assertFalse(tab.run_btn.isEnabled())
        self.assertIs(tab._research_thread, worker)
        worker.progress.emit("Loading...")
        self.assertEqual(tab.status_lbl.text(), "Loading...")
        worker.finished.emit(None, "boom")
        self.assertTrue(tab.run_btn.isEnabled())
        self.assertIn("boom", tab.status_lbl.text())
        self.assertFalse(tab.save_btn.isEnabled())

    def test_stop_cancels_the_worker_and_reports_cancelled(self):
        from threads.strategy_threads import CANCELLED_MESSAGE
        tab = TrendFollowingTab()
        self.assertFalse(tab.stop_btn.isEnabled())
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab._on_run_clicked()
        worker = _FakeThread.instances[0]
        self.assertTrue(tab.stop_btn.isEnabled())
        with patch.object(worker, "isRunning", return_value=True):
            tab._on_stop_clicked()
        self.assertTrue(worker.cancelled)
        self.assertFalse(tab.stop_btn.isEnabled())
        self.assertIn("Stopping", tab.status_lbl.text())
        worker.finished.emit(None, CANCELLED_MESSAGE)
        self.assertEqual(tab.status_lbl.text(), CANCELLED_MESSAGE)
        self.assertTrue(tab.run_btn.isEnabled())
        self.assertFalse(tab.save_btn.isEnabled())

    def test_populate_from_a_real_result(self):
        ds = synthetic_dataset(with_flows=True)
        req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0", "A5"), cost_multipliers=(1.0,),
                              include_flows=True)
        result = run_research(ds, req)
        tab = TrendFollowingTab()
        tab._on_research_finished(result, "")
        self.assertEqual(tab.table.rowCount(), len(result.runs))
        self.assertEqual(tab.table.item(0, 0).text(), "A0")      # strategies first at 1x
        self.assertEqual(tab.table.item(0, 1).text(), "1x")
        self.assertEqual(tab.event_table.rowCount(), len(result.event_study))
        self.assertTrue(tab.save_btn.isEnabled())
        self.assertIn("Done", tab.status_lbl.text())
        self.assertGreater(len(tab.ax.lines), 0)
        tab.resize(1400, 900)
        tab.render(QPixmap(1400, 900))

    def test_chart_hover_shows_values_at_the_mouse(self):
        from types import SimpleNamespace
        from matplotlib.dates import date2num
        ds = synthetic_dataset()
        result = run_research(ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",),
                                                  cost_multipliers=(1.0,), run_event_study=False))
        tab = TrendFollowingTab()
        tab._on_research_finished(result, "")
        a0 = next(r for r in result.runs_at(1.0) if r.id == "A0")
        latest = tab.readout_lbl.text()
        self.assertIn(f"{a0.dates[-1]:%Y-%m-%d}", latest)
        self.assertFalse(tab._crosshair.get_visible())
        legend_labels = [t.get_text() for t in tab.ax.get_legend().get_texts()]
        self.assertNotIn("_nolegend_", legend_labels)

        k = len(a0.dates) // 2
        tab._on_chart_motion(SimpleNamespace(inaxes=tab.ax, xdata=float(date2num(a0.dates[k])) + 0.2))
        text = tab.readout_lbl.text()
        self.assertIn(f"{a0.dates[k]:%Y-%m-%d}", text)
        self.assertIn(f"{a0.nav[k] * 100:.1f}", text)
        self.assertTrue(tab._crosshair.get_visible())
        self.assertAlmostEqual(tab._crosshair.get_xdata()[0], float(date2num(a0.dates[k])))

        tab._on_chart_leave(None)
        self.assertFalse(tab._crosshair.get_visible())
        self.assertEqual(tab.readout_lbl.text(), latest)

    def test_save_writes_markdown(self):
        import os
        import tempfile
        ds = synthetic_dataset()
        result = run_research(ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",),
                                                  cost_multipliers=(1.0,), run_event_study=False))
        tab = TrendFollowingTab()
        tab._on_research_finished(result, "")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.md")
            with patch("ui.trend_following_tab.QFileDialog.getSaveFileName", return_value=(path, "")):
                tab._on_save_clicked()
            with open(path, encoding="utf-8") as f:
                self.assertIn("# Trend Following backtest report", f.read())

    def test_rejects_bad_window(self):
        tab = TrendFollowingTab()
        tab.start_edit.setDate(tab.end_edit.date())
        with patch("ui.trend_following_tab.QMessageBox.warning") as warn, \
             patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            _FakeThread.instances.clear()
            tab._on_run_clicked()
        warn.assert_called_once()
        self.assertEqual(_FakeThread.instances, [])


def _result(variants=("A0", "A1"), mults=(1.0,), **kw):
    ds = synthetic_dataset()
    return run_research(ds, ResearchRequest(start=date(2021, 1, 4), variant_ids=variants,
                                            cost_multipliers=mults, run_event_study=False, **kw))


class TestResultScreen(unittest.TestCase):
    """docs/ui_redesign_2026-11.md P1: segments, KPI cards, target check,
    drawdown chart and the result tabs, all redrawn from one result."""

    @classmethod
    def setUpClass(cls):
        cls.result = _result(("A0", "A1"), (0.0, 1.0, 2.0))

    def _tab(self):
        tab = TrendFollowingTab()
        tab._on_research_finished(self.result, "")
        return tab

    def test_default_selection_is_1x_and_the_best_sharpe_strategy(self):
        tab = self._tab()
        self.assertEqual(tab._mult, 1.0)
        strategies = self.result.runs_at(1.0, "strategy")
        best = max(strategies, key=lambda r: r.metrics["sharpe"])
        self.assertEqual(tab._sel_id, best.id)
        self.assertEqual(tab.variant_seg.current(), best.id)
        self.assertEqual(tab.cost_seg.current(), "1")
        self.assertTrue(tab.selector_row.isVisibleTo(tab))
        self.assertFalse(tab.form_box.isVisibleTo(tab))       # a result folds the form away
        self.assertFalse(tab.edit_btn.isChecked())

    def test_variant_pick_updates_cards_target_check_and_tabs(self):
        from ui.strategy_widgets import fmt_num, fmt_pct
        tab = self._tab()
        tab.variant_seg.buttons["A0"].click()
        a0 = next(r for r in self.result.runs_at(1.0) if r.id == "A0")
        cards = tab.kpi_row.cards
        self.assertEqual(cards["sharpe"].value.text(), fmt_num(a0.metrics["sharpe"]))
        self.assertEqual(cards["mdd"].value.text(), fmt_pct(a0.metrics["mdd"]))
        self.assertEqual(cards["trades"].value.text(), str(a0.metrics["n_trades"]))
        self.assertFalse(cards["sharpe"].badge.isHidden())
        self.assertIn("A0", tab.target_card.subject.text())
        self.assertEqual(tab.compare_table.item(tab.compare_table.currentRow(), 0).text(), "A0")
        self.assertEqual(tab.monthly_table.rowCount(), len({d.year for d in a0.dates}))
        self.assertEqual(tab.trades_table.rowCount(), len(a0.trades))
        self.assertEqual(tab.ax.get_legend().get_texts()[-1].get_text(), "A0")

    def test_benchmark_hides_the_badges(self):
        tab = self._tab()
        tab.select_variant("BM1")
        cards = tab.kpi_row.cards
        self.assertTrue(cards["sharpe"].badge.isHidden())
        self.assertTrue(cards["mdd"].badge.isHidden())
        self.assertEqual(tab.target_card.badges["sharpe"].text(), "Benchmark")
        self.assertEqual(tab.trades_table.rowCount(), 0)
        self.assertIn("Benchmarks have no trades", tab.trades_table._empty_hint.text)

    def test_comparison_row_click_selects_the_variant(self):
        tab = self._tab()
        row = next(i for i in range(tab.compare_table.rowCount()) if tab.compare_table.item(i, 0).text() == "BM2")
        tab.compare_table.setCurrentCell(row, 2)
        self.assertEqual(tab._sel_id, "BM2")
        self.assertEqual(tab.variant_seg.current(), "BM2")

    def test_cost_segment_switches_rows_and_keeps_the_selection(self):
        tab = self._tab()
        tab.select_variant("A0")
        tab.cost_seg.buttons["2"].click()
        self.assertEqual(tab._mult, 2.0)
        self.assertEqual(tab._sel_id, "A0")
        a0_2x = next(r for r in self.result.runs_at(2.0) if r.id == "A0")
        self.assertIs(tab.selected_run(), a0_2x)
        self.assertEqual(tab.compare_table.rowCount(), len(self.result.runs_at(2.0)))
        self.assertIn("2x", tab.ax.get_title(loc="left"))

    def test_only_run_costs_are_enabled(self):
        tab = TrendFollowingTab()
        tab._on_research_finished(_result(("A0",), (1.0,)), "")
        self.assertTrue(tab.cost_seg.buttons["1"].isEnabled())
        self.assertFalse(tab.cost_seg.buttons["0"].isEnabled())
        self.assertFalse(tab.cost_seg.buttons["2"].isEnabled())

    def test_chart_draws_selected_and_bm1_unless_show_all(self):
        tab = self._tab()
        n = len(self.result.runs_at(1.0))
        self.assertEqual(len(tab._chart_series), 2)
        tab.show_all_cb.setChecked(True)
        self.assertEqual(len(tab._chart_series), n)
        self.assertEqual(tab._chart_series[0][0], tab._sel_id)
        self.assertTrue(tab.ax_dd.collections)                   # the drawdown fill

    def test_all_metrics_tab_keeps_every_summary_column(self):
        from strategy.trend_following.research import SUMMARY_COLUMNS
        tab = self._tab()
        headers = [tab.table.horizontalHeaderItem(c).text() for c in range(tab.table.columnCount())]
        self.assertEqual(headers, ["ID", "Cost"] + [h for h, _ in SUMMARY_COLUMNS[1:]])
        self.assertEqual(tab.table.rowCount(), len(self.result.runs))

    def test_settings_chips_follow_the_form(self):
        tab = TrendFollowingTab()
        tab.universe_spin.setValue(120)
        tab.cost_combo.setCurrentIndex(1)
        texts = [c.text() for c in tab.chips if not c.isHidden()]
        self.assertIn("KOSPI Top 120", texts)
        self.assertIn("Costs 1x", texts)
        tab.edit_btn.click()
        self.assertFalse(tab.form_box.isVisibleTo(tab))

    def test_empty_state(self):
        tab = TrendFollowingTab()
        self.assertEqual(tab.kpi_row.cards["sharpe"].value.text(), "-")
        self.assertFalse(tab.selector_row.isVisibleTo(tab))
        self.assertEqual(tab.compare_table.rowCount(), 0)


class TestTargetsAndDrawdown(unittest.TestCase):
    def test_sharpe_and_mdd_badge_boundaries(self):
        from ui.strategy_widgets import sharpe_badge, mdd_badge, meets_targets
        self.assertEqual(sharpe_badge(1.5)[0], "ok")
        self.assertEqual(sharpe_badge(1.4999)[0], "fail")
        self.assertEqual(sharpe_badge(float("nan"))[0], "na")
        self.assertEqual(mdd_badge(-0.15)[0], "ok")
        self.assertEqual(mdd_badge(-0.1500001)[0], "fail")
        self.assertEqual(mdd_badge(None)[0], "na")
        self.assertTrue(meets_targets({"sharpe": 1.5, "mdd": -0.15}))
        self.assertFalse(meets_targets({"sharpe": 1.6, "mdd": -0.2}))
        self.assertIsNone(meets_targets({"sharpe": 1.6}))

    def test_drawdown_series_matches_max_drawdown(self):
        import numpy as np
        from strategy.trend_following.metrics import max_drawdown
        from ui.strategy_widgets import drawdown_series
        nav = np.array([0.98, 1.10, 1.05, 1.21, 0.99, 1.30])
        dd = drawdown_series(nav)
        expected = nav / np.maximum.accumulate(np.concatenate([[1.0], nav]))[1:] - 1
        np.testing.assert_allclose(dd, expected)
        self.assertAlmostEqual(dd[0], -0.02)                     # first session vs the 1.0 seed
        self.assertAlmostEqual(dd.min(), max_drawdown(nav))
        self.assertEqual(len(drawdown_series([])), 0)

    def test_kpi_row_reflows_when_narrow(self):
        from ui.strategy_widgets import KpiRow
        row = KpiRow(wrap_width=900)
        row.resize(1200, 100)
        row.show()
        app.processEvents()
        self.assertEqual(row._cols, 6)
        row.resize(600, 200)
        app.processEvents()
        self.assertEqual(row._cols, 3)
        row.hide()


class TestVariantPickerDialog(unittest.TestCase):
    def test_lists_every_variant_and_shows_its_description(self):
        from strategy.trend_following import VARIANTS
        from ui.dialogs import VariantPickerDialog
        dlg = VariantPickerDialog(["A0", "C1"], flows_on=True)
        self.assertEqual(set(dlg._items), set(VARIANTS))
        self.assertEqual(dlg.selected_ids(), ("A0", "C1"))
        self.assertIn("A0", dlg.detail.toPlainText())              # first row shown on open
        dlg.select("C2")
        text = dlg.detail.toPlainText()
        self.assertIn("C2", text)
        self.assertIn("변동성 정규화 없음", text)
        self.assertIn("10개 중 2개 선택", dlg.count_lbl.text())

    def test_all_none_and_flow_rows_without_flows(self):
        from strategy.trend_following import VARIANTS
        from ui.dialogs import VariantPickerDialog
        flow_ids = {vid for vid, v in VARIANTS.items() if v.needs_flows}
        dlg = VariantPickerDialog(list(VARIANTS), flows_on=False)
        self.assertFalse(flow_ids & set(dlg.selected_ids()))
        a3 = dlg._items["A3"]
        self.assertFalse(a3.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        self.assertIn("수급 필요", a3.text())
        dlg.select("A3")                                              # still readable
        self.assertIn("투자자 수급 데이터 필요", dlg.detail.toPlainText())
        dlg._set_all(False)
        self.assertEqual(dlg.selected_ids(), ())
        dlg._set_all(True)
        self.assertEqual(set(dlg.selected_ids()), set(VARIANTS) - flow_ids)
        self.assertEqual(list(dlg.selected_ids()), [v for v in VARIANTS if v not in flow_ids])   # matrix order


class TestStrategyTab(unittest.TestCase):
    def test_has_the_trend_following_sub_tab_and_merges_threads(self):
        tab = StrategyTab()
        self.assertEqual(tab.sub_tabs.count(), 1)
        self.assertEqual(tab.sub_tabs.tabText(0), "Trend Following")
        self.assertEqual(tab.collect_threads_to_stop(), [])
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab.trend_following_tab._on_run_clicked()
        self.assertEqual(len(tab.collect_threads_to_stop()), 1)


if __name__ == "__main__":
    unittest.main()
