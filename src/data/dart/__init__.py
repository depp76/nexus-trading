"""data.dart - OpenDART financial statements in dart.db (docs/dart_db_plan_2026-11-26.md).

Modules (plan 6): client (HTTP, key, limits, cache), corp (company list),
filings (periodic-report list), financials (fin_raw -> fin_std), store (schema),
query (read side, as-of aware), lock (inter-process lock), sync (``run_sync``,
the one collection entry point). Screens and strategies read through ``query``
only. Like the rest of ``data/``, nothing here imports Qt or a strategy package.
"""
