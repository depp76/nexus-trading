"""tests/test_language_switch.py — switching the UI language at run time
(docs/ui.md 8.20, 2026-11-15): the main window is rebuilt in the new language
without restarting the app, and what the user set up is carried over.

The switch re-imports every ui/threads module, which would leave the other
test modules holding stale classes, so the scenario runs in a child process.
It uses a temp DB, temp JSON files and a temp INI for every QSettings, and no
worker thread starts (QThread.start is a no-op), so nothing real is read or
written. QSettings(org, app) always goes to the Windows registry whatever
setDefaultFormat/setPath say, so the class itself is swapped for one that
writes the temp INI (an earlier version of this test wrote ui/language to the
real registry on every run).
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCRIPT = textwrap.dedent(r'''
    import os, re, sys, tempfile
    from datetime import date
    tmp = sys.argv[1]
    sys.path.insert(0, sys.argv[2])
    import PyQt6.QtCore as QtCore
    from PyQt6.QtCore import QThread, QSettings
    QThread.start = lambda self, *a, **k: None
    _INI = os.path.join(tmp, "settings.ini")

    class _TempSettings(QSettings):
        """Every QSettings(...) in this process reads and writes the temp INI."""
        def __init__(self, *_args, **_kwargs):
            super().__init__(_INI, QSettings.Format.IniFormat)

    QtCore.QSettings = _TempSettings       # before any app module imports it
    import paths, trade_db
    paths.UNIVERSE_CACHE_FILE = os.path.join(tmp, "universe_cache.json")
    paths.CUSTOM_SETTINGS_FILE = os.path.join(tmp, "custom_settings.json")
    trade_db._DB_FILE = os.path.join(tmp, "portfolio.db")
    trade_db.TRADING_RECORD_FILE = os.path.join(tmp, "trading_record.json")
    import i18n
    i18n.save_language("ko")
    import main
    from PyQt6.QtWidgets import QApplication, QWidget, QLabel, QAbstractButton, QLineEdit, QComboBox, QTabWidget
    app = QApplication.instance() or QApplication([])
    w = main.MainWindow()
    w.global_auto_timer.stop()
    w.show(); app.processEvents()

    w.universe_tab.search_input.setText("abc")
    w.trading_history_tab._on_period_filter_changed("3M")
    w.trading_record_tab._currency_btn.click()
    from strategy.trend_following.research import ResearchRequest, run_research
    from tests.strategy.trend_following.helpers import synthetic_dataset
    tf = w.strategy_tab.trend_following_tab
    req = ResearchRequest(start=date(2021, 1, 4), variant_ids=("A0",), cost_multipliers=(1.0,), run_event_study=False)
    tf.set_variant_choice(("A0",))
    tf._on_research_finished(run_research(synthetic_dataset(), req), "")
    w.tabs.setCurrentIndex(3)

    def state(win):
        t = win.strategy_tab.trend_following_tab
        return (win.universe_tab.search_input.text(), win.trading_history_tab._period_filter,
                win.trading_record_tab._currency, t.conditions.variant_ids,
                t._result is not None, t.compare_table.rowCount(), win.tabs.currentIndex())

    def texts(win):
        out = []
        for x in win.findChildren(QWidget):
            if isinstance(x, (QLabel, QAbstractButton)): out.append(x.text())
            if isinstance(x, QLineEdit): out.append(x.placeholderText())
            if isinstance(x, QComboBox): out += [x.itemText(i) for i in range(x.count())]
            if isinstance(x, QTabWidget): out += [x.tabText(i) for i in range(x.count())]
        return [s for s in out if s and s != "한국어"]

    han = re.compile("[가-힣]")
    before = state(w)
    old_mod = sys.modules["ui.universe_tab"]
    new = main.switch_language(w, "en")
    for _ in range(3): app.processEvents()
    print("RELOADED", sys.modules["ui.universe_tab"] is not old_mod)
    print("ONE_WINDOW", sum(1 for x in app.topLevelWidgets() if isinstance(x, main.MainWindow) and x.isVisible()) == 1)
    print("STATE_KEPT", state(new) == before)
    print("NO_HANGUL", not any(han.search(s) for s in texts(new)))
    print("SAVED_EN", i18n.saved_language() == "en")
    print("TEMP_INI_USED", os.path.isfile(_INI) and "language=en" in open(_INI, encoding="utf-8").read())
    back = main.switch_language(new, "ko")
    for _ in range(3): app.processEvents()
    print("BACK_KO", any(s == "거래 추가" for s in texts(back)) and state(back) == before)
    sys.stdout.flush()
    os._exit(0)
''')


class TestLanguageSwitchWithoutRestart(unittest.TestCase):
    def test_switch_rebuilds_the_window_and_keeps_the_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
            proc = subprocess.run([sys.executable, "-c", _SCRIPT, tmp, _SRC], cwd=_SRC, env=env,
                                  capture_output=True, text=True, encoding="utf-8", timeout=240)
        out = proc.stdout
        for flag in ("RELOADED", "ONE_WINDOW", "STATE_KEPT", "NO_HANGUL", "SAVED_EN", "TEMP_INI_USED", "BACK_KO"):
            self.assertIn(f"{flag} True", out, f"{flag} failed\n--- stdout\n{out}\n--- stderr\n{proc.stderr[-3000:]}")


if __name__ == "__main__":
    unittest.main()
