"""ui/strategy_check_section.py — the MA chart dialog's "전략 점검" block
(user direction 2026-11-16): pick a strategy variant in the menu and see
this stock's conditions under it, whether or not the Trading Universe has a
"전략 신호" run pinned.

Everything comes through strategy.signal_api and is drawn from its generic
``StockCheck``, so a newly registered strategy shows up here without touching
this file:
  * the strategy button's menu has a check box per variant that applies to
    the stock's market, exactly one ticked (user direction 2026-11-16: the
    same check-box menu as the Universe "전략 신호" button); the button shows
    the ticked one;
  * a variant that needs the whole universe (a rank within it; trend
    following C1-C3) reads a universe run of that variant from the shared
    ``SignalCache`` -- the Universe tab's own run when there is one -- and
    the block's "계산" button runs one on the Universe rows otherwise;
  * any other variant is checked for this stock alone in a worker
    (strategy_threads.StrategyCheckThread), once per dialog and variant.

Layout, top to bottom: title + strategy button, as-of caption, verdict (PROFIT for
entry/hold, LOSS for exit), the score line and metric cards when the check
has them, the entry conditions (Pass / Fail badges), the exit conditions
(Hit / None badges), a note, and the "계산" row when a universe run is missing.

One height whatever is picked (user direction 2026-11-16, docs/ui.md 8.22):
the body is fixed to the tallest of every offered variant's layout templates
(``signal_api.templates``: all rows, widest values, longest verdict) at the
body's current width, and its content sits at the top. Only a width change
(dialog resize, splitter move) measures again.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QGridLayout, QHBoxLayout, QLabel, QMenu, QSizePolicy, QVBoxLayout, QWidget, QWidgetAction,
)

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
        self.verdict_lbl = None
        self.score_lbl = None
        self.score_hint_lbl = None
        self.badges: dict[str, QLabel] = {}
        self.cards: dict[str, KpiCard] = {}

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        self.title_lbl = QLabel(tr("전략 점검", "Strategy Check"))
        self.title_lbl.setFont(create_font(FONT_BODY, QFont.Weight.Bold))
        head.addWidget(self.title_lbl)
        self._variants = signal_api.variants(market)
        self._variant = None
        self.variant_btn = make_button("", tooltip=tr("이 종목을 점검할 매매전략", "The strategy to check this stock against"))
        self.variant_btn.setMinimumWidth(1)          # the label is elided by the button, not the panel width
        self.variant_menu = self._build_menu()
        self.variant_btn.setMenu(self.variant_menu)
        head.addWidget(self.variant_btn, 1)
        box.addLayout(head)
        self.asof_lbl = _caption("")
        # One height empty or not (an empty label's height came from the
        # unpolished font, a dated one from the QSS caption font).
        self.asof_lbl.setText("2026-11-16 기준")
        self.asof_lbl.ensurePolished()
        self.asof_lbl.setFixedHeight(max(self.asof_lbl.sizeHint().height(), self.asof_lbl.fontMetrics().height()))
        self.asof_lbl.setText("")
        box.addWidget(self.asof_lbl)

        self.body = QWidget()
        self._body_box = self._new_body_box(self.body)
        box.addWidget(self.body)
        self._heights: dict[int, int] = {}       # body width -> the tallest template height
        self._fit_width = -1
        self.body.setMinimumWidth(self.template_min_width())     # one width whatever is picked

        start = self._ctx.default_variant or signal_api.default_variant()
        self._select(start if start in self._variants else (self._variants[0] if self._variants else None))
        # Only what it needs: the space below the investor table goes to the
        # table, and this block sits right above the "기업 정보" button.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._show()

    # ------------------------------------------------------------------ state
    def variant(self) -> str | None:
        return self._variant

    def _build_menu(self) -> QMenu:
        """A check box per variant, like the Universe "전략 신호" menu."""
        menu = QMenu(self)
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(12, 8, 16, 8)
        col.setSpacing(6)
        self.variant_checks: dict[str, QCheckBox] = {}
        for vid in self._variants:
            cb = QCheckBox(f"{vid}  {signal_api.title(vid)}")
            cb.clicked.connect(lambda _c, v=vid: self._on_variant_ticked(v))
            col.addWidget(cb)
            self.variant_checks[vid] = cb
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        return menu

    def _select(self, vid) -> None:
        """Tick `vid` alone and show it on the button."""
        self._variant = vid
        for v, cb in self.variant_checks.items():
            cb.setChecked(v == vid)
        self.variant_btn.setText(f"{vid}  {signal_api.title(vid)}" if vid else "-")

    def set_variant(self, vid: str) -> None:
        """Pick `vid` (as ticking its box does)."""
        if vid not in self.variant_checks or vid == self._variant:
            return
        self._select(vid)
        self._show()

    def _on_variant_ticked(self, vid: str) -> None:
        self.variant_menu.close()
        if vid == self._variant:
            self._select(vid)        # ticking the current one again keeps it ticked
            return
        self.set_variant(vid)

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

    @staticmethod
    def _new_body_box(w: QWidget) -> QVBoxLayout:
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        return lay

    # ------------------------------------------------------------------ height
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_height()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._fit_height()

    def _fit_height(self) -> None:
        """Fix the body to the tallest template at its current width."""
        width = self.body.width() if self.body.width() > 0 else self.width()
        if width <= 0 or width == self._fit_width:
            return
        self._fit_width = width
        h = self._heights.get(width)
        if h is None:
            h = self._heights[width] = self.template_height(width)
        # Compare the constraint, not the current height: the body can be
        # exactly h tall by chance (a C row) without being fixed to it.
        if h > 0 and (self.body.minimumHeight() != h or self.body.maximumHeight() != h):
            self.body.setFixedHeight(h)

    def template_height(self, width: int) -> int:
        """Height the body needs at `width` for the largest layout of any
        variant in the menu."""
        return self._measure(width)[1]

    def template_min_width(self) -> int:
        """The widest minimum width of any variant's layout (one-line labels
        such as the score hint), so the panel does not widen per strategy."""
        return self._measure(0)[0]

    def _measure(self, width: int) -> tuple[int, int]:
        """(minimum width, height at `width`) over every template of every
        variant in the menu, drawn into a probe widget with the same code
        that draws a real check."""
        saved = (self._body_box, self.check, self.calc_btn, self.status_lbl, self.badges, self.cards,
                 self.verdict_lbl, self.score_lbl, self.score_hint_lbl, self.asof_lbl.text())
        probe = QWidget()
        lay = self._new_body_box(probe)
        best = min_w = 0
        try:
            for vid in self._variants:
                for t in signal_api.templates(vid):
                    self._body_box, self.badges, self.cards = lay, {}, {}
                    self._render(t)
                    if signal_api.needs_universe(vid):
                        self._add_calc_row(vid)
                    probe.ensurePolished()
                    lay.activate()
                    min_w = max(min_w, lay.totalMinimumSize().width())
                    if width > 0:
                        h = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else lay.totalSizeHint().height()
                        best = max(best, h)
                    self._clear(lay)
        finally:
            (self._body_box, self.check, self.calc_btn, self.status_lbl, self.badges, self.cards,
             self.verdict_lbl, self.score_lbl, self.score_hint_lbl, asof) = saved
            self.asof_lbl.setText(asof)
            probe.deleteLater()
        return min_w, best

    # ------------------------------------------------------------------ views
    def _show(self) -> None:
        self._fill()
        self._body_box.addStretch(1)            # content at the top of the fixed-height body

    def _fill(self) -> None:
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

        self.score_lbl = self.score_hint_lbl = None
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
