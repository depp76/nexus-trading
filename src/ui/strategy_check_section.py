"""ui/strategy_check_section.py — the MA chart dialog's "전략 점검" block
(user direction 2026-11-16): pick a strategy variant in the combo and see
this stock's conditions under it, whether or not the Trading Universe has a
"전략 신호" run pinned.

Everything comes through strategy.signal_api and is drawn from its generic
``StockCheck``, so a newly registered strategy shows up here without touching
this file:
  * the combo lists the variants that apply to the stock's market;
  * a variant that needs the whole universe (a rank within it; trend
    following C1-C3) reads a universe run of that variant from the shared
    ``SignalCache`` -- the Universe tab's own run when there is one -- and
    the block's "계산" button runs one on the Universe rows otherwise;
  * any other variant is checked for this stock alone in a worker
    (strategy_threads.StrategyCheckThread), once per dialog and variant.

Layout, top to bottom: title + combo, as-of caption, verdict (PROFIT for
entry/hold, LOSS for exit), the score line and metric cards when the check
has them, the entry conditions (Pass / Fail badges), the exit conditions
(Hit / None badges), a note, and the "계산" row when a universe run is missing.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from i18n import tr
from strategy import signal_api
from threads.strategy_threads import CANCELLED_MESSAGE, SignalPicksThread, StrategyCheckThread
from ui.colors import LOSS, PROFIT
from ui.common import (
    FONT_BODY, FONT_CAPTION, FONT_HEADING, FONT_KPI, KpiCard, create_font, make_badge, make_button,
)


@dataclass
class SignalContext:
    """What the dialog's opener shares with the block."""
    items: list[dict] = field(default_factory=list)    # the Universe rows (a universe run's input)
    cache: signal_api.SignalCache = field(default_factory=signal_api.SignalCache)   # shared with the Universe tab
    default_variant: str = ""                          # the pinned "전략 신호" row; "" -> signal_api.default_variant()
    track: Callable | None = None                      # the owner's _track_thread (shutdown bookkeeping)


def has_section(market: str) -> bool:
    """True when some registered strategy applies to `market`."""
    return bool(signal_api.variants(market))


def _caption(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setFont(create_font(FONT_CAPTION))
    return lbl


class StrategyCheckSection(QWidget):
    def __init__(self, ticker: str, name: str, market: str, ctx: SignalContext | None = None, parent=None):
        super().__init__(parent)
        self._item = {"ticker": ticker, "name": name, "market": market}
        self._ticker = ticker
        self._ctx = ctx or SignalContext()
        self._checks: dict[str, object] = {}     # variant id -> StockCheck (this dialog's per-stock runs)
        self._errors: dict[str, str] = {}
        self._running: set[str] = set()          # variants with a universe run in flight from here
        self._threads: list = []                 # workers kept referenced while they run
        self.check = None                        # the StockCheck on screen
        self.calc_btn = None
        self.status_lbl = None

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_lbl = QLabel(tr("전략 점검", "Strategy Check"))
        self.title_lbl.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        head.addWidget(self.title_lbl)
        self.combo = QComboBox()
        for vid in signal_api.variants(market):
            self.combo.addItem(f"{vid}  {signal_api.title(vid)}", vid)
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(14)
        self.combo.view().setMinimumWidth(self.combo.view().sizeHintForColumn(0) + 24)
        self.combo.setToolTip(tr("이 종목을 점검할 매매전략", "The strategy to check this stock against"))
        head.addWidget(self.combo, 1)
        box.addLayout(head)
        self.asof_lbl = _caption("")
        box.addWidget(self.asof_lbl)

        self.body = QWidget()
        self._body_box = QVBoxLayout(self.body)
        self._body_box.setContentsMargins(0, 0, 0, 0)
        self._body_box.setSpacing(4)
        box.addWidget(self.body)

        start = self._ctx.default_variant or signal_api.default_variant()
        self.combo.setCurrentIndex(max(self.combo.findData(start), 0))
        self.combo.currentIndexChanged.connect(self._on_variant_changed)
        self._show()

    # ------------------------------------------------------------------ state
    def variant(self) -> str:
        return self.combo.currentData()

    def _on_variant_changed(self, _index: int) -> None:
        self._show()

    def _clear(self, lay) -> None:
        while lay.count():
            it = lay.takeAt(0)
            if it.widget() is not None:
                # hide + deleteLater, not setParent(None): the "계산" button
                # clears the body from its own clicked signal, and an
                # unparented button would be freed mid-emission.
                it.widget().hide()
                it.widget().deleteLater()
            elif it.layout() is not None:
                self._clear(it.layout())

    def _track(self, thread) -> None:
        if self._ctx.track is not None:
            self._ctx.track(thread)
        self._threads = [t for t in self._threads if t.isRunning()] + [thread]

    # ------------------------------------------------------------------ views
    def _show(self) -> None:
        self._clear(self._body_box)
        self.check, self.calc_btn, self.status_lbl = None, None, None
        self.badges: dict[str, QLabel] = {}
        self.cards: dict[str, KpiCard] = {}
        vid = self.variant()
        if vid is None:
            return
        if signal_api.needs_universe(vid):
            picks = self._ctx.cache.get(vid)
            self._render(signal_api.check_from_picks(vid, self._ticker, picks))
            if picks is None:
                self._add_calc_row(vid)
            return
        check = self._checks.get(vid)
        if check is not None:
            self._render(check)
            return
        self.asof_lbl.setText("")
        msg = self._errors.get(vid)
        self.status_lbl = _caption(msg or tr("계산 중...", "Computing..."))
        self.status_lbl.setWordWrap(True)
        self._body_box.addWidget(self.status_lbl)
        if msg is None:
            self._run_stock(vid)

    def _render(self, check) -> None:
        self.check = check
        self.asof_lbl.setText(tr(f"{check.as_of:%Y-%m-%d} 기준", f"As of {check.as_of:%Y-%m-%d}")
                              if check.as_of is not None else "")
        color = {"entry": PROFIT, "hold": PROFIT, "exit": LOSS}.get(check.verdict)
        self.verdict_lbl = QLabel(check.verdict_text)
        self.verdict_lbl.setFont(create_font(FONT_HEADING, QFont.Weight.Bold))
        if color:
            self.verdict_lbl.setStyleSheet(f"color:{color};")
        self.verdict_lbl.setWordWrap(True)
        self._body_box.addWidget(self.verdict_lbl)

        self.score_lbl = None
        if check.score:
            line = QHBoxLayout()
            line.setSpacing(8)
            line.addWidget(_caption(check.score_caption), 0, Qt.AlignmentFlag.AlignVCenter)
            self.score_lbl = QLabel(check.score)
            self.score_lbl.setFont(create_font(FONT_KPI, style_name="Semilight"))
            line.addWidget(self.score_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
            self.score_hint_lbl = QLabel(check.score_hint)
            self.score_hint_lbl.setObjectName("muted")
            self.score_hint_lbl.setFont(create_font(FONT_BODY))
            line.addWidget(self.score_hint_lbl, 1, Qt.AlignmentFlag.AlignVCenter)
            self._body_box.addLayout(line)
        if check.metrics:
            grid = QGridLayout()
            grid.setSpacing(6)
            for i, m in enumerate(check.metrics):
                card = KpiCard(m.label, m.help, value_pt=FONT_HEADING)
                card.set(m.value)
                grid.addWidget(card, 0, i)
                self.cards[m.label] = card
            self._body_box.addLayout(grid)

        for heading, rows, is_exit in ((tr("진입 조건", "Entry"), check.entry, False),
                                       (tr("청산 조건", "Exit"), check.exit, True)):
            if not rows:
                continue
            self._body_box.addWidget(_caption(heading))
            for r in rows:
                self._add_row(r, is_exit)
        if check.note:
            note = _caption(check.note)
            note.setWordWrap(True)
            self._body_box.addWidget(note)

    def _add_row(self, r, is_exit: bool) -> None:
        line = QHBoxLayout()
        name = QLabel(r.label)
        name.setFont(create_font(FONT_BODY))
        name.setWordWrap(True)
        line.addWidget(name, 1)
        if r.value:
            line.addWidget(_caption(r.value), 0, Qt.AlignmentFlag.AlignRight)
        badge = None
        if r.ok is None:
            if not r.value:
                badge = make_badge("na", "-")
        elif is_exit:
            badge = make_badge("fail" if r.ok else "na", tr("발생", "Hit") if r.ok else tr("없음", "None"))
        else:
            badge = make_badge("ok" if r.ok else "fail", tr("통과", "Pass") if r.ok else tr("미달", "Fail"))
        if badge is not None:
            line.addWidget(badge, 0, Qt.AlignmentFlag.AlignRight)
            self.badges[r.label] = badge
        self._body_box.addLayout(line)

    def _add_calc_row(self, vid: str) -> None:
        rows = signal_api.universe_rows(self._ctx.items, vid)
        row = QHBoxLayout()
        self.calc_btn = make_button(
            tr("계산", "Compute"), on_click=self._on_calc_clicked,
            tooltip=tr(f"이 전략을 Trading Universe의 {'/'.join(signal_api.markets(vid))} 종목 전체로 계산합니다 "
                       "(점수가 유니버스 안 순위라 전체 계산이 필요)",
                       f"Compute this strategy over the Universe's {'/'.join(signal_api.markets(vid))} rows "
                       "(the scores are ranks within the universe)"))
        running = vid in self._running
        self.calc_btn.setEnabled(bool(rows) and not running)
        row.addWidget(self.calc_btn)
        if running:
            text = tr(f"{len(rows)}종목 계산 중...", f"Computing {len(rows)} names...")
        else:
            text = self._errors.get(vid) or (tr("유니버스 계산이 필요합니다", "Needs a universe run") if rows
                                             else tr("Trading Universe를 먼저 새로고침하세요", "Refresh Trading Universe first"))
        self.status_lbl = _caption(text)
        self.status_lbl.setWordWrap(True)
        row.addWidget(self.status_lbl, 1)
        self._body_box.addLayout(row)

    # ------------------------------------------------------------------ runs
    def _run_stock(self, vid: str) -> None:
        thread = StrategyCheckThread(self._item, vid, self._ctx.cache.get(vid))
        thread.finished.connect(self._on_check_done)
        self._track(thread)
        thread.start()

    def _on_check_done(self, check, error: str, vid: str) -> None:
        if check is None:
            self._errors[vid] = "" if error == CANCELLED_MESSAGE else tr(f"계산 실패: {error}", f"Failed: {error}")
        else:
            self._checks[vid] = check
        if vid == self.variant():
            self._show()

    def _on_calc_clicked(self) -> None:
        vid = self.variant()
        if not signal_api.universe_rows(self._ctx.items, vid) or vid in self._running:
            return
        self._running.add(vid)
        self._errors.pop(vid, None)
        thread = SignalPicksThread(self._ctx.items, vid, n_top=10)
        thread.progress.connect(self._on_universe_progress)
        thread.finished.connect(self._on_universe_done)
        self._track(thread)
        self._show()
        thread.start()

    def _on_universe_progress(self, text: str) -> None:
        if self.status_lbl is not None and self.calc_btn is not None:
            try:
                self.status_lbl.setText(text)
            except RuntimeError:
                pass        # the label was replaced (another variant picked)

    def _on_universe_done(self, picks, error: str) -> None:
        thread = self.sender()
        vid = getattr(thread, "variant_id", None) or (picks.variant_id if picks is not None else self.variant())
        self._running.discard(vid)
        if picks is None:
            self._errors[vid] = "" if error == CANCELLED_MESSAGE else tr(f"계산 실패: {error}", f"Failed: {error}")
        else:
            self._ctx.cache.put(picks)        # the Universe tab sees it too
            self._checks.pop(vid, None)       # a B check may now show its rank
        if vid == self.variant():
            self._show()
