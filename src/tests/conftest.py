"""Puts src/ on sys.path once for every test module, so tests can import the
application packages (data, ui, threads, ...) regardless of the
directory pytest is launched from or how deep the test file is nested."""
import os
import sys

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

# Charts get the app's font family like main.py does at startup, so the Korean
# empty-state text does not warn about glyphs missing from matplotlib's DejaVu.
from ui.common import apply_matplotlib_font  # noqa: E402

apply_matplotlib_font()


# No test may write the real hold-out ledger (paths.HOLDOUT_LEDGER_FILE, trend_following.md
# 10-7): a test that runs TrendFollowingResearchThread with an end date past DEV_END would
# record a false "hold-out opened" line. Every test gets its own temporary file.
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_holdout_ledger(tmp_path, monkeypatch):
    import paths
    monkeypatch.setattr(paths, "HOLDOUT_LEDGER_FILE", str(tmp_path / "holdout_ledger.jsonl"))
    # Same for the Universe cache (2026-11-18: a Universe test's table render saved its one
    # fixture row over the real universe_cache.json).
    monkeypatch.setattr(paths, "UNIVERSE_CACHE_FILE", str(tmp_path / "universe_cache.json"))


# Close and delete the top-level widgets a test module leaves open (2026-11-20). Many UI tests
# show() a table or dialog and never close it; ~280 windows had piled up by the end of the
# suite, and the offscreen platform then crashed (access violation in QPainter.drawText while
# painting one of them during a later test's processEvents). Module scope keeps widgets a
# module shares between its tests alive until that module is done.
@pytest.fixture(autouse=True, scope="module")
def _close_leftover_windows():
    yield
    from PyQt6.QtCore import QCoreApplication, QEvent
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for w in QApplication.topLevelWidgets():
        try:
            w.close()
            w.deleteLater()
        except RuntimeError:        # already deleted on the C++ side
            pass
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
