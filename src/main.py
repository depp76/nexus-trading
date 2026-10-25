import sys
import os
from datetime import datetime
from dotenv import load_dotenv
import trade_db
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTabWidget,
    QCheckBox,
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QShortcut, QKeySequence

from paths import ENV_FILE, APP_LOG_FILE
from ui.common import (
    create_font,
    apply_matplotlib_font,
    FONT_FAMILY_CSS,
    FONT_TITLE,
    FONT_SMALL,
    cancel_thread,
    refresh_state,
)
from ui.theme import app_qss, TEXT_EMPTY
from ui.colors import OK, WARN, DANGER

load_dotenv(ENV_FILE)

import logging
logging.getLogger('matplotlib.font_manager').setLevel(logging.ERROR)

# ── Structured logging setup ──────────────────────────────────────────────────
# Root logger at INFO: WARNING+ goes to both app.log and the console, and INFO
# (e.g. _HIST_CACHE hit/miss stats, auto-backup completion) goes to app.log only.
# Leaving the root at DEBUG would make every third-party library's debug records
# get built and then thrown away by the handlers (roadmap 6-1a).
_log_formatter = logging.Formatter(
    "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler = logging.FileHandler(APP_LOG_FILE, encoding="utf-8")
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_log_formatter)
_stream_handler = logging.StreamHandler()
_stream_handler.setLevel(logging.WARNING)
_stream_handler.setFormatter(_log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[_file_handler, _stream_handler])
logger = logging.getLogger(__name__)  # module-level logger for main.py
# ─────────────────────────────────────────────────────────────────────────────


# --- Phase 3-1: split out to the threads/ package ---
from threads.fetch_threads import AutoBackupThread

# --- Phase 4: split out to ui/history_tab.py, ui/assets_tab.py ---
from ui.history_tab import TradingHistoryTab
from ui.assets_tab import TradingRecordTab

# --- Phase 5: split out to ui/universe_tab.py ---
from ui.universe_tab import UniverseTab

# Strategy research tab (re-introduced 2026-09-28 with strategy.trend_following v03)
from ui.strategy_tab import StrategyTab


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Nexus Trading")
        self.resize(1100, 850)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)

        # Header
        header_layout = QHBoxLayout()
        title_label = QLabel("Nexus Trading")
        title_label.setFont(create_font(FONT_TITLE, QFont.Weight.Bold))
        header_layout.addWidget(title_label)
        
        header_layout.addStretch()

        # Global Auto Timer
        self.global_auto_timer = QTimer(self)
        self.global_auto_timer.setInterval(self.AUTO_REFRESH_MS)
        self.global_auto_timer.timeout.connect(self._on_global_auto_timer)

        # Refresh status, all in one place on the header (docs/ui.md 1.10 /
        # 8.4): state dot . toggle with its interval . last update time. The
        # dot is OK while updates arrive, WARN once the last one is older than
        # two intervals, faint while auto-refresh is off. The time used to sit
        # in a footer row, apart from the toggle.
        self._last_sync_at = None
        self._last_failure = None   # (datetime, reason) of the latest failed auto-update
        self._refresh_dot = QLabel("●")
        self._refresh_dot.setFont(create_font(FONT_SMALL))
        header_layout.addWidget(self._refresh_dot)
        self.auto_refresh_cb = QCheckBox(f"Auto-refresh · {self.AUTO_REFRESH_MS // 1000}s")
        self.auto_refresh_cb.toggled.connect(self._toggle_global_auto_timer)
        header_layout.addWidget(self.auto_refresh_cb)
        self.update_time_label = QLabel("last -")
        self.update_time_label.setObjectName("faint")
        self.update_time_label.setFont(create_font(FONT_SMALL, style_name="Semilight"))
        header_layout.addSpacing(6)
        header_layout.addWidget(self.update_time_label)
        # Re-evaluates "late" between updates.
        self._indicator_timer = QTimer(self)
        self._indicator_timer.setInterval(15000)
        self._indicator_timer.timeout.connect(self._update_refresh_indicator)
        self._indicator_timer.start()

        # Check by default (this will trigger the toggled signal and start the timer)
        self.auto_refresh_cb.setChecked(True)

        main_layout.addLayout(header_layout)

        # Tab System
        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        main_layout.addWidget(self.tabs)

        # Schema + one-time JSON migrations before any tab reads the DB
        # (TradingRecordTab loads asset_records in its constructor).
        trade_db.init_db()

        # 1. Trading Universe Tab
        self.universe_tab = UniverseTab()
        self.tabs.addTab(self.universe_tab, "Trading Universe")

        # 2. Trading History Tab
        self.trading_history_tab = TradingHistoryTab()
        self.tabs.addTab(self.trading_history_tab, "Trading History")

        # 3. Total Assets Tab (Trading Record)
        self.trading_record_tab = TradingRecordTab()
        self.tabs.addTab(self.trading_record_tab, "Total Assets")

        # 4. Strategy Tab (research backtests; strategy.trend_following, spec v03).
        # Rebuilt from scratch on 2026-09-28 after the old strategy layer was reset.
        self.strategy_tab = StrategyTab()
        self.tabs.addTab(self.strategy_tab, "Strategy")

        self.trading_history_tab.total_asset_updated.connect(self.trading_record_tab.update_live_asset)
        self.trading_history_tab.status_message.connect(self._on_thread_status_message)
        self.trading_record_tab.status_message.connect(self._on_thread_status_message)

        # Native status bar: the one place for short-lived progress and result
        # text from every tab (e.g. "Fetching KOSPI quotes... (3/5)"). Each tab's
        # own lasting status line sits in that tab's footer (docs/ui.md 8.4).
        self.statusBar().showMessage("Ready")

        # Load Trading History from the database (primary source).
        self.trading_history_tab.load_from_db()

        # Wire Trading Universe tab -> header refresh status / native status bar /
        # cross-tab triggers (its status_text_changed goes to its own footer).
        self.universe_tab.sync_time_changed.connect(self._on_sync_time)
        self.universe_tab.refresh_failed.connect(self._on_refresh_failed)
        self.universe_tab.status_message.connect(self._on_thread_status_message)
        self.universe_tab.refresh_started.connect(self.trading_history_tab._reload_current)
        self.universe_tab.auto_lightweight_tick.connect(self.trading_history_tab._start_realtime_price_update)

        self.universe_tab.refresh_data()

        # ---Tab movement shortcuts (Ctrl+Tab / Ctrl+Shift+Tab) ---
        # QShortcut(WindowShortcut) works across the entire window regardless of focus position
        sc_next = QShortcut(QKeySequence("Ctrl+Tab"), self)
        sc_next.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_next.activated.connect(self._tab_next)

        sc_prev = QShortcut(QKeySequence("Ctrl+Shift+Tab"), self)
        sc_prev.setContext(Qt.ShortcutContext.WindowShortcut)
        sc_prev.activated.connect(self._tab_prev)

        # Automatic backup of portfolio.db (trades + asset snapshots) + custom_settings.json (roadmap 2-4).
        # Runs in the background so it never blocks startup.
        self._auto_backup_thread = AutoBackupThread()
        self._auto_backup_thread.backup_done.connect(self._on_thread_status_message)
        self._auto_backup_thread.start()

    def _tab_next(self):
        """Ctrl+Tab: Move to the Next Tab (Circular)."""
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % count)

    def _tab_prev(self):
        """Ctrl+Shift+Tab: Move to the Previous Tab (Circular)."""
        count = self.tabs.count()
        if count > 1:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % count)

    AUTO_REFRESH_MS = 60000

    def _toggle_global_auto_timer(self, checked):
        if checked:
            # The first tick comes one interval later, so toggling back and
            # forth does not fire a burst of fetches.
            self.global_auto_timer.start()
        else:
            self.global_auto_timer.stop()
        self._update_refresh_indicator()

    def _on_sync_time(self, _text: str):
        self._last_sync_at = datetime.now()
        self.update_time_label.setText(f"last {self._last_sync_at:%H:%M:%S}")
        self._update_refresh_indicator()

    def _on_refresh_failed(self, reason: str):
        self._last_failure = (datetime.now(), reason)
        self._update_refresh_indicator()

    _REFRESH_DOT_COLOR = {"off": TEXT_EMPTY, "waiting": TEXT_EMPTY, "failed": DANGER, "late": WARN, "ok": OK}

    def _update_refresh_indicator(self):
        kind, tip = refresh_state(self.auto_refresh_cb.isChecked(), self._last_sync_at, self._last_failure,
                                  datetime.now(), self.AUTO_REFRESH_MS)
        self._refresh_dot.setStyleSheet(f"color: {self._REFRESH_DOT_COLOR[kind]};")
        self._refresh_dot.setToolTip(tip)

    def _on_global_auto_timer(self):
        self.universe_tab.auto_update_tick()

    def _on_thread_status_message(self, msg: str):
        """Show a short-lived progress message from a background fetch thread
        in the native status bar (roadmap 2-3)."""
        self.statusBar().showMessage(msg, 5000)

    # ---Clean shutdown ---
    def closeEvent(self, event):
        """Stop all background QThread workers so the process exits cleanly."

        Without this, ThreadPoolExecutors inside QThread.run() keep non-daemon
        Python threads alive after the Qt event loop ends, which blocks the
        terminal from returning to the shell prompt.
        """
        # Collect every QThread this window may have started
        threads_to_stop = []

        abt = getattr(self, '_auto_backup_thread', None)
        if abt is not None:
            threads_to_stop.append(abt)

        if hasattr(self, 'universe_tab'):
            threads_to_stop.extend(self.universe_tab.collect_threads_to_stop())

        if hasattr(self, 'trading_history_tab'):
            threads_to_stop.extend(self.trading_history_tab.collect_threads_to_stop())

        if hasattr(self, 'trading_record_tab'):
            threads_to_stop.extend(self.trading_record_tab.collect_threads_to_stop())

        if hasattr(self, 'strategy_tab'):
            threads_to_stop.extend(self.strategy_tab.collect_threads_to_stop())

        for t in threads_to_stop:
            try:
                if t.isRunning():
                    cancel_thread(t)   # cancellable workers exit at their next check
                    t.quit()
                    if not t.wait(2000):   # wait up to 2 s
                        t.terminate()     # force-kill if still alive
                        t.wait(500)
            except Exception:
                logger.warning("Failed to cleanly stop background thread on close", exc_info=True)

        QApplication.instance().quit()
        event.accept()


if __name__ == "__main__":
    print("Starting Nexus Trading...")
    app = QApplication(sys.argv)
    # Windows' native style silently ignores parts of the stylesheet below
    # (notably QTabBar and QHeaderView theming); Fusion renders every rule
    # (docs/ui.md 6.4).
    app.setStyle("Fusion")
    app_font = create_font(style_name="Semilight")
    app.setFont(app_font)
    apply_matplotlib_font()
    app.setStyleSheet(app_qss(FONT_FAMILY_CSS))

    window = MainWindow()
    window.showMaximized()
    ret = app.exec()
    # Ensure all remaining non-daemon threads (ThreadPoolExecutor workers
    # started by data_fetcher) don't block process exit.
    os._exit(ret if ret else 0)

