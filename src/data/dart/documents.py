"""Income statement from a report's own document (document.xml), for periods the
financial-statement API answers 013 (docs/dart_db_plan_2026-11-26.md, 구현 메모).

Financial-sector filers (bank / insurance / financial holding companies, and a
few investment holding companies) have no ``fnlttSinglAcntAll`` (nor
``fnlttSinglAcnt``) data before 2023 H2. Their reports still carry the
statements, so the consolidated income statement table is read from the
document: "연결 손익계산서" (else "연결 포괄손익계산서"), else the separate one
("손익계산서" / "포괄손익계산서", fs_div OFS).

Layout read from the table head: unit (원 / 천원 / 백만원 / 억원), a 주석 (note)
column to skip, and whether quarterly columns are "3개월, 누적" pairs; without
3-month columns the first amount of a half / Q3 report is the cumulative one.
The result is rows shaped like the API's (``sj_div`` "IS", standard
``account_id``), so ``financials.store_period`` stores them the usual way.

Only the income-statement part of the document is cached (cache/dart/document/),
not the multi-megabyte zip.
"""
from __future__ import annotations

import html
import io
import logging
import os
import re
import zipfile

from data.dart.client import DartApiError
from data.dart.financials import ACCOUNT_IDS, ACCOUNT_NAMES, is_net_income_name, is_owners_name

logger = logging.getLogger(__name__)

_TITLE = re.compile(r"<TITLE[^>]*>(.*?)</TITLE>", re.S)
_TR = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S)
_CELL = re.compile(r"<T[DEHU][^>]*>(.*?)</T[DEHU]>", re.S)
_TABLE_END = re.compile(r"</TABLE>", re.S)
_FIN_WORDS = ("이자수익", "순이자이익", "보험수익", "보험서비스결과", "수수료수익", "투자서비스수익", "보험료수익")


def _text(cell: str) -> str:
    return re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", cell)))


def _amount(cell: str):
    t = _text(cell).replace(",", "").replace("△", "-").replace("▲", "-")
    if t in ("", "-"):
        return None
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _unit(head: str) -> float:
    """The last "단위 : ..." before the statement's first account row."""
    found = re.findall(r"단위\s*[:：]?\s*(억원|백만원|천원|원)", head)
    u = found[-1] if found else ""
    if "억원" in u:
        return 1e8
    if "백만원" in u:
        return 1e6
    if "천원" in u:
        return 1e3
    return 1.0


def find_income_section(doc: str) -> tuple[str, str] | None:
    """(fs_div, the text from the statement's title to the end of its table)."""
    # "2-2.연결포괄손익계산서(손익계산서)" -> "2-2.연결포괄손익계산서"
    titles = [(m, re.sub(r"\([^)]*\)$", "", _text(m.group(1)))) for m in _TITLE.finditer(doc)]

    def pick(consolidated: bool):
        cands = [(m, t) for m, t in titles
                 if ("연결" in t) == consolidated and t.endswith("손익계산서") and "요약" not in t]
        plain = [x for x in cands if not x[1].endswith("포괄손익계산서")]
        return (plain or cands or [None])[0]

    for consolidated, fs_div in ((True, "CFS"), (False, "OFS")):
        hit = pick(consolidated)
        if hit:
            start = hit[0].end()
            # The title is followed by a small heading table, then the statement table.
            ends = list(_TABLE_END.finditer(doc, start, start + 600_000))
            end = ends[1].end() if len(ends) > 1 else (ends[0].end() if ends else start + 200_000)
            return fs_div, doc[hit[0].start():end]
    # Older documents (2021-2022) have no per-statement titles: the statements
    # are consecutive tables under "2. 연결재무제표" / "4. 재무제표". The income
    # statement is the first of them with an operating-income or net-income row
    # (the balance sheet comes before it and has neither).
    for consolidated, fs_div in ((True, "CFS"), (False, "OFS")):
        for i, (m, t) in enumerate(titles):
            body = re.sub(r"^[\dIVX]+[-.\d]*\.?", "", t)
            if body != ("연결재무제표" if consolidated else "재무제표"):
                continue
            stop = titles[i + 1][0].start() if i + 1 < len(titles) else len(doc)
            part = doc[m.end():stop]
            for tm in re.finditer(r"<TABLE.*?</TABLE>", part, re.S):
                rows = [_CELL.findall(tr) for tr in _TR.findall(tm.group(0))]
                names = {_clean_name(c[0]) for c in rows if c}
                if any(n in ACCOUNT_NAMES["op_income"] or is_net_income_name(n) for n in names):
                    return fs_div, part[max(0, tm.start() - 15000):tm.end()]
    return None


def _clean_name(cell: str) -> str:
    name = re.sub(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX\d]+[.)]", "", _text(cell))
    return re.sub(r"\((주|주석|Note)[^)]*\)$", "", name)


def parse_income_table(section: str, quarterly: bool):
    """{metric: (first amount, second amount or None)} in KRW and whether
    the table has 3-month columns. Rows are matched like the API's names."""
    first_row = len(section)
    for tm in _TR.finditer(section):
        cells = _CELL.findall(tm.group(1))
        n = _clean_name(cells[0]) if cells else ""
        if n in ACCOUNT_NAMES["op_income"] or n in ACCOUNT_NAMES["revenue"] or is_net_income_name(n):
            first_row = tm.start()
            break
    head = _text(section[:first_row])
    unit = _unit(head)
    rows = [_CELL.findall(tr) for tr in _TR.findall(section)]
    note_col = None
    three_month = "3개월" in head
    for cells in rows[:12]:
        texts = [_text(c) for c in cells]
        if any(t in ("주석", "주") for t in texts):
            note_col = next(i for i, t in enumerate(texts) if t in ("주석", "주"))
            break
    out: dict = {}
    flat = _text(section)
    financial = any(w in flat for w in _FIN_WORDS)
    nci = "비지배" in flat
    for cells in rows:
        if len(cells) < 2:
            continue
        name = _clean_name(cells[0])
        values = [_amount(c) for i, c in enumerate(cells[1:], start=1) if i != note_col]
        values = [v for v in values if v is not None]
        if not values:
            continue
        metric = None
        if name in ACCOUNT_NAMES["revenue"]:
            metric = "revenue"
        elif name in ACCOUNT_NAMES["op_income"]:
            metric = "op_income"
        elif is_net_income_name(name):
            metric = "net_income"
        elif is_owners_name(name):
            metric = "net_income_owners"
        if metric and metric not in out:
            first = values[0] * unit
            second = values[1] * unit if len(values) > 1 else None
            out[metric] = (first, second)
    return out, three_month, financial, nci


def income_rows(doc: str, rcept_no: str, reprt_code: str):
    """(fs_div, API-shaped rows, financial, has a non-controlling line) from a document, or None."""
    found = find_income_section(doc)
    if not found:
        return None
    return section_rows(*found, rcept_no, reprt_code)


def section_rows(fs_div: str, section: str, rcept_no: str, reprt_code: str):
    """income_rows() of an already located statement section."""
    quarterly = reprt_code in ("11012", "11014")
    metrics, three_month, financial, nci = parse_income_table(section, quarterly)
    if not metrics:
        return None
    rows = []
    for i, (metric, (first, second)) in enumerate(metrics.items()):
        if reprt_code == "11013" or reprt_code == "11011":
            cur, add = first, None
        elif three_month:
            cur, add = first, second
        else:
            cur, add = None, first          # cumulative only: the quarter is derived
        rows.append({"rcept_no": rcept_no, "sj_div": "IS", "account_id": ACCOUNT_IDS[metric][0],
                     "account_nm": f"[원문] {metric}", "ord": str(i + 1),
                     "thstrm_amount": cur, "thstrm_add_amount": add, "currency": "KRW"})
    return fs_div, rows, financial, nci


def decode_document(raw: bytes) -> str:
    """Document text. Older documents declare utf-8 but are cp949 (KB금융
    2022 Q1), so a body that is not valid UTF-8 is read as cp949."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp949", errors="replace")


_MARK = re.compile(r"^<!--fs_div=(CFS|OFS)-->")


def fetch_document_income(client, rcept_no: str, reprt_code: str, cache_dir: str):
    """income_rows() of a filing's document. The statement section is cached
    with its fs_div in a leading comment (an empty file: no statement found)."""
    path = os.path.join(cache_dir, "document", rcept_no[:4], f"{rcept_no}_is.xml")
    cached = None
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cached = f.read()
        except OSError:
            cached = None
    if cached is not None:
        m = _MARK.match(cached)
        if m:
            return section_rows(m.group(1), cached[m.end():], rcept_no, reprt_code)
        if cached == "":
            return None
    try:
        body = client.get_bytes("document.xml", {"rcept_no": rcept_no})
        with zipfile.ZipFile(io.BytesIO(body)) as zf:
            names = zf.namelist()
            main = next((n for n in names if n == f"{rcept_no}.xml"), names[0])
            raw = zf.read(main)
    except (DartApiError, zipfile.BadZipFile, IndexError, KeyError) as e:
        logger.warning("DART document %s unavailable", rcept_no, exc_info=True)
        status = getattr(e, "status", "")
        if not (status == "network_error" or str(status).startswith("http")):
            _write_section(path, None)     # DART's own answer (e.g. 014 no file): not asked again
        return None
    found = find_income_section(decode_document(raw))
    _write_section(path, found)
    return section_rows(*found, rcept_no, reprt_code) if found else None


def _write_section(path: str, found) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"<!--fs_div={found[0]}-->{found[1]}" if found else "")
    except OSError:
        logger.warning("Could not cache DART document section %s", path, exc_info=True)
