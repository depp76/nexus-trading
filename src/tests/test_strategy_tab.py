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
        from strategy.trend_following import VARIANTS
        from ui.dialogs import TrendConditions
        tab = TrendFollowingTab()
        tab.set_conditions(TrendConditions(variant_ids=[v for v in VARIANTS if v != "A3"], universe_size=120,
                                           min_liquidity_eok=50, max_positions=8, check_weekday=2, cost_choice=1))
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
    def test_nothing_is_chosen_until_the_picker_is_used(self):
        """No rule at first; the status line says how to fix it, the 조건
        button is outlined and Run stays off (strategy_run_settings_ui.md 3)."""
        tab = TrendFollowingTab()
        self.assertEqual(tab._selected_variants(), ())
        self.assertEqual(tab.conditions_btn.text(), "조건...")
        self.assertFalse(hasattr(tab, "flows_cb"))
        self.assertFalse(hasattr(tab, "event_cb"))
        self.assertFalse(tab.build_request().run_event_study)
        self.assertFalse(tab.run_btn.isEnabled())
        self.assertEqual(tab.status_summary_lbl.text(), "조건에서 규칙을 1개 이상 선택하세요")
        self.assertEqual(tab.status_summary_lbl.objectName(), "fieldError")
        self.assertTrue(tab.conditions_btn.property("invalid"))
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            _FakeThread.instances.clear()
            tab._on_run_clicked()
        self.assertEqual(_FakeThread.instances, [])
        tab.set_variant_choice(["C1", "A0"])
        self.assertTrue(tab.run_btn.isEnabled())
        self.assertFalse(tab.conditions_btn.property("invalid"))
        self.assertTrue(tab.status_summary_lbl.text().startswith("규칙 2개 · 시총 상위 200 · 비용 0·1·2배 비교"))
        self.assertEqual(tab._selected_variants(), ("A0", "C1"))           # matrix order
    def test_flows_load_only_when_a_chosen_strategy_needs_them(self):
        from strategy.trend_following import VARIANTS
        flow_ids = [vid for vid, v in VARIANTS.items() if v.needs_flows]
        self.assertTrue(flow_ids)
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0", "B"])
        self.assertFalse(tab.build_request().include_flows)
        tab.set_variant_choice(["A0", flow_ids[0]])
        req = tab.build_request()
        self.assertTrue(req.include_flows)
        self.assertEqual(req.variant_ids, ("A0", flow_ids[0]))
    def test_conditions_dialog_applies_or_keeps(self):
        """적용 replaces the conditions, 취소 keeps them; focus returns to 조건."""
        from ui.dialogs import TrendConditions
        tab = TrendFollowingTab()
        tab.set_conditions(TrendConditions(variant_ids=("A0",), max_positions=8))
        seen = {}
        answer = {"code": QDialog.DialogCode.Accepted}

        class _Dlg:
            def __init__(self, conditions, parent=None):
                seen["in"] = conditions

            def exec(self):
                return answer["code"]

            def conditions(self):
                return TrendConditions(variant_ids=("A1", "C1"), max_positions=5)

        with patch("ui.trend_following_tab.TrendConditionsDialog", _Dlg), \
             patch.object(tab.conditions_btn, "setFocus") as focus:
            tab._on_conditions_clicked()
            self.assertEqual(seen["in"].max_positions, 8)
            self.assertEqual(tab._selected_variants(), ("A1", "C1"))
            self.assertEqual(tab.build_request().params.max_positions, 5)
            answer["code"] = QDialog.DialogCode.Rejected
            tab._on_conditions_clicked()
            self.assertEqual(tab._selected_variants(), ("A1", "C1"))
            self.assertEqual(focus.call_count, 2)

    def test_conditions_dialog_edits_a_copy(self):
        from ui.dialogs import TrendConditions, TrendConditionsDialog
        start = TrendConditions(variant_ids=("A3", "C1"), universe_size=150, cost_choice=1, target_sharpe=1.2)
        dlg = TrendConditionsDialog(start)
        self.assertEqual(dlg.conditions(), start)
        self.assertEqual(dlg.ok_btn.text(), "적용")
        self.assertEqual([lbl.text() for lbl in dlg.field_names],
                         ["시가총액 상위", "20일 평균 거래대금 하한", "최대 보유 종목 수",
                          "주간 점검 요일", "거래비용", "평가 기준 · 목표 Sharpe"])
        self.assertIn("원화 시가총액", dlg.market_note.text())
        dlg.positions_spin.setValue(9)
        dlg.select("A3")
        self.assertIn("최대 9종목", dlg.detail.toPlainText())                # descriptions follow the inputs
        self.assertEqual(dlg.conditions().max_positions, 9)
        self.assertEqual(start.max_positions, 6)
    def test_cost_choice_and_market_popup(self):
        from ui.dialogs import TrendConditions
        from ui.dialogs.trend_conditions import COST_CHOICES
        tab = TrendFollowingTab()
        self.assertEqual(tab.run_cost_mults(), tuple(COST_CHOICES[0][1]))
        tab.set_conditions(TrendConditions(variant_ids=("A0",), cost_choice=1))
        self.assertEqual(tab.build_request().cost_multipliers, (1.0,))
        self.assertIn("비용 1배만", tab.status_summary())
        # Markets are Universe-style toggles (2026-11-16), KOSPI on by default.
        self.assertEqual([(b.text(), b.isChecked()) for b in tab.market_buttons.values()],
                         [("KOSPI", True), ("KOSDAQ", False), ("NASDAQ", False), ("NYSE", False)])
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
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), "경고 (0)")
        tab._on_research_finished(result, "")
        n = len(result.warnings) + 1
        # ui_redesign_2026-11 P1-1 item 5: the toggle panel became a result tab.
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), f"경고 ({n})")
        self.assertEqual(tab.result_tabs.tabBar().tabTextColor(TAB_WARNINGS).name(), WARN)
        self.assertIn("A3 미실행", tab.warn_lbl.text())
        self.assertIn("건너뜀 A3", tab.status_lbl.text())

    def test_tables_copy_selected_rows(self):
        """review_codex.md S4: every result table gets the app-wide Ctrl+C."""
        tab = TrendFollowingTab()
        for tbl in (tab.table, tab.compare_table, tab.monthly_table, tab.trades_table):
            self.assertTrue(hasattr(tbl, "_table_keys"))

    def test_run_tooltip_lists_every_variant(self):
        from strategy.trend_following import VARIANTS
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0"])
        for vid in VARIANTS:
            self.assertIn(vid, tab.run_btn.toolTip())

    def test_run_starts_a_tracked_worker_and_disables_the_button(self):
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0"])
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
        tab.set_variant_choice(["A0"])
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
        self.assertIn("중지합니다", tab.status_lbl.text())
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
        self.assertTrue(tab.save_btn.isEnabled())
        self.assertIn("완료", tab.status_lbl.text())
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
        """A date error replaces the summary on the status line, the dates
        are outlined and Run is off."""
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0"])
        tab.start_edit.setDate(tab.end_edit.date())
        self.assertIn("시작일이 종료일보다 앞이어야 합니다", tab.status_summary_lbl.text())
        self.assertEqual(tab.status_summary_lbl.objectName(), "fieldError")
        self.assertTrue(tab.start_edit.property("invalid"))
        self.assertFalse(tab.run_btn.isEnabled())
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            _FakeThread.instances.clear()
            tab._on_run_clicked()
        self.assertEqual(_FakeThread.instances, [])
    def test_failed_rerun_keeps_the_last_result_and_its_save(self):
        tab = TrendFollowingTab()
        result = _result(("A0",), (1.0,))
        tab._on_research_finished(result, "")
        tab.set_variant_choice(["A0"])
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab._on_run_clicked()
        self.assertFalse(tab.run_btn.isEnabled())                  # running: one run per tab
        tab.set_variant_choice(["A0", "A1"])                        # an edit while running
        self.assertFalse(tab.run_btn.isEnabled())
        _FakeThread.instances[0].finished.emit(None, "boom")
        self.assertIs(tab._result, result)
        self.assertTrue(tab.save_btn.isEnabled())
        self.assertTrue(tab.run_btn.isEnabled())
        self.assertIn("boom", tab.status_lbl.text())


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
        self.assertTrue(tab.result_head.isVisibleTo(tab))

    def test_variant_pick_updates_cards_target_check_and_tabs(self):
        from ui.strategy_widgets import fmt_num, fmt_pct
        tab = self._tab()
        tab.select_variant("BM1")                    # A0 is not the focus before the pick
        if "A0" not in tab._sel_ids:
            tab.variant_seg.buttons["A0"].click()    # 비교 대상: added, the focus stays (8.17)
        self.assertEqual(tab._sel_id, "BM1")
        self.assertIn("A0", tab._sel_ids)
        tab.focus_combo.activated.emit(tab.focus_combo.findData("A0"))   # 상세 기준
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
        if a0.trades:                                            # name column next to the ticker
            t = max(a0.trades, key=lambda t: (t.exit_date, t.entry_date))
            self.assertEqual(tab.trades_table.item(0, 3).text(), t.ticker)
            self.assertEqual(tab.trades_table.item(0, 2).text(), self.result.dataset_info["names"].get(t.ticker, ""))
        self.assertEqual(tab.ax.get_legend().get_texts()[-1].get_text(), "A0")

    def test_result_basis_line_comes_from_the_result_not_the_form(self):
        # docs/ui.md 8.16: what the shown result was computed with.
        tab = self._tab()
        text = tab.basis_lbl.text()
        self.assertTrue(tab.basis_lbl.isVisibleTo(tab))
        self.assertTrue(text.startswith("현재 결과 기준 · 추세추종 · KOSPI 상위 200 · "))
        self.assertIn(str(self.result.dataset_info["eval_start"]), text)
        self.assertIn("규칙 A0, A1", text)
        self.assertIn("비용 1x", text)
        self.assertIn(f"상세 기준 {tab._sel_id}", text)
        import dataclasses
        tab.set_conditions(dataclasses.replace(tab.conditions, universe_size=250, target_sharpe=2.5))
        self.assertEqual(tab.basis_lbl.text(), text)
        tab.select_variant("BM1")
        self.assertIn("상세 기준 BM1", tab.basis_lbl.text())
        # Saving belongs to the shown result: on the result head, right of the basis.
        self.assertTrue(tab.result_head.isAncestorOf(tab.save_btn))
        self.assertTrue(tab.save_btn.isEnabled())

    def test_next_run_note_when_the_form_moves_on(self):
        """'다음 실행에 적용' only while the form differs from the shown run."""
        tab = TrendFollowingTab()
        req = self.result.request
        tab.set_variant_choice(req.variant_ids)
        tab.start_edit.setDate(req.start)
        tab.end_edit.setDate(req.end) if req.end else None
        tab._on_research_finished(self.result, "")
        if req.end is None:
            self.assertFalse(tab.pending_lbl.isHidden())             # the form ends today, the run did not
            return
        import dataclasses
        self.assertTrue(tab.pending_lbl.isHidden())
        before = tab.conditions
        tab.set_conditions(dataclasses.replace(before, max_positions=before.max_positions + 1))
        self.assertFalse(tab.pending_lbl.isHidden())
        tab.set_conditions(before)
        self.assertTrue(tab.pending_lbl.isHidden())

    def test_target_check_is_a_summary_line_with_expandable_detail(self):
        # docs/ui.md 8.16: verdict badges on one line, the four rows on demand.
        tab = self._tab()
        card = tab.target_card
        self.assertFalse(card.detail.isVisibleTo(tab))
        self.assertTrue(card.robust_note.isVisibleTo(tab))
        for key in ("sharpe", "mdd", "both"):
            self.assertEqual(card.summary[key].objectName(), card.badges[key].objectName())
        card.detail_btn.click()
        self.assertTrue(card.detail.isVisibleTo(tab))
        self.assertEqual(card.badges["robust"].text(), "앱에서 미실행")   # the not-run note stays in the detail
        self.assertEqual(card.detail_btn.text(), "접기")
        tab.select_variant("BM1")
        self.assertEqual(card.summary["sharpe"].text(), "Sharpe 벤치마크")

    def test_benchmark_hides_the_badges(self):
        tab = self._tab()
        tab.select_variant("BM1")
        cards = tab.kpi_row.cards
        self.assertTrue(cards["sharpe"].badge.isHidden())
        self.assertTrue(cards["mdd"].badge.isHidden())
        self.assertEqual(tab.target_card.badges["sharpe"].text(), "벤치마크")
        self.assertEqual(tab.trades_table.rowCount(), 0)
        self.assertIn("벤치마크는 매매 내역이 없습니다", tab.trades_table._empty_hint.text)

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
        from ui.trend_following_tab import SUMMARY_LABELS
        self.assertEqual(headers, ["ID", "비용"] + [SUMMARY_LABELS.get(h, h) for h, _ in SUMMARY_COLUMNS[1:]])
        self.assertIn("매매 수", headers)
        self.assertEqual(tab.table.rowCount(), len(self.result.runs))

    def test_toolbar_order_and_buttons(self):
        """전략 · 시장 · 기간 · 조건 ... 중지 백테스트 on one line when wide."""
        tab = TrendFollowingTab()
        self.assertEqual(tab.strategy_combo.currentText(), "추세추종")
        self.assertEqual(tab.run_btn.text(), "백테스트")
        self.assertEqual(tab.run_btn.objectName(), "primary")
        self.assertEqual(tab.stop_btn.objectName(), "")
        tab.show()
        tab.resize(max(1200, tab.toolbar.one_line_width() + 80), 900)
        for _ in range(4):
            app.processEvents()
        if tab.toolbar.width() >= tab.toolbar.one_line_width():
            self.assertTrue(tab.toolbar.one_line)
            xs = [w.mapTo(tab, w.rect().topLeft()).x() for w in
                  (tab.strategy_combo, *tab.market_buttons.values(), tab.start_edit, tab.end_edit, tab.conditions_btn,
                   tab.stop_btn, tab.run_btn)]
            self.assertEqual(xs, sorted(xs))
            ys = {w.mapTo(tab, w.rect().center()).y() for w in (tab.strategy_combo, tab.start_edit, tab.run_btn)}
            self.assertLessEqual(max(ys) - min(ys), 2)                       # one centre line
        self.assertEqual(tab.start_edit.width(), tab.end_edit.width())
        tab.hide()
    def test_tab_order_follows_the_card(self):
        tab = TrendFollowingTab()
        chain, w = [], tab.strategy_combo
        for _ in range(400):
            chain.append(w)
            w = w.nextInFocusChain()
            if w is chain[0]:
                break
        def pos(x):
            return chain.index(x)
        order = [tab.strategy_combo, *tab.market_buttons.values(), tab.start_edit, tab.end_edit, tab.conditions_btn,
                 tab.run_btn]
        self.assertEqual([pos(x) for x in order], sorted(pos(x) for x in order))
    def test_benchmarks_are_a_separate_segment_group(self):
        tab = TrendFollowingTab()
        tab._on_research_finished(self.result, "")
        lay = tab.variant_seg._layout
        kinds = [("gap" if lay.itemAt(i).spacerItem() else lay.itemAt(i).widget().text())
                 for i in range(lay.count())]
        self.assertEqual(kinds.count("gap"), 1)
        self.assertEqual(kinds[kinds.index("gap") + 1], "BM1")

    def test_empty_state(self):
        tab = TrendFollowingTab()
        self.assertEqual(tab.kpi_row.cards["sharpe"].value.text(), "-")
        self.assertFalse(tab.selector_row.isVisibleTo(tab))
        self.assertEqual(tab.compare_table.rowCount(), 0)



class TestWindowEndAndFormLayout(unittest.TestCase):
    """docs/ui.md 8.12 / spec 9-23: the window ends today by default; a request
    past DEV_END carries allow_holdout and the screen and report say so. The
    unfolded form is two rows in the chips' place."""

    def test_end_defaults_to_today_and_flags_the_holdout(self):
        from PyQt6.QtCore import QDate
        from strategy.trend_following import DEV_END
        tab = TrendFollowingTab()
        self.assertEqual(tab.end_edit.date(), QDate.currentDate())
        self.assertEqual(tab.end_edit.maximumDate(), QDate.currentDate())
        req = tab.build_request()
        self.assertEqual(req.end, date.today())
        self.assertTrue(req.allow_holdout)
        self.assertTrue(tab.holdout_lbl.isVisibleTo(tab))
        self.assertTrue(tab.dev_only_btn.isVisibleTo(tab))
        # the place on the status line stays reserved when hidden
        self.assertTrue(tab.holdout_lbl.sizePolicy().retainSizeWhenHidden())

        tab.end_edit.setDate(QDate(DEV_END.year, DEV_END.month, DEV_END.day))
        self.assertFalse(tab.build_request().allow_holdout)
        self.assertFalse(tab.holdout_lbl.isVisibleTo(tab))

    def test_dev_only_moves_the_end_and_falls_back_to_the_default_window(self):
        from PyQt6.QtCore import QDate
        from strategy.trend_following import DEV_END
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0"])                     # no error to outrank the note
        tab.start_edit.setDate(QDate(2022, 3, 2))
        tab.dev_only_btn.click()
        self.assertEqual(tab.end_edit.date().toPyDate(), DEV_END)
        self.assertEqual(tab.start_edit.date().toPyDate(), date(2022, 3, 2))
        self.assertNotIn("바꿨습니다", tab.status_summary_lbl.text())
        # a start past DEV_END leaves no valid window: the development default
        tab.end_edit.setDate(QDate.currentDate())
        tab.start_edit.setDate(QDate(DEV_END.year + 1, 2, 1))
        tab.dev_only_btn.click()
        self.assertEqual((tab.start_edit.date().toPyDate(), tab.end_edit.date().toPyDate()),
                         (date(2021, 1, 1), DEV_END))
        self.assertIn("2021-01-01 ~ 2024-12-31", tab.status_summary_lbl.text())
        tab.start_edit.setDate(QDate(2021, 2, 1))                            # the user edits: the note goes
        self.assertNotIn("바꿨습니다", tab.status_summary_lbl.text())

    def test_holdout_result_is_marked_in_warnings_and_report(self):
        import dataclasses
        import os
        import tempfile
        result = _result(("A0",), (1.0,))
        result.request = dataclasses.replace(result.request, allow_holdout=True)
        tab = TrendFollowingTab()
        tab._on_research_finished(result, "")
        self.assertIn("홀드아웃 구간", tab.warn_lbl.text())
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.md")
            with patch("ui.trend_following_tab.QFileDialog.getSaveFileName", return_value=(path, "")):
                tab._on_save_clicked()
            with open(path, encoding="utf-8") as f:
                self.assertIn("홀드아웃 구간", f.read())

    def test_thread_passes_the_holdout_switch_to_the_loader(self):
        from threads.strategy_threads import TrendFollowingResearchThread
        tab = TrendFollowingTab()
        req = tab.build_request()
        with patch("threads.strategy_threads.load_dataset", side_effect=RuntimeError("stop")) as load:
            TrendFollowingResearchThread(req).run()
        self.assertTrue(load.call_args.kwargs["allow_holdout"])

    # The layout checks below are relative to the widgets' own size hints, never
    # a pixel constant: font metrics differ between the Windows platform, the
    # offscreen platform and the app theme (review_codex.md, the two UI failures).
    def _settle(self, tab, width):
        tab.resize(width, 900)
        for _ in range(6):
            app.processEvents()

    @staticmethod
    def _top(tab, w) -> int:
        return w.mapTo(tab, w.rect().topLeft()).y()

    @staticmethod
    def _mid(tab, w) -> int:
        return w.mapTo(tab, w.rect().center()).y()

    def test_two_fixed_lines_when_narrow(self):
        """strategy_run_settings_ui.md 4: 전략 · 시장 · 조건 over 기간 · 중지 · 실행,
        switched by width only."""
        tab = TrendFollowingTab()
        tb = tab.toolbar
        self.assertLess(tb.two_line_width(), tb.one_line_width())
        tab.show()
        self._settle(tab, 1440)
        if tb.width() >= tb.one_line_width():
            self.assertTrue(tb.one_line)
        self._settle(tab, tab.minimumSizeHint().width())
        if tb.width() < tb.one_line_width():
            self.assertFalse(tb.one_line)
            mid = self._mid(tab, tab.strategy_combo)
            self.assertLessEqual(abs(self._mid(tab, tab.conditions_btn) - mid), 2)   # line one
            self.assertGreater(self._top(tab, tab.start_edit), mid)                  # line two
            self.assertGreater(self._top(tab, tab.run_btn), mid)
            right = tb.mapTo(tab, tb.rect().topRight()).x()
            self.assertLessEqual(tab.run_btn.mapTo(tab, tab.run_btn.rect().topRight()).x(), right)
        tab.hide()

    def test_choices_never_move_a_control(self):
        """Same width: strategy, market and rule choices leave every control
        and the result area where they were (proposal 5)."""
        tab = TrendFollowingTab()
        tab.show()
        self._settle(tab, 1100)
        watched = (tab.strategy_combo, *tab.market_buttons.values(), tab.start_edit, tab.end_edit, tab.conditions_btn,
                   tab.stop_btn, tab.run_btn, tab.kpi_row)

        def geoms():
            return [(w.mapTo(tab, w.rect().topLeft()), w.size()) for w in watched]
        before = geoms()
        tab.market_buttons["NASDAQ"].click()
        tab.market_buttons["KOSDAQ"].click()
        tab.set_variant_choice(["A0", "A3", "C1"])
        self._settle(tab, 1100)
        self.assertEqual(geoms(), before)
        tab.set_variant_choice([])
        self._settle(tab, 1100)
        self.assertEqual(geoms(), before)
        tab.hide()

    def test_inputs_lock_while_running(self):
        tab = TrendFollowingTab()
        tab.set_variant_choice(["A0"])
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab._on_run_clicked()
        for w in (*tab.market_buttons.values(), tab.start_edit, tab.end_edit, tab.conditions_btn):
            self.assertFalse(w.isEnabled())
        self.assertTrue(tab.strategy_combo.isEnabled())
        self.assertTrue(tab.stop_btn.isEnabled())
        _FakeThread.instances[0].finished.emit(None, "boom")
        for w in (*tab.market_buttons.values(), tab.start_edit, tab.end_edit, tab.conditions_btn):
            self.assertTrue(w.isEnabled())
    def test_checked_toggles_lose_the_accent_outline(self):
        from ui.theme import app_qss, ACCENT, LINE_STRONG
        qss = app_qss("")
        checked = qss[qss.index("QPushButton:checked {"):]
        checked = checked[:checked.index("}")]
        self.assertIn(f"border-color: {LINE_STRONG}", checked)
        self.assertNotIn(f"border-color: {ACCENT};", checked)
        self.assertIn("QPushButton#link", qss)
        self.assertIn("QLabel#fieldError", qss)
        self.assertIn('QPushButton[invalid="true"]', qss)


class TestFlowLayout(unittest.TestCase):
    def _flow(self, widths, inset=0, stretchy_last=False):
        from PyQt6.QtWidgets import QSizePolicy, QWidget
        from ui.common import FlowLayout
        host = QWidget()
        flow = FlowLayout(host, h_spacing=10, v_spacing=4)
        flow.set_first_line_inset(inset)
        items = []
        for k, w in enumerate(widths):
            item = QWidget()
            if stretchy_last and k == len(widths) - 1:
                item.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                item.setMinimumSize(w, 20)
            else:
                item.setFixedSize(w, 20)
            flow.addWidget(item)
            items.append(item)
        return host, flow, items

    def test_wraps_by_width_and_reports_its_height(self):
        host, flow, _ = self._flow([100, 100, 100])
        self.assertEqual([len(line) for line in flow.lines(330)], [3])
        self.assertEqual([len(line) for line in flow.lines(250)], [2, 1])
        self.assertEqual([len(line) for line in flow.lines(100)], [1, 1, 1])
        self.assertEqual(flow.heightForWidth(330), 20)
        self.assertEqual(flow.heightForWidth(250), 44)
        self.assertEqual(flow.minimumSize().width(), 100)

    def test_first_line_inset(self):
        host, flow, items = self._flow([100, 100, 100], inset=130)
        self.assertEqual([len(line) for line in flow.lines(330)], [1, 2])      # 200 left on line one: one item
        self.assertEqual([len(line) for line in flow.lines(340)], [2, 1])      # 210: two items fit exactly
        self.assertEqual(flow.minimumSize().width(), 230)
        host.resize(330, 100)
        host.show()
        app.processEvents()
        self.assertEqual(items[0].y(), 0)
        self.assertEqual((items[1].y(), items[2].y()), (24, 24))
        host.hide()

    def test_ignored_policy_item_stretches_to_the_line_end(self):
        host, flow, items = self._flow([100, 60], stretchy_last=True)
        host.resize(400, 50)
        host.show()
        app.processEvents()
        self.assertEqual(items[1].x(), 110)
        self.assertEqual(items[1].width(), 290)
        host.hide()

class TestMarketChoice(unittest.TestCase):
    """Spec 9-25: KOSPI / KOSDAQ / NASDAQ / NYSE, any combination, at least one."""

    def test_markets_go_into_the_request_and_the_chip(self):
        tab = TrendFollowingTab()
        self.assertEqual(tab.selected_markets(), ("KOSPI",))
        self.assertEqual(tab.build_request().markets, ("KOSPI",))
        tab.market_buttons["NASDAQ"].click()
        tab.market_buttons["KOSDAQ"].click()
        self.assertEqual(tab.build_request().markets, ("KOSPI", "KOSDAQ", "NASDAQ"))
        self.assertEqual([m for m, b in tab.market_buttons.items() if b.isChecked()], ["KOSPI", "KOSDAQ", "NASDAQ"])
        self.assertIn("KOSPI+KOSDAQ+NASDAQ 원화 시총 합산", tab.status_summary())
        tab.market_buttons["NASDAQ"].click()
        tab.market_buttons["KOSDAQ"].click()
        self.assertEqual(tab.selected_markets(), ("KOSPI",))
        self.assertNotIn("합산", tab.status_summary())

    def test_the_last_market_cannot_be_unticked(self):
        tab = TrendFollowingTab()
        tab.market_buttons["KOSPI"].click()
        self.assertTrue(tab.market_buttons["KOSPI"].isChecked())
        self.assertEqual(tab.selected_markets(), ("KOSPI",))
        tab.market_buttons["NYSE"].click()
        tab.market_buttons["KOSPI"].click()
        self.assertEqual(tab.selected_markets(), ("NYSE",))

    def test_thread_passes_the_markets(self):
        from threads.strategy_threads import TrendFollowingResearchThread
        tab = TrendFollowingTab()
        tab.market_buttons["NYSE"].click()
        with patch("threads.strategy_threads.load_dataset", side_effect=RuntimeError("stop")) as load:
            TrendFollowingResearchThread(tab.build_request()).run()
        self.assertEqual(load.call_args.kwargs["markets"], ("KOSPI", "NYSE"))



class TestMultiSelect(unittest.TestCase):
    """docs/ui.md 8.14: several strategies and benchmarks at once; one focus."""

    @classmethod
    def setUpClass(cls):
        cls.result = _result(("A0", "A1"), (0.0, 1.0, 2.0))

    def _tab(self):
        tab = TrendFollowingTab()
        tab._on_research_finished(self.result, "")
        return tab

    def _best(self):
        return max(self.result.runs_at(1.0, "strategy"), key=lambda r: r.metrics["sharpe"]).id

    def _other(self):
        return next(r.id for r in self.result.runs_at(1.0, "strategy") if r.id != self._best())

    def test_default_is_the_best_strategy_and_bm1(self):
        tab = self._tab()
        self.assertEqual(tab._sel_ids, [self._best(), "BM1"])
        self.assertEqual(tab._sel_id, self._best())
        self.assertEqual(set(tab.variant_seg.checked_keys()), {self._best(), "BM1"})
        self.assertTrue(tab.variant_seg.buttons[self._best()].property("segFocus"))

    def test_segments_pick_the_comparison_and_the_combo_the_detail(self):
        # docs/ui.md 8.17: 비교 대상 (segments, many) and 상세 기준 (combo, one) are separate.
        tab = self._tab()
        other = self._other()
        tab.variant_seg.buttons[other].click()            # unselected -> added, focus unchanged
        tab.variant_seg.buttons["BM3"].click()
        self.assertEqual(tab._sel_ids, [self._best(), "BM1", other, "BM3"])
        self.assertEqual(tab._sel_id, self._best())
        self.assertEqual([tab.focus_combo.itemData(i) for i in range(tab.focus_combo.count())], tab._sel_ids)
        tab.focus_combo.activated.emit(tab.focus_combo.findData("BM3"))
        self.assertEqual(tab._sel_id, "BM3")
        self.assertTrue(tab.kpi_row.cards["sharpe"].badge.isHidden())       # cards follow the focus (a BM)
        self.assertTrue(tab.variant_seg.buttons["BM3"].property("segFocus"))
        colors = {sr[0]: sr[1] for sr in tab._chart_series}
        tab.focus_combo.activated.emit(tab.focus_combo.findData(other))
        self.assertEqual(tab._sel_id, other)
        self.assertEqual(tab._sel_ids, [self._best(), "BM1", other, "BM3"])     # order (and colours) kept
        self.assertEqual({sr[0]: sr[1] for sr in tab._chart_series}, colors)
        self.assertIn(other, tab.target_card.subject.text())
        tab.variant_seg.buttons["BM1"].click()            # selected, not the focus -> removed, focus stays
        self.assertNotIn("BM1", tab._sel_ids)
        self.assertEqual(tab._sel_id, other)
        tab.variant_seg.buttons[other].click()            # the focus -> removed, focus moves on
        self.assertNotIn(other, tab._sel_ids)
        self.assertFalse(tab.variant_seg.buttons[other].isChecked())
        self.assertEqual(tab._sel_id, "BM3")
        self.assertEqual(tab.focus_combo.currentData(), "BM3")

    def test_the_last_run_cannot_be_removed(self):
        tab = self._tab()
        tab.variant_seg.buttons[self._best()].click()     # focus off -> BM1 remains
        self.assertEqual(tab._sel_ids, ["BM1"])
        tab.variant_seg.buttons["BM1"].click()
        self.assertEqual(tab._sel_ids, ["BM1"])
        self.assertTrue(tab.variant_seg.buttons["BM1"].isChecked())

    def test_chart_draws_every_selected_run_in_its_own_colour(self):
        from ui.colors import SERIES_BENCH
        tab = self._tab()
        for vid in (self._other(), "BM2"):
            tab.variant_seg.buttons[vid].click()
        ids = [sr[0] for sr in tab._chart_series]
        self.assertEqual(ids[0], tab._sel_id)                     # readout: the focus first
        self.assertEqual(set(ids), {self._best(), self._other(), "BM1", "BM2"})
        colors = {sr[0]: sr[1] for sr in tab._chart_series}
        self.assertEqual(len(set(colors.values())), 4)
        self.assertEqual(colors["BM1"], SERIES_BENCH)
        styles = {ln.get_label(): ln.get_linestyle() for ln in tab.ax.lines if not ln.get_label().startswith("_")}
        self.assertEqual(styles["BM2"], "--")
        self.assertEqual(styles[self._best()], "-")
        self.assertEqual(tab.ax.get_legend().get_texts()[-1].get_text(), tab._sel_id)   # focus drawn last
        curves = [ln for ln in tab.ax_dd.lines if len(ln.get_xdata()) > 2]
        self.assertEqual(len(curves), 4)                      # a drawdown curve per selected run
        self.assertIn("BM2", tab.readout_lbl.text())

    def test_comparison_table_selection_follows_and_drives(self):
        tab = self._tab()
        table = tab.compare_table
        selected = lambda: sorted(table.item(r, 0).text() for r in {i.row() for i in table.selectedIndexes()})   # noqa: E731
        self.assertEqual(selected(), sorted([self._best(), "BM1"]))
        row = {table.item(r, 0).text(): r for r in range(table.rowCount())}
        from PyQt6.QtCore import QItemSelectionModel
        model = table.selectionModel()
        model.select(table.model().index(row["BM4"], 0),
                     QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        self.assertIn("BM4", tab._sel_ids)
        self.assertEqual(set(tab.variant_seg.checked_keys()), {self._best(), "BM1", "BM4"})
        table.setCurrentCell(row["BM2"], 1)                   # plain click: that row only
        self.assertEqual(tab._sel_ids, ["BM2"])
        self.assertEqual(tab.variant_seg.checked_keys(), ["BM2"])

    def test_cost_switch_keeps_the_selection(self):
        tab = self._tab()
        tab.variant_seg.buttons["BM2"].click()
        tab.focus_combo.activated.emit(tab.focus_combo.findData("BM2"))
        before = list(tab._sel_ids)
        tab.cost_seg.buttons["2"].click()
        self.assertEqual(tab._sel_ids, before)
        self.assertEqual(tab._sel_id, "BM2")
        self.assertTrue(all(r.cost_mult == 2.0 for r in tab.selected_runs()))
        self.assertEqual(set(tab.variant_seg.checked_keys()), set(before))

class TestTradeList(unittest.TestCase):
    """docs/ui.md 8.13: the 매매 내역 tab and its popup show the same list
    (StrategyTradesView); the popup opens wide enough for every column."""

    @classmethod
    def setUpClass(cls):
        cls.result = _result(("A0", "A1"), (1.0,))

    def _tab(self, vid="A0"):
        tab = TrendFollowingTab()
        tab._on_research_finished(self.result, "")
        tab.select_variant(vid)
        return tab

    def _run(self, vid="A0"):
        return next(r for r in self.result.runs_at(1.0) if r.id == vid)

    def test_tab_list_matches_the_popup(self):
        from ui.dialogs.strategy_trades import COLUMNS
        from ui.strategy_widgets import fmt_price, reason_label
        tab = self._tab()
        run = self._run()
        self.assertTrue(run.trades)
        t = max(run.trades, key=lambda t: (t.exit_date, t.entry_date))
        headers = [tab.trades_table.horizontalHeaderItem(c).text() for c in range(tab.trades_table.columnCount())]
        self.assertEqual(headers, [h for h, _ in COLUMNS])
        cells = {h: tab.trades_table.item(0, c).text() for c, h in enumerate(headers)}
        self.assertEqual(cells["매입가"], fmt_price(t.entry_price))
        self.assertEqual(cells["매도가"], fmt_price(t.exit_price))
        self.assertEqual(cells["수량"], f"{t.qty:,}")
        self.assertEqual(cells["시장"], "KOSPI")
        for c in (0, 1):
            self.assertEqual(tab.trades_table.item(0, c).textAlignment(), int(Qt.AlignmentFlag.AlignCenter))
        self.assertEqual(cells["청산 사유"], reason_label(t.reason))
        self.assertEqual(tab.trades_table.item(0, 14).toolTip(), t.reason)
        self.assertIn(f"{len(run.trades)}건", tab.trades_summary_lbl.text())
        self.assertTrue(tab.trades_detail_btn.isEnabled())
        # same rows, same order, same text as the popup
        dlg = tab.open_trades_dialog()
        def grid(tbl):
            return [[tbl.item(r, c).text() for c in range(tbl.columnCount())] for r in range(tbl.rowCount())]
        self.assertEqual(grid(dlg.table), grid(tab.trades_table))
        dlg.close()

    def test_tab_list_filters_and_keeps_them_across_runs(self):
        tab = self._tab()
        run = self._run()
        code = sorted({t.reason for t in run.trades})[0]
        view = tab.trades_view
        view.reason_combo.setCurrentIndex(view.reason_combo.findData(code))
        self.assertEqual(len(view.visible_rows()), sum(1 for t in run.trades if t.reason == code))
        tab.select_variant("A1")            # A1 becomes the focus: filter carries over when the reason occurs
        a1 = self._run("A1")
        if any(t.reason == code for t in a1.trades):
            self.assertEqual(view.reason_combo.currentData(), code)
            self.assertEqual(len(view.visible_rows()), sum(1 for t in a1.trades if t.reason == code))
        else:
            self.assertEqual(view.reason_combo.currentIndex(), 0)

    def test_popup_is_wide_enough_for_every_column(self):
        tab = self._tab()
        dlg = tab.open_trades_dialog()
        self.assertGreaterEqual(dlg.width(), min(dlg.wanted_width(), dlg.screen().availableGeometry().width() - 40))
        if dlg.width() >= dlg.wanted_width():
            dlg.layout().activate()
            vp = dlg.table.viewport().width() + dlg.table.verticalScrollBar().sizeHint().width()
            cols = sum(dlg.table.columnWidth(c) for c in range(dlg.table.columnCount() - 1))
            self.assertLessEqual(cols, vp)
            self.assertFalse(dlg.table.horizontalScrollBar().isVisible())
        dlg.close()

    def test_benchmark_disables_the_popup(self):
        tab = self._tab("BM1")
        self.assertFalse(tab.trades_detail_btn.isEnabled())
        self.assertIsNone(tab.open_trades_dialog())

    def test_popup_shows_every_trade_with_amounts(self):
        from ui.dialogs.strategy_trades import COLUMNS
        tab = self._tab()
        run = self._run()
        dlg = tab.open_trades_dialog()
        self.assertIsNotNone(dlg)
        self.assertIn("A0", dlg.windowTitle())
        self.assertEqual(dlg.table.rowCount(), len(run.trades))
        headers = [dlg.table.horizontalHeaderItem(c).text() for c in range(len(COLUMNS))]
        row = 0
        t = dlg._row_trade(row)
        cells = {h: dlg.table.item(row, c).text() for c, h in enumerate(headers)}
        self.assertEqual(cells["수량"], f"{t.qty:,}")
        self.assertEqual(cells["매입금액"], f"{t.entry_price * t.qty:,.0f}")
        self.assertEqual(cells["매도금액"], f"{t.exit_price * t.qty:,.0f}")
        self.assertEqual(cells["손익(원)"], f"{t.pnl:+,.0f}")
        self.assertEqual(cells["비용(원)"], f"{t.cost.total:,.0f}")
        self.assertEqual(cells["시장"], "KOSPI")
        self.assertEqual(dlg.table.item(row, 0).textAlignment(), int(Qt.AlignmentFlag.AlignCenter))
        self.assertEqual(t.exit_date, max(x.exit_date for x in run.trades))   # newest exit first
        dlg.close()

    def test_popup_sorts_numerically_and_filters(self):
        tab = self._tab()
        run = self._run()
        dlg = tab.open_trades_dialog()
        dlg.table.sortItems(10, Qt.SortOrder.DescendingOrder)          # 손익(원)
        pnl = [dlg._row_trade(r).pnl for r in range(dlg.table.rowCount())]
        self.assertEqual(pnl, sorted(pnl, reverse=True))
        reasons = {t.reason for t in run.trades}
        code = sorted(reasons)[0]
        dlg.reason_combo.setCurrentIndex(dlg.reason_combo.findData(code))
        self.assertEqual(len(dlg.visible_rows()), sum(1 for t in run.trades if t.reason == code))
        if len(reasons) > 1:
            self.assertIn("필터", dlg.summary_lbl.text())
        dlg.reason_combo.setCurrentIndex(0)
        dlg.search.setText(run.trades[0].ticker.lower())
        self.assertEqual(len(dlg.visible_rows()), sum(1 for t in run.trades if t.ticker == run.trades[0].ticker))
        dlg.search.setText("no-such-name")
        self.assertEqual(dlg.visible_rows(), [])
        dlg.close()

    def test_double_click_opens_the_popup_on_that_trade(self):
        tab = self._tab()
        n = tab.trades_table.rowCount()
        row = n - 1
        ticker = tab.trades_table.item(row, 3).text()
        exit_date = tab.trades_table.item(row, 1).text()
        tab.trades_table.cellDoubleClicked.emit(row, 2)
        dlg = tab._trades_dialog
        sel = dlg.table.selectionModel().selectedRows()
        self.assertEqual(len(sel), 1)
        r = sel[0].row()
        self.assertEqual((dlg.table.item(r, 3).text(), dlg.table.item(r, 1).text()), (ticker, exit_date))
        dlg.close()

    def test_popup_rows_copy_as_tsv(self):
        tab = self._tab()
        dlg = tab.open_trades_dialog()
        self.assertTrue(hasattr(dlg.table, "_table_keys"))
        dlg.table.selectRow(0)
        from ui.common import copy_rows_tsv
        self.assertEqual(len(copy_rows_tsv(dlg.table).split("	")), dlg.table.columnCount())
        dlg.close()

class TestSharpeTarget(unittest.TestCase):
    """The Sharpe target is set in the form (default 1.0) and applies to the
    result of the run started with it."""

    @classmethod
    def setUpClass(cls):
        cls.result = _result(("A0", "A1"), (1.0,))

    def _run_with(self, tab, target):
        import dataclasses
        tab.set_conditions(dataclasses.replace(tab.conditions, variant_ids=("A0", "A1"), target_sharpe=target))
        _FakeThread.instances.clear()
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab._on_run_clicked()
        # changed while running: not this result's target
        tab.set_conditions(dataclasses.replace(tab.conditions, target_sharpe=2.5))
        _FakeThread.instances[0].finished.emit(self.result, "")

    def test_cost_switch_does_not_stack_segment_buttons(self):
        tab = TrendFollowingTab()
        tab._on_research_finished(self.result, "")
        tab.set_cost(1.0)
        tab.set_cost(1.0)                                 # no event loop in between
        lay = tab.variant_seg._layout
        shown = [lay.itemAt(i).widget() for i in range(lay.count())
                 if lay.itemAt(i).widget() is not None and not lay.itemAt(i).widget().isHidden()]
        self.assertEqual(len(shown), len(tab.variant_seg.buttons))

    def test_default_is_one_and_shows_in_the_summary(self):
        tab = TrendFollowingTab()
        import dataclasses
        self.assertEqual(tab.conditions.target_sharpe, 1.0)
        tab.set_conditions(dataclasses.replace(tab.conditions, variant_ids=("A0",), target_sharpe=1.3))
        self.assertIn("목표 Sharpe ≥ 1.3", tab.status_summary_lbl.text())

    def test_run_judges_against_the_target_it_was_started_with(self):
        from ui.common import _BADGE_NAMES
        from ui.strategy_widgets import SHARPE_COL, meets_targets
        tab = TrendFollowingTab()
        self._run_with(tab, 0.1)
        self.assertEqual(tab.target_sharpe, 0.1)
        self.assertEqual(tab.target_card.labels["sharpe"].text(), "Sharpe ≥ 0.1")
        self.assertIn("목표 ≥ 0.1", tab.kpi_row.cards["sharpe"].help.text())
        self.assertIn("0.1", tab.compare_table.horizontalHeaderItem(SHARPE_COL).text())
        self.assertEqual(tab._sharpe_delegate.target, 0.1)
        run = tab.selected_run()
        want = "ok" if run.metrics["sharpe"] >= 0.1 else "fail"
        self.assertEqual(tab.target_card.badges["sharpe"].objectName(), _BADGE_NAMES[want])
        row = next(r for r in range(tab.compare_table.rowCount()) if tab.compare_table.item(r, 0).text() == "A0")
        a0 = next(r for r in self.result.runs_at(1.0) if r.id == "A0")
        h = meets_targets(a0.metrics, 0.1)
        self.assertEqual(tab.compare_table.item(row, 8).text(), "-" if h is None else ("✓" if h else "✕"))
        self.assertIn("Sharpe ≥ 0.1", tab.compare_table.item(row, 8).toolTip())

        self._run_with(tab, 3.0)                          # a high bar fails every finite Sharpe below it
        self.assertEqual(tab.target_card.labels["sharpe"].text(), "Sharpe ≥ 3.0")
        run = tab.selected_run()
        if run.metrics["sharpe"] < 3.0:
            self.assertEqual(tab.target_card.badges["sharpe"].text(), "✕ 3.0 미만")


class TestTargetsAndDrawdown(unittest.TestCase):
    def test_sharpe_and_mdd_badge_boundaries(self):
        from ui.strategy_widgets import sharpe_badge, mdd_badge, meets_targets
        self.assertEqual(sharpe_badge(1.0)[0], "ok")               # default target 1.0
        self.assertEqual(sharpe_badge(0.9999)[0], "fail")
        self.assertEqual(sharpe_badge(1.4999, 1.5), ("fail", "✕ 1.5 미만"))
        self.assertEqual(sharpe_badge(1.5, 1.5)[0], "ok")
        self.assertFalse(meets_targets({"sharpe": 1.2, "mdd": -0.1}, 1.5))
        self.assertEqual(sharpe_badge(0.5), ("fail", "✕ 1.0 미만"))
        self.assertTrue(meets_targets({"sharpe": 1.2, "mdd": -0.1}))
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
        dlg = VariantPickerDialog(["A0", "C1"])
        self.assertEqual(set(dlg._items), set(VARIANTS))
        self.assertEqual(dlg.selected_ids(), ("A0", "C1"))
        self.assertIn("A0", dlg.detail.toPlainText())              # first row shown on open
        dlg.select("C2")
        text = dlg.detail.toPlainText()
        self.assertIn("C2", text)
        self.assertIn("변동성 정규화 없음", text)
        self.assertIn("10개 중 2개 선택", dlg.count_lbl.text())

    def test_all_none_and_flow_rows_are_tickable(self):
        from strategy.trend_following import VARIANTS
        from ui.dialogs import VariantPickerDialog
        dlg = VariantPickerDialog([])
        self.assertEqual(dlg.windowTitle(), "전략 선택")
        self.assertEqual(dlg.selected_ids(), ())
        a3 = dlg._items["A3"]
        self.assertTrue(a3.flags() & Qt.ItemFlag.ItemIsUserCheckable)
        dlg.select("A3")
        self.assertIn("투자자 수급 데이터 필요", dlg.detail.toPlainText())
        dlg._set_all(True)
        self.assertEqual(dlg.selected_ids(), tuple(VARIANTS))                   # matrix order
        dlg._set_all(False)
        self.assertEqual(dlg.selected_ids(), ())


class TestStrategyTab(unittest.TestCase):
    def test_has_the_trend_following_sub_tab_and_merges_threads(self):
        tab = StrategyTab()
        # no sub-tabs: the page's own selector lists the strategies
        self.assertEqual(tab.stack.count(), 1)
        page = tab.trend_following_tab
        self.assertEqual([page.strategy_combo.itemText(i) for i in range(page.strategy_combo.count())], ["추세추종"])
        self.assertEqual(tab.strategy_tabs, [page])
        self.assertEqual(tab.collect_threads_to_stop(), [])
        _FakeThread.instances.clear()
        tab.trend_following_tab.set_variant_choice(["A0"])
        with patch("ui.trend_following_tab.TrendFollowingResearchThread", _FakeThread):
            tab.trend_following_tab._on_run_clicked()
        self.assertEqual(len(tab.collect_threads_to_stop()), 1)


if __name__ == "__main__":
    unittest.main()


class TestToolbarLikeTheOtherTabs(unittest.TestCase):
    """User direction 2026-11-16 (docs/ui.md 8.17): the run toolbar's buttons
    (market toggles, Conditions, Stop, Run) and Save Report share the one
    toolbar width of Universe / History / Total Assets."""

    def test_shared_width(self):
        from ui.common import toolbar_button_width
        tab = TrendFollowingTab()
        buttons = [*tab.market_buttons.values(), tab.conditions_btn, tab.stop_btn, tab.run_btn, tab.save_btn]
        self.assertEqual({b.width() for b in buttons}, {toolbar_button_width()})
        for b in buttons:
            self.assertGreaterEqual(b.width(), b.sizeHint().width(), b.text())
            self.assertIsNone(b.menu())
