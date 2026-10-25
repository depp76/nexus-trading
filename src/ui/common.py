"""ui/common.py — Shared UI helpers, styling constants, validators, and atomic I/O.

Contains:
  - Font and styling factories (create_font, accent color constants)
  - QLineEdit auto-formatters and validation helpers
  - Atomic JSON persistence and safe loader functions
"""

import os
import json
import math
import tempfile
import logging
import datetime as _dt
from typing import Any, Callable

from PyQt6.QtCore import QObject, QEvent, Qt
from PyQt6.QtWidgets import (
    QLineEdit, QPushButton, QTableWidget, QHeaderView, QLabel, QFrame, QHBoxLayout, QVBoxLayout,
    QWidget, QApplication,
)
from PyQt6.QtGui import QFont, QPainter, QColor, QKeySequence

from ui.theme import TEXT, TEXT_FAINT
from ui.colors import OK, DANGER, DANGER_BG

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Font creation & typography
# ---------------------------------------------------------------------------
# One family for the whole app -- every widget, numeric table cells included,
# and matplotlib charts (apply_matplotlib_font). The three names are the same
# face under Qt's English/Korean family names plus the plain family as a
# fallback (user direction 2026-09-19; the docs/ui.md 1.3 "numbers in a
# monospace face" rule was dropped for this).
FONT_FAMILIES = ["Malgun Gothic Semilight", "맑은 고딕 Semilight", "Malgun Gothic"]

# Point-size scale. Anything not on it is a one-off to justify.
FONT_TITLE = 16     # tab titles
FONT_KPI = 13       # KPI-card values
FONT_HEADING = 11   # section headings
FONT_BODY = 10      # default (QApplication font)
FONT_SMALL = 9      # table cells, status lines
FONT_CAPTION = 8    # sub-labels, footnotes


def create_font(
    size: int = FONT_BODY,
    weight: QFont.Weight = QFont.Weight.Normal,
    style_name: str = None,
) -> QFont:
    """The app font at `size`: Malgun Gothic Semilight, or its Bold face."""
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPointSize(size)
    if weight == QFont.Weight.Bold:
        font.setWeight(QFont.Weight.Bold)
    else:
        font.setWeight(QFont.Weight.Light)
        font.setStyleName(style_name or "Semilight")
    return font


def apply_matplotlib_font() -> None:
    """Point matplotlib at the same family as the Qt widgets. Without this
    every chart (MA dialogs, the Total Assets trend) drew
    in matplotlib's bundled DejaVu Sans, which has no Hangul -- a stock name
    in a chart title rendered as boxes. Called once at startup."""
    from matplotlib import rcParams
    rcParams["font.family"] = FONT_FAMILIES + ["sans-serif"]
    rcParams["axes.unicode_minus"] = False  # DejaVu's U+2212 minus is not in Malgun Gothic


# ---------------------------------------------------------------------------
# Styling constants
# ---------------------------------------------------------------------------
_HIST_KEYS = ["3d", "5d", "10d", "20d", "60d", "120d"]
_MARKET_ORDER = {"KOSPI": 0, "KOSDAQ": 1, "NASDAQ 100": 2, "S&P500": 3}
_FIELD_ERROR_STYLE = f"border: 1px solid {DANGER}; background-color: {DANGER_BG};"

# Shared font-family declaration for inline stylesheets (roadmap 6-2e); the
# same three-family fallback list create_font() uses. Splice it into a
# stylesheet string: "QLabel { " + FONT_FAMILY_CSS + " font-size: 9pt; }"
FONT_FAMILY_CSS = "font-family: " + ", ".join(f"'{f}'" for f in FONT_FAMILIES) + ";"


# ---------------------------------------------------------------------------
# Buttons (docs/ui.md 1.6 / 8.1)
# ---------------------------------------------------------------------------
# One height for every push button in the app; width follows the text unless
# a caller has a reason to pin it (a toggle whose label changes, a segmented
# row whose buttons must line up). Labels are plain text: no emoji, no
# per-button bold, no per-button color. Color comes only from the objectName
# role ("primary" = the screen's one main action, "danger" = destructive)
# matched by ui/theme.py's QPushButton#primary/#danger rules.
BTN_H = 28


def make_button(text: str, role: str | None = None, on_click: Callable | None = None, *,
                width: int | None = None, tooltip: str = "", checkable: bool = False,
                checked: bool = False) -> QPushButton:
    btn = QPushButton(text)
    btn.setFixedHeight(BTN_H)
    if width:
        btn.setFixedWidth(width)
    if role:
        btn.setObjectName(role)
    if tooltip:
        btn.setToolTip(tooltip)
    if checkable:
        btn.setCheckable(True)
        btn.setChecked(checked)
    if on_click is not None:
        btn.clicked.connect(on_click)
    return btn


# ---------------------------------------------------------------------------
# Header refresh status (docs/ui.md 1.10 / 8.4 / 8.7)
# ---------------------------------------------------------------------------
def refresh_state(enabled: bool, last_sync, last_failure, now, interval_ms: int) -> tuple:
    """State of the header's auto-refresh dot: (kind, tooltip), kind one of
    "off", "waiting", "failed", "late", "ok". `last_failure` is (datetime,
    reason) or None; a failure counts only if it is newer than the last
    successful update. "late" = no update for more than two intervals."""
    if not enabled:
        return "off", "Auto-refresh is off"
    if last_failure is not None and (last_sync is None or last_failure[0] > last_sync):
        at, reason = last_failure
        return "failed", f"Update failed at {at:%H:%M:%S}: {reason}"
    if last_sync is None:
        return "waiting", "Waiting for the first update"
    if (now - last_sync).total_seconds() * 1000 > 2 * interval_ms:
        return "late", "Late: no update for more than two intervals"
    return "ok", "Updating"


# ---------------------------------------------------------------------------
# Tab skeleton (docs/ui.md 8.4)
# ---------------------------------------------------------------------------
# Every top-level tab: [toolbar row] -> [optional KPI strip or chart] ->
# [table] -> [footer line], with these margins and gaps.
TAB_MARGINS = (12, 10, 12, 10)
TAB_SPACING = 8


def make_footer_label(text: str = "") -> QLabel:
    """A tab's lasting status line under its table: small, muted, elided by
    word wrap rather than stretching the window."""
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setFont(create_font(FONT_SMALL, style_name="Semilight"))
    lbl.setWordWrap(True)
    return lbl


# ---------------------------------------------------------------------------
# Tables (docs/ui.md 8.2)
# ---------------------------------------------------------------------------
# One row height, one font size and one selection model for every data table;
# the frame/grid/header lines come from ui/theme.py's table rules. Sorting is
# left to the caller (header-click sort where the rows are plain records).
TABLE_ROW_H = 24


class _EmptyHint(QObject):
    """Paints a one-line hint in the middle of a table's viewport while the
    table has no rows, so an empty table says why it is empty instead of
    showing a blank grid."""

    def __init__(self, table: QTableWidget, text: str):
        super().__init__(table)
        self._table = table
        self.text = text
        table.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Paint and self._table.rowCount() == 0 and self.text:
            self._table.viewportEvent(event)   # the view paints its (empty) grid/background first
            painter = QPainter(obj)
            painter.setPen(QColor(TEXT_FAINT))
            painter.drawText(obj.rect(), Qt.AlignmentFlag.AlignCenter, self.text)
            painter.end()
            return True
        return False


def setup_table(table: QTableWidget, *, zebra: bool = True, empty_text: str = "",
                row_height: int = TABLE_ROW_H) -> QTableWidget:
    """Apply the app-wide table defaults: hidden row numbers, fixed row
    height, whole-row selection, zebra rows (off for a table that paints its
    own stripes), the table font and an optional empty-state hint."""
    table.setFont(create_font(FONT_SMALL, style_name="Semilight"))
    vh = table.verticalHeader()
    vh.setVisible(False)
    vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
    vh.setDefaultSectionSize(row_height)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(zebra)
    if empty_text:
        table._empty_hint = _EmptyHint(table, empty_text)
    return table


class _TableKeys(QObject):
    """Ctrl+C / Enter for a data table (docs/ui.md 1.8 / 8.6)."""

    def __init__(self, table: QTableWidget, on_activate, skip_row):
        super().__init__(table)
        self._table = table
        self._on_activate = on_activate
        self._skip_row = skip_row
        table.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.matches(QKeySequence.StandardKey.Copy):
            copy_rows_tsv(self._table, self._skip_row)
            return True
        if self._on_activate is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row, col = self._table.currentRow(), self._table.currentColumn()
            if row >= 0 and not (self._skip_row and self._skip_row(row)):
                self._on_activate(row, max(col, 0))
                return True
        return False


def copy_rows_tsv(table: QTableWidget, skip_row=None) -> str:
    """Copy the selected rows to the clipboard as tab-separated text, one line
    per row, visible columns in on-screen order; returns the text."""
    rows = sorted({idx.row() for idx in table.selectedIndexes()})
    header = table.horizontalHeader()
    cols = [header.logicalIndex(v) for v in range(header.count())]
    cols = [c for c in cols if not table.isColumnHidden(c)]
    lines = []
    for row in rows:
        if skip_row and skip_row(row):
            continue
        cells = []
        for col in cols:
            item = table.item(row, col)
            cells.append(item.text() if item else "")
        lines.append("\t".join(cells))
    text = "\n".join(lines)
    if text:
        QApplication.clipboard().setText(text)
    return text


def install_table_keys(table: QTableWidget, on_activate=None, skip_row=None) -> None:
    """Give a table the app-wide keyboard behaviour: Ctrl+C copies the
    selected rows as TSV; Enter does what a double-click on the current cell
    does (`on_activate(row, col)`). `skip_row(row)` excludes rows that are
    not records (History's month summary rows)."""
    table._table_keys = _TableKeys(table, on_activate, skip_row)


# ---------------------------------------------------------------------------
# KPI strip (docs/ui.md 3.6 / 8.6)
# ---------------------------------------------------------------------------
class KpiStrip(QFrame):
    """The row of KPI cells under a tab's toolbar: a small caps label, a
    large value and a faint sub-label per cell, equal stretch per cell.
    Shared by Trading History and Total Assets so both look the same.
    `cells[key]` is `(value QLabel | None, sub QLabel)`; a cell built with an
    input widget has no value label."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DashboardCard")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(16, 8, 16, 8)
        self._layout.setSpacing(0)
        self.cells: dict = {}

    def add(self, key: str, label: str, sub: str = "", widget: QWidget | None = None):
        box = QVBoxLayout()
        box.setSpacing(2)
        lbl = QLabel(label.upper())
        lbl.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        lbl.setStyleSheet(f"color:{TEXT_FAINT}; letter-spacing:.05em;")
        box.addWidget(lbl)
        value_lbl = None
        if widget is not None:
            widget.setFont(create_font(FONT_KPI, style_name="Semilight"))
            widget.setStyleSheet("border:none; padding:0px; background:transparent;")
            box.addWidget(widget)
        else:
            value_lbl = QLabel("-")
            value_lbl.setFont(create_font(FONT_KPI, style_name="Semilight"))
            box.addWidget(value_lbl)
        sub_lbl = QLabel(sub)
        sub_lbl.setFont(create_font(FONT_CAPTION, style_name="Semilight"))
        sub_lbl.setStyleSheet(f"color:{TEXT_FAINT};")
        box.addWidget(sub_lbl)
        self.cells[key] = (value_lbl, sub_lbl)
        cell_widget = QWidget()
        cell_widget.setLayout(box)
        self._layout.addWidget(cell_widget, 1)

    def set_value(self, key: str, text: str, color: str | None = None, tooltip: str | None = None):
        value_lbl, _ = self.cells[key]
        if value_lbl is None:
            return
        value_lbl.setText(text)
        value_lbl.setStyleSheet(f"color:{color or TEXT};")
        if tooltip is not None:
            value_lbl.setToolTip(tooltip)

    def set_sub(self, key: str, text: str):
        self.cells[key][1].setText(text)

# Status text colors (roadmap 7-2c) — e.g. a risk-gate PASS/FAIL cell. Reserved
# for status display only (roadmap 7-4d): an action button must not reuse these,
# even one whose action happens to succeed/fail, so status color always means
# "this is a status", never "this is a runnable action that turned out fine".
# (Button chrome itself comes from ui/theme.py's QPushButton rules and the
# #primary/#danger objectName roles; the per-role solid-fill helpers that used
# to live here -- action_button_style(), _ACTION_*_COLOR, _SECONDARY_BUTTON_STYLE
# -- are gone with docs/ui.md 1.6.)
_STATUS_SUCCESS_COLOR = OK
_STATUS_FAIL_COLOR = DANGER


# ---------------------------------------------------------------------------
# Input formatters & validators
# ---------------------------------------------------------------------------
def _fmt_num_edit(edit: QLineEdit, text: str, decimal: bool = False) -> None:
    """Re-format text with thousands commas and update QLineEdit in-place.
    Preserves cursor position. Supports optional decimal part.
    """
    raw = text.replace(',', '').strip()
    if not raw:
        return
    try:
        if decimal and '.' in raw:
            int_part, dec_part = raw.split('.', 1)
            int_part = int_part or '0'
            formatted = f"{int(int_part):,}.{dec_part}"
        else:
            formatted = f"{int(float(raw)):,}"
        if formatted != text:
            pos = edit.cursorPosition()
            delta = len(formatted) - len(text)
            edit.blockSignals(True)
            edit.setText(formatted)
            edit.setCursorPosition(max(0, pos + delta))
            edit.blockSignals(False)
    except (ValueError, OverflowError):
        pass


def _fmt_num_value(value) -> str:
    """Pre-fill text for an edit field: thousands commas, every stored decimal
    kept and trailing zeros dropped (123.45 -> "123.45", 700000.0 -> "700,000"),
    so opening an edit dialog and saving unchanged round-trips the value.
    Empty for 0/None, which the dialogs treat as "not entered"."""
    if not value:
        return ""
    return f"{float(value):,.10f}".rstrip("0").rstrip(".")


def _set_field_error(edit: QLineEdit, message: str = "") -> None:
    """Apply red-border error styling and tooltip on QLineEdit when message is truthy,
    or clear error styling when message is empty.
    """
    if message:
        edit.setStyleSheet(_FIELD_ERROR_STYLE)
        edit.setToolTip(message)
    else:
        edit.setStyleSheet("")
        edit.setToolTip("")


def _validate_date_str(text: str) -> bool:
    """True if text is a valid YYYY-MM-DD date.

    strptime's %Y-%m-%d accepts non-zero-padded month/day too (e.g. "2026-7-8"),
    so this validates parseability only -- callers that persist the value must
    run it through _normalize_date_str() first to get a consistent
    zero-padded form (portfolio.db's sell_date/buy_date columns previously
    ended up with a mix of both, which pandas parses fine but other tools may not).
    """
    text = text.strip()
    if not text:
        return False
    try:
        _dt.datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _normalize_date_str(text: str) -> str:
    """Reformats an already-valid YYYY-MM-DD date string to zero-padded form
    (e.g. "2026-7-8" -> "2026-07-08"). Returns the input stripped and
    unchanged if it doesn't parse -- callers should validate with
    _validate_date_str() first."""
    text = text.strip()
    try:
        return _dt.datetime.strptime(text, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return text


def _validate_positive_number(text: str) -> bool:
    """True if text parses (after stripping ',' and '%') to a strictly positive,
    finite float -- float() accepts "inf"/"1e309"/"nan" (review_codex.md #6)."""
    text = text.replace(",", "").replace("%", "").strip()
    if not text:
        return False
    try:
        v = float(text)
    except ValueError:
        return False
    return math.isfinite(v) and v > 0


def _mk_field_validator(edit: QLineEdit, check_fn: Callable[[str], bool], error_msg: str) -> Callable[[], bool]:
    """Build a no-argument validator closure connected to QLineEdit.textChanged for live feedback."""
    def _run(_ignored=None) -> bool:
        ok = check_fn(edit.text())
        _set_field_error(edit, "" if ok else error_msg)
        return ok
    return _run


# ---------------------------------------------------------------------------
# QThread lifecycle helper
# ---------------------------------------------------------------------------
def cancel_thread(thread) -> None:
    """Ask a worker that supports it (TrendFollowingResearchThread,
    TrendScoreThread) to stop at its next check; a no-op for the rest."""
    cancel = getattr(thread, "cancel", None)
    if callable(cancel):
        try:
            cancel()
        except Exception:
            logger.debug("thread cancel() failed", exc_info=True)


def retire_thread(owner, attr_name: str) -> None:
    """Abandon the QThread stored at ``owner.<attr_name>`` so a replacement can
    be started, without letting a still-running one be garbage-collected.

    If the thread is still running, its signals are blocked (so a late result
    cannot touch widgets the caller has moved on from) and the thread object
    is parked in ``owner._zombie_threads``, which collect_threads_to_stop()
    drains at shutdown. The attribute is then cleared. Replaces three
    hand-copied versions of this dance in the Universe and Trading History
    tabs (roadmap 6-2e).

    Uses blockSignals() rather than disconnect(): a wildcard disconnect()
    trips a benign but noisy "QObject::disconnect: wildcard call disconnects
    from destroyed signal" Qt warning when the connection's Python-slot side
    has no named C++ slot; blockSignals() gets the same "no late emissions
    reach the UI" effect without touching the connection list.
    """
    thread = getattr(owner, attr_name, None)
    if thread is None:
        return
    try:
        if thread.isRunning():
            thread.blockSignals(True)
            cancel_thread(thread)   # cooperative: stop the work, not just its signals
            zombies = [t for t in getattr(owner, "_zombie_threads", []) if t.isRunning()]
            zombies.append(thread)
            owner._zombie_threads = zombies
    except RuntimeError:
        pass  # underlying C++ object already deleted
    setattr(owner, attr_name, None)


class ThreadOwnerMixin:
    """Bookkeeping for the QThreads a tab starts, so MainWindow.closeEvent can
    stop every one of them.

    Tabs call ``self._track_thread(thread, attr)`` right after constructing a
    worker: the thread is remembered in ``self._tracked_threads`` (finished
    ones are pruned on each call) and, when ``attr`` is given, the previous
    thread stored under that attribute is retired first (see retire_thread)
    and the new one stored there. ``collect_threads_to_stop()`` returns the
    live tracked threads plus any retired-but-still-running zombies. Before
    this mixin each tab hand-listed its thread attributes in its own
    collect_threads_to_stop(), and two of them (the Universe tab's lightweight
    refresh and AI-filter threads, the History tab's AI-diagnosis thread) had
    been left out.
    """

    def _track_thread(self, thread, attr: str = None):
        if attr is not None:
            retire_thread(self, attr)
            setattr(self, attr, thread)
        tracked = [t for t in getattr(self, "_tracked_threads", []) if not _thread_is_finished(t)]
        tracked.append(thread)
        self._tracked_threads = tracked
        return thread

    def collect_threads_to_stop(self) -> list:
        """Every QThread this widget may still have running, for MainWindow.closeEvent."""
        seen: set = set()
        out = []
        for t in list(getattr(self, "_tracked_threads", [])) + list(getattr(self, "_zombie_threads", [])):
            if id(t) in seen or _thread_is_finished(t):
                continue
            seen.add(id(t))
            out.append(t)
        return out


def _thread_is_finished(thread) -> bool:
    try:
        return bool(thread.isFinished())
    except RuntimeError:   # underlying C++ object already deleted
        return True


# ---------------------------------------------------------------------------
# Atomic file I/O helpers
# ---------------------------------------------------------------------------
def atomic_save_json(file_path: str, data: Any, indent: int = 2) -> None:
    """Atomically save data to a JSON file using a temporary file and atomic replace.
    Prevents corrupting or zeroing out the destination file on sudden exit.
    """
    abs_path = os.path.abspath(file_path)
    dir_name = os.path.dirname(abs_path)
    os.makedirs(dir_name, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=dir_name, prefix=".tmp_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
        os.replace(temp_path, abs_path)
    except Exception as e:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                logger.debug("Could not remove temp file %s after a failed save", temp_path, exc_info=True)
        logger.error("Failed to atomically save JSON to %s: %s", file_path, e, exc_info=True)
        raise


def safe_load_json(file_path: str, default: Any = None) -> Any:
    """Safely load JSON data from file_path, returning default on error or missing file."""
    if not os.path.exists(file_path):
        return default if default is not None else {}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Failed to load JSON from %s: %s", file_path, e, exc_info=True)
        return default if default is not None else {}
