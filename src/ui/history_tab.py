"""ui/history_tab.py — TradingHistoryTab (Phase 4 split)

Split out from: main.py (2026-08-29 feat/3-1-modularize, Phase 4)
Contains:
  TradingHistoryTab
"""
from i18n import tr
import logging
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
    AccountDepositThread,
    SingleStockFetchThread,
)
from threads.realtime import RealtimePriceThread
from ui.table_headers import GroupedHeaderView
from ui.dialogs import (
    BuyEditDialog,
    SellEditDialog,
    TradeEntryDialog,
    StockTradeHistoryDialog,
)

logger = logging.getLogger(__name__)


from ui.common import (
    _fmt_num_edit, ThreadOwnerMixin,
    BTN_H, make_button, setup_table, make_footer_label, TAB_MARGINS, TAB_SPACING,
    KpiStrip, install_table_keys, clear_selection_on_outside_click,
)
from ui.colors import PROFIT, LOSS
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
# Toolbar cycle buttons (user direction 2026-11-16, like the Universe
# 종목(전체) button): (value, label) in click order, the first is the default
# and shows unchecked. The values stay what the filter code compares.
_PERIOD_STATES = (("All", tr("기간(전체)", "Period (All)")), ("1M", tr("기간(1개월)", "Period (1M)")),
                  ("3M", tr("기간(3개월)", "Period (3M)")), ("YTD", tr("기간(올해)", "Period (YTD)")))
_GROUP_STATES = (("status", tr("보기(상태별)", "View (Status)")), ("month", tr("보기(월별)", "View (Month)")))
_STALE_STATES = ((True, tr("시세(30일 숨김)", "Prices (30d hidden)")), (False, tr("시세(모두 표시)", "Prices (All)")))
_GROUP_GAP = 12      # between toolbar groups (docs/ui.md 8.17: 12~16px)
_MIN_BTN_W = 80      # floor of the one shared toolbar button width


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
        cell("total_asset", tr("총자산", "Total Assets"), tr("평가액 + 현금", "Valuation + cash"))
        cell("total_pl", tr("총 손익", "Total P/L"), tr("실현 + 평가 손익", "Realized + unrealized"))
        cell("total_pl_pct", tr("총 손익 (%)", "Total P/L (%)"), tr("원금 대비", "vs Principal"))
        card.add_divider()
        cell("cumulative_asset", tr("누적 자산", "Cumulative Assets"), tr("실시간 평가액 + 출금액", "Live NAV + withdrawals"), primary=False)
        self._principal_edit = self._make_money_input(tr("예: 50,000,000", "e.g. 50,000,000"))
        cell("principal", tr("원금", "Principal"), tr("입금 − 출금", "Deposits − withdrawals"), self._principal_edit, primary=False)
        cell("cost_basis", tr("매입 원가", "Cost Basis"), tr("보유 종목 매입액", "Open positions cost"), primary=False)
        self._deposit_edit = self._make_money_input(tr("예: 10,000,000", "e.g. 10,000,000"))
        cell("deposit", tr("예수금", "Deposit"), "", self._deposit_edit, primary=False)
        self._withdrawal_edit = self._make_money_input(tr("예: 5,000,000", "e.g. 5,000,000"))
        cell("withdrawal", tr("출금", "Withdrawal"), tr("누적", "Cumulative"), self._withdrawal_edit, primary=False)

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
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText(tr("종목명 또는 코드 검색", "Search name or ticker"))
        self._search_edit.setFixedHeight(_BTN_H)
        self._search_edit.setMinimumWidth(180)
        self._search_edit.setMaximumWidth(260)
        self._search_edit.setClearButtonEnabled(True)
        self._search_edit.textChanged.connect(self._apply_filter)
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
            _STALE_STATES, tr("매도 후 30일이 지난 거래의 현재가를 숨기거나 모두 표시합니다",
                              "Hide the current price of trades closed more than 30 days ago, or show all"),
            lambda: self._on_stale_toggle_changed(not self._hide_stale_closed))
        for btn in (self._period_btn, self._group_btn, self._stale_btn):
            row.addWidget(btn)
        row.addStretch()

        self._summary_btn = make_button(tr("종목별 요약", "Summary"), on_click=self._show_holdings_summary)
        self._fetch_dep_btn = make_button(tr("예수금 조회", "Fetch Deposit"), on_click=self._fetch_account_deposit)
        self._reload_btn = make_button(tr("다시 읽기", "Reload"), on_click=self._reload_current)
        # Add Trade is the tab's one primary (docs/ui.md 8.1), last.
        self._add_trade_btn = make_button(tr("거래 추가", "Add Trade"), "primary", self._show_add_trade_dialog)
        for btn in (self._summary_btn, self._fetch_dep_btn, self._reload_btn):
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
        """One width for every toolbar button: the widest label any of them
        can show (every cycle label included), never below _MIN_BTN_W."""
        buttons = [self._period_btn, self._group_btn, self._stale_btn, self._summary_btn,
                   self._fetch_dep_btn, self._reload_btn, self._add_trade_btn]
        width = _MIN_BTN_W
        for b in buttons:
            current = b.text()
            for label in getattr(b, "_cycle_labels", [current]):
                b.setText(label)
                width = max(width, b.sizeHint().width())
            b.setText(current)
        for b in buttons:
            b.setFixedWidth(width)

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

    def _fetch_account_deposit(self):
        """Fetch button: run the KIS balance inquiry off the UI thread (roadmap 6-1c)."""
        if self._deposit_thread is not None and self._deposit_thread.isRunning():
            return
        self._fetch_dep_btn.setEnabled(False)
        self.status_message.emit(tr("KIS에서 계좌 예수금 조회 중...", "Fetching account deposit from KIS..."))
        self._track_thread(AccountDepositThread(), '_deposit_thread')
        self._deposit_thread.finished.connect(self._on_account_deposit_fetched)
        self._deposit_thread.start()

    def _on_account_deposit_fetched(self, val: float, err: str):
        self._fetch_dep_btn.setEnabled(True)
        if err:
            self.status_message.emit(tr("계좌 예수금 조회 실패", "Account deposit fetch failed"))
            QMessageBox.critical(self, _TITLE, tr(f"계좌 예수금을 조회하지 못했습니다:\n{err}", f"Failed to fetch the account deposit:\n{err}"))
            return
        self._deposit_edit.setText(f"{int(val):,}")
        self._on_deposit_changed()
        # Result on the status bar (docs/ui.md 8.4: one place for transient
        # results), not a modal -- the field stays focused for an edit.
        self.status_message.emit(tr(f"계좌 예수금: {int(val):,} KRW (추정) -- 예수금에 입력했습니다", f"Account deposit: {int(val):,} KRW (estimate) -- filled into Deposit"))
        self._deposit_edit.selectAll()
        self._deposit_edit.setFocus()

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
        nav, cumulative_asset, total_pl = agg["nav"], agg["cumulative_asset"], agg["total_pl"]
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
            if key == "deposit":
                sub_lbl.setText(tr(f"투자금의 {agg['deposit_pct']:.1f}%", f"{agg['deposit_pct']:.1f}% of invested"))

        set_kpi("total_asset", f"{nav:,.0f}")
        set_kpi("cumulative_asset", f"{cumulative_asset:,.0f}")
        set_kpi("total_pl", f"{total_pl:+,.0f}", _pos_color(total_pl),
                 tooltip=tr(f"누적 자산 ({cumulative_asset:,.0f}) - 원금 ({principal:,.0f})", f"Cumulative Assets ({cumulative_asset:,.0f}) - Principal ({principal:,.0f})"))
        set_kpi("total_pl_pct", f"{agg['total_pl_pct']:+.1f}%", _pos_color(agg["total_pl_pct"]))
        set_kpi("cost_basis", f"{agg['cost_total']:,.0f}")
        set_kpi("deposit", None)  # value is the live QLineEdit; only the sub-label updates

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
        query = self._search_edit.text().strip().lower() if hasattr(self, "_search_edit") else ""
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
