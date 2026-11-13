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
