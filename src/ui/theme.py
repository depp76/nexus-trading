"""ui/theme.py — the app's single global QSS + design tokens (docs/ui.md
section 6, "Phase 0").

Before this module, `main.py`'s `app.setStyleSheet(...)` hardcoded a
different, older palette (grey #f0f0f0 window, solid-blue-fill buttons on
every screen, grey table headers) directly inline. That global sheet always
wins over anything a tab sets on itself, so it silently overrode every
attempt elsewhere in the app to follow docs/ui.md's palette -- coloring a
table cell did nothing for the button/header/tab chrome around it, since the
chrome came from this one block regardless. This module is now the single
place that chrome comes from; nothing else should call
`QWidget.setStyleSheet(...)` with a hardcoded hex color for something this
file already has a rule for -- add an objectName selector here instead (see
`QPushButton#primary` / `#danger` below).
"""
import os

from PyQt6.QtGui import QColor, QPalette

from ui.colors import (
    DANGER, DANGER_BG, DANGER_LINE, WARN,
    OK_BG, OK_TEXT, WARN_BG, WARN_TEXT, FAIL_BG, FAIL_TEXT, NA_BG, NA_TEXT,
)

# Soft Slate (docs/ui_design_proposal_2026-11-14.md, docs/ui.md 8.16): a
# lifted blue-gray dark theme with light text and a restrained lavender
# accent. Every text role below is an explicit light color -- nothing on a
# dark surface may inherit Qt's default black (app_palette() covers the
# widgets the QSS does not reach).
BG          = "#303844"   # app / window background
SURFACE     = "#3a4452"   # tables, inputs, charts, dialogs, cards
ZEBRA       = "#3e4857"   # even rows
HDR_BG      = "#424e5e"   # table header
GRP_BG      = "#46525f"   # two-row group header, top row
TEXT        = "#f0f2f6"   # names, numbers, required labels, input values
TEXT_SUB    = "#dce1e9"
TEXT_MUTED  = "#c6ceda"   # descriptions, units, times, column titles, placeholders
TEXT_FAINT  = "#afb9c6"
TEXT_EMPTY  = "#8592a3"   # "-" / disabled (readable gray, never black)
LINE_STRONG = "#66768a"   # input / button outlines
LINE        = "#526071"   # section dividers, card borders
LINE_SOFT   = "#465263"
LINE_GRID   = "#47535f"   # cell grid lines: quieter than the text (proposal 3.2)
LINE_GROUP  = "#5d6b7d"   # table frame + vertical divider between column groups
ACCENT      = "#c4b8f1"   # lavender: selection marker, main chart series
ACCENT_TEXT = "#d6cdf8"   # accent-colored text on the dark surfaces
ACCENT_BG   = "#514b69"   # selected row / checked state
PRIMARY_BG  = "#675a94"   # the one primary action per tab: filled
PRIMARY_HOVER = "#75689f"
PRIMARY_LINE = "#b8a9e3"
PRIMARY_TEXT = "#ffffff"
INK_ON_LIGHT = "#1f2530"  # text on a light chip/pill (chart end labels on a light series color)


# Spin-box arrows: once the QSS styles a QSpinBox, Fusion stops drawing its
# arrows, so they come from these two files (filled in TEXT_MUTED).
_ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons").replace("\\", "/")
ARROW_UP_ICON = f"{_ICON_DIR}/arrow_up.svg"
ARROW_DOWN_ICON = f"{_ICON_DIR}/arrow_down.svg"


def app_palette() -> QPalette:
    """The Qt palette matching the tokens above. Fusion draws menus, combo
    lists, tooltips, check boxes, spin boxes and scroll bars from the
    palette, so without it those keep Qt's light-theme black text."""
    pal = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: ZEBRA,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.PlaceholderText: TEXT_MUTED,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: PRIMARY_TEXT,
        QPalette.ColorRole.Highlight: ACCENT_BG,
        QPalette.ColorRole.HighlightedText: TEXT,
        QPalette.ColorRole.ToolTipBase: HDR_BG,
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.Link: ACCENT_TEXT,
        QPalette.ColorRole.LinkVisited: ACCENT,
        QPalette.ColorRole.Light: LINE_STRONG,
        QPalette.ColorRole.Midlight: LINE,
        QPalette.ColorRole.Mid: LINE_SOFT,
        QPalette.ColorRole.Dark: BG,
        QPalette.ColorRole.Shadow: "#20262f",
    }
    for role, hex_ in roles.items():
        pal.setColor(role, QColor(hex_))
    disabled = QPalette.ColorGroup.Disabled
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        pal.setColor(disabled, role, QColor(TEXT_EMPTY))
    return pal


def apply_matplotlib_theme() -> None:
    """Chart colors for the dark theme: figure/axes faces, axis lines,
    ticks, labels, legends and grid all get a theme color (proposal 3:
    no default black text on a dark chart). Figures read these rcParams
    when they are created, so call this once at startup."""
    from matplotlib import rcParams
    rcParams.update({
        "figure.facecolor": SURFACE,
        "figure.edgecolor": SURFACE,
        "savefig.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": LINE,
        "axes.labelcolor": TEXT_MUTED,
        "axes.titlecolor": TEXT,
        "text.color": TEXT,
        "xtick.color": TEXT_MUTED,
        "ytick.color": TEXT_MUTED,
        "xtick.labelcolor": TEXT_MUTED,
        "ytick.labelcolor": TEXT_MUTED,
        "grid.color": LINE,
        "legend.facecolor": HDR_BG,
        "legend.edgecolor": LINE,
        "legend.labelcolor": TEXT,
        "legend.framealpha": 0.92,
    })


def app_qss(font_css: str) -> str:
    """The whole app's QSS, built from the tokens above (docs/ui.md
    1.1-1.10, 8.16). Called once at startup from main.py, next to
    app_palette() and apply_matplotlib_theme()."""
    return f"""
    QMainWindow, QDialog, QWidget#Page {{ background-color: {BG}; color: {TEXT}; }}
    QLabel, QCheckBox, QRadioButton, QGroupBox {{ color: {TEXT}; }}
    QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled {{ color: {TEXT_EMPTY}; }}
    QToolTip {{ background: {HDR_BG}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px 6px; }}
    QStatusBar {{ color: {TEXT_MUTED}; }}

    /* Buttons: neutral outline by default. Accent is opt-in via objectName,
       never picked per function (docs/ui.md 1.6: button color means
       primary/secondary/danger, never "which feature this is"). The tab's
       one primary action is the only filled button (8.16). */
    QPushButton {{
        background: {SURFACE}; color: {TEXT};
        border: 1px solid {LINE_STRONG}; border-radius: 6px;
        padding: 5px 12px; font-weight: 400; {font_css}
        min-height: 16px;   /* 16 + 2x5 padding + 2x1 border = ui.common.BTN_H (28) */
    }}
    QPushButton:hover    {{ background: {HDR_BG}; }}
    QPushButton:pressed  {{ background: {LINE_SOFT}; }}
    QPushButton:disabled {{ color: {TEXT_EMPTY}; border-color: {LINE_SOFT}; background: {BG}; }}
    QPushButton:checked {{
        background: {ACCENT_BG}; color: {TEXT}; border-color: {ACCENT}; font-weight: 600;
    }}
    /* A multi-select segment's focused button (Strategy variants, docs/ui.md 8.14). */
    QPushButton[segFocus="true"]:checked {{ border: 2px solid {ACCENT}; }}
    QPushButton#primary {{
        background: {PRIMARY_BG}; color: {PRIMARY_TEXT}; border: 1px solid {PRIMARY_LINE}; font-weight: 600;
    }}
    QPushButton#primary:hover {{ background: {PRIMARY_HOVER}; }}
    QPushButton#primary:disabled {{ background: {ACCENT_BG}; color: {TEXT_FAINT}; border-color: {LINE}; }}
    QPushButton#danger  {{ color: {DANGER}; border-color: {DANGER_LINE}; }}
    QPushButton#danger:hover {{ background: {DANGER_BG}; }}
    QToolButton {{ color: {TEXT}; }}

    /* Tables (docs/ui.md 8.2 / 8.16): one frame line, a quiet cell grid
       (LINE_GRID, below the text in contrast) and the column-group dividers
       the tables paint themselves in LINE_GROUP. Row height / zebra /
       selection come from ui.common.setup_table(). */
    QTableWidget, QTableView, QListWidget, QListView, QTreeView {{
        background-color: {SURFACE}; color: {TEXT};
        alternate-background-color: {ZEBRA};
        gridline-color: {LINE_GRID};
        selection-background-color: {ACCENT_BG};
        selection-color: {TEXT};
        border: 1px solid {LINE_GROUP}; border-radius: 0px;
        {font_css}
    }}
    QTableWidget::item, QTableView::item {{ padding: 1px 3px; }}
    QTableWidget::item:hover {{ background: {ACCENT_BG}; }}
    QTableCornerButton::section {{ background: {HDR_BG}; border: none; }}

    QHeaderView {{ background-color: {HDR_BG}; }}
    QHeaderView::section {{
        background-color: {HDR_BG}; color: {TEXT_MUTED};
        border: none; border-bottom: 1px solid {LINE_GROUP};
        border-right: 1px solid {LINE_GRID};
        padding: 5px 8px; font-weight: 600; {font_css}
    }}

    QTabWidget::pane {{ border: 1px solid {LINE}; background: {SURFACE}; border-radius: 8px; }}
    QTabBar::tab {{
        background: transparent; color: {TEXT_MUTED};
        border: 1px solid transparent; border-radius: 6px;
        padding: 5px 12px; margin-right: 2px; font-weight: 400; {font_css}
    }}
    QTabBar::tab:selected {{ background: {ACCENT_BG}; color: {TEXT}; border-color: {ACCENT}; }}
    QTabBar::tab:hover:!selected {{ background: {HDR_BG}; color: {TEXT}; }}

    /* Top-level tabs only (Trading Universe/Trading History/Total Assets/
       Strategy), not a nested sub-tab bar: 11pt semibold (proposal 3: 15px
       in the mockup). */
    QTabWidget#MainTabs QTabBar::tab {{ font-size: 11pt; font-weight: 600; }}

    /* Strategy result tabs: the last one is "Warnings (n)", amber while n > 0
       (set through the "warn" property; docs/ui.md 8.10). */
    QTabWidget#ResultTabs[warn="true"] QTabBar::tab:last {{ color: {WARN}; }}

    /* Inputs: one look for text, combo, date and number fields (8.16). */
    QLineEdit, QComboBox, QDateEdit, QSpinBox, QDoubleSpinBox, QTextEdit, QPlainTextEdit {{
        background: {SURFACE}; color: {TEXT};
        border: 1px solid {LINE_STRONG}; border-radius: 6px;
        padding: 4px 9px; {font_css}
        selection-background-color: {ACCENT_BG}; selection-color: {TEXT};
    }}
    QSpinBox, QDoubleSpinBox {{ padding: 4px 20px 4px 9px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        subcontrol-origin: border; width: 16px;
        background: transparent; border: none; border-left: 1px solid {LINE};
    }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-position: top right; border-top-right-radius: 6px; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-position: bottom right; border-bottom-right-radius: 6px; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: {HDR_BG}; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url("{ARROW_UP_ICON}"); width: 8px; height: 5px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url("{ARROW_DOWN_ICON}"); width: 8px; height: 5px; }}
    QLineEdit:focus, QComboBox:focus, QDateEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus,
    QTextEdit:focus, QPlainTextEdit:focus {{ border-color: {ACCENT}; }}
    QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QSpinBox:disabled,
    QDoubleSpinBox:disabled {{ color: {TEXT_EMPTY}; background: {BG}; border-color: {LINE_SOFT}; }}
    QLineEdit[readOnly="true"] {{ background: transparent; border: none; color: {TEXT}; }}
    QComboBox QAbstractItemView {{
        background: {SURFACE}; color: {TEXT}; border: 1px solid {LINE};
        selection-background-color: {ACCENT_BG}; selection-color: {TEXT}; outline: 0;
    }}

    QMenu {{ background: {SURFACE}; color: {TEXT}; border: 1px solid {LINE}; padding: 4px 0; }}
    QMenu::item {{ padding: 5px 18px; }}
    QMenu::item:selected {{ background: {ACCENT_BG}; color: {TEXT}; }}
    QMenu::item:disabled {{ color: {TEXT_EMPTY}; }}
    QMenu::separator {{ height: 1px; background: {LINE}; margin: 4px 8px; }}

    QSplitter::handle {{ background: {BG}; }}
    QSplitter::handle:hover {{ background: {LINE}; }}

    /* QDateEdit's calendar popup: the generic table rules above would
       inflate its fixed-size day grid, so scope them back out. */
    QCalendarWidget QTableView {{
        border: none; border-radius: 0;
        selection-background-color: {ACCENT_BG}; selection-color: {TEXT};
    }}
    QCalendarWidget QTableView::item {{ padding: 0; }}
    QCalendarWidget QWidget#qt_calendar_navigationbar {{ background: {HDR_BG}; }}
    QCalendarWidget QToolButton {{
        background: transparent; color: {TEXT}; border: none; border-radius: 4px;
        padding: 3px 8px; font-weight: 600; {font_css}
    }}
    QCalendarWidget QToolButton:hover {{ background: {ACCENT_BG}; }}
    QCalendarWidget QToolButton::menu-indicator {{ image: none; }}

    QFrame#DashboardCard {{
        background-color: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px;
    }}
    QFrame#kpiDivider {{ color: {LINE}; background: {LINE}; border: none; max-width: 1px; }}

    /* Sizes in pt so they scale with DPI like every create_font() call
       (docs/ui.md 6.4); the ui.common FONT_* scale. */
    QLabel#appTitle    {{ color: {TEXT}; }}
    QLabel#appSubtitle {{ color: {TEXT_MUTED}; }}
    QLabel#kpiLabel {{ color: {TEXT_MUTED}; font-size: 8pt; font-weight: 600; }}
    QLabel#kpiValue {{ color: {TEXT}; font-size: 13pt; }}
    QLabel#kpiSub   {{ color: {TEXT_FAINT}; font-size: 8pt; }}

    /* Secondary text: subtitles, status lines, footnotes. */
    QLabel#muted {{ color: {TEXT_MUTED}; }}
    QLabel#faint {{ color: {TEXT_FAINT}; }}
    /* Caveats attached to a result (Strategy's Warnings panel): caution, not failure (8.3). */
    QLabel#warn  {{ color: {WARN}; }}
    /* What a Strategy result was computed with (8.16). */
    QLabel#resultBasis {{ color: {TEXT_MUTED}; }}

    /* Status badges (docs/ui.md 8.3 / 8.10): a judgement, not a button, so
       a symbol in the text is allowed. ui.common.make_badge() sets these. */
    QLabel#badgeOk   {{ color: {OK_TEXT};   background: {OK_BG};   border-radius: 9px; padding: 1px 8px; font-weight: 600; font-size: 8pt; }}
    QLabel#badgeWarn {{ color: {WARN_TEXT}; background: {WARN_BG}; border-radius: 9px; padding: 1px 8px; font-weight: 600; font-size: 8pt; }}
    QLabel#badgeFail {{ color: {FAIL_TEXT}; background: {FAIL_BG}; border-radius: 9px; padding: 1px 8px; font-weight: 600; font-size: 8pt; }}
    QLabel#badgeNa   {{ color: {NA_TEXT};   background: {NA_BG};   border-radius: 9px; padding: 1px 8px; font-weight: 600; font-size: 8pt; }}

    /* Card-style KPI (ui.common.KpiCard; Strategy results). */
    QFrame#KpiCard {{ background: {SURFACE}; border: 1px solid {LINE}; border-radius: 8px; }}
    QFrame#KpiCard QLabel#kpiValue {{ font-size: 13pt; }}   /* FONT_KPI (proposal 3: 18px) */
    QLabel#kpiHelp {{ color: {TEXT_MUTED}; font-size: 8pt; }}
    QLabel#settingsChip {{ background: {HDR_BG}; color: {TEXT_SUB}; border-radius: 4px; padding: 2px 8px; }}
    QLabel#settingsChip[warn="true"] {{ color: {WARN}; font-weight: 600; }}   /* hold-out included (8.12) */
    """
