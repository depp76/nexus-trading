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
        self.assertIn("투자자 수급 필요", tab.variants_lbl.text())

        tab.flows_cb.setChecked(True)
        self.assertEqual(tab._selected_variants(), tuple(VARIANTS))
        self.assertEqual(tab.variants_lbl.text(), f"변형 {len(VARIANTS)}개 전체")
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
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), "경고 (0)")
        tab._on_research_finished(result, "")
        n = len(result.warnings) + 1
        # ui_redesign_2026-11 P1-1 item 5: the toggle panel became a result tab.
        self.assertEqual(tab.result_tabs.tabText(TAB_WARNINGS), f"경고 ({n})")
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
        # The form stays open after a run (back to choosing, docs/ui.md 8.12).
        self.assertTrue(tab.form_box.isVisibleTo(tab))
        self.assertTrue(tab.edit_btn.isChecked())

    def test_variant_pick_updates_cards_target_check_and_tabs(self):
        from ui.strategy_widgets import fmt_num, fmt_pct
        tab = self._tab()
        tab.select_variant("BM1")                    # A0 is not the focus before the click
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
        if a0.trades:                                            # name column next to the ticker
            t = max(a0.trades, key=lambda t: (t.exit_date, t.entry_date))
            self.assertEqual(tab.trades_table.item(0, 3).text(), t.ticker)
            self.assertEqual(tab.trades_table.item(0, 2).text(), self.result.dataset_info["names"].get(t.ticker, ""))
        self.assertEqual(tab.ax.get_legend().get_texts()[-1].get_text(), "A0")

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
        self.assertEqual(headers, ["ID", "Cost"] + [h for h, _ in SUMMARY_COLUMNS[1:]])
        self.assertEqual(tab.table.rowCount(), len(self.result.runs))

    def test_settings_chips_follow_the_form(self):
        tab = TrendFollowingTab()
        tab.universe_spin.setValue(120)
        tab.cost_combo.setCurrentIndex(1)
        texts = [c.text() for c in tab.chips if not c.isHidden()]
        self.assertIn("KOSPI 상위 120", texts)
        self.assertIn("비용 1x", texts)
        tab.edit_btn.click()
        self.assertFalse(tab.form_box.isVisibleTo(tab))

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
        chip = next(c for c in tab.chips if c.text() == "홀드아웃 포함")
        self.assertTrue(chip.property("warn"))

        tab.end_edit.setDate(QDate(DEV_END.year, DEV_END.month, DEV_END.day))
        self.assertFalse(tab.build_request().allow_holdout)
        self.assertFalse(tab.holdout_lbl.isVisibleTo(tab))
        self.assertNotIn("홀드아웃 포함", [c.text() for c in tab.chips if not c.isHidden()])

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
    @staticmethod
    def _two_line_width(tab) -> int:
        """A tab width at which the form is guaranteed to fit on two lines."""
        flow = tab.form_flow
        widths = [flow._width(flow.itemAt(i)) for i in range(flow.count())
                  if not flow.itemAt(i).widget().isHidden()]
        need = (sum(widths) + flow._h * len(widths) + flow._inset + max(widths)) // 2 + 60
        return need + (tab.width() - tab.form_box.width())

    def _settle(self, tab, width):
        tab.resize(width, 900)
        for _ in range(3):
            app.processEvents()

    def test_form_takes_the_chips_place_in_two_lines(self):
        tab = TrendFollowingTab()
        tab.show()
        self._settle(tab, 1440)
        self._settle(tab, self._two_line_width(tab))
        self.assertTrue(tab.form_box.isVisible())
        self.assertFalse(tab.chips_box.isVisible())
        lines = tab.form_flow.lines(tab.form_box.width())
        self.assertLessEqual(len(lines), 2)
        rows = {w.mapTo(tab, w.rect().center()).y() for w in
                (tab.start_edit, tab.end_edit, tab.universe_spin, tab.liquidity_spin, tab.positions_spin,
                 tab.weekday_combo, tab.cost_combo, tab.flows_cb, tab.event_cb, tab.variants_btn,
                 *tab.market_buttons.values())}
        self.assertLessEqual(len({y // 12 for y in rows}), 2)             # two lines of controls
        # the summary label keeps at least its floor and takes the rest of its line
        from ui.trend_following_tab import _VARIANTS_LBL_MIN_W
        self.assertGreaterEqual(tab.variants_lbl.width(), _VARIANTS_LBL_MIN_W)
        # the first line stops short of the buttons laid over the corner
        first_right = max(i.widget().mapTo(tab, i.widget().rect().topRight()).x() for i in lines[0])
        self.assertLess(first_right, tab.edit_btn.mapTo(tab, tab.edit_btn.rect().topLeft()).x())
        tab.edit_btn.click()
        self.assertFalse(tab.form_box.isVisible())
        self.assertTrue(tab.chips_box.isVisible())
        tab.hide()

    def test_form_wraps_instead_of_widening_the_tab(self):
        """At the tab's own minimum width the form takes more lines and every
        control is still fully inside the card and clear of the buttons."""
        tab = TrendFollowingTab()
        tab.show()
        self._settle(tab, 1440)
        wide_lines = len(tab.form_flow.lines(tab.form_box.width()))
        min_w = tab.minimumSizeHint().width()
        self.assertLess(min_w, self._two_line_width(tab))                  # narrower than the two-line layout
        self._settle(tab, min_w)
        self.assertEqual(tab.width(), min_w)
        lines = tab.form_flow.lines(tab.form_box.width())
        self.assertGreater(len(lines), wide_lines)
        card_right = tab.form_box.mapTo(tab, tab.form_box.rect().topRight()).x()
        btn_left = tab.edit_btn.mapTo(tab, tab.edit_btn.rect().topLeft()).x()
        btn_bottom = tab.edit_btn.mapTo(tab, tab.edit_btn.rect().bottomLeft()).y()
        for g in tab.form_groups:
            if g is tab.variants_lbl or g.isHidden():
                continue
            right = g.mapTo(tab, g.rect().topRight()).x()
            top = g.mapTo(tab, g.rect().topLeft()).y()
            self.assertLessEqual(right, card_right)                        # nothing cut at the card edge
            if top < btn_bottom:
                self.assertLess(right, btn_left)                           # nothing under the buttons

    def test_form_minimum_width_is_one_group_not_a_row(self):
        tab = TrendFollowingTab()
        tab.show()
        app.processEvents()
        flow = tab.form_flow
        widths = [flow._width(flow.itemAt(i)) for i in range(flow.count())
                  if not flow.itemAt(i).widget().isHidden()]
        self.assertEqual(tab.form_box.minimumSizeHint().width(), max(max(widths), widths[0] + flow._inset))
        # a long variant summary does not change it
        before = tab.minimumSizeHint().width()
        tab.variants_lbl.setText("A0, A1, A2, A3, A4, A5, B, C1, C2, C3 " * 6)
        app.processEvents()
        self.assertEqual(tab.minimumSizeHint().width(), before)
        # folded: the chip line does not set the width either
        tab.edit_btn.click()
        app.processEvents()
        self.assertLessEqual(tab.minimumSizeHint().width(), before)
        tab.hide()


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
        self.assertIn("KOSPI+KOSDAQ+NASDAQ 상위 200", [c.text() for c in tab.chips])

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

    def test_segment_clicks_add_focus_and_remove(self):
        tab = self._tab()
        other = self._other()
        tab.variant_seg.buttons[other].click()            # unselected -> added and focused
        tab.variant_seg.buttons["BM3"].click()
        self.assertEqual(tab._sel_ids, [self._best(), "BM1", other, "BM3"])
        self.assertEqual(tab._sel_id, "BM3")
        self.assertTrue(tab.kpi_row.cards["sharpe"].badge.isHidden())       # cards follow the focus (a BM)
        colors = {sr[0]: sr[1] for sr in tab._chart_series}
        tab.variant_seg.buttons[other].click()            # selected, not the focus -> focus
        self.assertEqual(tab._sel_id, other)
        self.assertEqual(tab._sel_ids, [self._best(), "BM1", other, "BM3"])     # order (and colours) kept
        self.assertEqual({sr[0]: sr[1] for sr in tab._chart_series}, colors)
        self.assertTrue(tab.variant_seg.buttons[other].isChecked())
        self.assertIn(other, tab.target_card.subject.text())
        tab.variant_seg.buttons[other].click()            # the focus -> removed, focus moves on
        self.assertNotIn(other, tab._sel_ids)
        self.assertFalse(tab.variant_seg.buttons[other].isChecked())
        self.assertEqual(tab._sel_id, "BM3")

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
        grid = lambda tbl: [[tbl.item(r, c).text() for c in range(tbl.columnCount())] for r in range(tbl.rowCount())]
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
