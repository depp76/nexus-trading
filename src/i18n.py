"""i18n.py — UI language (Korean / English) for visible labels (docs/ui.md 8.20).

Every visible string that has a Korean form is written as `tr("한국어", "English")`
next to where it is used, so the two stay side by side and tests that assert the
Korean literal keep working under the default language.

The language is fixed for the life of the process: `main.py` calls
`load_language()` before it imports any `ui` module, so module-level constants
built with `tr()` (column lists, help texts) come out in the chosen language.
Switching languages saves the choice and restarts the app (`MainWindow`).

No Qt widgets here and no imports from `ui`/`data`/`strategy`, so any layer
(including `strategy.trend_following.variant_info`) may use `tr()`.
"""
from __future__ import annotations

LANGUAGES = ("ko", "en")
DEFAULT_LANGUAGE = "ko"
LANGUAGE_NAMES = {"ko": "한국어", "en": "English"}

# QSettings scope shared with Trading History's principal/deposit values; the
# language is not in custom_settings.json, which UniverseTab rewrites whole.
_SETTINGS_SCOPE = ("PortfolioManagement", "PortfolioManagement")
_SETTINGS_KEY = "ui/language"

_language = DEFAULT_LANGUAGE


def language() -> str:
    return _language


def set_language(lang: str) -> None:
    """Set the in-process language (unknown values fall back to the default)."""
    global _language
    _language = lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def tr(ko: str, en: str) -> str:
    """The Korean or English form of one visible string."""
    return en if _language == "en" else ko


def _settings():
    from PyQt6.QtCore import QSettings
    return QSettings(*_SETTINGS_SCOPE)


def saved_language() -> str:
    try:
        value = _settings().value(_SETTINGS_KEY, DEFAULT_LANGUAGE)
    except Exception:
        return DEFAULT_LANGUAGE
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def load_language() -> str:
    """Apply the saved language to this process; returns it."""
    set_language(saved_language())
    return _language


def save_language(lang: str) -> None:
    if lang not in LANGUAGES:
        raise ValueError(f"unknown language: {lang!r}")
    s = _settings()
    s.setValue(_SETTINGS_KEY, lang)
    s.sync()
