"""fnlttSinglAcntAll -> fin_raw -> fin_std (docs/dart_db_plan_2026-11-26.md 4).

One fetch per (company, fiscal year, report code):

- 4-4 consolidated first: CFS, and OFS only when CFS answers 013. The statement
  actually used is ``fs_div``; a change from the company's previous period is
  logged as a warning.
- The API serves the newest version of a period, so its rows carry the receipt
  number of the latest correction; the numbers are stored under that receipt
  (a stub filing is added if the list did not have it), and every pending
  filing of the period is marked fetched (``fin_fetch``). The original numbers
  of a corrected report are not available from this endpoint.
- 4-1 accounts by ``account_id`` (IFRS tag) first, then the company's entries in
  ``account_map.json``, then Korean account names; a metric that none of them
  find is listed in ``fin_unmapped``.
- 4-2 IS first, CIS when the metric is not in IS.
- 4-3 quarter values: Q1 = thstrm; half / Q3 store the 3-month thstrm and the
  cumulative thstrm_add (a missing one is derived from the previous quarter);
  Q4 = annual - Q3 cumulative (same fs_div, latest Q3 received by the annual's
  date), ``derived = 1``.
- 4-6 amounts are in the reported currency's unit; ``currency`` is kept in fin_raw.
"""
from __future__ import annotations

import json
import logging
import os
import re

from data.dart.filings import Q_BY_REPRT, ensure_filing, link_corrections

logger = logging.getLogger(__name__)

METRICS = ("revenue", "op_income", "net_income", "net_income_owners")

ACCOUNT_IDS = {
    "revenue": ("ifrs-full_Revenue", "ifrs_Revenue",
                "ifrs-full_RevenueFromContractsWithCustomers", "ifrs_RevenueFromContractsWithCustomers"),
    "op_income": ("dart_OperatingIncomeLoss",),
    "net_income": ("ifrs-full_ProfitLoss", "ifrs_ProfitLoss"),
    "net_income_owners": ("ifrs-full_ProfitLossAttributableToOwnersOfParent",
                          "ifrs_ProfitLossAttributableToOwnersOfParent"),
}
# Korean account names (spaces removed) for filers without standard tags; the
# financial-company revenue names ("영업수익") are here too (plan 4-1).
ACCOUNT_NAMES = {
    "revenue": ("매출액", "매출", "수익(매출액)", "매출액(수익)", "영업수익", "매출및지분법손익", "영업수익(매출액)"),
    "op_income": ("영업이익", "영업이익(손실)", "영업손익", "영업손실", "영업이익(영업손실)"),
}
# Net income is named by period and statement ("연결분기순이익(손실)", "반기순손익", ...).
_NET_INCOME_NAME = re.compile(r"^(연결)?(당기|분기|반기|당분기|당반기)(연결)?순(이익|손익|손실)(\(손실\))?$")
_ACCOUNT_MAP_FILE = os.path.join(os.path.dirname(__file__), "account_map.json")
_account_map_cache: dict | None = None


def load_account_map() -> dict:
    """{corp_code: {metric: [account_id, ...]}} from account_map.json (plan 4-1)."""
    global _account_map_cache
    if _account_map_cache is None:
        try:
            with open(_ACCOUNT_MAP_FILE, encoding="utf-8") as f:
                data = json.load(f)
            _account_map_cache = {k: v for k, v in data.items() if not k.startswith("_")}
        except (OSError, ValueError):
            logger.warning("account_map.json unreadable; using the built-in rules only", exc_info=True)
            _account_map_cache = {}
    return _account_map_cache


def parse_amount(value):
    """A DART amount string ("1,234", "-5", "", "-", None) as a float or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    if neg:
        s = s[1:-1]
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _norm(name: str) -> str:
    return "".join((name or "").split())


def is_owners_name(name: str) -> bool:
    """Owners-of-parent profit line: "지배기업 소유주지분", "지배회사소유주지분순이익",
    "지배주주순이익", ... but not a non-controlling or comprehensive-income line."""
    n = _norm(name)
    if "비지배" in n or "포괄" in n:
        return False
    return any(k in n for k in ("지배기업", "지배회사", "지배주주")) and any(k in n for k in ("소유주", "지분", "주주", "순이익"))


def is_net_income_name(name: str) -> bool:
    """"당기순이익(손실)", "분기연결순이익", "연결당반기순이익", "당(분)기연결순이익", ..."""
    return bool(_NET_INCOME_NAME.match(_norm(name).replace("(분)", "분").replace("(반)", "반")))


# Accounts only a financial-sector filer (bank, insurer, financial holding)
# has; such a filer presents no revenue line, so a missing revenue is "not
# applicable" there, not unmapped (user decision 2026-11-26, plan 4-1 note).
_FIN_IDS = {"ifrs-full_RevenueFromInterest", "ifrs-full_InterestRevenueExpense", "ifrs-full_InsuranceRevenue",
            "ifrs-full_InsuranceServiceResult", "dart_InsuranceRevenueExpense", "ifrs-full_FeeAndCommissionIncome",
            "ifrs-full_InterestRevenueCalculatedUsingEffectiveInterestMethod", "dart_InvestmentIncomeExpenses",
            "dart_OperatingIncomeInsurance"}
_FIN_NAMES = ("이자수익", "순이자이익", "보험수익", "보험서비스결과", "수수료수익", "투자서비스수익", "보험료수익")


def is_financial(rows: list[dict]) -> bool:
    for r in rows:
        if r.get("sj_div") not in ("IS", "CIS"):
            continue
        if (r.get("account_id") or "").strip() in _FIN_IDS or _norm(r.get("account_nm")) in _FIN_NAMES:
            return True
    return False


def has_nci_line(rows: list[dict]) -> bool:
    """Whether the income statement splits profit to non-controlling interests."""
    return any(r.get("sj_div") in ("IS", "CIS") and ("비지배" in _norm(r.get("account_nm"))
               or "NoncontrollingInterests" in (r.get("account_id") or "")) for r in rows)


def record_issue(conn, rcept_no, corp_code, fiscal_year, fiscal_q, kind, detail="") -> None:
    """One line in fin_issue (plan 4-1 / 4-4 lists); not committed."""
    conn.execute("INSERT OR IGNORE INTO fin_issue(rcept_no, corp_code, fiscal_year, fiscal_q, kind, detail) "
                 "VALUES (?, ?, ?, ?, ?, ?)", (rcept_no, corp_code, fiscal_year, fiscal_q, kind, detail or ""))


def find_metric(rows: list[dict], metric: str, extra_ids=()) -> dict | None:
    """The row for `metric`: IS before CIS; within a statement the first (by
    ord) row whose account_id matches, else whose name matches."""
    ids = tuple(extra_ids) + ACCOUNT_IDS[metric]
    for sj in ("IS", "CIS"):
        stmt = sorted((r for r in rows if r.get("sj_div") == sj), key=lambda r: int(r.get("ord") or 0))
        if not stmt:
            continue
        for aid in ids:
            for r in stmt:
                if (r.get("account_id") or "").strip() == aid:
                    return r
        for r in stmt:
            n = _norm(r.get("account_nm"))
            if metric == "net_income_owners":
                if is_owners_name(n) and "ComprehensiveIncome" not in (r.get("account_id") or ""):
                    return r
            elif metric == "net_income":
                if is_net_income_name(n):
                    return r
            elif n in ACCOUNT_NAMES.get(metric, ()):
                return r
    return None


def fetch_period(client, corp_code: str, api_year: int, reprt_code: str, cache_tag: str):
    """(fs_div, rows) of the period: CFS, else OFS on 013; (None, []) when neither has data."""
    for fs_div in ("CFS", "OFS"):
        data = client.get_json("fnlttSinglAcntAll.json",
                               {"corp_code": corp_code, "bsns_year": str(api_year),
                                "reprt_code": reprt_code, "fs_div": fs_div},
                               cache_tag=cache_tag)
        if data.get("status") == "000" and data.get("list"):
            return fs_div, data["list"]
    return None, []


def store_period(conn, corp_code: str, fiscal_year: int, reprt_code: str, fs_div: str,
                 rows: list[dict], account_map: dict | None = None, *, raw: bool = True,
                 financial: bool | None = None, nci: bool | None = None) -> tuple[str, list[str]]:
    """Store one fetched period (fin_raw + fin_std); returns (the receipt number
    the numbers belong to, metrics not found). Not committed. `raw=False` for
    rows read from a document (fin_raw keeps API replies only); `financial` /
    `nci` override what is read from the rows.

    - A financial-sector filer without a revenue line: no revenue row, a
      ``revenue_na`` issue instead of an unmapped one.
    - Consolidated statements without any non-controlling line: the owners'
      profit is the whole net income (derived = 1, issue ``owners_from_net_income``)."""
    src = max(str(r.get("rcept_no")) for r in rows if r.get("rcept_no"))
    ensure_filing(conn, src, corp_code, fiscal_year, reprt_code)
    link_corrections(conn, corp_code)
    rcept_dt = conn.execute("SELECT rcept_dt FROM filing WHERE rcept_no = ?", (src,)).fetchone()[0]

    conn.executemany(
        "INSERT OR REPLACE INTO fin_raw(rcept_no, fs_div, sj_div, account_id, account_nm, ord, thstrm_amount, "
        "thstrm_add_amount, frmtrm_amount, frmtrm_add_amount, currency) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(src, fs_div, r.get("sj_div") or "", r.get("account_id"), r.get("account_nm"), int(r.get("ord") or 0),
          parse_amount(r.get("thstrm_amount")), parse_amount(r.get("thstrm_add_amount")),
          parse_amount(r.get("frmtrm_amount")), parse_amount(r.get("frmtrm_add_amount")), r.get("currency"))
         for r in rows if raw and r.get("sj_div") in ("IS", "CIS", "BS", "CF", "SCE")])

    q = Q_BY_REPRT[reprt_code]
    extra = (account_map or {}).get(corp_code, {})
    missing = []
    _warn_fs_div_change(conn, corp_code, fiscal_year, q, fs_div, src)
    financial = is_financial(rows) if financial is None else financial
    nci = has_nci_line(rows) if nci is None else nci
    for metric in METRICS:
        row = find_metric(rows, metric, extra.get(metric, ()))
        derived = 0
        if row is None and metric == "revenue" and financial:
            record_issue(conn, src, corp_code, fiscal_year, q, "revenue_na", "financial-sector statement")
            continue
        if row is None and metric == "net_income_owners" and fs_div == "CFS" and not nci:
            row = find_metric(rows, "net_income")
            if row is not None:
                derived = 1
                record_issue(conn, src, corp_code, fiscal_year, q, "owners_from_net_income", "no non-controlling line")
        if row is None:
            if metric != "net_income_owners" or fs_div == "CFS":
                missing.append(metric)
            continue
        cur = parse_amount(row.get("thstrm_amount"))
        add = parse_amount(row.get("thstrm_add_amount"))
        if q == 1:
            value_q, ytd = cur, add if add is not None else cur
        elif q == 4:
            value_q, ytd = None, cur          # filled by derive_q4
        else:
            value_q, ytd = cur, add
            prev = _ytd(conn, corp_code, fiscal_year, q - 1, metric, fs_div, rcept_dt)
            if ytd is None and value_q is not None and prev is not None:
                ytd = prev + value_q
            elif value_q is None and ytd is not None and prev is not None:
                value_q, derived = ytd - prev, 1
        if value_q is None and ytd is None:
            continue                          # the account is there but blank (e.g. OFS owners' line)
        conn.execute(
            "INSERT OR REPLACE INTO fin_std(corp_code, fiscal_year, fiscal_q, fs_div, metric, value_q, value_ytd, "
            "derived, rcept_no, rcept_dt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (corp_code, fiscal_year, q, fs_div, metric, value_q, ytd, derived, src, rcept_dt))
    if q in (3, 4):
        derive_q4(conn, corp_code, fiscal_year)
    conn.executemany("INSERT OR IGNORE INTO fin_unmapped(rcept_no, corp_code, metric) VALUES (?, ?, ?)",
                     [(src, corp_code, m) for m in missing])
    return src, missing


def derive_q4(conn, corp_code: str, fiscal_year: int) -> None:
    """Q4 value = annual - Q3 cumulative for every annual receipt of the year,
    using the newest Q3 (same fs_div) received on or before the annual's date.
    When the only stored Q3 is a correction received after the annual (the API
    serves the newest version only; KB금융 2023 Q3 corrected 2024-12), that Q3
    is used and the period gets a ``q4_uses_later_q3`` issue: the Q4 value then
    rests on numbers published after the annual's date."""
    for r in conn.execute(
            "SELECT metric, fs_div, value_ytd, rcept_no, rcept_dt FROM fin_std "
            "WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = 4", (corp_code, fiscal_year)).fetchall():
        q3 = _ytd(conn, corp_code, fiscal_year, 3, r["metric"], r["fs_div"], r["rcept_dt"])
        if q3 is None:
            q3 = _ytd(conn, corp_code, fiscal_year, 3, r["metric"], r["fs_div"], "9999-12-31")
            if q3 is not None:
                record_issue(conn, r["rcept_no"], corp_code, fiscal_year, 4, "q4_uses_later_q3", r["metric"])
        value_q = r["value_ytd"] - q3 if (r["value_ytd"] is not None and q3 is not None) else None
        conn.execute(
            "UPDATE fin_std SET value_q = ?, derived = 1 WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = 4 "
            "AND metric = ? AND rcept_no = ?", (value_q, corp_code, fiscal_year, r["metric"], r["rcept_no"]))


def _ytd(conn, corp_code, fiscal_year, q, metric, fs_div, as_of_dt):
    row = conn.execute(
        "SELECT value_ytd FROM fin_std WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = ? AND metric = ? "
        "AND fs_div = ? AND rcept_dt <= ? ORDER BY rcept_no DESC LIMIT 1",
        (corp_code, fiscal_year, q, metric, fs_div, as_of_dt)).fetchone()
    return row[0] if row else None


def _warn_fs_div_change(conn, corp_code, fiscal_year, q, fs_div, rcept_no) -> None:
    row = conn.execute(
        "SELECT fs_div, fiscal_year, fiscal_q FROM fin_std WHERE corp_code = ? AND (fiscal_year < ? OR "
        "(fiscal_year = ? AND fiscal_q < ?)) ORDER BY fiscal_year DESC, fiscal_q DESC LIMIT 1",
        (corp_code, fiscal_year, fiscal_year, q)).fetchone()
    if row and row["fs_div"] != fs_div:
        record_issue(conn, rcept_no, corp_code, fiscal_year, q, "fs_div_change",
                     f"{row['fs_div']} ({row['fiscal_year']} Q{row['fiscal_q']}) -> {fs_div}")
        logger.warning("DART %s: statement changed %s (%s Q%s) -> %s (%s Q%s)", corp_code, row["fs_div"],
                       row["fiscal_year"], row["fiscal_q"], fs_div, fiscal_year, q)
