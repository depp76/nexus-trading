"""ui/history_tab.py — TradingHistoryTab (Phase 4 split)

Split out from: main.py (2026-08-29 feat/3-1-modularize, Phase 4)
Contains:
  TradingHistoryTab
"""
from i18n import tr
import logging
import sqlite3
import datetime as _dt

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QTableWidget, QLineEdit, QHeaderView, QPushButton, QMessageBox, QDialog, QInputDialog, QMenu,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QSettings, QTimer
from PyQt6.QtGui import QKeySequence, QShortcut

import trade_db
from data_fetcher import is_kr_code

from threads.fetch_threads import (
    PositionPriceFetchThread,
    KisAccountThread,
    SingleStockFetchThread,
)
from threads.realtime import RealtimePriceThread
from ui.table_headers import GroupedHeaderView
from ui.dialogs import (
    BuyEditDialog,
    SellEditDialog,
    TradeEntryDialog,
    StockTradeHistoryDialog,
    KisSyncDialog,
)
from ui.kis_sync import plan_kis_import

logger = logging.getLogger(__name__)


from ui.common import (
    _fmt_num_edit, ThreadOwnerMixin, SearchLineEdit,
    BTN_H, make_button, setup_table, make_footer_label, TAB_MARGINS, TAB_SPACING,
    KpiStrip, install_table_keys, clear_selection_on_outside_click,
    fix_toolbar_widths, register_toolbar_labels,
)
from ui.colors import PROFIT, LOSS
from ui.theme import ACCENT, LINE_STRONG
from ui.history_calc import compute_pl_fields, build_monthly_rows, summarize_positions
from ui.history_table import fill_table_rows, SectionTable, SECTIONS, COLUMNS
from ui.delegates import TradeStateDelegate
from ui.dialogs.holdings_summary import show_holdings_summary


# ── Dashboard card styling / widget factories for TradingHistoryTab._build_ui ──
# docs/ui.md Phase 0 (section 6.2): inputs/combos/cards/the position-summary
# table used to carry their own hardcoded QSS here, fighting whatever the
# global stylesheet said. They now inherit ui/theme.py's QLineEdit/QComboBox/
# QFrame#DashboardCard/QTableWidget rules instead -- nothing left to set here.
_BTN_H = BTN_H   # inputs on the toolbar line up with the buttons


def _make_rw_edit(placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setPlaceholderText(placeholder)
    e.setAlignment(Qt.AlignmentFlag.AlignRight)
    e.setFixedWidth(110)
    e.setFixedHeight(_BTN_H)
    return e


def _toolbar_hbox() -> QHBoxLayout:
    """One line of the controls row: 8px spacing, no margins."""
    row = QHBoxLayout()
    row.setSpacing(8)
    row.setContentsMargins(0, 0, 0, 0)
    return row


# Message boxes are titled with the tab name (docs/ui.md 8.5).
_TITLE = "Trading History"
# KIS 조회 with nothing imported yet: how far back to ask for executions (the recent TR covers ~3 months).
_KIS_FIRST_LOOKBACK_DAYS = 89
# Toolbar cycle buttons (user direction 2026-11-16, like the Universe
# 종목(전체) button): (value, label) in click order, the first is the default
# and shows unchecked. The values stay what the filter code compares.
_PERIOD_STATES = (("All", tr("기간(전체)", "Period (All)")), ("1M", tr("기간(1개월)", "Period (1M)")),
                  ("3M", tr("기간(3개월)", "Period (3M)")), ("YTD", tr("기간(올해)", "Period (YTD)")))
_GROUP_STATES = (("status", tr("보기(상태별)", "View (Status)")), ("month", tr("보기(월별)", "View (Month)")))
# Short labels (2026-11-16): "시세(30일 숨김)" (110px) set the toolbar width
# shared by all three tabs and pushed the Universe toolbar past 1440px; the
# tooltip spells the 30-day rule out.
_STALE_STATES = ((True, tr("시세(30일)", "Prices (30d)")), (False, tr("시세(전체)", "Prices (All)")))
_GROUP_GAP = 12      # between toolbar groups (docs/ui.md 8.17: 12~16px)


class TradingHistoryTab(ThreadOwnerMixin, QWidget):
    """Trading History tab - load from Excel and display closed/open positions."""
    total_asset_updated = pyqtSignal(float)
    # 60 s price refresh result -> MainWindow's header refresh status (docs/ui.md 8.8)
    price_refresh_failed = pyqtSignal(str)
    price_refresh_ok = pyqtSignal()
    status_message = pyqtSignal(str)  # forwards background-thread progress text to MainWindow's status bar

    # Column headers/widths/section grouping all come from history_table.COLUMNS
    # now (docs/ui.md issue #9: this list, SECTIONS and _fit_columns's own
    # `mins` list used to be three places kept in sync by hand -- the Excel
    # header mapping this list used to mirror belongs in the importer only,
    # per that same issue's fix). Column order: Trading(3) | Buy(4) | Sell(7)
    # | Position(4) | Past(3).
    _COLS = [c.label for c in COLUMNS]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._closed_data  = []
        self._open_data    = []
        self._current_path = ""
        self._price_thread: QThread | None = None
        # The running price fetch: its id and, per input index, the record it
        # was asked for with that record's company/ticker at the time
        # (review_codex.md C10; see _on_prices_ready).
        self._price_req_id = 0
        self._price_req: list = []
        self._deposit_thread = None
        self._kis_dialog = None          # the last KIS 조회 preview (tests drive it)
        self._row_data: list = []   # (kind, rec) per visible table row
        self._settings = QSettings("PortfolioManagement", "PortfolioManagement")
        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.setInterval(400)
        self._settings_save_timer.timeout.connect(self._flush_settings)
        self._build_ui()
        self._load_settings()

        # Real-time price refresh is driven solely by MainWindow's global 60s
        # auto-update timer (UniverseTab.auto_lightweight_tick ->
        # _start_realtime_price_update), so the "Auto Update" checkbox really
        # does stop KIS/Yahoo polling. This tab used to run its own unconditional
        # QTimer on top of that (roadmap 6-1b).
        self._rt_price_thread = None

    _SETTINGS_KEYS = ("principal", "deposit", "withdrawal")
    # QSettings scope the app shipped with before roadmap 6-2f; read once so the
    # user does not lose the saved principal/deposit/withdrawal on upgrade.
    _LEGACY_SETTINGS_SCOPE = ("MyCompany", "PortfolioManager")

    def _migrate_legacy_settings(self):
        if any(self._settings.value(f"trading_history/{k}", "") for k in self._SETTINGS_KEYS):
            return
        legacy = QSettings(*self._LEGACY_SETTINGS_SCOPE)
        migrated = False
        for k in self._SETTINGS_KEYS:
            v = legacy.value(f"trading_history/{k}", "")
            if v:
                self._settings.setValue(f"trading_history/{k}", v)
                migrated = True
        if migrated:
            logger.info("Migrated Trading History settings from legacy QSettings scope")

    def _load_settings(self):
        self._migrate_legacy_settings()
        principal   = self._settings.value("trading_history/principal", "")
        deposit     = self._settings.value("trading_history/deposit", "")
        withdrawal  = self._settings.value("trading_history/withdrawal", "")
        if principal:
            self._principal_edit.setText(str(principal))
            _fmt_num_edit(self._principal_edit, str(principal))
        if deposit:
            self._deposit_edit.setText(str(deposit))
            _fmt_num_edit(self._deposit_edit, str(deposit))
        if withdrawal:
            self._withdrawal_edit.setText(str(withdrawal))
            _fmt_num_edit(self._withdrawal_edit, str(withdrawal))

    # ---UI construction ---
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(TAB_SPACING)
        root.setContentsMargins(*TAB_MARGINS)

        # docs/ui.md 8.4: toolbar -> KPI strip -> table -> footer, the same
        # order as every tab.
        root.addLayout(self._build_controls_row())
        root.addWidget(self._build_kpi_strip(), 0)
        root.addWidget(self._build_history_table(), 1)   # stretch=1: fills the rest
        # Footer: the source of the loaded data (was a caption at the end of
        # the toolbar).
        self._path_label = make_footer_label()
        root.addWidget(self._path_label)

    def _build_kpi_strip(self) -> KpiStrip:
        """Read-only KPI strip (docs/ui.md 3.6, issue #7): Total Asset/Total
        P/L/Total P/L(%)/Total Invest render as plain KPI text now instead
        of QLineEdit(readOnly) fields styled like inputs you could type
        into. Principal/Deposit/Withdrawal are the tab's only input-able
        values (issue #7's fix: "입력 가능한 항목만 실제 필드로 남긴다"), so
        each keeps a real editable field, just styled to sit in the strip."""
        # The cell layout is ui.common.KpiStrip, shared with Total Assets
        # (docs/ui.md 8.6).
        card = KpiStrip()
        self._kpi_labels = card.cells  # key -> (value QLabel | None, sub QLabel)
        cell = card.add

        # Headline figures first at the KPI size, then the supporting ones
        # (and the three inputs) at body size after a divider (docs/ui.md
        # 8.16): every value and input stays in the strip.
        # 총자산 = 평가액 + 예수금 + 출금 (user direction 2026-11-16): what the
        # principal has grown into so far, withdrawals included, so the 원금
        # input sits right next to it. The former "누적 자산" cell showed the
        # same figure and is gone; the live NAV without withdrawals is no
        # longer a cell of its own.
        # No explanation lines under the figures (user direction 2026-11-16):
        # the formulas are on the tooltips; only figures stay under a value
        # (원금의 n배, 투자금의 n%).
        cell("total_asset", tr("총자산", "Total Assets"))
        self._principal_edit = self._make_money_input(tr("예: 350,000,000", "e.g. 350,000,000"))
        cell("principal", tr("원금", "Principal"), "", self._principal_edit)
        # Re-deposits (user decision 2026-11-16): money taken out and put back
        # into the account lowers 출금 (net withdrawal) and lands in 예수금, so
        # 총자산 does not jump on the day it comes back; only money beyond
        # everything withdrawn so far is new principal.
        self._principal_edit.setToolTip(tr("처음 넣은 돈. 출금했던 돈을 다시 넣으면 원금이 아니라 출금에서 빼고, "
                                           "출금 누계보다 많이 넣은 초과분만 원금에 더합니다.",
                                           "The money first put in. Money taken out and put back lowers the "
                                           "withdrawal instead; only a deposit beyond all withdrawals so far "
                                           "adds to the principal."))
        # Left-aligned like the figures around them (all three inputs).
        for edit in (self._principal_edit,):
            edit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        cell("total_pl", tr("총 손익", "Total P/L"))
        cell("total_pl_pct", tr("총 손익 (%)", "Total P/L (%)"))
        card.add_divider()
        cell("cost_basis", tr("매입 원가", "Cost Basis"))
        # Open positions at the current price and their P/L against the cash
        # they were bought from (user direction 2026-11-16, ui.history_calc).
        cell("eval_total", tr("평가액", "Valuation"))
        cell("pos_pl", tr("평가 손익", "Position P/L"))
        self._deposit_edit = self._make_money_input(tr("예: 10,000,000", "e.g. 10,000,000"))
        cell("deposit", tr("예수금", "Deposit"), "", self._deposit_edit)
        self._withdrawal_edit = self._make_money_input(tr("예: 5,000,000", "e.g. 5,000,000"))
        cell("withdrawal", tr("출금", "Withdrawal"), "", self._withdrawal_edit)
        self._withdrawal_edit.setToolTip(tr("순출금: 뺀 돈 − 다시 넣은 돈(다시 넣은 돈은 예수금으로)",
                                            "Net withdrawal: money taken out − money put back (which goes to the deposit)"))

        # Figures only, all at the KPI size (user direction 2026-11-16): no
        # line under any value, so the empty sub-labels take no height.
        for _val, sub_lbl in self._kpi_labels.values():
            sub_lbl.hide()
        # The inputs as tall as a figure label, so their numbers sit on the same
        # line as the figures beside them (a 28 px field rode ~5 px higher).
        figure_h = self._kpi_labels["total_asset"][0].sizeHint().height()
        for edit in (self._principal_edit, self._deposit_edit, self._withdrawal_edit):
            edit.setFixedHeight(figure_h)
            edit.setTextMargins(0, 0, 0, 0)

        # The three inputs read as plain figures in the strip, so nothing showed
        # they can be typed into (user report 2026-11-16: "출금을 편집할 수
        # 없는 것으로 아는데"). A dashed underline marks them, solid accent
        # while editing; the tooltip says what goes in.
        for edit in (self._principal_edit, self._deposit_edit, self._withdrawal_edit):
            edit.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            edit.setStyleSheet(
                f"QLineEdit {{ border:none; border-bottom:1px dashed {LINE_STRONG}; padding:0px; background:transparent; }}"
                f"QLineEdit:hover {{ border-bottom:1px solid {LINE_STRONG}; }}"
                f"QLineEdit:focus {{ border-bottom:1px solid {ACCENT}; }}")

        # Every cell has the same stretch factor, so the columns come out
        # equal whenever the window is wide enough; when it is not, each cell
        # shrinks to its own content width. Cells used to be pinned to the
        # widest cell's width, which made the strip 8x that width and pushed
        # the tab past the screen edge (user report 2026-10-15).
        return card

    def _make_money_input(self, placeholder: str) -> QLineEdit:
        """Editable KRW field: re-formats with thousands separators as you type
        and schedules a debounced QSettings save."""
        edit = _make_rw_edit(placeholder)
        edit.textEdited.connect(self._on_deposit_changed)
        edit.textEdited.connect(lambda t, e=edit: _fmt_num_edit(e, t))
        return edit

    def _build_controls_row(self) -> QHBoxLayout:
        """The tab's one toolbar row (docs/ui.md 8.17, 2026-11-16 revision,
        modelled on Trading Universe): live company search, then the three
        cycle buttons for period / grouping / the 30-day rule | Summary,
        Fetch Deposit, Reload, and Add Trade (the primary) last. Every button
        has one width, so a cycle never resizes it."""
        row = _toolbar_hbox()
        self._search_edit = SearchLineEdit()     # counts the IME syllable being composed
        self._search_edit.setPlaceholderText(tr("종목명 또는 코드 검색", "Search name or ticker"))
        self._search_edit.setFixedHeight(_BTN_H)
        self._search_edit.setMinimumWidth(180)
        self._search_edit.setMaximumWidth(260)
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.searchTextChanged.connect(self._apply_filter)
        row.addWidget(self._search_edit)
        row.addSpacing(_GROUP_GAP)

        self._period_filter = "All"
        self._sort_by_date = False
        self._hide_stale_closed = True
        self._period_btn = self._cycle_button(
            _PERIOD_STATES, tr("누를 때마다 매수일 기준 기간이 바뀝니다: 전체 → 1개월 → 3개월 → 올해",
                               "Each click changes the buy-date period: All -> 1M -> 3M -> YTD"),
            lambda: self._on_period_filter_changed(self._next_value(_PERIOD_STATES, self._period_filter)))
        self._group_btn = self._cycle_button(
            _GROUP_STATES, tr("상태별: 매도 완료 → 보유 순 / 월별: 매수일 최신순 + 월 요약 행",
                              "By status: closed then open / By month: newest buys first with a month summary row"),
            lambda: self._on_grouping_changed("status" if self._sort_by_date else "month"))
        # docs/ui.md 3.5: the 30-day rule used to be hardcoded with no UI
        # trace of it, so a blank current-price cell had no explanation.
        self._stale_btn = self._cycle_button(
            _STALE_STATES, tr("시세(30일): 매도 후 30일이 지난 거래의 현재가를 숨깁니다 / 시세(전체): 모두 표시합니다",
                              "Prices (30d): hide the current price of trades closed more than 30 days ago / Prices (All): show all"),
            lambda: self._on_stale_toggle_changed(not self._hide_stale_closed))
        for btn in (self._period_btn, self._group_btn, self._stale_btn):
            row.addWidget(btn)
        row.addStretch()

        self._summary_btn = make_button(tr("종목별 요약", "Summary"), on_click=self._show_holdings_summary)
        # KIS 조회 (was 예수금 조회; 2026-11-18, docs/ui.md 8.28): deposit + executed orders in one go.
        self._fetch_dep_btn = make_button(
            tr("KIS 조회", "KIS Sync"), on_click=self._fetch_kis_account,
            tooltip=tr("KIS 계좌의 예수금과 새 체결 내역을 가져와, 확인 후 Trading History에 반영합니다",
                       "Fetch the KIS account's deposit and new executions and apply them after a review"))
        # No 다시 읽기 (Reload) button since 2026-11-18 (user decision, docs/ui.md 8.27): every
        # Trading Universe refresh runs _reload_current (MainWindow wires refresh_started),
        # this tab refreshes itself after its own edits, and portfolio.db is not edited
        # from outside the app.
        # Add Trade is the tab's one primary (docs/ui.md 8.1), last.
        self._add_trade_btn = make_button(tr("거래 추가", "Add Trade"), "primary", self._show_add_trade_dialog)
        for btn in (self._summary_btn, self._fetch_dep_btn):
            row.addWidget(btn)
        row.addSpacing(_GROUP_GAP)
        row.addWidget(self._add_trade_btn)
        self._unify_button_widths()
        self._sync_cycle_buttons()
        return row

    @staticmethod
    def _cycle_button(states, tooltip: str, on_click) -> QPushButton:
        btn = make_button(states[0][1], tooltip=tooltip, checkable=True)
        btn._cycle_labels = [label for _, label in states]
        btn.clicked.connect(lambda _checked: on_click())
        return btn

    @staticmethod
    def _next_value(states, current):
        values = [v for v, _ in states]
        return values[(values.index(current) + 1) % len(values)] if current in values else values[0]

    def _unify_button_widths(self) -> None:
        """The one toolbar width shared with Universe and Total Assets
        (ui.common.toolbar_button_width; this tab's labels, every cycle label
        included, are registered at the bottom of this module)."""
        fix_toolbar_widths([self._period_btn, self._group_btn, self._stale_btn, self._summary_btn,
                            self._fetch_dep_btn, self._add_trade_btn])

    def _sync_cycle_buttons(self) -> None:
        """Label and checked state of the three cycle buttons from the current
        filter state; checked = not the default, so a narrowed table shows."""
        for btn, states, value in ((self._period_btn, _PERIOD_STATES, self._period_filter),
                                   (self._group_btn, _GROUP_STATES, "month" if self._sort_by_date else "status"),
                                   (self._stale_btn, _STALE_STATES, self._hide_stale_closed)):
            label = next((lbl for v, lbl in states if v == value), states[0][1])
            btn.setText(label)
            btn.setChecked(value != states[0][0])

    # ---Language switch (docs/ui.md 8.20): MainWindow rebuilds every tab in
    # the new language and carries this state over. Plain values only (the
    # ui classes themselves are re-imported).
    def export_state(self) -> dict:
        return {"period": self._period_filter, "by_month": self._sort_by_date,
                "hide_stale": self._hide_stale_closed, "search": self._search_edit.text()}

    def import_state(self, state: dict) -> None:
        self._search_edit.blockSignals(True)
        self._search_edit.setText(state.get("search", ""))
        self._search_edit.blockSignals(False)
        self._hide_stale_closed = state["hide_stale"]
        self._on_grouping_changed("month" if state["by_month"] else "status")
        self._on_period_filter_changed(state["period"])

    def _on_grouping_changed(self, key: str):
        self._sort_by_date = key == "month"
        self._sync_cycle_buttons()
        self._apply_filter()

    def _on_period_filter_changed(self, period: str):
        self._period_filter = period
        self._sync_cycle_buttons()
        self._apply_filter()

    def _on_stale_toggle_changed(self, hide: bool):
        self._hide_stale_closed = hide
        self._sync_cycle_buttons()
        self._apply_filter()

    def _build_history_table(self) -> QTableWidget:
        """The unified closed/open/monthly trade grid with the two-row grouped header."""
        tbl = self._table = SectionTable(SECTIONS)
        tbl.setColumnCount(len(self._COLS))
        # Zebra off: fill_table_rows paints its own stripes per item.
        setup_table(tbl, zebra=False, empty_text=tr("거래가 없습니다 -- 거래 추가로 기록하세요", "No trades yet -- Add Trade records one"))
        tbl.setSortingEnabled(False)
        tbl.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Vertical bar always shown: with AsNeeded it came and went as the
        # toolbar filters changed the row count, the viewport width changed
        # by the bar's width and _fit_columns resized Company (2026-11-15).
        tbl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)

        grouped_hdr = GroupedHeaderView(SECTIONS, self._COLS, tbl)
        grouped_hdr.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        grouped_hdr.setMinimumSectionSize(40)
        tbl.setHorizontalHeader(grouped_hdr)
        tbl.setItemDelegateForColumn(0, TradeStateDelegate(tbl))
        # Ctrl+C copies the selected trades, Enter edits like a double-click
        # (docs/ui.md 1.8 / 8.6); month summary rows are not trades.
        install_table_keys(tbl, on_activate=self._on_cell_double_clicked, skip_row=self._is_summary_row)
        # Clicking or tabbing elsewhere drops the row highlight (it used to stay
        # on the last clicked trade until another row was clicked).
        clear_selection_on_outside_click(tbl, focus_root=self)
        tbl.cellDoubleClicked.connect(self._on_cell_double_clicked)
        # Delete: right-click menu or the Delete key on the selected rows.
        # trade_db.delete_trade() existed for a long time with no UI path to
        # it, so a mistyped trade could only be removed by editing the DB.
        tbl.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        tbl.customContextMenuRequested.connect(self._on_table_context_menu)
        self._delete_shortcut = QShortcut(QKeySequence.StandardKey.Delete, tbl)
        self._delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._delete_shortcut.activated.connect(self._delete_selected_trades)
        return tbl

    def _is_summary_row(self, row: int) -> bool:
        return row < len(self._row_data) and self._row_data[row][0] == "monthly"

    def _selected_trade_records(self) -> list:
        """(kind, rec) for every selected row that is a real trade (month
        group-header rows are skipped)."""
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()})
        return [self._row_data[r] for r in rows if r < len(self._row_data) and self._row_data[r][0] != "monthly"]

    def _on_table_context_menu(self, pos):
        targets = self._selected_trade_records()
        if not targets:
            return
        menu = QMenu(self._table)
        # The stock's trade history (2026-11-16): it used to open only from
        # the removed holdings combo (open rows) and Search button (closed rows).
        history_action = menu.addAction(tr("종목 거래 이력", "Stock Trade History"))
        history_action.setEnabled(len({rec.get("company", "") for _, rec in targets}) == 1)
        delete_action = menu.addAction(tr("거래 삭제...", "Delete Trade..."))
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is delete_action:
            self._delete_selected_trades()
        elif chosen is history_action:
            kind, rec = targets[0]
            self._show_stock_history(rec.get("company", ""), is_open=(kind == "open"))

    def _delete_selected_trades(self):
        targets = self._selected_trade_records()
        if not targets:
            return
        names = ", ".join(f"{rec.get('company', '')} ({rec.get('buy_date', '')})" for _, rec in targets[:3])
        if len(targets) > 3:
            names += tr(f" 외 {len(targets) - 3}건", f" and {len(targets) - 3} more")
        reply = QMessageBox.question(
            self, _TITLE,
            tr(f"거래 {len(targets)}건을 삭제할까요?\n{names}\n\nportfolio.db에서 지워집니다.", f"Delete {len(targets)} trade(s)?\n{names}\n\nThis removes them from portfolio.db."),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        deleted_keys = set()
        for _kind, rec in targets:
            key = rec.get("orig_key")
            try:
                if key:
                    trade_db.delete_trade(key)
                    deleted_keys.add(key)
            except Exception as e:
                logger.error("Failed to delete trade %s: %s", key, e, exc_info=True)
                QMessageBox.warning(self, _TITLE, tr(f"거래를 삭제하지 못했습니다:\n{e}", f"Failed to delete trade:\n{e}"))
                break
        # Filter by orig_key rather than list.remove(rec): dict equality would
        # match a different row with identical field values (e.g. two same-day
        # same-qty buys of the same stock), deleting the wrong one.
        if deleted_keys:
            self._closed_data[:] = [r for r in self._closed_data if r.get("orig_key") not in deleted_keys]
            self._open_data[:] = [r for r in self._open_data if r.get("orig_key") not in deleted_keys]
        self._refresh_summary()
        self._apply_filter()

    def _fit_columns(self):
        """Set column widths to fill the viewport without horizontal scrolling
        -- min-widths come from history_table.COLUMNS (docs/ui.md issue #9)
        instead of a `mins` list hand-aligned to _COLS by comment. Only
        Company (col 0) flexes to fill the remaining space; every other
        column is a genuinely fixed-content width."""
        tbl = self._table
        viewport_w = tbl.viewport().width()
        if viewport_w <= 0:
            return

        fixed_total = sum(c.min_width for c in COLUMNS[1:])
        name_w = max(COLUMNS[0].min_width, viewport_w - fixed_total)

        # If everything doesn't fit, allow horizontal scroll instead of squeezing
        # (Allow horizontal scroll -> prevent text cutoff)
        tbl.setColumnWidth(0, name_w)
        for i, spec in enumerate(COLUMNS[1:], start=1):
            tbl.setColumnWidth(i, spec.min_width)


    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_columns()

    def showEvent(self, event):
        """Triggered when the tab becomes visible - ensures columns fill the viewport."""
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_columns)

    # --- Load from SQLite (portfolio.db) ---
    def load_from_db(self):
        """Load all trade data from SQLite DB (portfolio.db)."
        This is the primary data source, replacing the old JSON files."""
        all_trades = trade_db.load_all_trades()
        self._closed_data = []
        self._open_data   = []
        for rec in all_trades:
            self._compute_pl_fields(rec)
            if rec.get("sell_date") or rec.get("sell_price"):
                self._closed_data.append(rec)
            else:
                self._open_data.append(rec)
        self._refresh_summary()
        self._apply_filter()
        self._start_price_fetch()

    def _reload_current(self):
        self.load_from_db()

    def _save_custom_trade(self, record) -> bool:
        """Persist a manually-added trade to the SQLite database."""
        try:
            saved_key = trade_db.upsert_trade(record)
            record["orig_key"] = saved_key
            return True
        except Exception as e:
            logger.error("Failed to save trade to DB: %s", e, exc_info=True)
            QMessageBox.critical(
                self, _TITLE,
                tr(f"거래를 데이터베이스에 저장하지 못했습니다:\n{e}\n\n기록이 추가되지 않았습니다.", f"Failed to save trade to database:\n{e}\n\nThe record was not added.")
            )
            return False


    # ---Delegates to the split-out modules (kept so callers/tests are unchanged) ---
    @staticmethod
    def _compute_pl_fields(rec: dict) -> None:
        """See ui.history_calc.compute_pl_fields."""
        compute_pl_fields(rec)

    @staticmethod
    def _build_monthly_rows(all_rows: list) -> list:
        """See ui.history_calc.build_monthly_rows."""
        return build_monthly_rows(all_rows)

    def _show_holdings_summary(self):
        if not show_holdings_summary(self, self._closed_data, self._open_data):
            self.status_message.emit(tr("종목별 요약: 기록된 거래가 없습니다", "Summary: no trades recorded yet"))

    def _save_overrides(self, records: list) -> bool:
        """Persist just the given edited records back to the DB in one
        transaction; True on success.

        Used to re-upsert every closed/overridden/custom trade on each edit
        (one cell change -> N rows rewritten, growing with the trade log);
        every call site knows exactly which record(s) it changed, so it
        passes those instead."""
        try:
            if records:
                trade_db.upsert_trades(records)
            return True
        except Exception as e:
            logger.error("Failed to save overrides to DB: %s", e, exc_info=True)
            QMessageBox.warning(
                self, _TITLE,
                tr(f"수정한 거래를 데이터베이스에 저장하지 못했습니다:\n{e}", f"Failed to save modified trades to database:\n{e}")
            )
            return False


    # ---Real-time lightweight price fetch (1-min loop) ---
    def _start_realtime_price_update(self):
        if not self._open_data and not self._closed_data:
            return
        
        kr_tickers = set()
        us_tickers = set()
        
        # Open positions
        for r in self._open_data:
            ticker = r.get("ticker")
            if not ticker:
                continue
            market = r.get("market", "")
            if market in ("KOSPI", "KOSDAQ") or is_kr_code(ticker):
                kr_tickers.add(ticker)
            else:
                us_tickers.add(ticker)
                
        # Recently closed positions (Opportunity Cost tracking)
        for r in self._closed_data:
            if r.get("curr_days", 999) <= 30:
                ticker = r.get("ticker")
                if not ticker:
                    continue
                market = r.get("market", "")
                if market in ("KOSPI", "KOSDAQ") or is_kr_code(ticker):
                    kr_tickers.add(ticker)
                else:
                    us_tickers.add(ticker)
                
        if not kr_tickers and not us_tickers:
            return
            
        if self._rt_price_thread is not None and self._rt_price_thread.isRunning():
            return
            
        self._track_thread(RealtimePriceThread(list(kr_tickers), list(us_tickers)), '_rt_price_thread')
        self._rt_price_thread.failed.connect(self.price_refresh_failed)
        self._rt_price_thread.prices_fetched.connect(self._on_realtime_prices_fetched)
        self._rt_price_thread.status_message.connect(self.status_message.emit)
        self._rt_price_thread.start()

    def _on_realtime_prices_fetched(self, prices_dict, complete: bool = True):
        """RealtimePriceThread.prices_fetched. Partial quotes are applied, but
        only a complete answer clears the failure indicator (review_codex.md C12)."""
        if not prices_dict:
            return
        if complete:
            self.price_refresh_ok.emit()
            
        updated = False
        
        # Update open positions
        for r in self._open_data:
            ticker = r.get("ticker", "")
            if ticker in prices_dict:
                new_price = prices_dict[ticker]
                if r.get("curr_price", 0.0) != new_price:
                    r["curr_price"] = new_price
                    updated = True
                    
        # Update recently closed positions
        for r in self._closed_data:
            if r.get("curr_days", 999) <= 30:
                ticker = r.get("ticker", "")
                if ticker in prices_dict:
                    new_price = prices_dict[ticker]
                    if r.get("curr_price", 0.0) != new_price:
                        r["curr_price"] = new_price
                        # Update curr_pl_pct for historical closed items (opportunity cost %)
                        sell_price = r.get("sell_price", 0.0)
                        if sell_price > 0:
                            r["curr_pl_pct"] = (new_price - sell_price) / sell_price * 100
                        updated = True
                        
        if updated:
            self._refresh_summary()
            self._apply_filter()


    # ---Full price fetch for open positions ---
    def _start_price_fetch(self):
        """Launch a background thread to fetch current prices for open positions, and tickers for all."""
        if not self._open_data and not self._closed_data:
            return

        names      = []
        tickers    = []
        markets    = []
        buy_prices = []
        qtys       = []
        buy_amts   = []
        is_open    = []
        skip_fetch = []
        
        for r in self._closed_data:
            days_since = 0
            if r.get("sell_date"):
                try:
                    sd = _dt.datetime.strptime(r["sell_date"], "%Y-%m-%d").date()
                    days_since = (_dt.date.today() - sd).days
                except Exception:
                    logger.debug(
                        "days_since calculation failed for sell_date=%s", r.get("sell_date"), exc_info=True,
                    )

            names.append(r["company"])
            tickers.append(r.get("ticker", ""))
            markets.append(r.get("market", ""))
            
            skip_fetch.append(days_since > 30)
            
            buy_prices.append(0)
            qtys.append(0)
            buy_amts.append(0)
            is_open.append(False)
            
        for r in self._open_data:
            names.append(r["company"])
            tickers.append(r.get("ticker", ""))
            markets.append(r.get("market", ""))
            skip_fetch.append(False)
            buy_prices.append(r["buy_price"])
            qtys.append(r["qty"])
            buy_amts.append(r["buy_amount"])
            is_open.append(True)

        self._price_req_id += 1
        self._price_req = [(r, r.get("company"), r.get("ticker", ""))
                           for r in self._closed_data + self._open_data]
        thread = self._track_thread(PositionPriceFetchThread(
            names, tickers, markets, buy_prices, qtys, buy_amts, is_open, skip_fetch,
            request_id=self._price_req_id,
        ), '_price_thread')
        thread.prices_ready.connect(self._on_prices_ready)
        thread.status_message.connect(self.status_message.emit)
        self._path_label.setText(tr("현재가 불러오는 중...", "Loading current prices..."))
        thread.start()

    def _on_prices_ready(self, results: list, request_id: int):
        """PositionPriceFetchThread.prices_ready. Each result dict has
        index (into the inputs of that run), curr_price, curr_pl, curr_pl_pct,
        ticker, market, name, wk1, wk2, mth1.

        Results are applied to the record each input came from, not to the
        same list position now (review_codex.md C10): a trade deleted, sold
        or split while the fetch ran used to shift the lists, so a late result
        wrote another trade's ticker, company and price. A result from an
        older run, for a record no longer listed, or for one whose company or
        ticker was edited since the request is dropped."""
        if request_id != self._price_req_id:
            return
        self._path_label.setText("")
        closed_ids = {id(r) for r in self._closed_data}
        open_ids = {id(r) for r in self._open_data}
        updated = False

        for res in results:
            idx = res["index"]
            if not 0 <= idx < len(self._price_req):
                continue
            rec, company, ticker = self._price_req[idx]
            if rec.get("company") != company or rec.get("ticker", "") != ticker:
                continue
            is_closed = id(rec) in closed_ids
            if not is_closed and id(rec) not in open_ids:
                continue          # deleted (or the lists were reloaded) since the request

            if res.get("ticker"):
                rec["ticker"] = res.get("ticker")
            if res.get("market"):
                rec["market"] = res.get("market")
            if res.get("name"):
                rec["company"] = res.get("name")
            curr_price = res.get("curr_price", 0.0)
            # Open: curr_price only, P/L is calculated in _refresh_summary.
            rec["curr_price"] = curr_price
            if is_closed and curr_price > 0:
                # col 17 "Position P/L(%)" = change of the current price vs the
                # sell price (opportunity cost after selling)
                sell_price = rec.get("sell_price", 0.0)
                if sell_price > 0:
                    rec["curr_pl_pct"] = (curr_price - sell_price) / sell_price * 100
            for key in ("wk1", "wk2", "mth1"):
                if res.get(key, 0.0) != 0.0:
                    rec[key] = res[key]
            updated = True

        if updated:
            self._refresh_summary()
            self._apply_filter()

    # ---Input handler ---
    def _flush_settings(self):
        """Flush principal/deposit/withdrawal to QSettings (called by debounce timer)."""
        self._settings.setValue("trading_history/principal",  self._principal_edit.text())
        self._settings.setValue("trading_history/deposit",    self._deposit_edit.text())
        self._settings.setValue("trading_history/withdrawal", self._withdrawal_edit.text())

    def _on_deposit_changed(self, *args, **kwargs):
        """Recompute summary whenever the user edits input fields."""
        try:
            # Debounce: write settings 400 ms after the last keystroke
            self._settings_save_timer.start()
            self._refresh_summary()
        except Exception as e:
            logger.error("Error in _on_deposit_changed: %s", e, exc_info=True)

    def _get_deposit(self) -> float:
        raw = self._deposit_edit.text().replace(',', '').strip()
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def _get_withdrawal(self) -> float:
        raw = self._withdrawal_edit.text().replace(',', '').strip()
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def _get_principal(self) -> float:
        raw = self._principal_edit.text().replace(',', '').strip()
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def _kis_query_start(self) -> _dt.date:
        """First day to ask KIS for executions: the last imported order's day (an
        order may have filled more since; kis_imports keeps only the difference),
        else _KIS_FIRST_LOOKBACK_DAYS back."""
        last = trade_db.last_kis_import_date()
        if last:
            try:
                return _dt.datetime.strptime(last, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("kis_imports: bad order date %r", last)
        return _dt.date.today() - _dt.timedelta(days=_KIS_FIRST_LOOKBACK_DAYS)

    def _fetch_kis_account(self):
        """KIS 조회: deposit, holdings and executions off the UI thread (roadmap 6-1c)."""
        if self._deposit_thread is not None and self._deposit_thread.isRunning():
            return
        self._fetch_dep_btn.setEnabled(False)
        self.status_message.emit(tr("KIS에서 예수금·체결 내역 조회 중...", "Fetching deposit and executions from KIS..."))
        self._track_thread(KisAccountThread(self._kis_query_start()), '_deposit_thread')
        self._deposit_thread.finished.connect(self._on_kis_account_fetched)
        self._deposit_thread.start()

    def _on_kis_account_fetched(self, result: dict, err: str):
        self._fetch_dep_btn.setEnabled(True)
        if err:
            self.status_message.emit(tr("KIS 조회 실패", "KIS fetch failed"))
            QMessageBox.critical(self, _TITLE, tr(f"KIS 계좌를 조회하지 못했습니다:\n{err}",
                                                  f"Failed to query the KIS account:\n{err}"))
            return
        val = result["deposit"]
        self._deposit_edit.setText(f"{int(val):,}")
        self._on_deposit_changed()
        # Results on the status bar (docs/ui.md 8.4), not a modal.
        deposit_msg = tr(f"계좌 예수금: {int(val):,} KRW (추정) -- 예수금에 입력했습니다",
                         f"Account deposit: {int(val):,} KRW (estimate) -- filled into Deposit")
        if result.get("exec_error"):
            self.status_message.emit(deposit_msg)
            QMessageBox.warning(self, _TITLE, tr(
                f"예수금은 입력했지만 체결 내역을 조회하지 못했습니다:\n{result['exec_error']}",
                f"The deposit was filled in, but the executions could not be fetched:\n{result['exec_error']}"))
            return
        try:
            imported = trade_db.load_kis_imports()
        except sqlite3.Error as e:
            logger.error("kis_imports read failed", exc_info=True)
            QMessageBox.critical(self, _TITLE, tr(f"DB를 읽지 못했습니다:\n{e}", f"Could not read the DB:\n{e}"))
            return
        plan = plan_kis_import(result["executions"], imported, self._open_data, result.get("markets"),
                               result.get("holdings"))
        if not plan.rows and not plan.warnings:
            self.status_message.emit(deposit_msg + tr(" · 새 체결 없음", " · no new executions"))
            return
        dlg = KisSyncDialog(plan, val, self)
        self._kis_dialog = dlg               # tests drive it
        if dlg.exec() != QDialog.DialogCode.Accepted or not plan.applicable:
            self.status_message.emit(deposit_msg)
            return
        try:
            trade_db.apply_kis_import(plan.records, plan.imports)
        except (sqlite3.Error, ValueError) as e:
            logger.error("KIS import save failed", exc_info=True)
            QMessageBox.critical(self, _TITLE, tr(f"체결 내역을 저장하지 못했습니다:\n{e}",
                                                  f"Could not save the executions:\n{e}"))
            return
        n = sum(1 for r in plan.rows if r.applied)
        self.load_from_db()          # one source of truth after a multi-row change
        self.status_message.emit(deposit_msg + tr(f" · 체결 {n}건 반영", f" · {n} execution(s) applied"))

    # ---Summary ---
    def _refresh_summary(self):
        """Recompute the position aggregates (ui.history_calc.summarize_positions)
        and render them into the KPI strip."""
        deposit    = self._get_deposit()
        withdrawal = self._get_withdrawal()
        principal  = self._get_principal()

        agg = summarize_positions(
            self._open_data, self._closed_data,
            deposit=deposit, withdrawal=withdrawal, principal=principal,
        )
        cumulative_asset, total_pl = agg["cumulative_asset"], agg["total_pl"]
        # Account valuation + cumulative withdrawals (user direction
        # 2026-10-25): this feeds Total Assets' "Current Total Assets" and its
        # weekly-record autofill, which track the account's value as if
        # nothing had been withdrawn, so a withdrawal is not a weekly loss.
        # (2026-09-20 to 2026-10-25 this sent live NAV without withdrawals.)
        self.total_asset_updated.emit(cumulative_asset)

        def _pos_color(v): return LOSS if v < 0 else PROFIT

        # ---Update the KPI strip (docs/ui.md 3.6) ---
        def set_kpi(key, text, color=None, tooltip=None):
            val_lbl, sub_lbl = self._kpi_labels[key]
            if val_lbl is not None:
                val_lbl.setText(text)
                if color:
                    val_lbl.setStyleSheet(f"color:{color};")
                if tooltip:
                    val_lbl.setToolTip(tooltip)

        # No lines under the figures (user direction 2026-11-16): the ratios
        # (원금의 n배, 운용 자금 대비 %) are on the tooltips.
        times = tr(f" · 원금의 {cumulative_asset / principal:.2f}배", f" · {cumulative_asset / principal:.2f}x principal")             if principal > 0 else ""
        set_kpi("total_asset", f"{cumulative_asset:,.0f}",
                tooltip=tr(f"평가액 {agg['eval_total']:,.0f} + 예수금 {deposit:,.0f} + 출금 {withdrawal:,.0f}",
                           f"Valuation {agg['eval_total']:,.0f} + cash {deposit:,.0f} + withdrawals {withdrawal:,.0f}") + times)
        set_kpi("total_pl", f"{total_pl:+,.0f}", _pos_color(total_pl),
                 tooltip=tr(f"총자산 ({cumulative_asset:,.0f}) - 원금 ({principal:,.0f})", f"Total Assets ({cumulative_asset:,.0f}) - Principal ({principal:,.0f})"))
        set_kpi("total_pl_pct", f"{agg['total_pl_pct']:+.1f}%", _pos_color(agg["total_pl_pct"]),
                tooltip=tr("총 손익 ÷ 원금", "Total P/L ÷ principal"))
        set_kpi("cost_basis", f"{agg['cost_total']:,.0f}", tooltip=tr("보유 종목 매입액", "Cost of the open positions"))
        set_kpi("eval_total", f"{agg['eval_total']:,.0f}", tooltip=tr("보유 종목 현재가 평가액", "Open positions at the current price"))
        pos_pl, capital = agg["pos_pl"], agg["capital"]
        set_kpi("pos_pl", f"{pos_pl:+,.0f}", _pos_color(pos_pl) if pos_pl else None,
                tooltip=tr(f"평가액 − 매입 원가 = 운용 자금 {capital:,.0f}(예수금 + 매입 원가)의 {agg['pos_pl_pct_capital']:+.1f}%",
                           f"Valuation − cost = {agg['pos_pl_pct_capital']:+.1f}% of the trading capital {capital:,.0f} (deposit + cost)"))

    def _stock_history_rows(self, company: str, is_open: bool):
        """(matches, total P/L, total buy, total sell) of one company's closed
        trades, or of its open lots valued at the current price."""
        matches, total_pl, total_buy, total_sell = [], 0.0, 0.0, 0.0
        if not is_open:
            for item in self._closed_data:
                if item.get("company", "") == company:
                    matches.append(item)
                    total_pl += float(item.get("pl", 0.0))
                    total_buy += float(item.get("buy_amount", 0.0))
                    total_sell += float(item.get("sell_amount", 0.0))
            return matches, total_pl, total_buy, total_sell
        for item in self._open_data:
            if item.get("company", "") != company:
                continue
            rec = item.copy()
            curr_price = float(rec.get("curr_price", 0.0))
            qty = float(rec.get("qty", 0.0))
            buy_amt = float(rec.get("buy_amount", 0.0))
            eval_amt = curr_price * qty
            pl = eval_amt - buy_amt
            rec.update(sell_date="Open", sell_price=curr_price, sell_qty=qty, sell_amount=eval_amt,
                       pl=pl, pl_pct=(pl / buy_amt * 100) if buy_amt > 0 else 0.0)
            matches.append(rec)
            total_pl += pl
            total_buy += buy_amt
            total_sell += eval_amt
        return matches, total_pl, total_buy, total_sell

    def _show_stock_history(self, company: str, is_open: bool):
        """The table's "종목 거래 이력": closed trades of a closed row's company,
        open lots (at the current price) of an open row's company."""
        matches, total_pl, total_buy, total_sell = self._stock_history_rows(company, is_open)
        if not matches:
            self.status_message.emit(tr(f"'{company}'의 거래가 없습니다", f"No trades found for '{company}'"))
            return
        StockTradeHistoryDialog(company, matches, total_pl, total_buy, total_sell, self,
                                is_open_position=is_open).exec()

    # ---Filter / refresh ---
    def _apply_filter(self, *_):
        state = getattr(self, "_state_filter", "All")
        period = getattr(self, "_period_filter", "All")

        # docs/ui.md 3.3: the status filter narrows which rows show, never
        # which columns exist (Buy/Sell/Position/Past structure is fixed).
        closed_src = self._closed_data if state in ("All", "Closed") else []
        open_src = self._open_data if state in ("All", "Open") else []

        cutoff = self._period_cutoff_date(period)
        if cutoff is not None:
            closed_src = [r for r in closed_src if r.get("buy_date", "") >= cutoff]
            open_src = [r for r in open_src if r.get("buy_date", "") >= cutoff]

        # Live search (2026-11-16, like Trading Universe): company name or
        # ticker contains the text, case-insensitive; both open and closed rows.
        query = self._search_edit.search_text().strip().lower() if hasattr(self, "_search_edit") else ""
        if query:
            def hit(r):
                return query in str(r.get("company", "")).lower() or query in str(r.get("ticker", "")).lower()
            closed_src = [r for r in closed_src if hit(r)]
            open_src = [r for r in open_src if hit(r)]

        closed_rows = [("closed", r) for r in closed_src]
        open_rows   = [("open", r) for r in open_src]

        sort_by_date = getattr(self, "_sort_by_date", False)

        if sort_by_date:
            # All rows sorted by buy_date descending (closed + open together,
            # most recently bought first), with a summary row appended after
            # each calendar month's group (build_monthly_rows groups rows by
            # first-seen month, so the most recent month lands first here too).
            all_rows = closed_rows + open_rows
            all_rows.sort(key=lambda x: x[1]["buy_date"], reverse=True)
            rows = self._build_monthly_rows(all_rows)
        else:
            # Default: closed (most recently bought first) then open (most
            # recently bought first) -- user request: recently bought stocks
            # at the top instead of the bottom.
            closed_rows.sort(key=lambda x: x[1]["buy_date"], reverse=True)
            open_rows.sort(key=lambda x: x[1]["buy_date"], reverse=True)
            rows = closed_rows + open_rows
        self._fill_table(rows)

    @staticmethod
    def _period_cutoff_date(period: str):
        """1M/3M/YTD/All toolbar filter -- returns the earliest buy_date
        (YYYY-MM-DD string) to include, or None for no cutoff."""
        today = _dt.date.today()
        if period == "1M":
            return (today - _dt.timedelta(days=30)).strftime("%Y-%m-%d")
        if period == "3M":
            return (today - _dt.timedelta(days=90)).strftime("%Y-%m-%d")
        if period == "YTD":
            return _dt.date(today.year, 1, 1).strftime("%Y-%m-%d")
        return None

    # ---Table item helpers ---
    # ---Unified table fill ---
    def _fill_table(self, rows: list):
        tbl = self._table
        tbl.setSortingEnabled(False)
        tbl.setUpdatesEnabled(False)
        try:
            self._row_data = fill_table_rows(
                tbl, rows, hide_stale_closed=getattr(self, "_hide_stale_closed", True),
            )
        finally:
            tbl.setUpdatesEnabled(True)
        self._fit_columns()
        tbl.scrollToTop()

    # ---Buy/Sell cell double-click edit ---
    # Editable columns: Buy(3=Date, 4=Price, 5=Qty, 6=Amount), Sell(8=Date, 10=Price, 11=Qty, 12=Amount)
    _EDITABLE_COLS = {
        3:  ("buy_date",    "Buy Date (YYYY-MM-DD)", "str"),
        4:  ("buy_price",   "Buy Price",               "float"),
        5:  ("qty",         "Buy Quantity",               "float"),
        6:  ("buy_amount",  "Buy Amount",               "float"),
        7:  ("sell_date",   "Sell Date (YYYY-MM-DD)", "str"),
        9:  ("sell_price",  "Sell Price",               "float"),
        10: ("sell_qty",    "Sell Quantity",               "float"),
        11: ("sell_amount", "Sell Amount",               "float"),
    }
    # Derived from _EDITABLE_COLS (the single source of truth for column ->
    # field) instead of separately hardcoded {3,4,5,6}/{7,9,10,11} literals,
    # so the two can't drift out of sync with it (review_agy.md #5).
    _SELL_EDIT_COLS = {c for c, (key, _, _) in _EDITABLE_COLS.items() if key.startswith("sell_")}
    _BUY_EDIT_COLS = set(_EDITABLE_COLS) - _SELL_EDIT_COLS

    def _on_cell_double_clicked(self, row: int, col: int):
        """Edit a Buy/Sell field of a position via double-click."""
        if row >= len(self._row_data):
            return
        kind, rec = self._row_data[row]

        # docs/ui.md 3.4: a month group-header row is not a trade -- it used
        # to have no guard here at all, so double-clicking one opened
        # Buy/SellEditDialog on the fake aggregate record (silently
        # discarded on save since it's never in _open_data/_closed_data, but
        # confusing: nothing told the user their "edit" went nowhere).
        if kind == "monthly":
            return

        # Every edit is built on a copy and applied to `rec` only once the DB
        # write succeeded, so a failed save leaves screen and DB on the
        # pre-edit state (review_codex.md C11; the sell edit below did this
        # already).
        if col == 0:
            self._edit_company_cell(rec)
        elif col == 2:
            self._edit_ticker_cell(rec)
        elif col in self._BUY_EDIT_COLS:
            self._edit_buy_fields(rec)
        elif col in self._SELL_EDIT_COLS:
            self._edit_sell_fields(kind, rec)

    def _edit_company_cell(self, rec: dict) -> None:
        """Company-name cell: rename the position."""
        curr_val = rec.get("company", "")
        new_str, ok = QInputDialog.getText(self, tr("수정", "Edit"), tr("종목명:", "Company Name:"), text=str(curr_val))
        if ok and new_str.strip():
            edited = dict(rec, company=new_str.strip(), is_overridden=True)
            if not self._save_overrides([edited]):
                return
            rec.update(edited)
            self._refresh_summary()
            self._apply_filter()

    def _edit_ticker_cell(self, rec: dict) -> None:
        """Ticker cell: change the code, refetch prices and resolve the name."""
        curr_val = rec.get("ticker", "")
        new_str, ok = QInputDialog.getText(self, tr("수정", "Edit"), tr("코드:", "Ticker:"), text=str(curr_val))
        if ok and new_str.strip():
            new_ticker = new_str.strip()
            edited = dict(rec, ticker=new_ticker, is_overridden=True)
            if not self._save_overrides([edited]):
                return            # no price or name lookup for an unsaved ticker
            rec.update(edited)

            self._start_price_fetch()
            self._refresh_summary()
            self._apply_filter()

            # Resolve the company name for the new ticker in the background
            # (used to be a synchronous fetch_single_stock call on the UI
            # thread, roadmap 6-1c). The record is updated again when it lands.
            thread = self._track_thread(SingleStockFetchThread(rec.get("market", ""), new_ticker))
            thread.finished.connect(self._on_ticker_name_resolved)
            thread.start()

    def _edit_buy_fields(self, rec: dict) -> None:
        """Buy date/price/qty/amount cells: BuyEditDialog."""
        dlg = BuyEditDialog(rec, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            res = dlg.result_data
            edited = dict(rec)
            edited["buy_date"]   = res["buy_date"]
            edited["buy_price"]  = res["buy_price"]
            edited["qty"]        = res["qty"]
            edited["buy_amount"] = res["buy_amount"]
            edited["is_overridden"] = True
            if not self._save_overrides([edited]):
                return
            rec.update(edited)
            self._compute_pl_fields(rec)
            self._refresh_summary()
            self._apply_filter()

    def _edit_sell_fields(self, kind: str, rec: dict) -> None:
        """Sell date/price/qty/amount cells: SellEditDialog; a partial sell of
        an open lot splits it (see _split_partial_sell)."""
        dlg = SellEditDialog(rec, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            res = dlg.result_data
            orig_qty = rec.get("qty", 0.0)
            orig_buy_amount = rec.get("buy_amount", 0.0)
            sell_qty = res["sell_qty"]

            # Build the edited lot on a copy and only apply it to `rec`
            # (and the open/closed lists) once the DB write succeeded, so a
            # failed save leaves screen and DB on the pre-edit state.
            edited = dict(rec)
            edited["sell_date"]   = res["sell_date"]
            edited["sell_price"]  = res["sell_price"]
            edited["sell_qty"]    = sell_qty
            edited["sell_amount"] = res["sell_amount"]  # dialog already fills price*qty when blank
            edited["is_overridden"] = True

            is_now_closed = bool(edited.get("sell_date") or edited.get("sell_price"))
            remainder = None
            if kind == "open" and is_now_closed and 0 < sell_qty < orig_qty:
                remainder = self._split_partial_sell(edited, orig_qty, orig_buy_amount, sell_qty)

            # The closed sub-lot UPDATE and the remainder INSERT go through
            # one upsert_trades() transaction: saved separately, a failure
            # of either left the DB with the original unsold lot plus the
            # remainder, or the closed sub-lot with no remainder (review_codex.md #1).
            if not self._save_overrides([edited] if remainder is None else [edited, remainder]):
                return

            rec.update(edited)
            self._compute_pl_fields(rec)
            self._move_after_sell_edit(kind, rec, is_now_closed, remainder)

            self._refresh_summary()
            self._apply_filter()

    def _split_partial_sell(self, edited: dict, orig_qty: float, orig_buy_amount: float, sell_qty: float) -> dict:
        """Partial sell: split the lot instead of moving the whole
        record to closed, which used to make the untouched
        remainder disappear from open holdings/valuation
        entirely (review_agy.md #5). `edited` becomes the closed
        sub-lot sized to what was actually sold (changed in place); the
        returned remainder keeps tracking as its own open position with its own
        DB row (buy_amount split proportionally, no sell fields)."""
        remainder_qty = orig_qty - sell_qty
        edited["qty"] = sell_qty
        edited["buy_amount"] = orig_buy_amount * (sell_qty / orig_qty) if orig_qty else 0.0
        remainder = dict(edited)
        remainder["orig_key"] = None
        remainder["qty"] = remainder_qty
        remainder["buy_amount"] = orig_buy_amount - edited["buy_amount"]
        remainder["sell_date"] = ""
        remainder["sell_price"] = 0.0
        remainder["sell_qty"] = 0.0
        remainder["sell_amount"] = 0.0
        remainder["curr_price"] = 0.0
        remainder["curr_pl"] = 0.0
        remainder["curr_pl_pct"] = 0.0
        remainder["curr_pct_pl"] = 0.0
        remainder["position_w"] = 0.0
        self._compute_pl_fields(remainder)
        return remainder

    def _move_after_sell_edit(self, kind: str, rec: dict, is_now_closed: bool, remainder: dict | None) -> None:
        """Move `rec` between the open and closed lists after a saved sell
        edit, and add a partial sell's remainder to the open list."""
        # Filter by orig_key rather than list.remove(rec): dict
        # equality would risk matching a different row (review_agy.md #2).
        if kind == "open" and is_now_closed:
            self._open_data[:] = [r for r in self._open_data if r.get("orig_key") != rec.get("orig_key")]
            self._closed_data.append(rec)
        elif kind == "closed" and not is_now_closed:
            self._closed_data[:] = [r for r in self._closed_data if r.get("orig_key") != rec.get("orig_key")]
            self._open_data.append(rec)
        if remainder is not None:
            self._open_data.append(remainder)
    def _on_ticker_name_resolved(self, result, error: str, ticker: str):
        """SingleStockFetchThread.finished for the ticker-cell edit above: apply the
        resolved company name to every record now carrying that ticker."""
        name = (result or {}).get("name")
        if not name:
            return
        targets = [rec for rec in self._open_data + self._closed_data
                   if rec.get("ticker") == ticker and rec.get("company") != name]
        if not targets:
            return
        edited = [dict(rec, company=name, is_overridden=True) for rec in targets]
        if not self._save_overrides(edited):     # C11: nothing applied unless saved
            return
        for rec, new in zip(targets, edited):
            rec.update(new)
        self._refresh_summary()
        self._apply_filter()
        # The price fetch the ticker edit started was asked for under the old
        # company name; its results for these records are dropped
        # (_on_prices_ready), so ask again under the resolved one.
        self._start_price_fetch()

    def _show_add_trade_dialog(self):
        """Open TradeEntryDialog to add manual trade record."""
        dlg = TradeEntryDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_data:
            res = dlg.result_data
            buy_price = res["buy_price"]
            qty = res["qty"]
            buy_amount = res["buy_amount"]  # dialog already fills price*qty when blank
            
            sell_price = res["sell_price"]
            sell_qty = res["sell_qty"]
            sell_amount = res["sell_amount"]
            
            pl = sell_amount - buy_amount if (sell_amount > 0 and buy_amount > 0) else 0.0
            pl_pct = (pl / buy_amount * 100) if buy_amount > 0 else 0.0
            
            is_closed = bool(res["sell_date"] or res["sell_price"])
            
            try:
                bd = _dt.datetime.strptime(res["buy_date"], "%Y-%m-%d").date()
                if res["sell_date"]:
                    sd = _dt.datetime.strptime(res["sell_date"], "%Y-%m-%d").date()
                    days_held = (sd - bd).days
                    curr_days = 0
                else:
                    days_held = 0
                    curr_days = (_dt.date.today() - bd).days
            except Exception:
                days_held = 0
                curr_days = 0
                
            # No orig_key here on purpose: trade_db.upsert_trade() generates a
            # collision-free key inside the INSERT itself (roadmap 6-1e), and
            # _save_custom_trade() writes the returned key back into `record`.
            record = {
                "company":     res.get("company", ""),
                "market":      res.get("market", ""),
                "ticker":      res.get("ticker", ""),
                "buy_date":    res["buy_date"],
                "buy_price":   buy_price,
                "qty":         qty,
                "buy_amount":  buy_amount,
                "position_w":  0.0,
                "sell_date":   res["sell_date"],
                "days_held":   days_held,
                "sell_price":  sell_price,
                "sell_qty":    sell_qty,
                "sell_amount": sell_amount,
                "pl":          pl,
                "pl_pct":      pl_pct,
                "curr_days":   curr_days,
                "curr_price":  0.0,
                "curr_pl":     0.0,
                "curr_pl_pct": 0.0,
                "curr_pct_pl": 0.0,
                "wk1": 0.0, "wk2": 0.0, "mth1": 0.0,
                "is_custom":   True  # flag to indicate it's a manual entry if needed
            }
            
            if not self._save_custom_trade(record):
                return

            if is_closed:
                self._closed_data.append(record)
            else:
                self._open_data.append(record)
                
            self._refresh_summary()
            self._start_price_fetch()
            self._apply_filter()


# Every label this tab's toolbar buttons can show (the shared toolbar width,
# ui.common.toolbar_button_width).
register_toolbar_labels(
    *(label for states in (_PERIOD_STATES, _GROUP_STATES, _STALE_STATES) for _, label in states),
    tr("종목별 요약", "Summary"), tr("KIS 조회", "KIS Sync"),
    tr("거래 추가", "Add Trade"),
)
