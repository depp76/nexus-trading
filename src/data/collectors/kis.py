"""data/collectors/kis.py — Korea Investment & Securities (KIS) REST/WebSocket
client for quotes, daily OHLCV, account balance, investor trend, and real-time prices.

Replaces data/collectors/kiwoom.py. Real (live) trading environment only.
"""
import os
import json
import threading
import time
import logging
from datetime import datetime, timedelta, timezone, time as dt_time

import pandas as pd
import requests

from paths import KIS_TOKEN_CACHE_FILE
from data.cache import PAYLOAD_ERRORS, _KIS_SESSION
from data.collectors.naver import _fetch_investor_trend_naver, _fetch_index_investor_trend

logger = logging.getLogger(__name__)

_KIS_BASE_URL = "https://openapi.koreainvestment.com:9443"
_KIS_WS_URL = "ws://ops.koreainvestment.com:21000"

_KIS_KEYS_CACHE: dict = {}
_KIS_TOKEN_CACHE: dict = {"token": None, "expires": 0}
_KIS_APPROVAL_CACHE: dict = {"approval_key": None, "expires": 0}
_KIS_TOKEN_LOCK = threading.Lock()
_KIS_APPROVAL_LOCK = threading.Lock()
# _get_kis_keys runs under either lock above (token and approval key), so the
# key cache needs its own: two threads could otherwise see "appkey" cached
# before "appsecret" was stored (review_agy M4).
_KIS_KEYS_LOCK = threading.Lock()

_KIS_TOKEN_CACHE_PATH = KIS_TOKEN_CACHE_FILE

# Proactively reissue the access token / approval_key this long before their actual
# expiry, rather than waiting until they're about to lapse. Since _get_kis_token()
# and _get_kis_approval_key() are called on every REST/WS call (e.g. the 60s
# real-time price loop), any call inside this window naturally triggers a refresh —
# no separate scheduler needed.
_KIS_TOKEN_REFRESH_MARGIN_SEC = 6 * 3600
_KIS_APPROVAL_ASSUMED_LIFETIME_SEC = 24 * 3600

_KST = timezone(timedelta(hours=9))

# What one KIS REST call can raise on an expected failure: PAYLOAD_ERRORS for the HTTP
# round trip and the reply, OSError for missing key/account files (FileNotFoundError),
# ValueError for a refused token, and RuntimeError for an rt_cd error reply.
KIS_ERRORS = (*PAYLOAD_ERRORS, OSError, RuntimeError)


def is_krx_market_open() -> bool:
    """Check if the Korean stock market (KRX) is currently open for regular trading.
    Regular trading hours: Monday through Friday, 09:00 - 15:30 KST.
    """
    now_kst = datetime.now(_KST)
    if now_kst.weekday() >= 5:  # Saturday (5) or Sunday (6)
        return False
    current_time = now_kst.time()
    return dt_time(9, 0) <= current_time <= dt_time(15, 30)


def _get_kis_keys():
    """Reads appkey and appsecret from the KIS_KEY_PATH folder (default: the
    Trading MCP sibling folder, an external-secrets location shared across
    brokerage-API projects on this machine), using an in-memory cache to avoid
    repeated disk I/O."""
    with _KIS_KEYS_LOCK:
        cached = _KIS_KEYS_CACHE.get("keys")
        if cached is not None:
            return cached
        keys = _read_kis_keys()
        _KIS_KEYS_CACHE["keys"] = keys
        return keys


def _read_kis_keys():
    """(appkey, appsecret) from the key files; see _get_kis_keys."""
    key_dir = os.environ.get("KIS_KEY_PATH", r"D:\Source Code\Trading MCP")
    appkey_path = os.path.join(key_dir, "kis_appkey.txt")
    secret_path = os.path.join(key_dir, "kis_secretkey.txt")
    if not os.path.exists(appkey_path) or not os.path.exists(secret_path):
        raise FileNotFoundError(f"KIS API key files not found in {key_dir}")
    with open(appkey_path, encoding="utf-8") as f:
        appkey = f.read().strip()
    with open(secret_path, encoding="utf-8") as f:
        appsecret = f.read().strip()
    return appkey, appsecret


def _get_kis_account():
    """Reads the account number (10 digits: 8-digit CANO + 2-digit product code)
    from kis_account.txt in the KIS_KEY_PATH folder, and splits it into
    (cano, acnt_prdt_cd).

    Re-read from disk on every call rather than cached forever: unlike appkey/
    appsecret (used on every request but never edited live), this file is the one
    most likely to get corrected in place after a first-time typo, and KIS's deposit
    API has no equivalent to the old Kiwoom flow's live ka00001 account-list lookup
    to catch that — the file read itself is a few bytes and negligible overhead next
    to the network call each caller makes right after this.
    """
    key_dir = os.environ.get("KIS_KEY_PATH", r"D:\Source Code\Trading MCP")
    account_path = os.path.join(key_dir, "kis_account.txt")
    if not os.path.exists(account_path):
        raise FileNotFoundError(f"KIS account number file not found: {account_path}")
    with open(account_path, encoding="utf-8") as f:
        acct = f.read().strip().replace("-", "").replace(" ", "")
    if len(acct) != 10 or not acct.isdigit():
        raise ValueError(
            f"{account_path} must contain 10 digits (8-digit CANO + 2-digit "
            f"product code, e.g. 1234567801 or 12345678-01) — found {acct!r}."
        )
    return acct[:8], acct[8:]


def _kis_oauth_post(path: str, json_payload: dict, max_attempts: int = 3):
    """POST to a KIS OAuth endpoint (token/approval-key issuance) with a short
    retry for transient network errors, mirroring naver._fast_kr_history's
    retry pattern (roadmap 2026-09-18, review.md 3-3).

    Only smooths over connection errors/timeouts and real HTTP error statuses.
    KIS returns issuance failures (bad keys, rate-limited) as HTTP 200 with an
    error payload, which _get_kis_token()/_get_kis_approval_key() still raise
    on immediately after this returns -- retrying those instantly would just
    fail again given KIS's ~once/minute-per-appkey issuance limit.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            res = _KIS_SESSION.post(
                f"{_KIS_BASE_URL}{path}",
                headers={"content-type": "application/json; charset=utf-8"},
                json=json_payload,
                timeout=5,
            )
            res.raise_for_status()
            return res
        except requests.RequestException as e:
            last_exc = e
            if attempt == max_attempts - 1:
                raise
            logger.debug(
                "KIS OAuth POST %s failed (attempt %d/%d), retrying: %s",
                path, attempt + 1, max_attempts, e,
            )
            time.sleep(1)
    raise last_exc


def _load_kis_token_cache() -> dict:
    if os.path.exists(_KIS_TOKEN_CACHE_PATH):
        try:
            with open(_KIS_TOKEN_CACHE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            logger.debug("Failed to load KIS token cache file, starting fresh", exc_info=True)
    return {}


def _save_kis_token_cache(cache: dict):
    try:
        temp_path = _KIS_TOKEN_CACHE_PATH + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(temp_path, _KIS_TOKEN_CACHE_PATH)
    except OSError:
        logger.warning("KIS token cache save error", exc_info=True)


def _get_kis_token():
    """Returns a cached or newly-fetched KIS REST API access token.

    KIS rate-limits token issuance to roughly once per minute per appkey, so the
    token is persisted to disk (kis_token_cache.json) in addition to the in-memory
    cache, to survive app restarts within the ~24h token lifetime. The cached
    "expires" is set _KIS_TOKEN_REFRESH_MARGIN_SEC (6h) before the token's real
    expiry, so any call made within that window transparently reissues a fresh
    token instead of risking one that expires mid-session.
    """
    with _KIS_TOKEN_LOCK:
        now = time.time()
        if _KIS_TOKEN_CACHE["token"] and now < _KIS_TOKEN_CACHE["expires"]:
            appkey, appsecret = _get_kis_keys()
            return _KIS_TOKEN_CACHE["token"], appkey, appsecret

        appkey, appsecret = _get_kis_keys()

        disk_cache = _load_kis_token_cache()
        if disk_cache.get("token") and now < disk_cache.get("expires", 0):
            _KIS_TOKEN_CACHE["token"] = disk_cache["token"]
            _KIS_TOKEN_CACHE["expires"] = disk_cache["expires"]
            return disk_cache["token"], appkey, appsecret

        res = _kis_oauth_post(
            "/oauth2/tokenP",
            {"grant_type": "client_credentials", "appkey": appkey, "appsecret": appsecret},
        )
        data = res.json()
        token = data.get("access_token")
        if not token:
            reason = data.get("error_description") or data.get("msg1") or str(data)
            raise ValueError(
                f"Failed to issue KIS access token: {reason} "
                "(check kis_appkey.txt/kis_secretkey.txt in the KIS_KEY_PATH folder are "
                "correct and current, that the account is registered for real/live "
                "Open API access, and that token issuance hasn't been rate-limited — "
                "KIS allows roughly one issuance per minute per appkey)."
            )
        expires_in = int(data.get("expires_in", 86400))
        expires = now + max(expires_in - _KIS_TOKEN_REFRESH_MARGIN_SEC, 60)
        _KIS_TOKEN_CACHE["token"] = token
        _KIS_TOKEN_CACHE["expires"] = expires
        _save_kis_token_cache({"token": token, "expires": expires})
        return token, appkey, appsecret


def _get_kis_approval_key():
    """Returns a cached or newly-fetched approval_key for WebSocket subscriptions
    (valid ~24h; separate from the REST access token)."""
    with _KIS_APPROVAL_LOCK:
        now = time.time()
        if _KIS_APPROVAL_CACHE["approval_key"] and now < _KIS_APPROVAL_CACHE["expires"]:
            return _KIS_APPROVAL_CACHE["approval_key"]

        appkey, appsecret = _get_kis_keys()
        res = _kis_oauth_post(
            "/oauth2/Approval",
            {"grant_type": "client_credentials", "appkey": appkey, "secretkey": appsecret},
        )
        data = res.json()
        approval_key = data.get("approval_key")
        if not approval_key:
            raise ValueError(f"Failed to issue KIS approval_key: {data}")
        _KIS_APPROVAL_CACHE["approval_key"] = approval_key
        _KIS_APPROVAL_CACHE["expires"] = now + max(
            _KIS_APPROVAL_ASSUMED_LIFETIME_SEC - _KIS_TOKEN_REFRESH_MARGIN_SEC, 60
        )
        return approval_key


# KIS reply codes for a token the server no longer accepts (EGW00121 invalid,
# EGW00123 expired). Seen when another process reissued the token or the
# server expired it before our cached "expires" (review_agy M4).
_KIS_TOKEN_REJECTED_CODES = frozenset({"EGW00121", "EGW00123"})


def _token_rejected(r) -> bool:
    """True when reply `r` says the access token itself was refused."""
    if getattr(r, "status_code", None) == 401:
        return True
    try:
        d = r.json()
    except ValueError:
        return False
    return isinstance(d, dict) and str(d.get("msg_cd", "")).strip() in _KIS_TOKEN_REJECTED_CODES


def _drop_kis_token(token: str) -> None:
    """Forget `token` in memory and on disk (only if it is still the cached
    one), so the next _get_kis_token issues or reloads a fresh token."""
    with _KIS_TOKEN_LOCK:
        if _KIS_TOKEN_CACHE["token"] == token:
            _KIS_TOKEN_CACHE["token"] = None
            _KIS_TOKEN_CACHE["expires"] = 0
        if _load_kis_token_cache().get("token") == token:
            _save_kis_token_cache({})


def _kis_get(path: str, tr_id: str, params: dict, timeout: float, tr_cont: str = ""):
    """GET a KIS REST endpoint with fresh headers. A reply refusing the token
    (HTTP 401 / EGW00121 / EGW00123) drops the cached token and retries once
    with a new one. Returns the response unchecked (callers raise_for_status)."""
    r = None
    for attempt in range(2):
        headers = _kis_headers(tr_id)
        if tr_cont:
            headers["tr_cont"] = tr_cont
        r = _KIS_SESSION.get(f"{_KIS_BASE_URL}{path}", headers=headers, params=params, timeout=timeout)
        if attempt == 0 and _token_rejected(r):
            logger.warning("[KIS %s] access token refused; reissuing and retrying once", tr_id)
            _drop_kis_token(str(headers.get("authorization", "")).removeprefix("Bearer "))
            continue
        return r
    return r


def _kis_headers(tr_id: str) -> dict:
    token, appkey, appsecret = _get_kis_token()
    return {
        "content-type": "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey": appkey,
        "appsecret": appsecret,
        "tr_id": tr_id,
        "custtype": "P",
    }


def fetch_kis_stock_info(code: str):
    """Fetches current price, name, market cap via KIS inquire-price (FHKST01010100).

    Confirmed against live output (2026-09-13): this TR does not return a Korean
    stock name field, so `name` is always "" here. Not an issue for current
    callers (the app already has ticker->name from get_stock_listing()).
    """
    r = _kis_get("/uapi/domestic-stock/v1/quotations/inquire-price", "FHKST01010100",
                 {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code}, timeout=5)
    r.raise_for_status()
    d = r.json()
    if d.get("rt_cd") != "0":
        return None
    out = d.get("output", {})
    price = int(out.get("stck_prpr", "0") or "0")
    market_cap = 0
    try:
        # hts_avls is in 100M KRW units.
        market_cap = int(float(out.get("hts_avls", "0") or "0") * 100_000_000)
    except (ValueError, TypeError):
        pass
    return {
        "name": out.get("hts_kor_isnm", ""),
        "price": price,
        "market_cap": market_cap,
        "open": int(out.get("stck_oprc", "0") or "0"),
        "high": int(out.get("stck_hgpr", "0") or "0"),
        "low": int(out.get("stck_lwpr", "0") or "0"),
    }


def fetch_kis_daily_ohlcv(code: str, days: int = 30):
    """Fetches recent daily OHLCV via KIS inquire-daily-itemchartprice (FHKST03010100).
    Returns pd.DataFrame."""
    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6) + 10)  # pad for weekends/holidays
    r = _kis_get(
        "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
        {
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            "FID_PERIOD_DIV_CODE": "D",
            "FID_ORG_ADJ_PRC": "0",
        },
        timeout=5,
    )
    r.raise_for_status()
    d = r.json()
    items = d.get("output2", [])
    if not items:
        return None
    rows = []
    for item in items:
        try:
            dt = pd.to_datetime(item["stck_bsop_date"], format="%Y%m%d")
            rows.append({
                "Date": dt,
                "Open": int(item.get("stck_oprc", "0") or "0"),
                "High": int(item.get("stck_hgpr", "0") or "0"),
                "Low": int(item.get("stck_lwpr", "0") or "0"),
                "Close": int(item.get("stck_clpr", "0") or "0"),
                "Volume": int(item.get("acml_vol", "0") or "0"),
            })
        except (KeyError, ValueError, TypeError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values("Date").reset_index(drop=True)
    return df.tail(days).reset_index(drop=True)


def fetch_kis_holiday_page(base_date: str) -> list:
    """One page of KIS chk-holiday (CTCA0903R): about 24 consecutive calendar days starting at
    `base_date` (YYYYMMDD), weekends included, each with bass_dt and the opnd_yn open-day flag.
    Page forward by asking again from the day after the last row (data/krx_calendar.py caches
    the result; KIS asks callers to keep this TR to about one use a day)."""
    r = _kis_get("/uapi/domestic-stock/v1/quotations/chk-holiday", "CTCA0903R",
                 {"BASS_DT": base_date, "CTX_AREA_NK": "", "CTX_AREA_FK": ""}, timeout=10)
    r.raise_for_status()
    d = r.json()
    if d.get("rt_cd") != "0":
        raise RuntimeError(f"[chk-holiday] KIS API error: {d.get('msg1', d)}")
    return d.get("output") or []


def _to_float(s) -> float:
    try:
        return float(str(s).replace(",", "").strip() or "0")
    except (ValueError, TypeError):
        return 0.0


# inquire-balance pages hold up to 50 names each; this many pages is far more
# than a personal account needs and only stops a server that keeps answering
# "more data".
_BALANCE_MAX_PAGES = 20


def _to_float_or_none(s) -> float | None:
    """A numeric KIS field, or None when it is missing or not a number."""
    if s is None:
        return None
    try:
        text = str(s).replace(",", "").strip()
        return float(text) if text else None
    except (ValueError, TypeError):
        return None


def _deposit_of(summary) -> float:
    """The deposit from an inquire-balance output2 row; NaN when the row has no
    readable deposit field at all, so the caller can keep the value it has
    instead of storing a 0 KIS never reported (review_codex.md 2026-11-20 #3).
    A real "0" is returned as 0.0."""
    if not isinstance(summary, dict):
        return float("nan")
    primary = _to_float_or_none(summary.get("dnca_tot_amt"))
    secondary = _to_float_or_none(summary.get("prvs_rcdl_excc_amt"))
    if primary is None and secondary is None:
        return float("nan")
    if primary is None:
        return secondary
    # Mirrors the old Kiwoom flow's rule: don't trust a "0" in the primary deposit
    # field when a secondary field shows a real balance.
    if primary == 0.0 and secondary is not None and secondary > 0.0:
        return secondary
    return primary


def fetch_account_balance() -> dict:
    """The account's deposit and stock holdings via KIS inquire-balance (TTTC8434R).

    Returns {"deposit": float KRW, "holdings": [{"ticker", "name", "qty", "avg_price"}],
    "holdings_complete": bool}: holdings with a positive quantity from every page
    (continuation via tr_cont / ctx_area_*; review_codex.md 2026-11-20 #4), and
    holdings_complete False when paging stopped at _BALANCE_MAX_PAGES. The
    deposit is NaN when the reply carries no readable deposit field (an empty
    output2), never a made-up 0. Unlike the old Kiwoom flow, KIS balance
    inquiry needs no account password/OTP. Raises a KIS_ERRORS member on failure."""
    cano, acnt_prdt_cd = _get_kis_account()
    base = {
        "CANO": cano,
        "ACNT_PRDT_CD": acnt_prdt_cd,
        "AFHR_FLPR_YN": "N",
        "OFL_YN": "",
        "INQR_DVSN": "02",
        "UNPR_DVSN": "01",
        "FUND_STTL_ICLD_YN": "N",
        "FNCG_AMT_AUTO_RDPT_YN": "N",
        "PRCS_DVSN": "01",
    }
    holdings: list = []
    summary = None
    complete = False
    fk = nk = ""
    for _page in range(_BALANCE_MAX_PAGES):
        params = dict(base, CTX_AREA_FK100=fk, CTX_AREA_NK100=nk)
        r = _kis_get("/uapi/domestic-stock/v1/trading/inquire-balance", "TTTC8434R", params, timeout=5,
                     tr_cont="N" if (fk or nk) else "")
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") != "0":
            raise RuntimeError(f"[inquire-balance] KIS API error: {d.get('msg1', d)}")
        for row in d.get("output1") or []:
            qty = _to_float(row.get("hldg_qty"))
            if qty > 0:
                holdings.append({"ticker": str(row.get("pdno", "")).strip(),
                                 "name": str(row.get("prdt_name", "")).strip(),
                                 "qty": qty, "avg_price": _to_float(row.get("pchs_avg_pric"))})
        if summary is None:
            output2 = d.get("output2") or []
            if output2:
                summary = output2[0]
        if r.headers.get("tr_cont") not in ("M", "F"):
            complete = True
            break
        fk, nk = d.get("ctx_area_fk100", ""), d.get("ctx_area_nk100", "")
        time.sleep(0.1)           # KIS allows ~20 calls a second; stay well under
    if not complete:
        logger.warning("[inquire-balance] stopped after %d pages; holdings may be incomplete", _BALANCE_MAX_PAGES)
    return {"deposit": _deposit_of(summary), "holdings": holdings, "holdings_complete": complete}


def fetch_account_deposit() -> float:
    """The account deposit (cash balance) in KRW; see fetch_account_balance."""
    return fetch_account_balance()["deposit"]


# inquire-daily-ccld comes as two TRs: TTTC0081R for the last three months and
# CTSC9215R for anything older. Days at or after today - _CCLD_RECENT_DAYS go to
# the first (a little inside three months, so a range edge never falls between).
_CCLD_RECENT_DAYS = 89
_CCLD_MAX_PAGES = 50
_SIDE_CODES = {"01": "sell", "02": "buy"}


def _fetch_ccld_range(tr_id: str, cano: str, prdt: str, start, end) -> list:
    """Every executed-order row of inquire-daily-ccld for start..end (dates), all pages."""
    rows: list = []
    fk = nk = ""
    for _page in range(_CCLD_MAX_PAGES):
        params = {
            "CANO": cano, "ACNT_PRDT_CD": prdt,
            "INQR_STRT_DT": start.strftime("%Y%m%d"), "INQR_END_DT": end.strftime("%Y%m%d"),
            "SLL_BUY_DVSN_CD": "00",      # both sides
            "INQR_DVSN": "00",            # oldest first
            "PDNO": "", "CCLD_DVSN": "01",   # executed orders only
            "ORD_GNO_BRNO": "", "ODNO": "", "INQR_DVSN_3": "00", "INQR_DVSN_1": "",
            "CTX_AREA_FK100": fk, "CTX_AREA_NK100": nk,
            "EXCG_ID_DVSN_CD": "ALL",     # KRX and NXT
        }
        r = _kis_get("/uapi/domestic-stock/v1/trading/inquire-daily-ccld", tr_id, params, timeout=10,
                     tr_cont="N" if (fk or nk) else "")
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") != "0":
            raise RuntimeError(f"[inquire-daily-ccld {tr_id}] KIS API error: {d.get('msg1', d)}")
        rows.extend(d.get("output1") or [])
        if r.headers.get("tr_cont") not in ("M", "F"):
            return rows
        fk, nk = d.get("ctx_area_fk100", ""), d.get("ctx_area_nk100", "")
        time.sleep(0.1)           # KIS allows ~20 calls a second; stay well under
    logger.warning("[inquire-daily-ccld] stopped after %d pages", _CCLD_MAX_PAGES)
    return rows


def parse_execution(row: dict) -> dict | None:
    """One inquire-daily-ccld output1 row -> {"date" (YYYY-MM-DD), "time", "order_no",
    "ticker", "name", "side" ("buy"/"sell"), "qty", "amount", "price"} with the
    order's cumulative fill so far; None for a row with no fill or an unknown side."""
    side = _SIDE_CODES.get(str(row.get("sll_buy_dvsn_cd", "")).strip())
    qty = _to_float(row.get("tot_ccld_qty"))
    ord_dt = str(row.get("ord_dt", "")).strip()
    if side is None or qty <= 0 or len(ord_dt) != 8:
        return None
    amount = _to_float(row.get("tot_ccld_amt"))
    price = _to_float(row.get("avg_prvs")) or (amount / qty)
    if amount <= 0:
        amount = price * qty
    return {
        "date": f"{ord_dt[:4]}-{ord_dt[4:6]}-{ord_dt[6:]}",
        "time": str(row.get("ord_tmd", "")).strip(),
        "order_no": str(row.get("odno", "")).strip(),
        "ticker": str(row.get("pdno", "")).strip(),
        "name": str(row.get("prdt_name", "")).strip(),
        "side": side, "qty": qty, "amount": amount, "price": price,
    }


def fetch_kis_executions(start, end=None, today=None) -> list:
    """The account's executed orders between `start` and `end` (dates, inclusive;
    end defaults to today), oldest first, each parsed by parse_execution. Splits
    the range over the recent / older TRs. Raises a KIS_ERRORS member on failure."""
    today = today or datetime.now(_KST).date()
    end = min(end or today, today)
    if start > end:
        return []
    cano, prdt = _get_kis_account()
    recent_from = today - timedelta(days=_CCLD_RECENT_DAYS)
    raw: list = []
    if start < recent_from:
        raw += _fetch_ccld_range("CTSC9215R", cano, prdt, start, min(end, recent_from - timedelta(days=1)))
    if end >= recent_from:
        raw += _fetch_ccld_range("TTTC0081R", cano, prdt, max(start, recent_from), end)
    out = [e for e in (parse_execution(r) for r in raw) if e is not None]
    out.sort(key=lambda e: (e["date"], e["time"], e["order_no"]))
    return out


def fetch_investor_trend(ticker: str, days: int = 60) -> list:
    """Fetches investor net-purchase data via KIS inquire-investor (FHKST01010900).

    Falls back to Naver scraping if the KIS call fails or returns nothing.

    Confirmed against live output (2026-09-13): this TR only breaks investors into
    Foreigner/Institution/Retail (frgn_ntby_qty/orgn_ntby_qty/prsn_ntby_qty) — unlike
    Kiwoom's ka10059, it has no separate InvestmentTrust/PrivateEquity sub-fields, so
    those two columns are always 0 here (StockMaDialog already hides zero-only columns).
    """
    if isinstance(ticker, str) and ticker.isdigit():
        ticker = ticker.zfill(6)
    elif isinstance(ticker, int):
        ticker = f"{ticker:06d}"

    if ticker in ("KS11", "KQ11", "KOSPI", "KOSDAQ", "^KS11", "^KQ11"):
        market_str = "KOSPI" if ticker in ("KS11", "KOSPI", "^KS11") else "KOSDAQ"
        return _fetch_index_investor_trend(market_str, days)

    try:
        r = _kis_get("/uapi/domestic-stock/v1/quotations/inquire-investor", "FHKST01010900",
                     {"FID_INPUT_ISCD": ticker, "FID_COND_MRKT_DIV_CODE": "J"}, timeout=8)
        r.raise_for_status()
        d = r.json()
        if d.get("rt_cd") != "0":
            raise RuntimeError(d.get("msg1", "unknown error"))
        items = d.get("output", [])
    except KIS_ERRORS:
        logger.warning("[fetch_investor_trend] KIS inquire-investor failed, falling back to Naver", exc_info=True)
        return _fetch_investor_trend_naver(ticker, days)

    rows = []
    for item in items[:days]:
        dt_raw = str(item.get("stck_bsop_date", "")).strip()
        if len(dt_raw) < 8:
            continue
        date_str = f"{dt_raw[:4]}.{dt_raw[4:6]}.{dt_raw[6:8]}"
        try:
            close_price = abs(int(item.get("stck_clpr", "0") or "0"))
        except ValueError:
            close_price = 0

        def _signed(key, item=item):
            try:
                return int(str(item.get(key, "0") or "0").replace(",", "").strip())
            except ValueError:
                return 0

        rows.append({
            "Date": date_str,
            "Close": close_price,
            "Foreigner": _signed("frgn_ntby_qty"),
            "Institution": _signed("orgn_ntby_qty"),
            "Retail": _signed("prsn_ntby_qty"),
            "InvestmentTrust": _signed("ivtr_ntby_qty"),
            "PrivateEquity": _signed("orgn_prvt_ntby_qty"),
        })

    if not rows:
        logger.warning("[fetch_investor_trend] KIS returned no rows, falling back to Naver")
        return _fetch_investor_trend_naver(ticker, days)

    return rows


# Hybrid realtime path: the WebSocket session is opened fresh on every 60 s
# refresh (connect, subscribe, wait for ticks, unsubscribe, close), which only
# pays off once there are enough tickers to amortise it. Below this count a
# handful of REST quotes (parallel, ~0.3 s) beats a handshake plus up to
# _KIS_WS_TIMEOUT s of waiting for a tick that a thin stock may never print.
_KIS_WS_MIN_TICKERS = 4
_KIS_WS_TIMEOUT = 3.0


def _kis_ws_message(approval_key, code: str, tr_type: str) -> str:
    """H0STCNT0 subscribe ("1") / unsubscribe ("2") request for one ticker."""
    return json.dumps({
        "header": {"approval_key": approval_key, "custtype": "P", "tr_type": tr_type, "content-type": "utf-8"},
        "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
    })


def _kis_ws_answer_control(ws, websocket, frame: str) -> None:
    """JSON control frame -- echo PINGPONG to keep the connection alive."""
    try:
        msg = json.loads(frame)
        if msg.get("header", {}).get("tr_id") == "PINGPONG":
            ws.send(frame)
    except (ValueError, AttributeError, websocket.WebSocketException, OSError):
        # AttributeError: a control frame that decodes to a non-dict.
        logger.debug("[fetch_kis_realtime_prices] ignoring unparseable control frame", exc_info=True)


def _kis_ws_tick_fields(frame):
    """Fields of a pipe-delimited real-time data frame, or None when it is not a
    usable H0STCNT0 frame.

    Frame: "0|H0STCNT0|<count>|field^field^...". Field order per KIS docs: [0]=stock
    code, [2]=current price (execution price). Only the first record's fields are read
    here (data_count>1 frames are rare for a single subscribed ticker); confirmed
    against a live capture before relying on this."""
    parts = frame.split("|")
    if len(parts) < 4 or parts[1] != "H0STCNT0":
        return None
    fields = parts[3].split("^")
    if len(fields) < 3:
        return None
    return fields


def _kis_ws_collect_ticks(ws, websocket, approval_key, tickers: list, timeout: float, prices: dict) -> None:
    """Subscribes every ticker and records into `prices` the first tick price seen per
    ticker until all have one, the socket fails, or `timeout` seconds pass."""
    for code in tickers:
        ws.send(_kis_ws_message(approval_key, code, "1"))

    deadline = time.time() + timeout
    pending = set(tickers)
    while pending and time.time() < deadline:
        ws.settimeout(max(0.1, deadline - time.time()))
        try:
            frame = ws.recv()
        except (websocket.WebSocketException, OSError):
            break

        if not isinstance(frame, str):
            continue          # a binary frame is never an H0STCNT0 tick (review_agy M4)
        if frame.startswith("{"):
            _kis_ws_answer_control(ws, websocket, frame)
            continue

        fields = _kis_ws_tick_fields(frame)
        if fields is None:
            continue
        code = fields[0]
        if code in pending:
            try:
                prices[code] = float(fields[2])
                pending.discard(code)
            except (ValueError, IndexError):
                continue


def _kis_ws_close(ws, websocket, approval_key, tickers: list) -> None:
    """Unsubscribes every ticker and closes the socket, logging (not raising) failures."""
    try:
        for code in tickers:
            ws.send(_kis_ws_message(approval_key, code, "2"))
    except (websocket.WebSocketException, OSError):
        logger.debug("[fetch_kis_realtime_prices] unsubscribe failed (connection likely gone)", exc_info=True)
    try:
        ws.close()
    except (websocket.WebSocketException, OSError):
        logger.debug("[fetch_kis_realtime_prices] ws.close() failed", exc_info=True)


def fetch_kis_realtime_prices(tickers: list, timeout: float = _KIS_WS_TIMEOUT) -> dict:
    """Opens a short-lived KIS WebSocket session, subscribes to real-time trade ticks
    (H0STCNT0) for each KR ticker, and collects the first tick price seen per ticker
    within `timeout` seconds. Tickers with no tick in that window (e.g. no trades yet,
    or market closed) fall back to a per-ticker REST quote via fetch_kis_stock_info.

    Meant for small ticker sets (open/recently-closed positions), not bulk watchlists.
    Fewer than _KIS_WS_MIN_TICKERS tickers, or a closed market, skip the WebSocket
    entirely and go straight to REST.
    """
    prices: dict = {}
    if not tickers:
        return prices

    # Outside regular KRX market hours (e.g. weekends, nights), no ticks are generated.
    # Bypass the WebSocket connection and immediately use REST fallback to avoid timeout delays.
    if not is_krx_market_open():
        logger.debug("[fetch_kis_realtime_prices] KRX is closed; bypassing WebSocket and using REST fallback")
        return _kis_rest_price_fallback(tickers, {})

    if len(tickers) < _KIS_WS_MIN_TICKERS:
        logger.debug("[fetch_kis_realtime_prices] %d ticker(s): REST is cheaper than a WebSocket session", len(tickers))
        return _kis_rest_price_fallback(tickers, {})

    try:
        import websocket  # websocket-client
    except ImportError:
        logger.warning("[fetch_kis_realtime_prices] websocket-client not installed, using REST fallback for all tickers")
        return _kis_rest_price_fallback(tickers, {})

    try:
        approval_key = _get_kis_approval_key()
        ws = websocket.create_connection(_KIS_WS_URL, timeout=timeout)
    except (*KIS_ERRORS, websocket.WebSocketException):
        logger.warning("[fetch_kis_realtime_prices] WebSocket connect failed, using REST fallback", exc_info=True)
        return _kis_rest_price_fallback(tickers, {})

    try:
        _kis_ws_collect_ticks(ws, websocket, approval_key, tickers, timeout, prices)
    finally:
        _kis_ws_close(ws, websocket, approval_key, tickers)

    missing = [t for t in tickers if t not in prices]
    if missing:
        prices.update(_kis_rest_price_fallback(missing, prices))
    return prices


def _kis_rest_price_fallback(tickers: list, existing: dict) -> dict:
    out = {}
    if not tickers:
        return out

    def _fetch_one(code):
        try:
            info = fetch_kis_stock_info(code)
            # `is not None` (not a truthy check on price): a halted or not-yet-traded
            # ticker can legitimately report price 0, and that's still a real answer —
            # `if info.get("price")` would silently drop it as if the fetch had failed.
            if info is not None:
                return code, float(info.get("price", 0))
        except KIS_ERRORS:
            logger.debug("[fetch_kis_realtime_prices] REST fallback failed for %s", code, exc_info=True)
        return code, None

    if len(tickers) == 1:
        c, p = _fetch_one(tickers[0])
        if p is not None:
            out[c] = p
        return out

    from concurrent.futures import ThreadPoolExecutor, as_completed
    max_workers = min(len(tickers), 5)
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_fetch_one, code): code for code in tickers}
        for future in as_completed(futures):
            try:
                c, p = future.result()
                if p is not None:
                    out[c] = p
            except Exception:
                # boundary: one worker must not lose the other prices; _fetch_one catches fetch errors itself, so only bugs land here.
                logger.warning("[fetch_kis_realtime_prices] REST fallback worker for %s raised", futures[future], exc_info=True)
    return out
