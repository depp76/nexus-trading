"""ui/universe_tab.py — UniverseTab (Phase 5 split)

Split out from: main.py MainWindow (2026-08-29 feat/3-1-modularize, Phase 5)
Contains:
  UniverseTab — "Trading Universe" tab: watchlist table, ticker add/search
  controls, and all market-data-refresh orchestration that used to
  live directly on MainWindow.

Cross-tab communication (mirrors the TradingHistoryTab pattern from Phase 4):
  status_text_changed — this tab's own footer line (footer_lbl) under the
    table (docs/ui.md 8.4; it used to be MainWindow's shared footer row).
  sync_time_changed — MainWindow's header refresh status (dot + last time).
  status_message — short-lived progress text, forwarded to MainWindow's
    native status bar via _on_thread_status_message (same as
    TradingHistoryTab.status_message).
  refresh_started — emitted whenever refresh_data() runs, so MainWindow can
    also reload the Trading History tab (mirrors the original unconditional
    call to trading_history_tab._reload_current() inside refresh_data()).
  auto_lightweight_tick — emitted when the 60s auto-timer takes the
    lightweight-update path, so MainWindow can also trigger the Trading
    History tab's realtime price update (mirrors the original call to
    trading_history_tab._start_realtime_price_update()).
"""
import logging
from collections import Counter
from datetime import datetime

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QComboBox, QPushButton, QMessageBox, QInputDialog, QMenu, QCheckBox, QWidgetAction,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer

import paths
from paths import CUSTOM_SETTINGS_FILE
from threads.fetch_threads import (
    SingleStockFetchThread,
    AllDataFetchThread,
    DEFAULT_UNIVERSE_MARKETS,
    UNIVERSE_MARKET_SIZES,
    UniverseLightweightFetchThread,
    GeminiStockReportThread,
)
from threads.strategy_threads import SignalPicksThread, CANCELLED_MESSAGE
from strategy import signal_api
from ui.universe_table import StockTable
from ui.heat_legend import HeatLegend
from ui.filter_popups import RangeFilterPopup
from ui.universe_views import (
    BUILTIN_VIEWS, MARKETS, count_matches, market_display, describe_ranges, describe_view, empty_state as empty_view,
    market_list, migrate_views, normalize as normalize_view, same_state as same_view, view_display_name,
)
from i18n import tr
from ui.dialogs.stock_report import show_stock_report_result
from ui.dialogs.add_ticker import AddTickerDialog
from ui.ma_chart import StockMaLauncherMixin

logger = logging.getLogger(__name__)


from ui.common import (
    _MARKET_ORDER, SearchLineEdit,
    atomic_save_json,
    safe_load_json,
    ThreadOwnerMixin,
    make_button,
    make_footer_label,
    TAB_MARGINS,
    TAB_SPACING,
    fix_toolbar_widths,
    register_toolbar_labels,
)

# Message boxes are titled with the tab name (docs/ui.md 8.5).
_TITLE = "Trading Universe"


def _part_label(part: str) -> str:
    """Display name of a fetch part / row market in status lines: the index
    rows' "Indices"/"Index" key in the UI language, market names as-is."""
    if part in ("Indices", "Index"):
        return tr("지수", part)
    if part == "Unknown":
        return tr("기타", part)
    return market_display(part)


class UniverseTab(StockMaLauncherMixin, ThreadOwnerMixin, QWidget):
    """Trading Universe tab: watchlist table + ticker add/search controls."""

    status_text_changed = pyqtSignal(str)  # -> this tab's footer_lbl
    sync_time_changed = pyqtSignal(str)    # -> MainWindow's header refresh status
    refresh_failed = pyqtSignal(str)       # -> MainWindow's header refresh status (reason)
    status_message = pyqtSignal(str)       # -> MainWindow's native status bar (roadmap 2-3)
    refresh_started = pyqtSignal()         # -> MainWindow also reloads Trading History tab
    auto_lightweight_tick = pyqtSignal()   # -> MainWindow also triggers Trading History realtime update

    def __init__(self, parent=None):
        super().__init__(parent)
        self.all_data = []
        self.market_status = {}
        # Markets switched on in the toolbar: both what a refresh fetches and
        # which rows show (docs/ui.md 2.1). Restored from custom_settings.json
        # once it is loaded; KOSPI + KOSDAQ by default, the US ones off.
        self._markets = set(DEFAULT_UNIVERSE_MARKETS)
        self._markets_pending = False     # a market was switched on while a fetch ran
        self._status_filter_idx = 0      # index into _STATUS_FILTER_STATES
        self._add_market = "KOSPI"       # last market picked in the Add Ticker dialog
        self._ranges = {}                # Filters panel: column key -> [lo, hi] (docs/ui.md 8.9)
        # ticker -> (group 0=Top/1=Bottom, rank, tag, tooltip) of the pinned
        # "전략 신호" run; empty = default order (see _sort_all_data).
        self._signal_order = {}
        # "전략 신호" (user direction 2026-11-16): the strategy variant whose
        # Top / Bottom lists the button pins (the last one picked; nothing is
        # pinned at start, "선택 안 함" is checked).
        self._signal_variant = signal_api.default_variant()
        self._last_picks = None
        # Universe runs by variant id (strategy.signal_api.SignalCache), shared
        # with the MA chart's "전략 점검": a run here shows there, a "계산"
        # there is reused here. Valid for the day; a refresh with new prices clears it.
        self._signal_cache = signal_api.SignalCache()
        # User-added tickers re-fetched after a full refresh (on_finished_all)
        # each arrive on their own thread; rendering once, a beat after the
        # last one lands, instead of a full table rebuild per ticker.
        self._startup_render_timer = QTimer(self)
        self._startup_render_timer.setSingleShot(True)
        self._startup_render_timer.setInterval(150)
        self._startup_render_timer.timeout.connect(self._render_startup_batch)
        self._build_ui()
        self.load_custom_settings()
        self._markets = self._saved_markets()
        self._sync_market_buttons()
        self._update_filters_button()
        self._refresh_views_combo()   # filled even before any data arrives

        # Try to load cached universe data to make startup instant, but always refresh to latest afterwards.
        cached = safe_load_json(paths.UNIVERSE_CACHE_FILE, default=None)
        if cached:
            self.all_data = cached
            self._reload_table()
            self.filter_table()
            self.update_total_status(prefix=tr("저장된 유니버스를 불러왔습니다. 데이터 갱신 중...",
                                                    "Loaded cached universe. Refreshing data..."))

    def _build_ui(self):
        universe_layout = QVBoxLayout(self)
        universe_layout.setContentsMargins(*TAB_MARGINS)
        universe_layout.setSpacing(TAB_SPACING)

        # Toolbar (docs/ui.md 8.4): filters and search on the left, actions on
        # the right, the primary action last. Widgets take the app font and
        # the theme's input/button heights; no per-widget font.
        bar = QHBoxLayout()
        self._build_filter_bar(bar)
        self._build_action_buttons(bar)
        self._unify_button_widths()

        universe_layout.addLayout(bar)

        self._build_table(universe_layout)
        self._build_footer(universe_layout)

    def _build_filter_bar(self, bar: QHBoxLayout) -> None:
        """Left part of the toolbar: search, market toggles, status filter,
        range filters and saved views, then the stretch."""
        self.search_input = SearchLineEdit()     # counts the IME syllable being composed
        self.search_input.setPlaceholderText(tr("종목명 또는 코드 검색", "Search name or ticker"))
        self.search_input.setMinimumWidth(180)
        self.search_input.setMaximumWidth(260)
        self.search_input.searchTextChanged.connect(self.filter_table)
        bar.addWidget(self.search_input)
        # 종목 추가 right of the search box (user direction 2026-11-16): you
        # search for a name first and add it when it is not in the universe.
        self.add_ticker_btn = make_button(tr("종목 추가", "Add Ticker"), on_click=self._on_add_ticker_clicked)
        self.add_ticker_btn.setToolTip(tr("유니버스에 없는 종목을 시장과 코드로 하나 추가합니다",
                                          "Add one name that is not in the universe, by market and ticker"))
        bar.addWidget(self.add_ticker_btn)

        self._build_market_buttons(bar)

        # Status filter: one button cycling 종목(전체) -> 종목(보유) -> 종목(관심)
        # (user direction 2026-11-16; a bare "전체" next to the market toggles
        # read as "all markets"). 전체 = unchecked, 보유/관심 = checked; the
        # width fits the longest label, so a click never resizes it.
        self.tg_filter_btn = self._filter_button(self._STATUS_FILTER_STATES[0][1])
        self.tg_filter_btn.setToolTip(tr("누를 때마다 종목(전체) → 종목(보유) → 종목(관심) 순으로 표의 행을 거릅니다",
                                         "Each click cycles the rows shown: Stocks (All) -> (Held) -> (Target)"))
        self.tg_filter_btn.clicked.connect(self._on_status_filter_clicked)
        bar.addWidget(self.tg_filter_btn)

        # Numeric range filters and saved views (docs/ui.md 2.6 / 8.9).
        bar.addSpacing(self._GROUP_GAP)
        self.filters_btn = make_button(tr("필터", "Filters"), checkable=True, on_click=self._open_range_filters)
        bar.addWidget(self.filters_btn)
        self.views_combo = QComboBox()
        self.views_combo.setMinimumWidth(170)
        self.views_combo.setToolTip(tr("저장된 보기: 시장·상태·검색어·범위 필터 조합과 그 보기의 종목 수",
                                       "Saved views: a market / status / search / range-filter combination, "
                                       "with the number of rows it shows"))
        self.views_combo.activated.connect(self._on_view_activated)
        bar.addWidget(self.views_combo)

        bar.addStretch()

    def _build_market_buttons(self, bar: QHBoxLayout) -> None:
        # Markets (docs/ui.md 2.1): KOSPI/KOSDAQ/NASDAQ/S&P500 toggles, any
        # combination, at least one. A market that is on is fetched by a
        # refresh and shown; switching one on after a refresh fetches just
        # that market (user direction 2026-11-14; replaced the single-choice
        # ALL/KOSPI/KOSDAQ filter). Index rows always show. Every toolbar
        # button shares one width (_unify_button_widths, 2026-11-16).
        bar.addSpacing(self._GROUP_GAP)
        self._market_buttons = {}
        for m, label in MARKETS.items():
            text = self._MARKET_BUTTON_TEXT.get(m, label)
            btn = self._filter_button(text.replace("&", "&&"), checked=(m in DEFAULT_UNIVERSE_MARKETS))
            btn.setToolTip(tr(f"{label}: 켜면 새로고침이 이 시장(상위 {UNIVERSE_MARKET_SIZES[m]}종목)을 받아 와 "
                              "표에 보여 줍니다. 최소 한 시장은 켜져 있어야 합니다.",
                              f"{label}: when on, Refresh fetches this market (top {UNIVERSE_MARKET_SIZES[m]} names) "
                              "and shows it in the table. At least one market must stay on.")
                           + self._MARKET_TOOLTIP_NOTE.get(m, ""))
            btn.clicked.connect(lambda checked, mk=m: self._on_market_toggled(mk, checked))
            bar.addWidget(btn)
            self._market_buttons[m] = btn
        bar.addSpacing(self._GROUP_GAP)   # markets | status filter

    def _build_action_buttons(self, bar: QHBoxLayout) -> None:
        """Right end of the toolbar: 전략 신호, then the primary Refresh (종목
        추가 moved next to the search box, 2026-11-16)."""
        # "전략 신호" (user direction 2026-11-16; was "추세 점수", the C1 row
        # only): pick a strategy variant from the menu and its Top / Bottom
        # lists are pinned to the top of the table (universe_signals.py).
        # Checked while a run is pinned; "선택 안 함" in the menu unpins.
        self.signal_btn = make_button(tr("전략 신호", "Signals"), checkable=True)
        self.signal_btn.setToolTip(tr(
            "구현된 매매전략 중 하나를 골라 KOSPI/KOSDAQ 종목의 매수 후보(상위)와 매도·회피 신호(하위)를 "
            "최대 10개씩 표 맨 위에 고정합니다. 백테스트와 같은 조건 코드로 계산하며, 연구용 신호입니다.",
            "Pick one of the implemented strategies: its buy candidates (top) and sell / avoid signals "
            "(bottom), up to ten each, are pinned to the top of the table. Same signal code as the "
            "backtest; research signals, not advice."))
        self._signal_menu = self._build_signal_menu()
        self.signal_btn.setMenu(self._signal_menu)
        bar.addWidget(self.signal_btn)
        bar.addSpacing(self._GROUP_GAP)   # analysis | primary action

        # Refresh is this tab's one primary action (docs/ui.md 1.6 / 8.1).
        self.refresh_btn = make_button(tr("새로고침", "Refresh"), "primary", self.refresh_data)
        bar.addWidget(self.refresh_btn)

    def _build_table(self, universe_layout: QVBoxLayout) -> None:
        self.table = StockTable()
        self.table.col_filter_changed.connect(self.filter_table)
        self.table.ai_report_requested.connect(self._on_ai_report_requested)
        self.table.ma_chart_requested.connect(self._on_ma_chart_requested)
        self.table.delete_requested.connect(self.delete_stock)
        self.table.user_sorted.connect(self._on_user_sorted)
        self.table.toggle_requested.connect(self.toggle_stock)

        universe_layout.addWidget(self.table, 1)

    def _build_footer(self, universe_layout: QVBoxLayout) -> None:
        # Footer: this tab's lasting status line (load/progress/Trend Score
        # results). It used to be MainWindow's shared footer row, visible on
        # every tab though only this tab wrote to it (docs/ui.md 8.4).
        self.footer_lbl = make_footer_label()
        self.status_text_changed.connect(self.footer_lbl.setText)
        footer = QHBoxLayout()
        footer.addWidget(self.footer_lbl, 1)
        # Heatmap legend, always visible under the table (docs/ui.md 1.2).
        self.heat_legend = HeatLegend()
        footer.addWidget(self.heat_legend, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        universe_layout.addLayout(footer)

    _FILTER_BTN_W = 80
    _GROUP_GAP = 12   # between toolbar groups (docs/ui.md 8.17: 12~16px)
    # Toggle text that differs from the MARKETS display name: the NASDAQ toggle
    # fetches the 150 largest NASDAQ-listed names, so it reads "NASDAQ 150"
    # (user direction 2026-11-18, docs/ui.md 8.32; was "NASDAQ", 2026-11-16).
    # Every toolbar button takes the widest registered label's width
    # (_unify_button_widths), so the other buttons follow this label.
    _MARKET_BUTTON_TEXT = {"NASDAQ 100": "NASDAQ 150"}
    # Toggle tooltips that need more than the generic sentence.
    _MARKET_TOOLTIP_NOTE = {
        "NASDAQ 100": tr(" 나스닥100 지수 구성이 아니라 NASDAQ 상장 종목 중 시총 상위이며 해외 기업 ADR(SKHY, ASML 등)도 포함합니다.",
                         " Not the Nasdaq-100 index: the largest NASDAQ-listed names, foreign ADRs (SKHY, ASML, ...) included."),
    }
    # (highlight status to show, button label): "" = no status filter. The
    # status values are the ones custom_settings.json stores ("On" = Held,
    # "Tg" = Target; docs/ui.md 1.7).
    _STATUS_FILTER_STATES = (("", tr("종목(전체)", "Stocks (All)")), ("On", tr("종목(보유)", "Stocks (Held)")),
                             ("Tg", tr("종목(관심)", "Stocks (Target)")))

    @classmethod
    def _filter_button(cls, text: str, checked: bool = False) -> QPushButton:
        # checked state: ui/theme.py's QPushButton:checked rule
        return make_button(text, width=cls._FILTER_BTN_W, checkable=True, checked=checked)

    def _unify_button_widths(self) -> None:
        """Give every toolbar button the one width shared with the History
        and Total Assets toolbars (ui.common.toolbar_button_width; user
        direction 2026-11-16): the widest label any of them can show, so no
        label change resizes a button. This tab's labels are registered at
        the bottom of this module."""
        fix_toolbar_widths([*self._market_buttons.values(), self.tg_filter_btn, self.filters_btn,
                            self.add_ticker_btn, self.signal_btn, self.refresh_btn])

    # ---Markets (docs/ui.md 2.1) ---
    def selected_markets(self) -> list:
        return [m for m in MARKETS if m in self._markets]

    def _saved_markets(self) -> set:
        saved = market_list(self.custom_settings.get("markets"))
        return set(DEFAULT_UNIVERSE_MARKETS) if saved == "ALL" else set(saved)

    def _sync_market_buttons(self):
        for m, btn in self._market_buttons.items():
            btn.setChecked(m in self._markets)

    def _on_market_toggled(self, market: str, checked: bool):
        if not checked and self._markets == {market}:
            self._market_buttons[market].setChecked(True)     # at least one market stays on
            return
        self.set_markets(self._markets | {market} if checked else self._markets - {market})

    def set_markets(self, markets, refilter: bool = True):
        """Switch the markets on/off: remembered in custom_settings.json; a
        market switched off drops its rows (user-added tickers stay, hidden),
        one switched on is fetched unless its rows are already here."""
        picked = market_list(markets)
        markets = set(DEFAULT_UNIVERSE_MARKETS) if picked == "ALL" else set(picked)
        removed = self._markets - markets
        self._markets = markets
        self._sync_market_buttons()
        self.custom_settings["markets"] = self.selected_markets()
        self.save_custom_settings()
        if removed:
            self.all_data = self._rows_for_markets(self.all_data)
            self._sort_all_data()
            self._reload_table()
            self._save_universe_cache()
        if refilter:
            self.filter_table()
        self._fetch_missing_markets()

    def _fetch_missing_markets(self, only=None):
        """Fetch the switched-on markets that have no rows yet (only those
        rows, no indices); `only` limits it to those markets. Waits for a
        running full refresh, which calls this again for the markets switched
        on while it ran (a market that refresh itself failed to load is not
        retried here); a second switch while one fetch runs is picked up when
        it ends."""
        if not self.refresh_btn.isEnabled():
            return
        running = getattr(self, "_market_fetch_thread", None)
        if running is not None and running.isRunning():
            self._markets_pending = True
            return
        # A user-added ticker alone does not make its market "loaded".
        added = self._added_tickers()
        present = {x.get("market") for x in self.all_data if x.get("ticker") not in added}
        missing = [m for m in self.selected_markets() if m not in present and (only is None or m in only)]
        if not missing:
            return
        for m in missing:
            self.market_status[m] = tr("대기", "Waiting")
        thread = AllDataFetchThread(markets=missing, include_indices=False)
        self._track_thread(thread, "_market_fetch_thread")
        thread.market_progress.connect(self.on_market_progress)
        thread.failed.connect(self._on_market_fetch_failed)
        thread.finished_all.connect(self._on_markets_fetched)
        thread.start()

    def _on_market_fetch_failed(self, reason: str, parts: list):
        shown = ", ".join(_part_label(p) for p in parts)
        self.status_text_changed.emit(tr(f"{shown}을(를) 불러오지 못했습니다 ({reason}).",
                                         f"Could not load {', '.join(parts)} ({reason})."))
        self.refresh_failed.emit(tr(f"시장 받기: {reason}", f"market fetch: {reason}"))

    def _added_tickers(self) -> set:
        return {x.get("ticker") for x in self.custom_settings.get("added", [])}

    def _rows_for_markets(self, rows: list) -> list:
        """The rows the switched-on markets keep: index rows, rows of an
        on-market, and user-added tickers (hidden while their market is off).
        A row of an off-market that also belongs to an on-market (`also_in`:
        AAPL in NASDAQ 100 and S&P500) moves under that market instead of
        going."""
        added = self._added_tickers()
        out = []
        for x in rows:
            market = x.get("market")
            if market == "Index" or market in self._markets:
                out.append(x)
                continue
            other = next((m for m in MARKETS if m in self._markets and m in x.get("also_in", ())), None)
            if other is not None:
                x["also_in"] = [market] + [m for m in x.get("also_in", []) if m not in (market, other)]
                x["market"] = other
                out.append(x)
            elif x.get("ticker") in added:
                out.append(x)
        return out

    def _on_markets_fetched(self, data: list):
        """Rows of markets switched on after a refresh, merged in place. A
        name that is already here records the new market in `also_in`."""
        # This fetch is over even if its QThread has not quite returned from
        # run() yet (a queued finished_all can arrive first): forget it, so a
        # market switched on meanwhile is fetched below instead of waiting on
        # it forever. The object stays in _tracked_threads for shutdown.
        self._market_fetch_thread = None
        deleted = set(self.custom_settings.get("deleted", []))
        by_ticker = {x.get("ticker"): x for x in self.all_data}
        for x in data:
            old = by_ticker.get(x.get("ticker"))
            market = x.get("market")
            if old is not None and old.get("market") not in ("Index", market) \
                    and market not in old.get("also_in", []):
                old.setdefault("also_in", []).append(market)
        have = set(by_ticker)
        new = [x for x in data if x.get("market") in self._markets
               and x.get("ticker") not in deleted and x.get("ticker") not in have]
        if new:
            self.all_data.extend(new)
            self._sort_all_data()
            self._reload_table()
            self.filter_table()
            self._save_universe_cache()
            self.update_total_status(prefix=tr(f"{len(new)}행을 불러왔습니다.", f"Loaded {len(new)} rows."))
        if self._markets_pending:
            self._markets_pending = False
            self._fetch_missing_markets()

    @property
    def _status_filter(self) -> str:
        return self._STATUS_FILTER_STATES[self._status_filter_idx][0]

    def _on_status_filter_clicked(self):
        """Advance the status filter: 종목(전체) -> 종목(보유) -> 종목(관심) -> 종목(전체)."""
        self._set_status_filter((self._status_filter_idx + 1) % len(self._STATUS_FILTER_STATES))
        self.filter_table()

    def _set_status_filter(self, idx: int):
        self._status_filter_idx = idx
        status, label = self._STATUS_FILTER_STATES[idx]
        self.tg_filter_btn.setText(label)
        self.tg_filter_btn.setChecked(bool(status))   # override the click's own toggle

    def load_custom_settings(self):
        self.custom_settings = {"added": [], "deleted": [], "highlights": {}}
        data = safe_load_json(CUSTOM_SETTINGS_FILE, default={})
        if data:
            self.custom_settings.update(data)
            # Column-group and density toggles were removed on 2026-09-19
            # (user direction); drop their saved state so it does not linger.
            for stale in ("column_groups", "density"):
                self.custom_settings.pop(stale, None)
            # Migration (2026-11-18): saved views filtering "MA50 Div" now filter "MA60 Div".
            if migrate_views(self.custom_settings.get("views")):
                self.save_custom_settings()
                QTimer.singleShot(0, self._announce_view_migration)
            # Migration: old array "highlighted" -> dict "highlights"
            if isinstance(self.custom_settings.get("highlighted"), list):
                hl_dict = self.custom_settings.setdefault("highlights", {})
                for t in self.custom_settings["highlighted"]:
                    hl_dict[t] = "Tg"
                del self.custom_settings["highlighted"]
                self.save_custom_settings()

    def _announce_view_migration(self):
        """Once, after the window is wired: saved views moved from MA50 Div to MA60 Div."""
        self.status_message.emit(tr("저장된 보기의 'MA50 Div' 필터를 'MA60 Div'로 옮겼습니다 (추세선 60일, 스펙 v07).",
                                    "Saved views: the 'MA50 Div' filter now applies to 'MA60 Div' (60-day trend line, spec v07)."))

    def save_custom_settings(self):
        try:
            atomic_save_json(CUSTOM_SETTINGS_FILE, self.custom_settings)
        except Exception:
            logger.warning("Failed to save custom_settings.json", exc_info=True)

    # ---Table reload ---
    def _reload_table(self):
        """Rebuild the table from self.all_data. Call after anything that
        replaces all_data wholesale (cache load, add, delete, full refresh)
        -- the incremental lightweight-refresh path has its own leaner
        version in _on_universe_lightweight_loaded. Index/yield/commodity
        rows are ordinary rows here (user direction 2026-09-19; the
        docs/ui.md 2.4 Market rail they used to live in is gone)."""
        self.table.load_data(self.all_data, self.custom_settings.get("highlights", {}))

    def delete_stock(self, ticker):
        reply = QMessageBox.question(
            self, _TITLE,
            tr(f"'{ticker}'을(를) 유니버스에서 제거할까요?", f"Remove '{ticker}' from the universe?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # Use in-memory settings (no extra disk reload needed)
        if ticker not in self.custom_settings.setdefault("deleted", []):
            self.custom_settings["deleted"].append(ticker)
        self.custom_settings["added"] = [
            x for x in self.custom_settings.get("added", []) if x["ticker"] != ticker
        ]
        self.save_custom_settings()

        self.all_data = [x for x in self.all_data if x.get("ticker", "") != ticker]
        self._reload_table()
        self.filter_table()
        self.update_total_status(prefix=tr(f"'{ticker}'을(를) 삭제했습니다.", f"Deleted '{ticker}'."))

    def toggle_stock(self, ticker):
        """Cycles a ticker's highlight state -/On/Tg (docs/ui.md 1.7: shown
        as the fixed-size blank/Port/Target button in the identity cell,
        which cycles on click; the table's context menu does the same)."""
        highlights = self.custom_settings.setdefault("highlights", {})
        current = highlights.get(ticker, "-")

        if current == "-":
            highlights[ticker] = "On"
        elif current == "On":
            highlights[ticker] = "Tg"
        else:
            highlights.pop(ticker, None)

        self.save_custom_settings()
        self.table.update_row_status(ticker, highlights.get(ticker, "-"))

    def _on_add_ticker_clicked(self):
        dlg = AddTickerDialog(self._add_market, self)
        if dlg.exec() != AddTickerDialog.DialogCode.Accepted:
            return
        market, ticker = dlg.values()
        self._add_market = market
        self.add_ticker(market, ticker)

    def add_ticker(self, market: str, ticker: str):
        """Fetch one name typed in the Add Ticker dialog and add it to the table."""
        ticker = ticker.strip().upper()
        if not ticker:
            return

        self.load_custom_settings()

        existing = {d['ticker'] for d in self.all_data}
        if ticker in existing:
            self.status_text_changed.emit(tr(f"'{ticker}'은(는) 이미 표에 있습니다.",
                                             f"'{ticker}' is already in the table."))
            return

        if ticker in self.custom_settings.get("deleted", []):
            self.custom_settings["deleted"].remove(ticker)
            self.save_custom_settings()

        self.add_ticker_btn.setEnabled(False)
        self.status_text_changed.emit(tr(f"{market}에서 '{ticker}' 받는 중...",
                                         f"Fetching '{ticker}' from {market}..."))

        thread = self._track_thread(SingleStockFetchThread(market, ticker), '_single_fetch_thread')
        thread.finished.connect(self.on_single_stock_loaded)
        thread.start()

    def on_single_stock_loaded(self, result, error, ticker=""):
        """SingleStockFetchThread.finished for the Add button."""
        self._handle_single_stock_loaded(result, error, is_startup=False, ticker_hint=ticker)

    def _on_startup_stock_loaded(self, result, error, ticker=""):
        """SingleStockFetchThread.finished for user-added tickers re-fetched after a
        full refresh (on_finished_all); a failure removes the ticker from settings."""
        self._handle_single_stock_loaded(result, error, is_startup=True, ticker_hint=ticker)

    def _handle_single_stock_loaded(self, result, error, is_startup, ticker_hint):
        if not is_startup:
            self.add_ticker_btn.setEnabled(True)
        if error or result is None:
            if not is_startup:
                self.status_text_changed.emit(tr(f"오류: {error}", f"Error: {error}") if error
                                              else tr("종목을 찾지 못했습니다.", "Stock not found."))
            else:
                label = ticker_hint or (result.get('ticker', '') if result else '?')
                logger.warning("[Startup] Added ticker '%s' failed to load: %s", label, error or "No data returned")
                if label:
                    self.load_custom_settings()
                    added_list = self.custom_settings.get("added", [])
                    new_added = [x for x in added_list if x.get("ticker") != label]
                    if len(new_added) != len(added_list):
                        self.custom_settings["added"] = new_added
                        self.save_custom_settings()
                        logger.info("[Startup] Cleaned up invalid ticker '%s' from custom settings", label)
            return

        self.load_custom_settings()
        ticker = result.get('ticker', '')
        if ticker in self.custom_settings.get("deleted", []):
            return
        if any(x.get('ticker') == ticker for x in self.all_data):
            if not is_startup:
                self.status_text_changed.emit(tr(f"'{ticker}'은(는) 이미 표에 있습니다.",
                                                 f"'{ticker}' is already in the table."))
            return

        target_market = result.get('market', '') or self._add_market
        added_list = self.custom_settings.setdefault("added", [])
        if not any(x.get("ticker") == ticker for x in added_list):
            added_list.append({"market": target_market, "ticker": ticker})
            self.save_custom_settings()

        self.all_data.append(result)
        if is_startup:
            self._startup_render_timer.start()   # (re)arms: one render after the last arrival
            return

        self._sort_all_data()
        self._reload_table()
        self.filter_table()  # restore filter state
        self.update_total_status(prefix=tr(f"'{result.get('name', ticker)}' ({ticker})을(를) 추가했습니다.",
                                                   f"Added '{result.get('name', ticker)}' ({ticker})."))
        self._save_universe_cache()

    def _render_startup_batch(self):
        self._sort_all_data()
        self._reload_table()
        self.filter_table()
        self._save_universe_cache()

    def _save_universe_cache(self):
        """Persist ``all_data`` so the next start's instant table already holds
        the user-added tickers. on_finished_all() wrote the cache before those
        were re-fetched, so they were missing until the next full refresh
        (review_agy.md 2.4, 2026-09-29)."""
        if not self.all_data:
            # Never overwrite the last good cache with an empty table (a
            # refresh that fetched nothing and had nothing to fall back on).
            return
        try:
            # paths.UNIVERSE_CACHE_FILE is read here, not imported, so tests can point it elsewhere.
            atomic_save_json(paths.UNIVERSE_CACHE_FILE, self.all_data)
        except Exception as e:
            logger.warning("Error caching universe data: %s", e, exc_info=True)

    @staticmethod
    def _sort_key(item):
        return (
            0 if item.get('is_index') else 1,
            item.get('index_order', 99) if item.get('is_index') else _MARKET_ORDER.get(item.get('market', ''), 99),
            -float(item.get('market_cap', 0) or 0),
        )

    def _order_key(self, item):
        entry = self._signal_order.get(item.get('ticker'))
        if entry is not None:
            return (0, entry[0], entry[1])
        return (1,) + self._sort_key(item)

    def _sort_all_data(self):
        """Index rows first (in their fixed order), then each market by
        market cap descending. While a Trend Score run is pinned
        (_signal_order), its Top rows come first in rank order, then
        its Bottom rows, then everything else in that default order; the
        pinned items get a `signal_tag` label the identity cell shows after
        the market, every other item loses it."""
        order = self._signal_order
        for item in self.all_data:
            entry = order.get(item.get('ticker'))
            if entry is None:
                item.pop('signal_tag', None)
                item.pop('signal_tip', None)
            else:
                item['signal_tag'] = entry[2]
                if len(entry) > 3:
                    item['signal_tip'] = entry[3]
        self.all_data.sort(key=self._order_key)

    def filter_table(self, text=None):
        if text is None:
            text = self.search_input.search_text()
        self.table.apply_col_filters(text, status=self._status_filter, market=self.selected_markets(),
                                     ranges=self._ranges)
        self._update_filters_button()
        self._refresh_views_combo()

    # ---Range filters and saved views (docs/ui.md 2.6 / 8.9) ---
    def current_view_state(self) -> dict:
        return normalize_view({"market": self.selected_markets(), "status": self._status_filter,
                               "search": self.search_input.text(), "ranges": self._ranges})

    def apply_view_state(self, state: dict):
        """Set every filter control from a view state and re-filter once."""
        state = normalize_view(state)
        # "ALL" (built-in views, clear filters) leaves the markets as they are.
        if state["market"] != "ALL" and set(state["market"]) != self._markets:
            self.set_markets(state["market"], refilter=False)
        self._status_filter_idx = next(
            (i for i, (s, _) in enumerate(self._STATUS_FILTER_STATES) if s == state["status"]), 0)
        self._set_status_filter(self._status_filter_idx)
        self.search_input.blockSignals(True)
        self.search_input.setText(state["search"])
        self.search_input.blockSignals(False)
        self._ranges = state["ranges"]
        self.filter_table()


    # ---Language switch (docs/ui.md 8.20): MainWindow rebuilds every tab in
    # the new language and carries this state over. Plain values only (the
    # ui classes themselves are re-imported).
    def export_state(self) -> dict:
        return {"all_data": list(self.all_data), "view": self.current_view_state(),
                "picks": self._last_picks, "signal_cache": self._signal_cache,
                "pinned": bool(self._signal_order), "signal_variant": self._signal_variant,
                "refreshing": not self.refresh_btn.isEnabled()}

    def import_state(self, state: dict) -> None:
        if state.get("all_data"):
            self.all_data = list(state["all_data"])
            self._sort_all_data()            # also drops the old Top/Bottom tags
            self._reload_table()
        self.apply_view_state(state["view"])
        if state.get("signal_cache") is not None:
            self._signal_cache = state["signal_cache"]
        self._signal_variant = state.get("signal_variant", self._signal_variant)
        picks = state.get("picks")
        if picks is not None and state.get("pinned"):
            self._on_signals_finished(picks, "")   # re-pin with tags in the new language

    def _update_filters_button(self):
        n = len(self._ranges)
        self.filters_btn.setText(tr(f"필터 ({n})", f"Filters ({n})") if n else tr("필터", "Filters"))
        self.filters_btn.setChecked(bool(n))
        self.filters_btn.setToolTip(describe_ranges(self._ranges) if n else
                                    tr("숫자 범위 필터: 시총, PER, 등락, 52주 위치, MA Div, 모멘텀",
                                       "Numeric range filters: Cap, PER, change, 52W position, MA Div, momentum"))

    def _open_range_filters(self):
        self._update_filters_button()          # undo the click's own toggle
        popup = RangeFilterPopup(self._ranges)
        popup.applied.connect(self._on_ranges_applied)
        popup.move(self.filters_btn.mapToGlobal(self.filters_btn.rect().bottomLeft()))
        popup.show()
        self._range_popup = popup              # keep a reference while it is open

    def _on_ranges_applied(self, ranges: dict):
        self._ranges = normalize_view({"ranges": ranges})["ranges"]
        self.filter_table()

    def _user_views(self) -> list:
        return self.custom_settings.setdefault("views", [])

    def _all_views(self) -> list:
        return [dict(v, builtin=True) for v in BUILTIN_VIEWS] + [dict(v, builtin=False) for v in self._user_views()]

    def _refresh_views_combo(self):
        """Rebuild the Views list with each view's live row count; select the
        view that matches the current filters, if any."""
        rows = self.table.all_row_values()
        current = self.current_view_state()

        def resolved(view):
            # A view with market "ALL" means "the markets that are on".
            return dict(view, market=current["market"]) if market_list(view.get("market")) == "ALL" else view

        is_filtered = not same_view(current, resolved(empty_view()))
        combo = self.views_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr("저장된 보기", "Views"), None)
        active = 0
        for v in self._all_views():
            combo.addItem(f"{view_display_name(v['name'])}  ({count_matches(rows, resolved(v))})", ("view", v["name"]))
            tip = describe_view(v)
            combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
            if is_filtered and active == 0 and same_view(resolved(v), current):
                active = combo.count() - 1
        combo.insertSeparator(combo.count())
        if is_filtered:
            combo.addItem(tr("전체 보기 (필터 해제)", "Show all (clear filters)"), ("clear",))
            combo.addItem(tr("현재 필터를 보기로 저장...", "Save current filters as a view..."), ("save",))
        if self._user_views():
            combo.addItem(tr("저장된 보기 삭제...", "Delete a saved view..."), ("delete",))
        combo.setCurrentIndex(active)
        combo.blockSignals(False)

    def _on_view_activated(self, index: int):
        data = self.views_combo.itemData(index)
        if data is None:
            self._refresh_views_combo()
            return
        kind = data[0]
        if kind == "view":
            view = next((v for v in self._all_views() if v["name"] == data[1]), None)
            if view is not None:
                self.apply_view_state(view)
                return
        elif kind == "clear":
            self.apply_view_state(empty_view())
            return
        elif kind == "save":
            self._save_current_view()
        elif kind == "delete":
            self._delete_view()
        self._refresh_views_combo()

    def _save_current_view(self):
        name, ok = QInputDialog.getText(self, _TITLE, tr("보기 이름:", "Name for this view:"))
        name = (name or "").strip()
        if not ok or not name:
            return
        if any(name in (v["name"], view_display_name(v["name"])) for v in BUILTIN_VIEWS):
            QMessageBox.warning(self, _TITLE, tr(f"'{name}'은(는) 기본 보기 이름입니다. 다른 이름을 입력하세요.",
                                                 f"'{name}' is a built-in view. Choose another name."))
            return
        views = self._user_views()
        existing = next((i for i, v in enumerate(views) if v["name"] == name), None)
        if existing is not None:
            reply = QMessageBox.question(self, _TITLE, tr(f"저장된 보기 '{name}'을(를) 덮어쓸까요?", f"Replace the saved view '{name}'?"),
                                         QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                         QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                return
        view = dict(self.current_view_state(), name=name)
        if existing is None:
            views.append(view)
        else:
            views[existing] = view
        self.save_custom_settings()
        self.status_message.emit(tr(f"보기 '{name}' 저장함", f"Saved view '{name}'"))

    def _delete_view(self):
        names = [v["name"] for v in self._user_views()]
        if not names:
            return
        name, ok = QInputDialog.getItem(self, _TITLE, tr("삭제할 보기:", "Delete which saved view?"), names, 0, False)
        if not ok or not name:
            return
        reply = QMessageBox.question(self, _TITLE, tr(f"저장된 보기 '{name}'을(를) 삭제할까요?", f"Delete the saved view '{name}'?"),
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                     QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        self.custom_settings["views"] = [v for v in self._user_views() if v["name"] != name]
        self.save_custom_settings()
        self.status_message.emit(tr(f"보기 '{name}' 삭제함", f"Deleted view '{name}'"))

    # ---Per-stock MA (20 + 60) ---
    def _on_ma_chart_requested(self, ticker: str):
        """StockTable.ma_chart_requested (double-click/Enter/context menu on
        a row) -- replaces the old per-row 📈 button, which carried its own
        name/market/change_mode via the callback closure; those are looked
        up from all_data instead now."""
        item = next((d for d in self.all_data if d.get('ticker') == ticker), None)
        if item is None:
            return
        self._show_stock_ma(ticker, item.get('name', ticker), item.get('market', ''), item.get('change_mode', 'pct'))

    def _on_stock_ma_status(self, msg: str):
        self.status_text_changed.emit(msg)

    # ---AI Stock Report (roadmap 2-1, review.md 2-1) ---
    def _on_ai_report_requested(self, ticker: str):
        item = next((d for d in self.all_data if d.get('ticker') == ticker), None)
        if item is None:
            QMessageBox.warning(self, _TITLE, tr(f"AI 종목 리포트: {ticker}의 데이터를 찾지 못했습니다.",
                                                 f"AI Stock Report: no data found for {ticker}."))
            return
        name = item.get('name', ticker)
        self.status_text_changed.emit(tr(f"{name} ({ticker}) AI 리포트 생성 중...",
                                         f"Generating AI report for {name} ({ticker})..."))
        thread = self._track_thread(GeminiStockReportThread(item))
        thread.finished.connect(self._on_stock_report_ready)
        thread.start()

    def _on_stock_report_ready(self, ticker: str, name: str, result_text: str, error: str):
        if error:
            self.status_text_changed.emit(tr(f"{name} ({ticker}) AI 리포트 생성 실패.",
                                             f"AI report failed for {name} ({ticker})."))
            QMessageBox.warning(self, _TITLE, tr(f"AI 종목 리포트 생성 실패:\n{error}", f"AI Stock Report failed:\n{error}"))
            return
        self.status_text_changed.emit(tr(f"{name} ({ticker}) AI 리포트 준비됨.",
                                         f"AI report ready for {name} ({ticker})."))
        show_stock_report_result(self, ticker, name, result_text)

    # ---"전략 신호": a registered strategy's current lists (strategy.signal_api) ---
    SIGNAL_TOP_N = 10

    def _build_signal_menu(self) -> QMenu:
        """A check box per strategy row (user direction 2026-11-16: "체크박스로
        선택"), with "선택 안 함" first; exactly one is checked. Ticking a
        strategy runs it and pins its lists, ticking "선택 안 함" unpins. The
        row titles are the Strategy tab's variant titles."""
        menu = QMenu(self)
        panel = QWidget()
        col = QVBoxLayout(panel)
        col.setContentsMargins(12, 8, 16, 8)
        col.setSpacing(6)
        self._signal_none_check = QCheckBox(tr("선택 안 함", "None"))
        self._signal_none_check.setChecked(True)
        self._signal_none_check.clicked.connect(lambda _c: self._on_signal_check(None))
        col.addWidget(self._signal_none_check)
        self._signal_checks = {}
        for vid in signal_api.variants():      # every registered strategy (strategy/signal_api.py)
            cb = QCheckBox(f"{vid}  {signal_api.title(vid)}")
            cb.clicked.connect(lambda _c, v=vid: self._on_signal_check(v))
            col.addWidget(cb)
            self._signal_checks[vid] = cb
        action = QWidgetAction(menu)
        action.setDefaultWidget(panel)
        menu.addAction(action)
        return menu

    def _sync_signal_checks(self, vid):
        """Tick `vid` (None = "선택 안 함") and untick the rest."""
        self._signal_none_check.setChecked(vid is None)
        for v, cb in self._signal_checks.items():
            cb.setChecked(v == vid)

    def _on_signal_check(self, vid):
        """A box was ticked: run that strategy (replacing a pinned one), or unpin."""
        self._signal_menu.close()
        if vid is None:
            if self._signal_order:
                self._clear_signals()
            self._sync_signal_checks(None)
            return
        if vid == self._signal_variant and self._signal_order:
            self._sync_signal_checks(vid)        # already pinned: ticking it again keeps it
            return
        self._signal_variant = vid
        self._sync_signal_checks(vid)
        if self._signal_order:
            self._signal_order = {}
            self._apply_signal_order()
        self._on_signal_clicked()

    def _signal_context(self):
        """What an MA chart opened from here gets for its "전략 점검": the
        Universe rows, the shared universe runs, the pinned variant (else the
        default), and this tab's thread bookkeeping."""
        from ui.strategy_check_section import SignalContext
        return SignalContext(items=list(self.all_data), cache=self._signal_cache,
                             default_variant=self._signal_variant if self._signal_order else "",
                             track=self._track_thread, generation=self._signal_cache.generation)

    def _signal_items(self, vid: str) -> list:
        """The rows a run of `vid` takes: the markets its strategy applies to
        (strategy.signal_api.markets), no index/yield/commodity rows."""
        return signal_api.universe_rows(self.all_data, vid)

    def _on_signal_clicked(self):
        """Not pinned -> score the KR rows and pin the Top/Bottom lists to the
        top of the table (the button turns checked when the run succeeds);
        pinned -> unpin and restore the default order."""
        if self._signal_order:
            self._clear_signals()
            return
        self.signal_btn.setChecked(False)   # checked means "pinned"
        vid = self._signal_variant
        items = self._signal_items(vid)
        if not items:
            self._sync_signal_checks(None)
            ms = "/".join(signal_api.markets(vid))
            self.status_text_changed.emit(tr(f"전략 신호: 계산할 {ms} 종목이 없습니다 -- 먼저 유니버스를 새로고침하세요",
                                             f"Signals: no {ms} rows to compute -- Refresh the universe first"))
            return
        cached = self._signal_cache.get(vid)
        if cached is not None:                  # today's run (here or from an MA chart's "계산")
            self._on_signals_finished(cached, "")
            return
        self.signal_btn.setEnabled(False)
        self.status_text_changed.emit(tr(f"{vid} 전략 신호 계산 중 ({len(items)}종목)...",
                                         f"Computing {vid} signals ({len(items)} names)..."))
        thread = self._track_thread(SignalPicksThread(items, vid, n_top=self.SIGNAL_TOP_N),
                                    "_signal_thread")
        thread.cache_generation = self._signal_cache.generation    # the rows `items` came from
        thread.progress.connect(self._on_signals_progress)
        thread.finished.connect(self._on_signals_finished)
        thread.start()

    def _on_signals_progress(self, text: str):
        self.status_text_changed.emit(tr(f"전략 신호: {text}", f"Signals: {text}"))

    def _on_signals_finished(self, picks, error: str, generation: int | None = None):
        """`picks`: universe_signals.SignalPicks (None on failure). A run whose
        rows were replaced by a refresh since it started is dropped
        (SignalCache.generation; review_codex.md 2026-11-18 #3)."""
        self.signal_btn.setEnabled(True)
        if generation is None:
            generation = getattr(self.sender(), "cache_generation", None)
        if picks is not None and not self._signal_cache.is_current(generation):
            self.signal_btn.setChecked(False)
            self._sync_signal_checks(None)
            self.status_text_changed.emit(tr(
                f"{picks.variant_id} 전략 신호: 계산 중에 유니버스가 새로 고쳐져 결과를 버렸습니다 -- 다시 누르세요",
                f"{picks.variant_id} signals: the universe was refreshed while computing; result dropped -- press again"))
            return
        if picks is None:
            self.signal_btn.setChecked(False)
            self._sync_signal_checks(None)
            if error != CANCELLED_MESSAGE:
                QMessageBox.warning(self, _TITLE, tr(f"전략 신호 계산 실패:\n{error}", f"Signals failed:\n{error}"))
            self.status_text_changed.emit(tr(f"전략 신호: {error}", f"Signals: {error}"))
            return
        vid = picks.variant_id
        words = signal_api.labels(vid)
        # Short tags so the name stays readable ("A0 상위 1", user direction
        # 2026-11-16); the figure the list is ordered by is on the tooltip.
        order = {}
        for i, s in enumerate(picks.top, 1):
            order[s.ticker] = (0, i, f"{vid} {words.top_tag} {i}", s.value_label)
        for i, s in enumerate(picks.bottom, 1):
            order[s.ticker] = (1, i, f"{vid} {words.bottom_tag} {i}", s.value_label)
        self._signal_order = order
        self._last_picks = picks
        if self._signal_cache.get(vid) is not picks:
            self._signal_cache.put(picks)
        self._signal_variant = vid
        self._sync_signal_checks(vid)
        self.signal_btn.setChecked(True)
        self._apply_signal_order()
        self.status_text_changed.emit(self._signal_status(picks))

    @staticmethod
    def _signal_status(picks) -> str:
        """One status line: what the two lists are, how many met the
        condition, and the market regime when the row uses it."""
        vid = picks.variant_id
        words = signal_api.labels(vid)
        top_what, bottom_what = words.top_what, words.bottom_what
        regime = ""
        if picks.uses_regime and picks.regime_on is not None and words.regime_name:
            regime = (tr(f" · {words.regime_name} 켜짐", f" · {words.regime_name} on") if picks.regime_on else
                      tr(f" · {words.regime_name} 꺼짐: 전략상 신규 매수 보류",
                         f" · {words.regime_name} off: no new entries under this strategy"))
        return tr(
            f"{vid} 전략 신호 {picks.as_of:%Y-%m-%d} 기준: {top_what} {len(picks.top)}/{picks.n_top_candidates}, "
            f"{bottom_what} {len(picks.bottom)}/{picks.n_bottom_candidates} 고정 (유니버스 {picks.n_universe}, "
            f"유동성 통과 {picks.n_liquid}){regime}. 연구용 신호이며 매매 권유가 아닙니다.",
            f"{vid} signals as of {picks.as_of:%Y-%m-%d}: {top_what} {len(picks.top)}/{picks.n_top_candidates}, "
            f"{bottom_what} {len(picks.bottom)}/{picks.n_bottom_candidates} pinned (universe {picks.n_universe}, "
            f"liquid {picks.n_liquid}){regime}. Research signals, not advice.")

    def _on_user_sorted(self):
        """A header click re-sorts the whole table, which would scatter the
        pinned rows; treat it as leaving the Trend Score order (button
        unchecked, tags removed) instead of showing a stale pin. The user's
        sort stays (review_agy.md 2.2, 2026-09-29)."""
        if not self._signal_order:
            return
        self._signal_order = {}
        self.signal_btn.setChecked(False)
        self._sort_all_data()      # strips the signal_tag tags, default data order
        self._reload_table()       # re-applies the user's sort
        self.filter_table()
        self._sync_signal_checks(None)
        self.status_text_changed.emit(tr("전략 신호 순서 해제 (열 정렬).", "Signals order cleared (column sort)."))

    def _clear_signals(self):
        self._signal_order = {}
        self._sync_signal_checks(None)
        self.signal_btn.setChecked(False)
        self._apply_signal_order()
        self.status_text_changed.emit(tr("전략 신호 순서 해제, 기본 순서로 복원.",
                                         "Signals order cleared; default order restored."))

    def _apply_signal_order(self):
        """Re-sort all_data (pinned rows first, if any), drop the user's
        column sort so the data order is what shows, and rebuild the table."""
        self._sort_all_data()
        self.table.reset_sort()
        self._reload_table()
        self.filter_table()
        self.table.scrollToTop()

    def refresh_data(self):
        self.refresh_btn.setEnabled(False)
        # Rows to fall back on for any part the refresh fails to fetch
        # (docs/ui.md 8.8): a failed refresh used to leave those rows out of
        # the table and the cache, or empty both when the network was down.
        self._prev_data = list(self.all_data)
        self._full_failure = None
        self._signal_cache.bump()      # signal runs started on the old rows are stale from here
        self.all_data = []
        self._refresh_markets = set(self._markets)
        self.market_status = {m: tr("대기", "Waiting") for m in ["Indices"] + self.selected_markets()}
        self.update_status_display()

        self._track_thread(AllDataFetchThread(markets=self.selected_markets()), 'fetch_thread')
        self.fetch_thread.market_loaded.connect(self.on_market_loaded)
        self.fetch_thread.market_progress.connect(self.on_market_progress)
        self.fetch_thread.failed.connect(self._on_full_refresh_failed)
        self.fetch_thread.finished_all.connect(self.on_finished_all)
        self.fetch_thread.start()

        self.refresh_started.emit()

    def auto_update_tick(self):
        """Called every 60s by MainWindow's global auto timer (roadmap: Auto Update checkbox)."""
        if not self.refresh_btn.isEnabled():
            return
        market_fetch = getattr(self, "_market_fetch_thread", None)
        if market_fetch is not None and market_fetch.isRunning():
            return          # rows are being added; the tick's copy would drop them

        if self.all_data:
            # If all_data is already populated, do a lightweight in-place update for Universe
            if getattr(self, '_lw_fetch_thread', None) is not None and self._lw_fetch_thread.isRunning():
                return

            self._lw_failure = ""
            self._track_thread(UniverseLightweightFetchThread(self.all_data), '_lw_fetch_thread')
            self._lw_fetch_thread.failed.connect(self._on_universe_lightweight_failed)
            self._lw_fetch_thread.finished_all.connect(self._on_universe_lightweight_loaded)
            self._lw_fetch_thread.status_message.connect(self.status_message)
            self._lw_fetch_thread.start()

            self.auto_lightweight_tick.emit()
        else:
            # If empty, do a full refresh
            self.refresh_data()

    def _on_universe_lightweight_failed(self, reason: str):
        # Arrives before finished_all (same emitting thread, queued in order).
        self._lw_failure = reason

    def _on_universe_lightweight_loaded(self, updated_data):
        reason = getattr(self, "_lw_failure", "")
        if reason:
            # Nothing new arrived: keep the table and the last good update
            # time, and say so (header dot turns red; docs/ui.md 8.7).
            self._lw_failure = ""
            self.status_text_changed.emit(tr(f"자동 갱신 실패: {reason}", f"Auto-update failed: {reason}"))
            self.refresh_failed.emit(reason)
            return
        # UniverseLightweightFetchThread preserves all_data's order/length
        # and only reassigns item["changes"] (to a freshly-fetched dict) for
        # tickers whose price actually moved -- every unchanged item keeps
        # the exact same "changes" dict object it had before the thread ran.
        # That lets us tell which rows actually need a UI refresh by object
        # identity, without a value-by-value diff.
        #
        if [x.get("ticker") for x in updated_data] != [x.get("ticker") for x in self.all_data]:
            # Rows were added or removed while the tick ran (a market switched
            # on/off, a ticker added): its copy is stale, the next tick redoes it.
            return
        changed_rows = {
            i for i, (old_item, new_item) in enumerate(zip(self.all_data, updated_data))
            if old_item.get("changes") is not new_item.get("changes")
        }

        self.all_data = updated_data
        highlights = self.custom_settings.get("highlights", {})

        # Save current scroll position
        v_scroll = self.table.verticalScrollBar().value()

        # Incremental update: only the rows whose price/changes actually
        # changed get their cells rebuilt (identity/cap/PER never change on
        # this path, so there's no need to touch them at all, unlike a full
        # load_data()).
        self.table.update_changed_rows(updated_data, changed_rows, highlights)
        if changed_rows:
            self.filter_table(self.search_input.text())

        # Restore scroll position to prevent jumping
        self.table.verticalScrollBar().setValue(v_scroll)

        self.update_total_status(tr("가격을 자동 갱신했습니다.", "Auto-updated prices."))
        self.update_last_sync_time()

    def on_market_progress(self, market, current, total):
        self.market_status[market] = tr(f"불러오는 중 ({current}/{total})", f"Loading ({current}/{total})")
        self.update_status_display()
        self.status_message.emit(tr(f"{_part_label(market)} 시세 받는 중... ({current}/{total})",
                                    f"Fetching {market} quotes... ({current}/{total})"))

    def update_status_display(self):
        parts = " | ".join(f"{_part_label(m)}: {s}" for m, s in self.market_status.items())
        self.status_text_changed.emit(tr(f"상태: {parts}", f"Status: {parts}"))

    def update_last_sync_time(self):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.sync_time_changed.emit(tr(f"갱신: {now_str}", f"Updated: {now_str}"))

    def update_total_status(self, prefix=None):
        if prefix is None:
            prefix = tr("데이터를 불러왔습니다.", "Data Loaded.")
        market_counts = Counter(item.get("market", "Unknown") for item in self.all_data)
        order = ["Index", *MARKETS]
        summary_parts = [
            f"{_part_label(m)} {market_counts[m]}" for m in order if m in market_counts
        ] + [
            f"{_part_label(m)} {c}" for m, c in sorted(market_counts.items()) if m not in order
        ]
        summary = ", ".join(summary_parts)
        self.status_text_changed.emit(tr(f"{prefix} 전체 {len(self.all_data)}종목 ({summary})",
                                         f"{prefix} Total {len(self.all_data)} stocks ({summary})"))

    def on_market_loaded(self, market, data):
        self.market_status[market] = tr(f"완료({len(data)})", f"Done({len(data)})")
        self.update_status_display()
        # Accumulate data incrementally so the table updates as each market finishes
        self.all_data.extend(data)

    _PART_MARKET = {"Indices": "Index", **{m: m for m in MARKETS}}

    def _on_full_refresh_failed(self, reason: str, parts: list):
        # Arrives before finished_all (same emitting thread, queued in order).
        self._full_failure = (reason, parts)

    def _with_previous_rows(self, all_data: list, parts: list) -> list:
        """all_data plus the previous refresh's rows for the parts that
        failed this time (by market), so a failed market keeps its last
        known rows instead of disappearing."""
        markets = {self._PART_MARKET[p] for p in parts if p in self._PART_MARKET}
        have = {x.get("ticker") for x in all_data}
        kept = [x for x in getattr(self, "_prev_data", [])
                if x.get("market") in markets and x.get("ticker") not in have]
        return all_data + kept

    def on_finished_all(self, all_data):
        failure = getattr(self, "_full_failure", None)
        self._full_failure = None
        try:
            self.load_custom_settings()
            deleted = set(self.custom_settings.get("deleted", []))
            added = self.custom_settings.get("added", [])

            if failure:
                all_data = self._with_previous_rows(all_data, failure[1])
            # A market switched off while the refresh ran: its rows go
            # (user-added tickers stay, hidden by the market filter).
            filtered_data = self._rows_for_markets(
                [x for x in all_data if x.get("ticker", "") not in deleted])

            self.all_data = filtered_data
            # New prices: universe runs are recomputed on demand (a pinned run stays as it was shown).
            self._signal_cache.clear(keep=self._last_picks if self._signal_order else None)
            self._sort_all_data()      # keeps a pinned Trend Score run on top
            self._reload_table()
            self.filter_table(self.search_input.text())
            if failure:
                reason, parts = failure
                shown = ", ".join(_part_label(p) for p in parts)
                self.update_total_status(prefix=tr(f"갱신 실패 ({reason}). {shown}은(는) 이전 행을 유지했습니다.",
                                                   f"Refresh failed ({reason}); kept the previous rows for "
                                                   f"{', '.join(parts)}."))
                self.refresh_failed.emit(tr(f"전체 갱신: {reason}", f"full refresh: {reason}"))
            else:
                self.update_total_status()
                self.update_last_sync_time()

            # Cache the newly fetched data (the user-added tickers below are
            # appended by _render_startup_batch, which saves again)
            self._save_universe_cache()

            existing_tickers = {x.get("ticker") for x in self.all_data}
            missing_added = [x for x in added if x["ticker"] not in existing_tickers and x["ticker"] not in deleted]

            for item in missing_added:
                thread = self._track_thread(SingleStockFetchThread(item["market"], item["ticker"]))
                thread.finished.connect(self._on_startup_stock_loaded)
                thread.start()

        except Exception as e:
            logger.error("Data sort/load error in on_finished_all", exc_info=True)
            self.status_text_changed.emit(tr(f"데이터 정렬/불러오기 오류: {e}", f"Data sort/load error: {e}"))
        finally:
            self.refresh_btn.setEnabled(True)
            # a market switched on while the refresh ran
            self._fetch_missing_markets(only=self._markets - getattr(self, "_refresh_markets", self._markets))


# Every label this tab's toolbar buttons can show (the shared toolbar width,
# ui.common.toolbar_button_width): market toggles, the status cycle, "필터 (n)".
register_toolbar_labels(
    *(UniverseTab._MARKET_BUTTON_TEXT.get(m, label).replace("&", "&&") for m, label in MARKETS.items()),
    *(label for _, label in UniverseTab._STATUS_FILTER_STATES),
    tr("필터", "Filters"), tr("필터 (99)", "Filters (99)"),
    tr("종목 추가", "Add Ticker"), tr("전략 신호", "Signals"), tr("새로고침", "Refresh"),
)
