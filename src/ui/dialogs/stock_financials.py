"""The MA chart's 기업 정보 (company info) tab (docs/dart_db_plan_2026-11-26.md 13, stage 5;
docs/ui.md 8.34): DART quarterly results and revenue by segment of one KOSPI /
KOSDAQ stock, laid out as the approved mockup
(docs/ui_mockups/dart_financials_mockup.html):

    [기준 2026 3분기보고서 · 접수 … · 연결] [기간 …] [단위 억원]   [분기|연간]   DART 11-26 19:42
    [매출] [영업이익 (이익률)] [지배주주순이익] [최근 4분기 합계 매출]
    [실적: 분기 | 연간 -- revenue · margin chart | results table
           ── 사업부별: 제품별 | 부문별 -- revenue by line chart | segment table, notes ]   (same periods)
    footnotes

The data is read once, off the UI thread (threads.dart_threads), the first
time the tab is shown. Numbers come from ui.dart_financials_model. Colors
follow the app rule: blue (PROFIT) up / profit, red (LOSS) down / loss.
"""
from __future__ import annotations

import datetime as _dt
import logging

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter
from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QScrollBar,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from i18n import tr
from threads.dart_threads import FinancialsLoadThread
from ui import dart_financials_model as fm
from ui.colors import LOSS, OK, PROFIT, SERIES_PALETTE, WARN
from ui.common import FONT_BODY, FONT_CAPTION, FONT_HEADING, KpiCard, create_font, setup_table
from ui.theme import ACCENT, LINE, SURFACE, TEXT, TEXT_EMPTY, TEXT_MUTED

logger = logging.getLogger(__name__)

BAR_COLOR = SERIES_PALETTE[6]          # revenue bars: a calm blue, not the PROFIT "gain" color
SEGMENT_COLORS = SERIES_PALETTE
_CHECK_KIND = {"ok": OK, "sum_mismatch": WARN, "review": TEXT_EMPTY, "na": TEXT_EMPTY}
_SOURCE_LABELS = {"biz_product": ("사업의 내용 제품표", "business section, products"),
                  "biz_segment": ("사업의 내용 부문표", "business section, segments"),
                  "xbrl": ("XBRL", "XBRL"), "html_segment": ("주석 부문표", "segment note"),
                  "html_product": ("주석 제품표", "note's product table"), "single": ("단일부문", "single segment")}


def _pct(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _color(v) -> str:
    if v is None:
        return TEXT_EMPTY
    return PROFIT if v > 0 else LOSS if v < 0 else TEXT_MUTED


class _PinnedScroll:
    """A table's sideways scroll bar under its period columns only: Qt's own
    bar runs under the frozen item names (the vertical header) too, so it is
    switched off and this one mirrors it. Kept at the right end (the newest
    period) until the user scrolls; ``pin()`` after a refill."""

    def __init__(self, table: QTableWidget):
        self.table = table
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = table.horizontalScrollBar()      # still tracks range / value with the policy off
        self.bar = QScrollBar(Qt.Orientation.Horizontal)
        self.lead, self.tail = QWidget(), QWidget()
        self.layout = QHBoxLayout()
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)
        self.layout.addWidget(self.lead)
        self.layout.addWidget(self.bar, 1)
        self.layout.addWidget(self.tail)
        self.at_end = True
        inner.rangeChanged.connect(self.sync)
        inner.actionTriggered.connect(self.release)       # wheel on the table
        self.bar.actionTriggered.connect(self.release)    # the bar itself
        inner.valueChanged.connect(self.bar.setValue)
        self.bar.valueChanged.connect(inner.setValue)
        table.verticalHeader().geometriesChanged.connect(self.sync)
        table.verticalScrollBar().rangeChanged.connect(self.sync)
        self.sync()

    def release(self, *_args) -> None:
        self.at_end = False

    def pin(self) -> None:
        self.at_end = True
        self.sync()

    def sync(self, *_args) -> None:
        """Mirror Qt's bar; until the user scrolls, keep it at the right end:
        the column widths (and so the range) settle only once the table is
        laid out, which can be after the fill."""
        t, bar = self.table, self.bar
        inner = t.horizontalScrollBar()
        if self.at_end:
            inner.setValue(inner.maximum())
        bar.setRange(inner.minimum(), inner.maximum())
        bar.setPageStep(inner.pageStep())
        bar.setSingleStep(inner.singleStep())
        bar.setValue(inner.value())
        frame = t.frameWidth()
        vh = t.verticalHeader()
        self.lead.setFixedWidth(frame + (vh.width() if vh.isVisibleTo(t) else 0))
        vbar = t.verticalScrollBar()
        self.tail.setFixedWidth(frame + (vbar.sizeHint().width() if vbar.maximum() > 0 else 0))
        bar.setVisible(inner.maximum() > 0)


def _select_column(table: QTableWidget, col) -> None:
    """Select one period column (selection only: no scroll, no current cell), or none."""
    table.clearSelection()
    model = table.selectionModel()
    if col is not None and model is not None and 0 <= col < table.columnCount():
        model.select(table.model().index(0, col),
                     QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Columns)


def _quarter_ticks(ax, keys) -> None:
    """The quarterly x axis of both charts: the year under its first quarter
    (or the first column), a faint line between years."""
    xs = list(range(len(keys)))
    ax.set_xticks(xs)
    ax.set_xticklabels([str(fy) if fq == 1 or x == 0 else "" for x, (fy, fq) in zip(xs, keys)])
    for x, (_fy, fq) in zip(xs, keys):
        if fq == 1 and x:
            ax.axvline(x - 0.5, color=TEXT_EMPTY, linewidth=0.6, alpha=0.5)


class StockFinancialsTab(QWidget):
    """The tab; ``ensure_loaded()`` starts the read the first time it is shown."""

    def __init__(self, ticker: str, name: str, market: str, parent=None):
        super().__init__(parent)
        self.ticker, self.name, self.market = ticker, name, market
        self._thread: FinancialsLoadThread | None = None
        self._data: dict | None = None
        self._mode = "q"                      # "q" quarterly | "y" annual
        self._seg_level: str | None = None     # "product" | "segment"; None until the data is in
        self._quarters: list = []
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self._scroll)
        self._status = QLabel(tr("재무 데이터를 불러오는 중…", "Loading financials…"))
        self._status.setObjectName("muted")
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._scroll.setWidget(self._status)

    # ---- loading ----------------------------------------------------------------

    def ensure_loaded(self) -> None:
        if self._thread is not None or self._data is not None:
            return
        self._thread = FinancialsLoadThread(self.ticker)
        self._thread.finished_load.connect(self._on_loaded)
        self._thread.start()

    def shutdown(self, wait_ms: int = 2000) -> None:
        """Dialog closing: wait briefly for a read in flight (a local query)."""
        t = self._thread
        if t is not None:
            try:
                t.blockSignals(True)
                if t.isRunning():
                    t.wait(wait_ms)
            except RuntimeError:
                pass

    def _on_loaded(self, data, error: str, ticker: str) -> None:
        if ticker != self.ticker:
            return
        if error:
            self._show_message(tr(f"재무 데이터를 읽지 못했습니다: {error}", f"Could not read financials: {error}"))
            return
        if not data or not data.get("rows"):
            self._show_message(tr("이 종목의 DART 재무 데이터가 아직 없습니다.\n"
                                  "앱이 백그라운드에서 수집합니다(상단 DART 상태 참고).",
                                  "No DART financials for this stock yet.\n"
                                  "The app collects them in the background (see the DART status at the top)."))
            return
        self._data = data
        self._quarters = fm.quarter_rows(data)
        self._build()

    def _show_message(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setWordWrap(True)
        self._scroll.setWidget(lbl)

    # ---- layout -----------------------------------------------------------------

    def _build(self) -> None:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(8, 6, 8, 8)
        col.setSpacing(10)
        col.addLayout(self._build_bar())
        col.addLayout(self._build_kpis())

        col.addWidget(self._build_results_card())
        foot = QLabel(tr("† 4분기 = 사업보고서 연간값 − 3분기 누적(계산값). ⓡ 정정보고서 반영. "
                         "‡ 사업보고서 이후 정정된 3분기로 계산한 값. YoY = 전년 동기 대비, QoQ = 직전 분기 대비. "
                         "출처 OpenDART 정기공시, 접수일 기준 저장.",
                         "† Q4 = annual report total − Q3 cumulative (derived). ⓡ corrected report. "
                         "‡ derived from a Q3 corrected after the annual report. YoY = vs the same quarter a year "
                         "earlier, QoQ = vs the previous quarter. Source: OpenDART periodic reports, stored by "
                         "receipt date."))
        foot.setObjectName("faint")
        foot.setWordWrap(True)
        foot.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        col.addWidget(foot)
        col.addStretch(1)
        self._scroll.setWidget(page)
        self._refresh_mode()

    def _build_results_card(self) -> QFrame:
        """One '실적' card: the revenue / margin chart and the results table
        show the same periods, so they share one head -- the title, a note
        on what the chart marks, and the 분기 | 연간 switch that drives both
        -- and under them the segment section on the same periods (user
        direction 2026-11-27)."""
        card, self._table_title, box = self._card(tr("실적", "Results"))
        box.removeWidget(self._table_title)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self._table_title)
        self._chart_title = QLabel()             # empty unless the chart leaves periods out
        self._chart_title.setObjectName("muted")
        self._chart_title.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        head.addWidget(self._chart_title)
        head.addStretch(1)
        for btn in self._build_mode_buttons():
            head.addWidget(btn)
        box.insertLayout(0, head)

        body = QHBoxLayout()
        body.setSpacing(12)
        self._fig = Figure(figsize=(6.4, 2.8), linewidth=0)
        self._canvas = FigureCanvas(self._fig)
        self._canvas.setStyleSheet("border: none;")
        self._canvas.setMinimumHeight(260)
        self._canvas.mpl_connect("motion_notify_event", self._on_chart_motion)
        self._canvas.mpl_connect("figure_leave_event", lambda _e: self._hover(None))
        body.addWidget(self._canvas, 5)
        self._table = QTableWidget()
        setup_table(self._table)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(260)
        grid_box = QVBoxLayout()                 # the table with its scroll bar right under it
        grid_box.setSpacing(0)
        grid_box.addWidget(self._table, 1)
        self._results_scroll = _PinnedScroll(self._table)
        self.results_hbar = self._results_scroll.bar
        grid_box.addLayout(self._results_scroll.layout)
        body.addLayout(grid_box, 4)
        box.addLayout(body, 1)
        self._build_segments_section(box)
        return card

    def _card(self, title: str):
        frame = QFrame()
        frame.setObjectName("finCard")
        frame.setStyleSheet(f"QFrame#finCard {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px; }}")
        box = QVBoxLayout(frame)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(6)
        head = QLabel(title)
        head.setFont(create_font(FONT_HEADING, style_name="Semilight"))
        box.addWidget(head)
        return frame, head, box

    def _chip(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("muted")
        lbl.setFont(create_font(FONT_BODY, style_name="Semilight"))
        return lbl

    def _build_bar(self) -> QHBoxLayout:
        b = fm.basis_report(self._quarters)
        _div, unit_ko, unit_en = fm.unit_of(b.get("currency"))
        kind = {"CFS": tr("연결", "consolidated"), "OFS": tr("별도", "separate")}.get(b.get("fs_div"), "")
        report = {1: tr("1분기보고서", "Q1 report"), 2: tr("반기보고서", "half-year report"),
                  3: tr("3분기보고서", "Q3 report"), 4: tr("사업보고서", "annual report")}[b["fiscal_q"]]
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(self._chip(tr(f"기준 {b['fiscal_year']} {report} · 접수 {b['rcept_dt']} · {kind}",
                                    f"Basis {b['fiscal_year']} {report} · received {b['rcept_dt']} · {kind}")))
        fy0, fq0 = b["first"]
        row.addWidget(self._chip(tr(f"기간 {fy0} {fq0}Q ~ {b['fiscal_year']} {b['fiscal_q']}Q",
                                    f"Period {fy0} Q{fq0} – {b['fiscal_year']} Q{b['fiscal_q']}")))
        row.addWidget(self._chip(tr(f"단위 {unit_ko}", f"Unit {unit_en}")))
        row.addStretch(1)
        last = self._data.get("last_run")
        try:
            when = _dt.datetime.fromisoformat(last).strftime("%m-%d %H:%M") if last else "-"
        except ValueError:
            when = "-"
        sync = self._chip(f"DART {when}")
        sync.setObjectName("faint")
        row.addWidget(sync)
        return row

    def _build_mode_buttons(self) -> list:
        self._mode_group = QButtonGroup(self)
        self.mode_buttons = {}
        for key, label in (("q", tr("분기", "Quarterly")), ("y", tr("연간", "Annual"))):
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setChecked(key == self._mode)
            btn.setFont(create_font(FONT_BODY, style_name="Semilight"))
            btn.clicked.connect(lambda _c, k=key: self.set_mode(k))
            self._mode_group.addButton(btn)
            self.mode_buttons[key] = btn
        return list(self.mode_buttons.values())

    def set_mode(self, mode: str) -> None:
        """"q" quarterly or "y" annual: the chart and the results table."""
        if mode not in ("q", "y") or self._data is None:
            return
        self._mode = mode
        self.mode_buttons[mode].setChecked(True)
        self._refresh_mode()

    # ---- KPI cards ----------------------------------------------------------------

    def _build_kpis(self) -> QGridLayout:
        k = fm.kpis(self._quarters)
        last = k["last"]
        fy, fq = k["quarter"]
        div, _unit_ko, _ = fm.unit_of(last["currency"])
        q_label = f"{fy} {fq}Q"
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        self.kpi_cards = []

        def amount(v):
            return "—" if v is None else f"{v / div:,.0f}"

        def card(label, value, color, help_text):
            c = KpiCard(label)
            c.set(value, color, help=help_text)
            self.kpi_cards.append(c)
            grid.addWidget(c, 0, len(self.kpi_cards) - 1)

        if last["revenue"] is None:
            card(tr(f"매출 · {q_label}", f"Revenue · {q_label}"), "—", TEXT_EMPTY,
                 tr("금융업: 매출 해당 없음", "Financial filer: no revenue line"))
        else:
            card(tr(f"매출 · {q_label}", f"Revenue · {q_label}"), amount(last["revenue"]), TEXT,
                 f"YoY {_pct(last['revenue_yoy'])} · QoQ {_pct(last['revenue_qoq'])}")
        margin = "" if last["op_margin"] is None else f" ({last['op_margin'] * 100:.1f}%)"
        mchg = k["margin_change"]
        mtxt = "" if mchg is None else tr(f" · 이익률 {mchg * 100:+.1f}%p", f" · margin {mchg * 100:+.1f}pp")
        card(tr(f"영업이익 · {q_label}", f"Operating income · {q_label}"), amount(last["op_income"]) + margin,
             LOSS if (last["op_income"] or 0) < 0 else TEXT, f"YoY {_pct(last['op_yoy'])}{mtxt}")
        ni_label = tr("지배주주순이익", "Owners' net income") if last["net_is_owners"] else tr("순이익", "Net income")
        card(f"{ni_label} · {q_label}", amount(last["net_income"]),
             LOSS if (last["net_income"] or 0) < 0 else TEXT,
             f"YoY {_pct(last['net_yoy'])} · QoQ {_pct(last['net_qoq'])}")
        if k["ttm"] is None:
            card(tr("최근 4분기 합계 매출", "Revenue, last 4 quarters"), "—", TEXT_EMPTY, "")
        else:
            card(tr("최근 4분기 합계 매출", "Revenue, last 4 quarters"), amount(k["ttm"]), TEXT,
                 tr(f"직전 4분기 대비 {_pct(k['ttm_growth'])}", f"vs the 4 quarters before {_pct(k['ttm_growth'])}"))
        return grid

    # ---- quarterly / annual chart and table -----------------------------------------

    def _refresh_mode(self) -> None:
        if self._mode == "q":
            last_fy = self._quarters[-1]["fiscal_year"]
            rows = [q for q in self._quarters if q["fiscal_year"] >= last_fy - 4]
            self._period_keys = [(r["fiscal_year"], r["fiscal_q"]) for r in rows]
        else:
            rows = fm.annual_rows(self._quarters)
            self._period_keys = [(r["fiscal_year"], r["partial_q"] or 4) for r in rows]
        self._chart_title.setText("")
        self._draw_results_chart(rows)
        self._fill_results_table(rows)
        self._refresh_segments()

    def _period_label(self, r) -> str:
        if self._mode == "q":
            return f"{r['fiscal_year']} {r['fiscal_q']}Q"
        if r.get("partial_q"):
            return tr(f"{r['fiscal_year']} {r['partial_q']}Q누적", f"{r['fiscal_year']} Q{r['partial_q']} YTD")
        return str(r["fiscal_year"])

    def _draw_results_chart(self, rows) -> None:
        fig = self._fig
        fig.clear()
        ax = fig.add_subplot(111)
        div, unit_ko, unit_en = fm.unit_of(rows[-1]["currency"] if rows else "KRW")
        xs = list(range(len(rows)))
        # One axis, one unit: periods reported in another currency than the
        # newest are left out of the bars (they are in the table, in their own
        # unit) and the title says so (review_codex 2 P2).
        other = [r for r in rows if rows and (r["currency"] or "KRW") != (rows[-1]["currency"] or "KRW")]
        if other:
            self._chart_title.setText(tr("차트: 통화가 다른 기간 제외", "Chart: other-currency periods left out"))
        for x, r in zip(xs, rows):
            if r["revenue"] is None or r in other:
                continue
            loss = (r["op_income"] or 0) < 0
            hatched = r.get("derived") if self._mode == "q" else bool(r.get("partial_q"))
            ax.bar(x, r["revenue"] / div, width=0.7, color=LOSS if loss else BAR_COLOR,
                   hatch="////" if hatched else None, edgecolor=SURFACE if hatched else None, linewidth=0)
        ax.set_ylabel(tr(f"매출 ({unit_ko})", f"Revenue ({unit_en})"), color=TEXT_MUTED)
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))
        ax.set_xticks(xs)
        if self._mode == "q":
            _quarter_ticks(ax, [(r["fiscal_year"], r["fiscal_q"]) for r in rows])
        else:
            ax.set_xticklabels([self._period_label(r) for r in rows])
        margins = [(x, r["op_margin"] * 100) for x, r in zip(xs, rows) if r["op_margin"] is not None]
        if margins:
            ax2 = ax.twinx()
            ax2.plot([m[0] for m in margins], [m[1] for m in margins], color=ACCENT, linewidth=1.8, marker="o",
                     markersize=2.5)
            ax2.axhline(0, color=ACCENT, linewidth=0.8, linestyle=(0, (2, 3)), alpha=0.5)
            ax2.set_ylabel(tr("영업이익률 (%)", "Operating margin (%)"), color=ACCENT)
            ax2.tick_params(axis="y", colors=ACCENT)
        ax.legend(handles=[Patch(color=BAR_COLOR, label=tr("매출", "Revenue")),
                           Patch(color=LOSS, label=tr("영업적자", "Operating loss"))],
                  loc="upper left", fontsize=FONT_CAPTION, frameon=False)
        ax.set_xlim(-0.6, max(len(rows) - 0.4, 0.6))      # the segment chart uses the same range
        self._build_chart_marker(ax, ax2 if margins else None, rows, div)
        fig.tight_layout()
        self._results_ax = ax
        self._canvas.draw_idle()

    # ---- marker (hover) on the results chart ---------------------------------------

    def _build_chart_marker(self, ax, ax2, rows, div) -> None:
        """The hover marker: a band over the period's bar, a dot on the margin
        line and a box with the period's numbers; the table's column for the
        same period is selected too. Hidden until the mouse is over a bar."""
        self._marker_rows, self._marker_div, self._marker_idx = rows, div, None
        self._marker_ax = ax
        top = ax2 if ax2 is not None else ax     # the twin axes is drawn over the bars
        self._marker_band = top.axvspan(-0.45, 0.45, color=TEXT_MUTED, alpha=0.28, linewidth=0, visible=False)
        self._marker_dot = None
        if ax2 is not None:
            (self._marker_dot,) = ax2.plot([0], [0], linestyle="none", marker="o", markersize=7,
                                           color=ACCENT, markeredgecolor=SURFACE, markeredgewidth=1.5,
                                           visible=False, zorder=5)
        self._marker_box = top.annotate(
            "", xy=(0, 1), xycoords=("data", "axes fraction"), xytext=(10, -8), textcoords="offset points",
            ha="left", va="top", multialignment="left", fontsize=FONT_CAPTION, color=TEXT, visible=False, zorder=10,
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SURFACE, edgecolor=LINE, alpha=0.95))

    def _on_chart_motion(self, event) -> None:
        rows = getattr(self, "_marker_rows", None)
        if not rows or event.inaxes is None or event.xdata is None:
            self._hover(None)
            return
        idx = int(round(event.xdata))
        self._hover(idx if 0 <= idx < len(rows) and abs(event.xdata - idx) <= 0.45 else None)

    def _hover(self, idx) -> None:
        """Mark period ``idx`` (one index for both charts and tables), or none."""
        self._set_chart_marker(idx)
        if getattr(self, "_seg_marker_sv", None) is not None:
            self._set_seg_marker(idx)

    def _set_chart_marker(self, idx) -> None:
        """Show the marker on period ``idx`` (an index into the chart's rows,
        oldest first -- the same order as the table's columns), or hide it."""
        if getattr(self, "_marker_rows", None) is None or idx == self._marker_idx:
            return
        self._marker_idx = idx
        artists = [a for a in (self._marker_band, self._marker_dot, self._marker_box) if a is not None]
        if idx is None:
            for a in artists:
                a.set_visible(False)
            self._table.clearSelection()
            self._canvas.draw_idle()
            return
        r, div = self._marker_rows[idx], self._marker_div
        self._marker_band.set_x(idx - 0.45)
        self._marker_band.set_visible(True)
        if self._marker_dot is not None:
            on_line = r["op_margin"] is not None
            if on_line:
                self._marker_dot.set_data([idx], [r["op_margin"] * 100])
            self._marker_dot.set_visible(on_line)
        lines = [self._period_label(r) + ("†" if r.get("derived") else "")]
        lines.append(tr("매출 ", "Revenue ") + self._amt(r["revenue"], div) + f"  YoY {_pct(r['revenue_yoy'], 0)}")
        lines.append(tr("영업이익 ", "Op. income ") + self._amt(r["op_income"], div)
                     + f"  YoY {_pct(r['op_yoy'], 0)}")
        if r["op_margin"] is not None:
            lines.append(tr("영업이익률 ", "Op. margin ") + f"{r['op_margin'] * 100:.1f}%")
        box = self._marker_box
        box.set_text("\n".join(lines))
        box.xy = (idx, 1)
        right = idx > (len(self._marker_rows) - 1) * 0.6        # flip to the left near the right edge
        box.set_horizontalalignment("right" if right else "left")
        box.set_position((-10 if right else 10, -8))
        box.set_visible(True)
        _select_column(self._table, idx)        # the same period in the table
        self._canvas.draw_idle()

    def _fill_results_table(self, rows) -> None:
        """Items down the side, periods across (oldest on the left, like the
        chart and the segment table), shown scrolled to the newest. The item
        names are the vertical header, so they stay put while the periods
        scroll sideways."""
        yoy = "  YoY"
        items = [tr("매출", "Revenue"), yoy, tr("영업이익", "Op. income"), yoy, tr("이익률", "Margin"),
                 tr("지배순이익", "Owners' NI") if any(r.get("net_is_owners") for r in self._quarters)
                 else tr("순이익", "Net income"), yoy, tr("비고", "Notes"), tr("접수일", "Received")]
        periods = list(rows)
        t = self._table
        t.setUpdatesEnabled(False)
        try:
            t.clear()
            t.setColumnCount(len(periods))
            t.setHorizontalHeaderLabels([
                self._period_label(r) + ("†" if r.get("derived") else "") + ("‡" if r.get("lookahead") else "")
                for r in periods])
            t.setRowCount(len(items))
            vh = t.verticalHeader()
            for i, name in enumerate(items):
                head = QTableWidgetItem(name)
                head.setTextAlignment(int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
                t.setVerticalHeaderItem(i, head)
            vh.setVisible(True)                      # setup_table hides it; here it is the frozen column
            base_currency = (rows[-1]["currency"] or "KRW") if rows else "KRW"
            for j, r in enumerate(periods):
                div, unit_ko, unit_en = fm.unit_of(r["currency"])       # each period in its own unit
                cells = [
                    (self._amt(r["revenue"], div), None, None),
                    (_pct(r["revenue_yoy"], 0), _color(r["revenue_yoy"]), None),
                    (self._amt(r["op_income"], div), LOSS if (r["op_income"] or 0) < 0 else None, None),
                    (_pct(r["op_yoy"], 0), _color(r["op_yoy"]), None),
                    ("—" if r["op_margin"] is None else f"{r['op_margin'] * 100:.1f}%",
                     LOSS if (r["op_margin"] or 0) < 0 else None, None),
                    (self._amt(r["net_income"], div), LOSS if (r["net_income"] or 0) < 0 else None, None),
                    (_pct(r["net_yoy"], 0), _color(r["net_yoy"]), None),
                    self._note(r, tr(f"단위 {unit_ko}", f"unit {unit_en}")
                               if (r["currency"] or "KRW") != base_currency else ""),
                    (r["rcept_dt"], TEXT_MUTED, Qt.AlignmentFlag.AlignCenter),
                ]
                for i, (text, color, align) in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(int(align or (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)))
                    if color:
                        item.setForeground(QColor(color))
                    t.setItem(i, j, item)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectColumns)
        finally:
            t.setUpdatesEnabled(True)
        self._results_scroll.pin()               # newest in view until the user scrolls

    @staticmethod
    def _amt(v, div) -> str:
        return "—" if v is None else f"{v / div:,.0f}"

    @staticmethod
    def _note(r, unit_note: str = ""):
        parts = [unit_note] if unit_note else []
        if r.get("basis_changed"):
            parts.append(tr("기준 변경", "basis changed"))
        turn = r.get("turn")
        color = None
        if turn in fm.TURN_LABELS:
            parts.append(tr(*fm.TURN_LABELS[turn]))
            color = PROFIT if turn == "turn_profit" else LOSS
        if r.get("corrected"):
            parts.append(tr("ⓡ 정정", "ⓡ corrected"))
        return " ".join(parts), color, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter

    # ---- segments ----------------------------------------------------------------

    def _build_segments_section(self, box: QVBoxLayout) -> None:
        """The segment section of the 실적 card (user direction 2026-11-27):
        under a rule, a head (title, a caption on the view, the 제품별 | 부문별
        switch when both breakdowns exist), the chart and the table with the
        results' 5 : 4 split and the results' periods slot for slot (the
        charts' plot areas aligned, one hover marks both), items down the
        table's frozen side; notes under both."""
        rule = QFrame()
        rule.setFrameShape(QFrame.Shape.HLine)
        rule.setStyleSheet(f"color: {LINE}; background: {LINE}; max-height: 1px; border: none;")
        box.addWidget(rule)
        self._seg_title = QLabel(tr("사업부별", "By segment"))
        self._seg_title.setFont(create_font(FONT_HEADING, style_name="Semilight"))
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(self._seg_title)
        self._seg_caption = QLabel()
        self._seg_caption.setObjectName("muted")
        self._seg_caption.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        head.addWidget(self._seg_caption)
        head.addStretch(1)
        box.addLayout(head)
        self.segment_data = fm.segment_view(self._data)
        self._seg_empty = not self.segment_data["columns"]
        self._seg_level = self.segment_data["level"]
        self.level_buttons = {}
        if len(self.segment_data["levels"]) > 1:
            # Products (the business section's table) or segments: one breakdown
            # for every period, picked here (user direction 2026-11-27).
            self._seg_level_group = QButtonGroup(self)
            for key, label in (("product", tr("제품별", "By product")), ("segment", tr("부문별", "By segment"))):
                btn = QPushButton(label)
                btn.setCheckable(True)
                btn.setChecked(key == self._seg_level)
                btn.setFont(create_font(FONT_BODY, style_name="Semilight"))
                btn.clicked.connect(lambda _c, k=key: self.set_segment_level(k))
                self._seg_level_group.addButton(btn)
                head.addWidget(btn)
                self.level_buttons[key] = btn
        if self._seg_empty:
            lbl = QLabel(tr("사업부별 매출이 아직 없습니다 (tools/dart_sync.py --segments).",
                            "No segment revenue yet (tools/dart_sync.py --segments)."))
            lbl.setObjectName("muted")
            lbl.setWordWrap(True)
            box.addWidget(lbl)
            return
        body = QHBoxLayout()
        body.setSpacing(12)
        self._seg_fig = Figure(figsize=(6.4, 2.8), linewidth=0)
        self._seg_canvas = FigureCanvas(self._seg_fig)
        self._seg_canvas.setStyleSheet("border: none;")
        self._seg_canvas.setMinimumHeight(260)
        self._seg_canvas.mpl_connect("motion_notify_event", self._on_seg_motion)
        self._seg_canvas.mpl_connect("figure_leave_event", lambda _e: self._hover(None))
        body.addWidget(self._seg_canvas, 5)
        self.segment_table = QTableWidget()
        setup_table(self.segment_table, row_height=36)
        self.segment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.segment_table.setMinimumHeight(260)
        grid_box = QVBoxLayout()
        grid_box.setSpacing(0)
        grid_box.addWidget(self.segment_table, 1)
        self._seg_scroll = _PinnedScroll(self.segment_table)
        grid_box.addLayout(self._seg_scroll.layout)
        body.addLayout(grid_box, 4)
        box.addLayout(body, 1)
        self._seg_notes = QLabel()
        self._seg_notes.setObjectName("faint")
        self._seg_notes.setWordWrap(True)
        self._seg_notes.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        box.addWidget(self._seg_notes)

    def set_segment_level(self, level: str) -> None:
        """"product" or "segment": the breakdown the segment card shows."""
        if level not in self.level_buttons or self._data is None:
            return
        self._seg_level = level
        self.level_buttons[level].setChecked(True)
        self._refresh_segments()

    def _refresh_segments(self) -> None:
        """Redraw the segment card for the current 분기 | 연간 mode."""
        if self._mode == "q":
            caption = tr("분기 매출액 · 누적 차이로 계산", "quarterly revenue, from cumulatives")
        else:
            caption = tr("연간 매출액 · 진행 중인 해는 누적(빗금)", "annual revenue; the year in progress cumulative (hatched)")
        self._seg_caption.setText(caption)
        if self._seg_empty:
            return
        sv = fm.segment_view(self._data, mode=self._mode, level=self._seg_level, keys=self._period_keys)
        self.segment_data = sv
        self._draw_segment_chart(sv)
        self._fill_segment_table(sv)
        notes = self._segment_notes(sv)
        if not sv["has_values"]:
            notes = tr("이 기준의 분기 자료가 없습니다 — 연간 보기나 다른 기준을 고르세요.",
                       "No quarterly data on this breakdown: pick the annual view or the other breakdown.")
        self._seg_notes.setText(notes)

    def _draw_segment_chart(self, sv) -> None:
        """Revenue amount per segment, the segments side by side within each
        period (user direction 2026-11-27: amounts, not shares, so each
        segment's rise or fall reads across the periods). A period that is not
        a full year (the year in progress) is hatched and paler: its amount is
        a quarter / year-to-date figure, not comparable with the annual bars.
        The x axis is labelled as the results chart's."""
        fig = self._seg_fig
        fig.clear()
        ax = fig.add_subplot(111)
        cols, segs = sv["columns"], sv["segments"]
        div, unit_ko, unit_en = fm.unit_of(sv["currency"])
        xs = list(range(len(cols)))
        n = max(len(segs), 1)
        group_w = 0.8
        bar_w = group_w / n
        for i, s in enumerate(segs):
            color = SEGMENT_COLORS[i % len(SEGMENT_COLORS)]
            offset = -group_w / 2 + bar_w * (i + 0.5)
            for x, c, v in zip(xs, cols, s["values"]):
                if v is None:
                    continue
                partial = c["partial"]
                ax.bar(x + offset, v / div, width=bar_w * 0.92, color=color, edgecolor=SURFACE if not partial
                       else color, linewidth=0.6, alpha=0.45 if partial else 1.0,
                       hatch="///" if partial else None)
        ax.axhline(0, color=LINE, linewidth=0.8)
        if sv.get("mode") == "q":
            _quarter_ticks(ax, [(c["fiscal_year"], c["fiscal_q"]) for c in cols])
        else:
            ax.set_xticks(xs)
            ax.set_xticklabels([c["label"] + ("*" if c["gross"] else "") for c in cols])
        ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _p: f"{v:,.0f}"))
        ax.set_ylabel(tr(f"매출 ({unit_ko})", f"Revenue ({unit_en})"), color=TEXT_MUTED)
        ax.set_xlim(-0.6, max(len(cols) - 0.4, 0.6))
        ax.legend(handles=[Patch(color=SEGMENT_COLORS[i % len(SEGMENT_COLORS)], label=s["name"])
                           for i, s in enumerate(segs)],
                  loc="upper left", fontsize=FONT_CAPTION, frameon=False, ncols=min(len(segs), 3) or 1)
        # Hover marker, as on the results chart.
        self._seg_marker_sv, self._seg_marker_div, self._seg_marker_idx = sv, div, None
        self._seg_marker_ax = ax
        self._seg_marker_band = ax.axvspan(-0.45, 0.45, color=TEXT_MUTED, alpha=0.28, linewidth=0, visible=False)
        self._seg_marker_box = ax.annotate(
            "", xy=(0, 1), xycoords=("data", "axes fraction"), xytext=(10, -8), textcoords="offset points",
            ha="left", va="top", multialignment="left", fontsize=FONT_CAPTION, color=TEXT, visible=False, zorder=10,
            bbox=dict(boxstyle="round,pad=0.4", facecolor=SURFACE, edgecolor=LINE, alpha=0.95))
        fig.tight_layout()
        # The results chart's plot area: the same periods sit one above the other.
        if getattr(self, "_results_ax", None) is not None:
            ax.set_position(self._results_ax.get_position())
        self._seg_canvas.draw_idle()

    def _on_seg_motion(self, event) -> None:
        sv = getattr(self, "_seg_marker_sv", None)
        if not sv or not sv["columns"] or event.inaxes is None or event.xdata is None:
            self._hover(None)
            return
        idx = int(round(event.xdata))
        n = len(sv["columns"])
        self._hover(idx if 0 <= idx < n and abs(event.xdata - idx) <= 0.45 else None)

    def _set_seg_marker(self, idx) -> None:
        """Show the period ``idx``'s amounts, shares and changes in a box over
        its bars and select its column in the segment table; None hides it."""
        sv = getattr(self, "_seg_marker_sv", None)
        if sv is None or idx == self._seg_marker_idx:
            return
        self._seg_marker_idx = idx
        if idx is None:
            self._seg_marker_band.set_visible(False)
            self._seg_marker_box.set_visible(False)
            self.segment_table.clearSelection()
            self._seg_canvas.draw_idle()
            return
        if idx >= len(sv["columns"]):
            return
        c, div = sv["columns"][idx], self._seg_marker_div
        lines = [c["label"] + ("*" if c["gross"] else "")]
        for s in sv["segments"]:
            v, sh, g = s["values"][idx], s["shares"][idx], s["yoy"][idx]
            if v is None:
                continue
            extra = " · ".join(x for x in ((f"{sh * 100:.0f}%" if sh is not None else ""),
                                           (_pct(g, 0) if g is not None else "")) if x)
            lines.append(f"{s['name']} {self._amt(v, div)}" + (f"  {extra}" if extra else ""))
        if len(lines) == 1:
            lines.append(tr("사업부 자료 없음", "no segment data"))
        self._seg_marker_band.set_x(idx - 0.45)
        self._seg_marker_band.set_visible(True)
        box = self._seg_marker_box
        box.set_text("\n".join(lines))
        box.xy = (idx, 1)
        right = idx > (len(sv["columns"]) - 1) * 0.6
        box.set_horizontalalignment("right" if right else "left")
        box.set_position((-10 if right else 10, -8))
        box.set_visible(True)
        _select_column(self.segment_table, idx)
        self._seg_canvas.draw_idle()

    def _fill_segment_table(self, sv) -> None:
        """Items (segments or products, in their chart colours) down the frozen
        side, then the 검산 row; periods across, oldest on the left and opened
        at the newest, plus the CAGR in the annual view -- as the results
        table. A cell: the amount, under it the share of revenue and the
        change."""
        cols, segs = sv["columns"], sv["segments"]
        div, _k, _e = fm.unit_of(sv["currency"])
        t = self.segment_table
        t.setUpdatesEnabled(False)
        try:
            annual = sv.get("mode", "y") != "q"
            t.clear()
            t.setColumnCount(len(cols) + (1 if annual else 0))
            t.setHorizontalHeaderLabels([*[c["label"] + ("*" if c["gross"] else "") for c in cols],
                                         *([tr("연평균", "CAGR")] if annual else [])])
            t.setRowCount(len(segs) + 1)
            for i, s in enumerate(segs):
                head = QTableWidgetItem(s["name"])
                head.setTextAlignment(int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
                head.setForeground(QColor(SEGMENT_COLORS[i % len(SEGMENT_COLORS)]))
                t.setVerticalHeaderItem(i, head)
                for j, (v, g, sh) in enumerate(zip(s["values"], s["yoy"], s["shares"])):
                    text = "—" if v is None else f"{v / div:,.0f}"
                    second = [f"{sh * 100:.0f}%"] if sh is not None else []
                    if g is not None:
                        second.append(_pct(g, 0))
                    if second:
                        text += "\n" + " · ".join(second)
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                    if g is not None:
                        item.setForeground(QColor(_color(g)))
                    t.setItem(i, j, item)
                if annual:
                    cagr = QTableWidgetItem(_pct(s["cagr"]))
                    cagr.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                    cagr.setForeground(QColor(_color(s["cagr"])))
                    t.setItem(i, len(cols), cagr)
            check = len(segs)
            head = QTableWidgetItem(tr("검산", "Check"))
            head.setTextAlignment(int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter))
            head.setForeground(QColor(TEXT_MUTED))
            t.setVerticalHeaderItem(check, head)
            for j, c in enumerate(cols):
                label = tr(*fm.CHECK_LABELS.get(c["status"], (c["status"], c["status"])))
                if c.get("source") == "single":
                    label = tr("단일부문", "Single segment")
                item = QTableWidgetItem(label)
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                item.setForeground(QColor(_CHECK_KIND.get(c["status"], TEXT_EMPTY)))
                t.setItem(check, j, item)
            t.verticalHeader().setVisible(True)      # setup_table hides it; here it is the frozen column
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectColumns)
        finally:
            t.setUpdatesEnabled(True)
        self._seg_marker_idx = None
        self._seg_scroll.pin()

    @staticmethod
    def _segment_notes(sv) -> str:
        quarterly = sv.get("mode") == "q"
        lines = [tr("셀 아래 줄: 매출 비중(손익계산서 매출 대비) · " + ("전년 동기 대비" if quarterly else "전년 대비")
                    + ". 검산: 부문 합계 + 조정 vs 손익계산서 매출, 차이 1% 이내 = ok.",
                    "Second line: share of income-statement revenue · " + ("YoY" if quarterly else "change on the year")
                    + ". Check: segments + adjustments vs income-statement revenue, within 1 % = ok.")]
        sources = list(dict.fromkeys(c.get("source") for c in sv["columns"] if c.get("source")))
        if sources:
            lines.append(tr("출처: ", "Source: ") + ", ".join(tr(*_SOURCE_LABELS.get(x, (x, x))) for x in sources))
        if "biz_product" in sources:
            lines.append(tr("제품별: 사업의 내용 '매출 및 수주상황' / '주요 제품' 표. 표에 없는 품목은 비중 합계에서 빠짐.",
                            "By product: the business section's sales / main products table. Lines the table "
                            "leaves out are not in the shares."))
        if quarterly:
            lines.append(tr("분기 값 = 누적 − 직전 분기 누적(같은 표 기준끼리만). 분기별 사업부 자료는 최근 2개 사업연도만 수집.",
                            "Quarter = cumulative − the previous quarter's cumulative (same table kind only). "
                            "Quarterly segment data is collected for the last two fiscal years."))
        if any(c["gross"] for c in sv["columns"]):
            lines.append(tr("* 내부거래 제거 전 총액 기준(나머지는 외부 매출). 기준이 다른 해끼리는 증감률을 계산하지 않음.",
                            "* gross, before intersegment eliminations (others: external revenue). No change is "
                            "computed between years on different bases."))
        for kind, u in sv["notes"]:
            if kind == "map":
                lines.append(tr(f"묶음: '{u['segment_raw']}' → {u['segment_code']}",
                                f"Mapped: '{u['segment_raw']}' → {u['segment_code']}"))
            else:
                years = ",".join(map(str, u["years"]))
                lines.append(tr(f"이어지지 않는 부문명: '{u['segment_raw']}' ({years}) — segment_map에서 묶을 수 있음",
                                f"Name not continued: '{u['segment_raw']}' ({years}) — can be mapped in segment_map"))
        return "\n".join(lines[:8])

