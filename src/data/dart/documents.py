"""Income statement from a report's own document (document.xml), for periods the
financial-statement API answers 013 (docs/dart_db_plan_2026-11-26.md, 구현 메모).

Financial-sector filers (bank / insurance / financial holding companies, and a
few investment holding companies) have no ``fnlttSinglAcntAll`` (nor
``fnlttSinglAcnt``) data before 2023 H2. Their reports still carry the
statements, so the consolidated income statement table is read from the
document: "연결 손익계산서" (else "연결 포괄손익계산서"), else the separate one
("손익계산서" / "포괄손익계산서", fs_div OFS).

Layout read from the table head: unit (원 / 천원 / 백만원 / 억원) and currency,
a 주석 (note) column to skip, and whether quarterly columns are "3개월, 누적"
pairs; without 3-month columns the first amount of a half / Q3 report is the
cumulative one. Amount cells keep their column position: the current period is
the first block of columns, so a blank current cell ("-") is a missing value
and never replaced by the next column's number (review_agy H2).
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
from data.dart.financials import ACCOUNT_IDS, ACCOUNT_NAMES, account_name, is_net_income_name, is_owners_name

logger = logging.getLogger(__name__)

_TITLE = re.compile(r"<TITLE[^>]*>(.*?)</TITLE>", re.S | re.I)
_TR = re.compile(r"<TR[^>]*>(.*?)</TR>", re.S | re.I)
_CELL = re.compile(r"<T[DEHU][^>]*>(.*?)</T[DEHU]>", re.S | re.I)
_TABLE = re.compile(r"<TABLE.*?</TABLE>", re.S | re.I)
_TABLE_END = re.compile(r"</TABLE>", re.S | re.I)
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


_CURRENCIES = (("USD", ("USD", "US$", "달러", "미화")), ("JPY", ("JPY", "엔")), ("CNY", ("CNY", "RMB", "위안")),
               ("EUR", ("EUR", "유로")), ("HKD", ("HKD",)))


def _unit(head: str) -> tuple[float, str]:
    """(multiplier, currency) from the last "단위 : ..." before the statement's
    first account row: 원 / 천원 / 백만원 / 억원, and a foreign reporting currency
    ("천USD", "백만엔") is kept as such -- amounts are never converted (plan 4-6)."""
    found = re.findall(r"단위[:：]?([^)\]]{1,15})", head)
    u = found[-1].upper() if found else ""
    mult = 1e8 if "억" in u else 1e6 if "백만" in u else 1e3 if "천" in u else 1.0
    currency = next((code for code, words in _CURRENCIES if any(w in u for w in words)), "KRW")
    return mult, currency


def _has_account_row(table: str) -> bool:
    for tr in _TR.findall(table):
        cells = _CELL.findall(tr)
        n = _clean_name(cells[0]) if cells else ""
        if n in ACCOUNT_NAMES["op_income"] or n in ACCOUNT_NAMES["revenue"] or is_net_income_name(n):
            return True
    return False


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
            # The statement is the first table after the title with an account
            # row; heading tables (zero, one or two of them) come before it and
            # the next statement after it (review_agy H2).
            for tm in _TABLE.finditer(doc, start, start + 600_000):
                if _has_account_row(tm.group(0)):
                    return fs_div, doc[hit[0].start():tm.end()]
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
            for tm in _TABLE.finditer(part):
                rows = [_CELL.findall(tr) for tr in _TR.findall(tm.group(0))]
                names = {_clean_name(c[0]) for c in rows if c}
                if any(n in ACCOUNT_NAMES["op_income"] or is_net_income_name(n) for n in names):
                    return fs_div, part[max(0, tm.start() - 15000):tm.end()]
    return None


def _clean_name(cell: str) -> str:
    return account_name(_text(cell))


_CELL_TAG = re.compile(r"<T[DEHU]([^>]*)>(.*?)</T[DEHU]>", re.S | re.I)
_SPAN = re.compile(r"(ROWSPAN|COLSPAN)\s*=\s*\"?(\d+)", re.I)


def _grid(table: str) -> list[list[tuple[int, int, str]]]:
    """The table's rows as [(first column, last column, cell html)], with
    COLSPAN / ROWSPAN resolved to column positions (a cell spanning rows
    pushes the next rows' cells to the right, as a browser lays them out)."""
    out, carry = [], {}                 # carry: column -> rows it stays occupied
    for tr in _TR.findall(table):
        row, col = [], 0
        for attrs, body in _CELL_TAG.findall(tr):
            while carry.get(col, 0) > 0:
                col += 1
            spans = {k.upper(): int(v) for k, v in _SPAN.findall(attrs)}
            width, height = max(1, spans.get("COLSPAN", 1)), max(1, spans.get("ROWSPAN", 1))
            row.append((col, col + width - 1, body))
            if height > 1:
                for c in range(col, col + width):
                    carry[c] = height           # counted down below, this row included
            col += width
        for c in list(carry):
            carry[c] -= 1
            if carry[c] <= 0:
                del carry[c]
        out.append(row)
    return out


def _statement_table(section: str) -> str | None:
    """The income statement inside a located section: its first table with an
    account row (heading tables and, in older layouts, the tail of the balance
    sheet come before it and must not be read as statement rows)."""
    for tm in _TABLE.finditer(section):
        if _has_account_row(tm.group(0)):
            return tm.group(0)
    return None


def _columns(header: list[list[tuple[int, int, str]]]):
    """(columns of the current period's first amount, columns of its cumulative
    amount or None, note columns, has 3-month columns) from the header rows.

    With "3개월" / "누적" headers the first of each (the current period comes
    first) gives the two column groups. Otherwise the first period header of
    the top row is the current period. A period shown over two columns
    (sub-amount | total, older layouts) keeps both: the filled one is read."""
    cells = [(a, b, _text(body)) for row in header for a, b, body in row]
    note = set()
    for a, b, t in cells:
        if t in ("주석", "주"):
            note.update(range(a, b + 1))
    three = sorted((a, b) for a, b, t in cells if "3개월" in t)
    if three:
        cum = sorted((a, b) for a, b, t in cells if "누적" in t)
        cur = range(three[0][0], three[0][1] + 1)
        return cur, (range(cum[0][0], cum[0][1] + 1) if cum else None), note, True
    for row in header:
        periods = sorted((a, b) for a, b, body in row if a > 0 and not set(range(a, b + 1)) & note and _text(body))
        if periods:
            return range(periods[0][0], periods[0][1] + 1), None, note, False
    return None, None, note, False


def parse_income_table(section: str, reprt_code: str = "11011"):
    """({metric: (first amount, second amount)}, has 3-month columns, financial
    filer, has a non-controlling line, currency). Amounts are in the reporting
    currency's unit; with 3-month columns first = 3 months, second = cumulative.

    Columns are taken from the table's own header (see _columns), so a blank
    cell of the current period is a missing value and never the next column's
    number (review_agy H2). Rows are matched like the API's names; the owners'
    line is read only below the net income line (the balance sheet has a
    "지배기업 소유주 ... 자본" line of its own)."""
    table = _statement_table(section)
    if table is None:
        return {}, False, False, False, "KRW"
    unit, currency = _unit(_text(section[:section.index(table)]) + _text(table[:3000]))
    grid = _grid(table)
    first_data = next((i for i, row in enumerate(grid)
                       if any(_amount(body) is not None for a, _b, body in row if a > 0)), len(grid))
    cur_cols, cum_cols, note_cols, three_month = _columns(grid[:first_data])
    flat = _text(table)
    financial = any(w in flat for w in _FIN_WORDS)
    nci = "비지배" in flat

    def value(row, cols):
        if cols is None:
            return None
        return _first(_amount(body) for a, _b, body in row if a in cols)

    out: dict = {}
    for row in grid[first_data:]:
        if len(row) < 2 or row[0][0] != 0:
            continue
        name = _clean_name(row[0][2])
        metric = None
        if name in ACCOUNT_NAMES["revenue"]:
            metric = "revenue"
        elif name in ACCOUNT_NAMES["op_income"]:
            metric = "op_income"
        elif is_net_income_name(name):
            metric = "net_income"
        elif is_owners_name(name) and "net_income" in out:
            metric = "net_income_owners"
        if not metric or metric in out:
            continue
        if cur_cols is None:
            # No usable header: the first filled amount cell that is not a note.
            first = _first(_amount(body) for a, _b, body in row if a > 0 and a not in note_cols)
            second = None
        else:
            first, second = value(row, cur_cols), value(row, cum_cols)
        if first is None and second is None:
            continue
        out[metric] = (None if first is None else first * unit, None if second is None else second * unit)
    return out, three_month, financial, nci, currency


def _first(values):
    return next((v for v in values if v is not None), None)


def income_rows(doc: str, rcept_no: str, reprt_code: str):
    """(fs_div, API-shaped rows, financial, has a non-controlling line) from a document, or None."""
    found = find_income_section(doc)
    if not found:
        return None
    return section_rows(*found, rcept_no, reprt_code)


def section_rows(fs_div: str, section: str, rcept_no: str, reprt_code: str):
    """income_rows() of an already located statement section."""
    metrics, three_month, financial, nci, currency = parse_income_table(section, reprt_code)
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
                     "thstrm_amount": cur, "thstrm_add_amount": add, "currency": currency})
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
    """Cache the statement section (empty file: none found). Written to a
    temporary file and renamed, so a process ending mid-write never leaves a
    truncated or empty file that would read as "no statement" for good."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"<!--fs_div={found[0]}-->{found[1]}" if found else "")
        os.replace(tmp, path)
    except OSError:
        logger.warning("Could not cache DART document section %s", path, exc_info=True)
