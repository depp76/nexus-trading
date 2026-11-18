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
