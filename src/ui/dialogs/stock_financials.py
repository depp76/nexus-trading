"""The MA chart's 재무 tab (docs/dart_db_plan_2026-11-26.md 13, stage 5;
docs/ui.md 8.34): DART quarterly results and revenue by segment of one KOSPI /
KOSDAQ stock, laid out as the approved mockup
(docs/ui_mockups/dart_financials_mockup.html):

    [기준 2026 3분기보고서 · 접수 … · 연결] [기간 …] [단위 억원]   [분기|연간]   DART 11-26 19:42
    [매출] [영업이익 (이익률)] [지배주주순이익] [최근 4분기 합계 매출]
    [분기 매출 · 영업이익률 chart]          [분기 실적 table]
    [사업부별 매출 비중 chart]              [사업부별 매출 · 증감 table + notes]
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
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from i18n import tr
from threads.dart_threads import FinancialsLoadThread
from ui import dart_financials_model as fm
from ui.colors import LOSS, OK, PROFIT, SERIES_PALETTE, WARN
from ui.common import FONT_BODY, FONT_CAPTION, FONT_HEADING, KpiCard, create_font, setup_table
from ui.theme import ACCENT, INK_ON_LIGHT, LINE, SURFACE, TEXT, TEXT_EMPTY, TEXT_MUTED

logger = logging.getLogger(__name__)

BAR_COLOR = SERIES_PALETTE[6]          # revenue bars: a calm blue, not the PROFIT "gain" color
SEGMENT_COLORS = SERIES_PALETTE
_CHECK_KIND = {"ok": OK, "sum_mismatch": WARN, "review": TEXT_EMPTY, "na": TEXT_EMPTY}


def _pct(v, digits: int = 1) -> str:
    return "—" if v is None else f"{v * 100:+.{digits}f}%"


def _color(v) -> str:
    if v is None:
        return TEXT_EMPTY
    return PROFIT if v > 0 else LOSS if v < 0 else TEXT_MUTED


class StockFinancialsTab(QWidget):
    """The tab; ``ensure_loaded()`` starts the read the first time it is shown."""

    def __init__(self, ticker: str, name: str, market: str, parent=None):
        super().__init__(parent)
        self.ticker, self.name, self.market = ticker, name, market
        self._thread: FinancialsLoadThread | None = None
        self._data: dict | None = None
        self._mode = "q"                      # "q" quarterly | "y" annual
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

        row1 = QHBoxLayout()
        row1.setSpacing(10)
        self._chart_card, self._chart_title, chart_box = self._card(tr("분기 매출 · 영업이익률", "Quarterly revenue · margin"))
        self._fig = Figure(figsize=(6.4, 2.8), linewidth=0)
        self._canvas = FigureCanvas(self._fig)
        self._canvas.setStyleSheet("border: none;")
        self._canvas.setMinimumHeight(260)
        chart_box.addWidget(self._canvas, 1)
        self._table_card, self._table_title, table_box = self._card(tr("분기 실적", "Quarterly results"))
        self._table = QTableWidget()
        setup_table(self._table)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setMinimumHeight(260)
        table_box.addWidget(self._table, 1)
        row1.addWidget(self._chart_card, 5)
        row1.addWidget(self._table_card, 4)
        col.addLayout(row1)

        col.addLayout(self._build_segments())
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
            row.addWidget(btn)
        last = self._data.get("last_run")
        try:
            when = _dt.datetime.fromisoformat(last).strftime("%m-%d %H:%M") if last else "-"
        except ValueError:
            when = "-"
        sync = self._chip(f"DART {when}")
        sync.setObjectName("faint")
        row.addSpacing(10)
        row.addWidget(sync)
        return row

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
            self._chart_title.setText(tr("분기 매출 · 영업이익률 (최근 5년, 4분기는 빗금 = 계산값)",
                                         "Quarterly revenue · operating margin (5 years, hatched Q4 = derived)"))
            self._table_title.setText(tr("분기 실적 (최신 정정본 기준)", "Quarterly results (latest corrections)"))
        else:
            rows = fm.annual_rows(self._quarters)
            self._chart_title.setText(tr("연간 매출 · 영업이익률 (진행 중인 해는 누적, 빗금)",
                                         "Annual revenue · operating margin (year in progress: cumulative, hatched)"))
            self._table_title.setText(tr("연간 실적", "Annual results"))
        self._draw_results_chart(rows)
        self._fill_results_table(rows)

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
            self._chart_title.setText(self._chart_title.text() + tr(" · 통화가 다른 기간 제외", " · other-currency periods left out"))
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
            ax.set_xticklabels([str(r["fiscal_year"]) if r["fiscal_q"] == 1 else "" for r in rows])
            for x, r in zip(xs, rows):
                if r["fiscal_q"] == 1 and x:
                    ax.axvline(x - 0.5, color=TEXT_EMPTY, linewidth=0.6, alpha=0.5)
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
        fig.tight_layout()
        self._canvas.draw_idle()

    def _fill_results_table(self, rows) -> None:
        heads = [tr("분기", "Quarter") if self._mode == "q" else tr("연도", "Year"), tr("매출", "Revenue"), "YoY",
                 tr("영업이익", "Op. income"), tr("이익률", "Margin"),
                 tr("지배순이익", "Owners' NI") if any(r.get("net_is_owners") for r in self._quarters)
                 else tr("순이익", "Net income"), tr("비고", "Notes"), tr("접수일", "Received")]
        t = self._table
        t.setUpdatesEnabled(False)
        try:
            t.clear()
            t.setColumnCount(len(heads))
            t.setHorizontalHeaderLabels(heads)
            t.setRowCount(len(rows))
            base_currency = (rows[-1]["currency"] or "KRW") if rows else "KRW"
            for i, r in enumerate(reversed(rows)):
                div, unit_ko, unit_en = fm.unit_of(r["currency"])       # each row in its own unit
                label = self._period_label(r) + ("†" if r.get("derived") else "") + ("‡" if r.get("lookahead") else "")
                cells = [
                    (label, None, Qt.AlignmentFlag.AlignCenter),
                    (self._amt(r["revenue"], div), None, None),
                    (_pct(r["revenue_yoy"], 0), _color(r["revenue_yoy"]), None),
                    (self._amt(r["op_income"], div), LOSS if (r["op_income"] or 0) < 0 else None, None),
                    ("—" if r["op_margin"] is None else f"{r['op_margin'] * 100:.1f}%",
                     LOSS if (r["op_margin"] or 0) < 0 else None, None),
                    (self._amt(r["net_income"], div), LOSS if (r["net_income"] or 0) < 0 else None, None),
                    self._note(r, tr(f"단위 {unit_ko}", f"unit {unit_en}")
                               if (r["currency"] or "KRW") != base_currency else ""),
                    (r["rcept_dt"], TEXT_MUTED, Qt.AlignmentFlag.AlignCenter),
                ]
                for j, (text, color, align) in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(int(align or (Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)))
                    if color:
                        item.setForeground(QColor(color))
                    t.setItem(i, j, item)
            hh = t.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        finally:
            t.setUpdatesEnabled(True)

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

    def _build_segments(self) -> QHBoxLayout:
        sv = fm.segment_view(self._data)
        self.segment_data = sv
        row = QHBoxLayout()
        row.setSpacing(10)
        chart_card, _t1, chart_box = self._card(tr("사업부별 매출 비중 (연간, 진행 중인 해는 최신 분기)",
                                                   "Revenue share by segment (annual; the year in progress: "
                                                   "latest quarter)"))
        table_card, _t2, table_box = self._card(tr("사업부별 매출 · 증감", "Revenue by segment · change"))
        row.addWidget(chart_card, 4)
        row.addWidget(table_card, 5)
        if not sv["columns"]:
            for box in (chart_box, table_box):
                lbl = QLabel(tr("사업부별 매출이 아직 없습니다 (tools/dart_sync.py --segments).",
                                "No segment revenue yet (tools/dart_sync.py --segments)."))
                lbl.setObjectName("muted")
                lbl.setWordWrap(True)
                box.addWidget(lbl)
            return row
        self._seg_fig = Figure(figsize=(4.8, 2.6), linewidth=0)
        self._seg_canvas = FigureCanvas(self._seg_fig)
        self._seg_canvas.setStyleSheet("border: none;")
        self._seg_canvas.setMinimumHeight(240)
        chart_box.addWidget(self._seg_canvas, 1)
        self._draw_segment_chart(sv)
        self.segment_table = QTableWidget()
        setup_table(self.segment_table, row_height=36)
        self.segment_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table_box.addWidget(self.segment_table, 1)
        self._fill_segment_table(sv)
        notes = self._segment_notes(sv)
        if notes:
            lbl = QLabel(notes)
            lbl.setObjectName("faint")
            lbl.setWordWrap(True)
            lbl.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
            table_box.addWidget(lbl)
        return row

    def _draw_segment_chart(self, sv) -> None:
        fig = self._seg_fig
        fig.clear()
        ax = fig.add_subplot(111)
        cols = sv["columns"]
        xs = list(range(len(cols)))
        bottoms = [0.0] * len(cols)
        for i, s in enumerate(sv["segments"]):
            color = SEGMENT_COLORS[i % len(SEGMENT_COLORS)]
            shares = [(v or 0) * 100 for v in s["shares"]]
            ax.bar(xs, shares, bottom=bottoms, width=0.64, color=color, edgecolor=SURFACE, linewidth=0.6,
                   label=s["name"])
            for x, b, v in zip(xs, bottoms, shares):
                if v >= 9:
                    ax.text(x, b + v / 2, f"{v:.0f}%", ha="center", va="center", fontsize=FONT_CAPTION,
                            color=INK_ON_LIGHT)
            bottoms = [b + v for b, v in zip(bottoms, shares)]
        ax.set_xticks(xs)
        ax.set_xticklabels([c["label"] for c in cols])
        ax.set_ylim(0, 100)
        ax.set_ylabel("%", color=TEXT_MUTED)
        ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), fontsize=FONT_CAPTION, frameon=False)
        fig.tight_layout()
        self._seg_canvas.draw_idle()

    def _fill_segment_table(self, sv) -> None:
        cols, segs = sv["columns"], sv["segments"]
        div, _k, _e = fm.unit_of(sv["currency"])
        t = self.segment_table
        t.setUpdatesEnabled(False)
        try:
            t.setColumnCount(len(cols) + 2)
            t.setHorizontalHeaderLabels([tr("부문", "Segment"), *[c["label"] + ("*" if c["gross"] else "")
                                                                for c in cols], tr("연평균", "CAGR")])
            t.setRowCount(len(segs) + 1)
            for i, s in enumerate(segs):
                name = QTableWidgetItem(s["name"])
                name.setForeground(QColor(SEGMENT_COLORS[i % len(SEGMENT_COLORS)]))
                t.setItem(i, 0, name)
                for j, (v, g) in enumerate(zip(s["values"], s["yoy"])):
                    text = "—" if v is None else f"{v / div:,.0f}"
                    if g is not None:
                        text += f"\n{_pct(g, 0)}"
                    item = QTableWidgetItem(text)
                    item.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                    if g is not None:
                        item.setForeground(QColor(_color(g)))
                    t.setItem(i, j + 1, item)
                cagr = QTableWidgetItem(_pct(s["cagr"]))
                cagr.setTextAlignment(int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
                cagr.setForeground(QColor(_color(s["cagr"])))
                t.setItem(i, len(cols) + 1, cagr)
            check = len(segs)
            head = QTableWidgetItem(tr("검산", "Check"))
            head.setForeground(QColor(TEXT_MUTED))
            t.setItem(check, 0, head)
            for j, c in enumerate(cols):
                label = tr(*fm.CHECK_LABELS.get(c["status"], (c["status"], c["status"])))
                if c.get("source") == "single":
                    label = tr("단일부문", "Single segment")
                item = QTableWidgetItem(label)
                item.setTextAlignment(int(Qt.AlignmentFlag.AlignCenter))
                item.setForeground(QColor(_CHECK_KIND.get(c["status"], TEXT_EMPTY)))
                t.setItem(check, j + 1, item)
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
            t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            t.setMinimumHeight(min(420, 40 + 36 * (len(segs) + 1)))
        finally:
            t.setUpdatesEnabled(True)

    @staticmethod
    def _segment_notes(sv) -> str:
        lines = [tr("검산: 부문 합계 + 조정 vs 손익계산서 매출, 차이 1% 이내 = ok.",
                    "Check: segments + adjustments vs income-statement revenue, within 1 % = ok.")]
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

