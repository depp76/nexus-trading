"""paths.py — Single source of truth for runtime file locations.

Every state/cache/log file the app reads or writes lives at the repository
root (one level above src/). Resolving them from this module's own location
rather than the current working directory means the app behaves identically
no matter which folder it is launched from (roadmap 6-1d).
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def root_path(*parts: str) -> str:
    """Absolute path of a file/folder under the repository root."""
    return os.path.join(BASE_DIR, *parts)


ENV_FILE = root_path(".env")
APP_LOG_FILE = root_path("app.log")

# Trade log (SQLite, source of truth)
DB_FILE = root_path("portfolio.db")

# Per-tab state / caches
CUSTOM_SETTINGS_FILE = root_path("custom_settings.json")
UNIVERSE_CACHE_FILE = root_path("universe_cache.json")
TRADING_RECORD_FILE = root_path("trading_record.json")
VKOSPI_CACHE_FILE = root_path("vkospi_cache.json")
KIS_TOKEN_CACHE_FILE = root_path("kis_token_cache.json")

# Automatic backups (AutoBackupThread) go to archive/auto_<timestamp>/
ARCHIVE_DIR = root_path("archive")

# Research data caches for the strategy layer (gitignored, rebuildable):
# per-ticker investor-flow history (data/flows.py) and the CD 91-day rate
# series (data/rates.py). An optional hand-made cd91.csv (Date,Rate in %) at
# the repo root is the offline source for the risk-free rate.
CACHE_DIR = root_path("cache")
FLOWS_CACHE_DIR = root_path("cache", "investor_flows")
CD91_CACHE_FILE = root_path("cache", "cd91.json")
CD91_CSV_FILE = root_path("cd91.csv")
# Last downloaded JP 10Y JGB yield series (data/collectors/krx.py), served when the
# MOF download fails.
JP10Y_CACHE_FILE = root_path("cache", "jp10y.json")
# KRX open/closed days from the KIS holiday API (data/krx_calendar.py).
KRX_CALENDAR_CACHE_FILE = root_path("cache", "krx_calendar.json")

# DART financial statements (src/data/dart/, docs/dart_db_plan_2026-11-26.md):
# the SQLite DB at the root (not a cache: it holds years of collected filings),
# and the raw API replies + the collector's lock file under cache/dart/.
DART_DB_FILE = root_path("dart.db")
DART_CACHE_DIR = root_path("cache", "dart")

# Backtest reports written by the Strategy tab ("Save Report").
REPORTS_DIR = root_path("reports")
# One JSON line per Strategy-tab research run whose dates reach past the
# development window (trend_following.md 6-5 / 9-23 / 10-7): the record of
# when the hold-out was opened. Not a cache -- never delete it to "clean up".
HOLDOUT_LEDGER_FILE = root_path("holdout_ledger.jsonl")
