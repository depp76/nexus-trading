"""corpCode.xml -> company (docs/dart_db_plan_2026-11-26.md 2, 6, 11-3-1).

DART's company list maps a 6-digit stock code to the 8-digit corp code. Only
rows with a stock code (listed companies) are kept; the market (KOSPI/KOSDAQ)
and fiscal month come from the filing list (filings.py), so they are left as
they are here.
"""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

from data.dart.store import now_iso

CORP_LIST_MAX_AGE_DAYS = 7     # plan 2: refresh weekly


def parse_corp_zip(body: bytes) -> list[dict]:
    """[{corp_code, corp_name, stock_code}] of the listed companies in corpCode.xml's zip."""
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        name = next(n for n in zf.namelist() if n.lower().endswith(".xml"))
        root = ET.fromstring(zf.read(name))
    out = []
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not stock:
            continue
        out.append({
            "corp_code": (el.findtext("corp_code") or "").strip(),
            "corp_name": (el.findtext("corp_name") or "").strip(),
            "stock_code": stock.zfill(6),
        })
    return [r for r in out if r["corp_code"]]


def store_companies(conn, rows: list[dict]) -> int:
    """Upsert `rows` into company (name and stock code only); returns rows written."""
    now = now_iso()
    conn.executemany(
        "INSERT INTO company(corp_code, stock_code, corp_name, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(corp_code) DO UPDATE SET stock_code = excluded.stock_code, "
        "corp_name = excluded.corp_name, updated_at = excluded.updated_at",
        [(r["corp_code"], r["stock_code"], r["corp_name"], now) for r in rows])
    return len(rows)


def sync_corp_codes(client, conn) -> int:
    """Download corpCode.xml (one call) and store the listed companies."""
    body = client.get_bytes("corpCode.xml", {})
    n = store_companies(conn, parse_corp_zip(body))
    conn.commit()
    return n
