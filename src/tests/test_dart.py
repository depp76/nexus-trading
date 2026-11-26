"""DART financial DB (docs/dart_db_plan_2026-11-26.md 8): parsing, quarter
maths, corrections, as-of queries, the client's limits and cache, and run_sync's
lock / cancel / budget / no-key behaviour -- all on a fake OpenDART."""
import datetime as dt
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

from data.dart import filings as fil
from data.dart import financials as fin
from data.dart import query
from data.dart import segments as seg
from data.dart.client import DartBudgetExceeded, DartCancelled, DartClient, calls_today
from data.dart.lock import SyncLock
from data.dart.store import connect
from data.dart.sync import run_sync

import paths  # noqa: E402

# The real dart.db, taken before conftest points paths.DART_DB_FILE at a temporary file.
REAL_DART_DB = paths.DART_DB_FILE

SAMSUNG = "00126380"
SMALL = "00999999"          # separate statements only (OFS)


def _fin_row(sj, aid, nm, ord_, cur, add=None, rcept="20250515000001"):
    return {"rcept_no": rcept, "sj_div": sj, "account_id": aid, "account_nm": nm, "ord": str(ord_),
            "thstrm_amount": cur, "thstrm_add_amount": add, "frmtrm_amount": "", "currency": "KRW"}


def _is_rows(rev, op, ni, owners, rcept, add=None, sj="IS"):
    add = add or {}
    return [
        _fin_row(sj, "ifrs-full_Revenue", "매출액", 1, rev, add.get("rev"), rcept),
        _fin_row(sj, "dart_OperatingIncomeLoss", "영업이익", 5, op, add.get("op"), rcept),
        _fin_row(sj, "ifrs-full_ProfitLoss", "당기순이익", 9, ni, add.get("ni"), rcept),
        _fin_row(sj, "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", 10, owners,
                 add.get("own"), rcept),
    ]


class FakeResp:
    def __init__(self, payload=None, content=None, status_code=200):
        self._payload, self.status_code = payload, status_code
        self.content = content if content is not None else json.dumps(payload).encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeDart:
    """A tiny OpenDART: companies, periodic-report list, financials by
    (corp, year, reprt, fs_div). Counts every request."""

    def __init__(self):
        self.requests = []
        self.limit_hit = False
        self.hook = None                  # hook(endpoint, params) -> a reply, an exception raised, or None
        self.list_rows = [
            # Samsung 2025: Q1, H1, Q3 (filed 2025-11-14), and a corrected H1.
            self._lr(SAMSUNG, "005930", "삼성전자", "Y", "분기보고서 (2025.03)", "20250515000001"),
            self._lr(SAMSUNG, "005930", "삼성전자", "Y", "반기보고서 (2025.06)", "20250814000002"),
            self._lr(SAMSUNG, "005930", "삼성전자", "Y", "분기보고서 (2025.09)", "20251114000003"),
            self._lr(SAMSUNG, "005930", "삼성전자", "Y", "[기재정정]반기보고서 (2025.06)", "20250901000004"),
            self._lr(SMALL, "123450", "작은회사", "K", "분기보고서 (2025.03)", "20250515000009"),
            self._lr(SMALL, "123450", "작은회사", "K", "주요사항보고서(자기주식취득결정)", "20250516000010"),
        ]
        self.fin = {
            (SAMSUNG, "2025", "11013", "CFS"): _is_rows("100", "10", "8", "7", "20250515000001"),
            # API serves the newest version of H1: the correction's receipt.
            (SAMSUNG, "2025", "11012", "CFS"): _is_rows("120", "12", "9", "8", "20250901000004",
                                                         add={"rev": "220", "op": "22", "ni": "17", "own": "15"}),
            (SAMSUNG, "2025", "11014", "CFS"): _is_rows("130", "-5", "-3", "-2", "20251114000003",
                                                         add={"rev": "350", "op": "17", "ni": "14", "own": "13"}),
            # The small company has no consolidated statements and reports in CIS only.
            (SMALL, "2025", "11013", "OFS"): _is_rows("1,000", "(50)", "-40", "", "20250515000009", sj="CIS"),
        }
        self.documents: dict = {}         # rcept_no -> document.xml text

    @staticmethod
    def _lr(corp, stock, name, cls, report, rcept):
        return {"corp_code": corp, "stock_code": stock, "corp_name": name, "corp_cls": cls,
                "report_nm": report, "rcept_no": rcept, "rcept_dt": rcept[:8]}

    def corp_zip(self):
        xml = ("<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
               "<stock_code>005930</stock_code></list><list><corp_code>00999999</corp_code>"
               "<corp_name>작은회사</corp_name><stock_code>123450</stock_code></list>"
               "<list><corp_code>00888888</corp_code><corp_name>은행지주</corp_name>"
               "<stock_code>105560</stock_code></list>"
               "<list><corp_code>00000001</corp_code><corp_name>비상장</corp_name><stock_code> </stock_code></list>"
               "</result>")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("CORPCODE.xml", xml)
        return buf.getvalue()

    def get(self, url, params=None, timeout=None):
        params = dict(params or {})
        self.requests.append((url.rsplit("/", 1)[-1], {k: v for k, v in params.items() if k != "crtfc_key"}))
        ep = url.rsplit("/", 1)[-1]
        if self.hook is not None:
            reply = self.hook(ep, params)
            if reply is not None:
                return reply
        if self.limit_hit:
            return FakeResp({"status": "020", "message": "limit"})
        if ep == "corpCode.xml":
            return FakeResp(content=self.corp_zip())
        if ep == "list.json":
            bgn, end = params["bgn_de"], params["end_de"]
            rows = [r for r in self.list_rows if bgn <= r["rcept_dt"] <= end
                    and (not params.get("corp_cls") or r["corp_cls"] == params["corp_cls"])
                    and (not params.get("corp_code") or r["corp_code"] == params["corp_code"])]
            if not rows:
                return FakeResp({"status": "013", "message": "no data"})
            return FakeResp({"status": "000", "total_page": 1, "list": rows})
        if ep == "fnlttSinglAcntAll.json":
            k = (params["corp_code"], params["bsns_year"], params["reprt_code"], params["fs_div"])
            if k in self.fin:
                return FakeResp({"status": "000", "list": self.fin[k]})
            return FakeResp({"status": "013", "message": "no data"})
        if ep == "document.xml" and params["rcept_no"] in self.documents:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr(params["rcept_no"] + ".xml", self.documents[params["rcept_no"]])
            return FakeResp(content=buf.getvalue())
        if ep == "document.xml":
            return FakeResp({"status": "014", "message": "file not found"})
        if ep == "fnlttXbrl.xml":
            return FakeResp({"status": "014", "message": "file not found"})
        return FakeResp({"status": "100", "message": "bad endpoint"})


class _TmpMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)          # registered first: runs after the connections close
        self.dir = self._tmp.name
        self.db = os.path.join(self.dir, "dart.db")
        self.cache = os.path.join(self.dir, "cache")
        self.lock = os.path.join(self.cache, "sync.lock")
        self.fake = FakeDart()

    def factory(self, **kw):
        def make(conn, key, run_id):
            return DartClient(conn, key, self.cache, run_id=run_id, session=self.fake, min_interval=0,
                              backoff=0, **kw)
        return make

    def sync(self, mode="auto", **kw):
        kw.setdefault("client_factory", self.factory())
        return run_sync(mode, "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                        lock_path=self.lock, today=dt.date(2025, 11, 26), priority=[], **kw)


class TestParsing(unittest.TestCase):
    def test_amounts_and_units(self):
        self.assertEqual(fin.parse_amount("1,234,567"), 1234567.0)
        self.assertEqual(fin.parse_amount("-5"), -5.0)
        self.assertEqual(fin.parse_amount("(50)"), -50.0)
        for blank in ("", "-", None, "N/A"):
            self.assertIsNone(fin.parse_amount(blank))

    def test_report_names(self):
        self.assertEqual(fil.parse_report_name("[기재정정]사업보고서 (2024.12)"), ("사업보고서", 2024, 12, ["기재정정"]))
        self.assertEqual(fil.parse_report_name("분기보고서 (2025.09)")[:3], ("분기보고서", 2025, 9))
        self.assertIsNone(fil.parse_report_name("주요사항보고서(자기주식취득결정)"))

    def test_classify_december_and_march_year_ends(self):
        self.assertEqual(fil.classify("분기보고서", 2025, 3, 12), (2025, 1, "11013"))
        self.assertEqual(fil.classify("반기보고서", 2025, 6, 12), (2025, 2, "11012"))
        self.assertEqual(fil.classify("분기보고서", 2025, 9, 12), (2025, 3, "11014"))
        self.assertEqual(fil.classify("사업보고서", 2025, 12, 12), (2025, 4, "11011"))
        # March year end: FY ending 2025-03 has Q1 = 2024.06, Q3 = 2024.12.
        self.assertEqual(fil.classify("분기보고서", 2024, 6, 3), (2025, 1, "11013"))
        self.assertEqual(fil.classify("반기보고서", 2024, 9, 3), (2025, 2, "11012"))
        self.assertEqual(fil.classify("분기보고서", 2024, 12, 3), (2025, 3, "11014"))
        self.assertEqual(fil.classify("사업보고서", 2025, 3, 3), (2025, 4, "11011"))

    def test_quarter_windows(self):
        w = fil.quarter_windows(dt.date(2025, 2, 10), dt.date(2025, 7, 3))
        self.assertEqual(w, [(dt.date(2025, 2, 10), dt.date(2025, 3, 31)), (dt.date(2025, 4, 1), dt.date(2025, 6, 30)),
                             (dt.date(2025, 7, 1), dt.date(2025, 7, 3))])


class TestFinancials(_TmpMixin, unittest.TestCase):
    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_q4_is_annual_minus_q3_cumulative(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2024, "11014", "CFS",
                         _is_rows("30", "3", "2", "2", "20241114000001",
                                  add={"rev": "90", "op": "9", "ni": "6", "own": "5"}))
        fin.store_period(conn, SAMSUNG, 2024, "11011", "CFS", _is_rows("130", "10", "-1", "1", "20250310000002"))
        rows = {r["metric"]: r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 4}
        self.assertEqual((rows["revenue"]["value_q"], rows["revenue"]["value_ytd"], rows["revenue"]["derived"]),
                         (40.0, 130.0, 1))
        self.assertEqual(rows["net_income"]["value_q"], -7.0)

    def test_q4_waits_for_q3_and_is_filled_when_it_arrives(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2024, "11011", "CFS", _is_rows("130", "10", "-1", "1", "20250310000002"))
        self.assertIsNone(query.get_financials(SAMSUNG, conn=conn)[0]["value_q"])
        fin.store_period(conn, SAMSUNG, 2024, "11014", "CFS",
                         _is_rows("30", "3", "2", "2", "20241114000001",
                                  add={"rev": "90", "op": "9", "ni": "6", "own": "5"}))
        q4 = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 4 and r["metric"] == "revenue"]
        self.assertEqual(q4[0]["value_q"], 40.0)

    def test_is_then_cis_and_names_without_tags(self):
        rows = [_fin_row("CIS", "-표준계정코드 미사용-", "영업수익", 1, "77"),
                _fin_row("IS", "ifrs-full_ProfitLoss", "당기순이익", 3, "5"),
                _fin_row("CIS", "ifrs-full_ProfitLoss", "당기순이익", 3, "6")]
        self.assertEqual(fin.find_metric(rows, "revenue")["thstrm_amount"], "77")
        self.assertEqual(fin.find_metric(rows, "net_income")["thstrm_amount"], "5")
        self.assertIsNone(fin.find_metric(rows, "op_income"))
        # Untagged net income named by period (Hyundai 2021 Q3: "연결분기순이익(손실)").
        for name in ("연결분기순이익(손실)", "반기순손익", "당기순이익"):
            self.assertIsNotNone(fin.find_metric([_fin_row("IS", "-표준계정코드 미사용-", name, 1, "1")], "net_income"))
        self.assertIsNone(fin.find_metric([_fin_row("IS", "x", "법인세비용차감전순이익(손실)", 1, "1")], "net_income"))

    def test_unmapped_metrics_are_listed(self):
        conn = self._conn()
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS",
                                         [_fin_row("IS", "x", "기타수익", 1, "5", rcept="20250515000001")])
        self.assertEqual(set(missing), {"revenue", "op_income", "net_income", "net_income_owners"})
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_unmapped").fetchone()[0], 4)

    def test_half_year_value_derived_from_cumulative(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS",
                         _is_rows("", "12", "9", "8", "20250814000002", add={"rev": "220"}))
        h1 = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 2 and r["metric"] == "revenue"]
        self.assertEqual((h1[0]["value_q"], h1[0]["derived"]), (120.0, 1))


class TestSyncAndQuery(_TmpMixin, unittest.TestCase):
    def test_full_run_loads_quarters_corrections_and_fallbacks(self):
        conn = connect(self.db)                       # a run whose process died mid-way
        conn.execute("INSERT INTO sync_run(run_id, trigger, mode, status) VALUES ('dead', 'app', 'auto', 'running')")
        conn.commit()
        conn.close()
        res = self.sync("auto")
        self.assertEqual(res.status, "done", res.error)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT status FROM sync_run WHERE run_id = 'dead'").fetchone()[0], "partial")
        # Companies: listed ones only, market from the list.
        self.assertEqual(conn.execute("SELECT market FROM company WHERE corp_code = ?", (SMALL,)).fetchone()[0],
                         "KOSDAQ")
        self.assertIsNone(conn.execute("SELECT 1 FROM company WHERE corp_code = '00000001'").fetchone())
        # The original H1 points at its correction; the correction is the newest.
        sup = dict(conn.execute("SELECT rcept_no, superseded_by FROM filing WHERE corp_code = ?", (SAMSUNG,)))
        self.assertEqual(sup["20250814000002"], "20250901000004")
        self.assertIsNone(sup["20250901000004"])
        # Quarterly values: Q2 is the 3-month amount, from the corrected report.
        rows = {(r["fiscal_q"], r["metric"]): r for r in query.get_financials("005930", conn=conn)}
        self.assertEqual(rows[(2, "revenue")]["value_q"], 120.0)
        self.assertEqual(rows[(2, "revenue")]["rcept_no"], "20250901000004")
        self.assertAlmostEqual(rows[(2, "revenue")]["qoq"], 0.2)
        self.assertIsNone(rows[(3, "op_income")]["turn"])          # no prior year in the window
        # The small company: CFS 013 -> OFS, CIS-only statement, "(50)" -> -50.
        small = {r["metric"]: r for r in query.get_financials("123450", conn=conn)}
        self.assertEqual((small["revenue"]["fs_div"], small["revenue"]["value_q"]), ("OFS", 1000.0))
        self.assertEqual(small["op_income"]["value_q"], -50.0)
        self.assertNotIn("net_income_owners", small)
        # Growth view agrees with the query.
        g = {(r["fiscal_q"], r["metric"]): r for r in query.get_growth("005930", conn=conn)}
        self.assertAlmostEqual(g[(2, "revenue")]["qoq"], 0.2)
        # Run record and usage counter.
        run = conn.execute("SELECT status, calls, trigger FROM sync_run WHERE run_id = ?", (res.run_id,)).fetchone()
        self.assertEqual(tuple(run), ("done", res.calls, "cli"))
        self.assertEqual(calls_today(conn), res.calls)
        self.assertEqual(len(self.fake.requests), res.calls)

    def test_as_of_hides_reports_not_yet_received(self):
        self.sync("auto")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        before = {(r["fiscal_year"], r["fiscal_q"]) for r in query.get_financials("005930", as_of="2025-11-10", conn=conn)}
        self.assertIn((2025, 1), before)
        self.assertNotIn((2025, 3), before)          # Q3 was received on 2025-11-14
        after = {(r["fiscal_year"], r["fiscal_q"]) for r in query.get_financials("005930", as_of=dt.date(2025, 11, 14),
                                                                                  conn=conn)}
        self.assertIn((2025, 3), after)
        # Before the correction (2025-09-01) H1 has no value: the API serves only the corrected numbers.
        aug = {(r["fiscal_q"], r["metric"]) for r in query.get_financials("005930", as_of="2025-08-20", conn=conn)}
        self.assertNotIn((2, "revenue"), aug)

    def test_correction_selection_prefers_newest_receipt(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("101", "10", "8", "7", "20250620000005"))
        latest = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["metric"] == "revenue"]
        self.assertEqual(latest[0]["value_q"], 101.0)
        early = [r for r in query.get_financials(SAMSUNG, as_of="2025-06-01", conn=conn) if r["metric"] == "revenue"]
        self.assertEqual(early[0]["value_q"], 100.0)
        view = conn.execute("SELECT value_q FROM v_fin_growth WHERE corp_code = ? AND metric = 'revenue'",
                            (SAMSUNG,)).fetchall()
        self.assertEqual([r[0] for r in view], [101.0])

    def test_second_daily_run_makes_few_calls(self):
        self.sync("auto")
        n = len(self.fake.requests)
        res = self.sync("daily")
        self.assertEqual(res.status, "done", res.error)
        self.assertLessEqual(len(self.fake.requests) - n, 10)
        self.assertEqual(res.filings_added, 0)

    def test_held_lock_skips(self):
        other = SyncLock(self.lock)
        self.assertTrue(other.acquire())
        try:
            res = self.sync("auto")
        finally:
            other.release()
        self.assertEqual(res.status, "skipped")
        self.assertEqual(self.fake.requests, [])

    def test_stale_lock_is_reclaimed(self):
        os.makedirs(self.cache, exist_ok=True)
        with open(self.lock, "w") as f:                 # a dead process
            json.dump({"pid": 2 ** 22 + 12345, "started": 0}, f)
        self.assertEqual(self.sync("auto").status, "done")
        with open(self.lock, "w") as f:                 # our own pid, but older than 2 h
            json.dump({"pid": os.getpid(), "started": 0}, f)
        lock = SyncLock(self.lock)
        self.assertTrue(lock.acquire())
        lock.release()
        self.assertFalse(os.path.exists(self.lock))

    def test_live_recent_lock_is_kept(self):
        os.makedirs(self.cache, exist_ok=True)
        import time
        with open(self.lock, "w") as f:
            json.dump({"pid": os.getpid(), "started": time.time()}, f)
        self.assertFalse(SyncLock(self.lock).acquire())

    def test_cancel_stops_at_boundary_and_rerun_resumes(self):
        stop = threading.Event()
        seen = []

        def progress(done, total, msg):
            seen.append(msg)
            if msg.startswith("financials 005930"):
                stop.set()                               # first financial period in flight
        res = self.sync("auto", cancel=stop, on_progress=progress)
        self.assertEqual(res.status, "partial")
        self.assertGreater(res.pending_left, 0)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT status FROM sync_run WHERE run_id = ?", (res.run_id,)).fetchone()[0],
                         "partial")
        # Nothing half-done is kept: the period in flight is rolled back and fetched by the rerun.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_std").fetchone()[0], 0)
        res2 = self.sync("auto")
        self.assertEqual((res2.status, res2.pending_left), ("done", 0))
        fetched = [p for ep, p in self.fake.requests if ep == "fnlttSinglAcntAll.json"]
        keys = [(p["corp_code"], p["reprt_code"], p["fs_div"]) for p in fetched]
        self.assertEqual(len(keys), len(set(keys)))       # nothing fetched twice

    def test_daily_limit_gives_partial_and_counts_shared_usage(self):
        conn = connect(self.db)
        conn.execute("INSERT INTO api_usage(kst_date, calls) VALUES (?, ?)", (dt.date.today().isoformat(), 0))
        conn.commit()
        conn.close()
        res = self.sync("auto", client_factory=self.factory(daily_limit=3, today=lambda: "2025-11-26"))
        self.assertEqual(res.status, "partial")
        self.assertEqual(len(self.fake.requests), 3)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(calls_today(conn, "2025-11-26"), 3)
        # Another process (same DB) finds the day used up without calling DART.
        res2 = self.sync("auto", client_factory=self.factory(daily_limit=3, today=lambda: "2025-11-26"))
        self.assertEqual((res2.status, len(self.fake.requests)), ("partial", 3))

    def test_server_limit_020_gives_partial(self):
        self.fake.limit_hit = True
        self.assertEqual(self.sync("auto").status, "partial")

    def test_no_key_makes_no_call(self):
        res = run_sync("auto", "app", api_key=None, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                       client_factory=self.factory(), priority=[])
        self.assertEqual((res.status, res.calls), ("no_key", 0))
        self.assertEqual(self.fake.requests, [])

    def test_tickers_mode_uses_per_company_lists(self):
        res = self.sync("backfill", tickers=["123450"])
        self.assertEqual(res.status, "done", res.error)
        lists = [p for ep, p in self.fake.requests if ep == "list.json"]
        self.assertTrue(lists and all(p.get("corp_code") == SMALL for p in lists))
        fetched = {p["corp_code"] for ep, p in self.fake.requests if ep == "fnlttSinglAcntAll.json"}
        self.assertEqual(fetched, {SMALL})


class TestClient(_TmpMixin, unittest.TestCase):
    def _client(self, **kw):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return DartClient(conn, "SECRETKEY123", self.cache, session=self.fake, min_interval=0, backoff=0, **kw)

    def test_013_is_data_not_error_and_cache_serves_reruns(self):
        c = self._client()
        params = {"corp_code": SAMSUNG, "bsns_year": "2019", "reprt_code": "11011", "fs_div": "CFS"}
        self.assertEqual(c.get_json("fnlttSinglAcntAll.json", params, cache_tag="r1")["status"], "013")
        c.get_json("fnlttSinglAcntAll.json", params, cache_tag="r1")
        self.assertEqual((c.calls, c.cache_hits), (1, 1))

    def test_020_raises_budget(self):
        self.fake.limit_hit = True
        with self.assertRaises(DartBudgetExceeded):
            self._client().get_json("list.json", {"bgn_de": "20250101", "end_de": "20250102"})

    def test_run_budget_stops_before_sending(self):
        c = self._client(budget=1)
        c.get_json("list.json", {"bgn_de": "20250101", "end_de": "20250102"})
        with self.assertRaises(DartBudgetExceeded):
            c.get_json("list.json", {"bgn_de": "20250101", "end_de": "20250103"})
        self.assertEqual(len(self.fake.requests), 1)

    def test_key_never_logged_or_cached(self):
        c = self._client()
        c.get_json("list.json", {"bgn_de": "20250501", "end_de": "20250531"}, cache_tag="t")
        logged = " ".join(r[0] or "" for r in c.conn.execute("SELECT params FROM fetch_log"))
        self.assertNotIn("SECRETKEY123", logged)
        for root, _dirs, files in os.walk(self.cache):
            for name in files:
                self.assertNotIn("SECRETKEY123", name)
                with open(os.path.join(root, name), encoding="utf-8") as f:
                    self.assertNotIn("SECRETKEY123", f.read())


# ---- follow-up 2026-11-26: plan items 1-7 -------------------------------------------

from data.dart import documents as docs  # noqa: E402

BANK = "00888888"


def _doc(titled=True, unit="백만원", note_col=True, three_month=True, owners="지배기업주주지분순이익"):
    """A small report document with a consolidated income statement table."""
    note_head = "<TD>주석</TD>" if note_col else ""
    note = "<TD>4,29</TD>" if note_col else ""
    cols = "<TD>3개월</TD><TD>누적</TD><TD>3개월</TD><TD>누적</TD>" if three_month else "<TD>당기</TD><TD>전기</TD>"

    def row(name, *vals):
        return f"<TR><TD>{name}</TD>{note}" + "".join(f"<TD>{v}</TD>" for v in vals) + "</TR>"
    title = '<TITLE ATOC="Y">2-2. 연결 포괄손익계산서(손익계산서)</TITLE>' if titled else ""
    section = '<TITLE ATOC="Y">2. 연결재무제표</TITLE>' if not titled else ""
    return (f"<DOCUMENT>{section}<TABLE><TR><TD>재무상태표</TD></TR><TR><TD>자산총계</TD><TD>9,999</TD></TR></TABLE>"
            f"{title}<TABLE><TR><TD>(단위 : {unit})</TD></TR></TABLE>"
            f"<TABLE><TR><TD>과목</TD>{note_head}{cols}</TR>"
            + row("Ⅰ.순이자이익", "300", "800", "250", "700")
            + row("1.이자수익", "500", "1,300", "450", "1,200")
            + row("IX.영업이익", "200", "550", "(10)", "400")
            + row("XⅢ.당(분)기연결순이익", "150", "420", "△5", "300")
            + row(owners, "140", "400", "△6", "280")
            + row("비지배지분순이익", "10", "20", "1", "20")
            + "</TABLE></DOCUMENT>")


class TestDocumentParser(unittest.TestCase):
    def test_titled_statement_with_note_column_and_unit(self):
        fs_div, rows, financial, nci = docs.income_rows(_doc(), "20220816000001", "11012")
        got = {r["account_id"]: (r["thstrm_amount"], r["thstrm_add_amount"]) for r in rows}
        self.assertEqual((fs_div, financial, nci), ("CFS", True, True))
        self.assertEqual(got["dart_OperatingIncomeLoss"], (200e6, 550e6))      # 백만원, note column skipped
        self.assertEqual(got["ifrs-full_ProfitLoss"], (150e6, 420e6))          # "당(분)기연결순이익"
        self.assertEqual(got["ifrs-full_ProfitLossAttributableToOwnersOfParent"], (140e6, 400e6))
        self.assertNotIn("ifrs-full_Revenue", got)

    def test_untitled_older_layout_and_cumulative_only_columns(self):
        fs_div, rows, *_ = docs.income_rows(_doc(titled=False, unit="천원", note_col=False, three_month=False),
                                            "20210816000001", "11012")
        op = next(r for r in rows if r["account_id"] == "dart_OperatingIncomeLoss")
        self.assertEqual((fs_div, op["thstrm_amount"], op["thstrm_add_amount"]), ("CFS", None, 200e3))

    def test_cp949_document_declared_utf8(self):
        raw = '<?xml version="1.0" encoding="utf-8"?><X>손익계산서</X>'.encode("cp949")
        self.assertIn("손익계산서", docs.decode_document(raw))

    def test_no_statement(self):
        self.assertIsNone(docs.income_rows("<DOCUMENT><TITLE>1. 회사의 개요</TITLE></DOCUMENT>", "r", "11013"))


class TestFollowUps(_TmpMixin, unittest.TestCase):
    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def test_financial_filer_revenue_not_applicable(self):
        conn = self._conn()
        r = "20250515000001"
        rows = [_fin_row("CIS", "ifrs-full_RevenueFromInterest", "이자수익", 1, "500", rcept=r),
                _fin_row("CIS", "dart_OperatingIncomeLoss", "영업이익", 2, "200", rcept=r),
                _fin_row("CIS", "ifrs-full_ProfitLoss", "분기순이익", 3, "150", rcept=r),
                _fin_row("CIS", "ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", 4, "140",
                         rcept=r),
                _fin_row("CIS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", 5, "10",
                         rcept=r)]
        _src, missing = fin.store_period(conn, BANK, 2025, "11013", "CFS", rows)
        self.assertEqual(missing, [])
        self.assertEqual([x[0] for x in conn.execute("SELECT kind FROM fin_issue")], ["revenue_na"])
        self.assertEqual({x["metric"] for x in query.get_financials(BANK, conn=conn)},
                         {"op_income", "net_income", "net_income_owners"})

    def test_owners_from_net_income_without_minority_line(self):
        conn = self._conn()
        rows = [r for r in _is_rows("100", "10", "8", "7", "20250515000001") if "Owners" not in r["account_id"]]
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows)
        owners = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["metric"] == "net_income_owners"]
        self.assertEqual((missing, owners[0]["value_q"], owners[0]["derived"]), ([], 8.0, 1))
        # With a non-controlling line the owners' share cannot be inferred: unmapped.
        rows2 = [dict(r, rcept_no="20250814000002") for r in rows]
        rows2.append(_fin_row("IS", "ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", 11, "1",
                              rcept="20250814000002"))
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS", rows2)
        self.assertEqual(missing, ["net_income_owners"])

    def test_owners_and_net_income_names(self):
        self.assertTrue(fin.is_owners_name("지배회사소유주지분순이익"))
        self.assertTrue(fin.is_owners_name("지배주주지분당반기순이익"))
        self.assertFalse(fin.is_owners_name("비지배지분순이익"))
        self.assertFalse(fin.is_owners_name("지배기업주주지분총포괄이익"))
        for n in ("분기연결순이익(손실)", "연결당반기순이익", "당(분)기연결순이익", "당기순이익"):
            self.assertTrue(fin.is_net_income_name(n), n)
        self.assertFalse(fin.is_net_income_name("법인세비용차감전순이익"))

    def test_q4_uses_a_later_corrected_q3_and_says_so(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2024, "11011", "CFS", _is_rows("130", "10", "-1", "1", "20250310000002"))
        fin.store_period(conn, SAMSUNG, 2024, "11014", "CFS",       # Q3 known only from a 2025-06 correction
                         _is_rows("30", "3", "2", "2", "20250620000009",
                                  add={"rev": "90", "op": "9", "ni": "6", "own": "5"}))
        q4 = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 4 and r["metric"] == "revenue"]
        self.assertEqual(q4[0]["value_q"], 40.0)
        self.assertIn("q4_uses_later_q3", [r[0] for r in conn.execute("SELECT kind FROM fin_issue")])

    def test_api_year_from_report_name(self):
        from data.dart.sync import api_years
        self.assertEqual(api_years([{"rcept_no": "1", "report_nm": "분기보고서 (2026.06)"}], 2027, 3), [2026])
        self.assertEqual(api_years([{"rcept_no": "1", "report_nm": None}], 2027, 3), [2027, 2026])
        self.assertEqual(api_years([{"rcept_no": "1", "report_nm": None}], 2025, 12), [2025])

    def test_calendar_period_in_query_and_view(self):
        self.assertEqual(query.calendar_period(2027, 1, 3), ("2026-06", 2026, 2))
        self.assertEqual(query.calendar_period(2026, 4, 3), ("2026-03", 2026, 1))
        self.assertEqual(query.calendar_period(2025, 3, 12), ("2025-09", 2025, 3))
        self.assertEqual(query.calendar_period(2025, 1, 2), ("2024-05", 2024, 2))
        conn = self._conn()
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name, fiscal_month) "
                     "VALUES ('M3', '067010', 'x', 3)")
        fin.store_period(conn, "M3", 2027, "11013", "OFS", _is_rows("5", "1", "1", "", "20260814000001"))
        r = conn.execute("SELECT period_end, cal_year, cal_q FROM v_fin_growth WHERE corp_code = 'M3' "
                         "AND metric = 'revenue'").fetchone()
        self.assertEqual(tuple(r), ("2026-06", 2026, 2))
        row = query.get_financials("067010", conn=conn)[0]
        self.assertEqual((row["period_end"], row["cal_year"], row["cal_q"]), ("2026-06", 2026, 2))

    def test_document_fallback_when_the_api_has_no_data(self):
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "반기보고서 (2025.06)",
                                                "20250814000020"))
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "[첨부정정]반기보고서 (2025.06)",
                                                "20250820000021"))
        self.fake.documents["20250814000020"] = _doc()       # the later correction has no statements
        res = self.sync("backfill", tickers=["105560"])
        self.assertEqual(res.status, "done", res.error)
        conn = self._conn()
        got = {r["metric"]: r for r in query.get_financials("105560", conn=conn)}
        self.assertEqual((got["op_income"]["value_q"], got["op_income"]["value_ytd"]), (200e6, 550e6))
        self.assertEqual(got["op_income"]["rcept_no"], "20250814000020")
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT kind FROM fin_issue")),
                         ["from_document", "revenue_na"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_raw WHERE rcept_no = '20250814000020'").fetchone()[0],
                         0)
        # Statement sections are cached: reprocessing calls no document again.
        n = len(self.fake.requests)
        conn.execute("DELETE FROM fin_fetch")
        conn.commit()
        self.sync("backfill", tickers=["105560"])
        self.assertEqual([p for ep, p in self.fake.requests[n:] if ep == "document.xml"], [])

    def test_no_document_statement_is_no_data(self):
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "분기보고서 (2025.03)",
                                                "20250515000030"))
        res = self.sync("backfill", tickers=["105560"])
        self.assertEqual(res.status, "done", res.error)
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20250515000030'").fetchone()[0],
                         "no_data")
        self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM fin_issue")], ["no_data"])

    def test_growth_table_and_issue_report(self):
        self.sync("auto")
        table = query.format_growth_table("005930", db_path=self.db)
        self.assertIn("2025Q2", table)
        self.assertIn("2025-06", table)
        rows = query.growth_table("005930", db_path=self.db)
        self.assertEqual(rows[-1]["fiscal_q"], 3)
        self.assertAlmostEqual(rows[-1]["op_margin"], -5 / 130)
        self.assertAlmostEqual(rows[-1]["revenue_qoq"], 130 / 120 - 1)
        early = query.growth_table("005930", "2025-11-10", db_path=self.db)
        self.assertEqual([r["fiscal_q"] for r in early], [1, 2])   # Q3 was received on 2025-11-14
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS",
                         [_fin_row("IS", "x", "기타", 1, "5", rcept="20250601000001")])
        conn.commit()
        text = query.format_issues(db_path=self.db)
        self.assertIn("계정 미매핑", text)
        self.assertIn("005930", text)

# ---- review_agy.md (2026-11-26) -----------------------------------------------------

import sqlite3  # noqa: E402
import sys  # noqa: E402

import requests  # noqa: E402

from data.dart import store  # noqa: E402
from data.dart import sync as dsync  # noqa: E402
from data.dart.lock import pid_alive  # noqa: E402

MARCH = "00777777"          # March year end


class TestReviewStore(_TmpMixin, unittest.TestCase):
    """H1: readers never write; the schema script runs only on a version change."""

    def test_current_schema_is_not_rewritten(self):
        connect(self.db).close()
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.total_changes, 0)

    def test_readonly_connection_cannot_write_and_reads_while_a_writer_holds_the_lock(self):
        writer = connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES ('C1', '000001', 'x')")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")                       # a collector mid-transaction
        ro = store.open_readonly(self.db)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("SELECT COUNT(*) FROM company").fetchone()[0], 1)
        self.assertEqual(query.get_financials("000001", db_path=self.db), [])
        self.assertEqual(dsync.collection_status(self.db)["periods_total"], 0)
        with self.assertRaises(sqlite3.OperationalError):
            ro.execute("INSERT INTO company(corp_code, corp_name) VALUES ('C2', 'y')")
        writer.rollback()

    def test_missing_db_is_empty_and_not_created(self):
        self.assertIsNone(store.open_readonly(self.db))
        self.assertEqual(query.get_financials("005930", db_path=self.db), [])
        self.assertEqual(query.get_growth("005930", db_path=self.db), [])
        self.assertEqual(query.issues(db_path=self.db)["counts"], [])
        st = dsync.collection_status(self.db)
        self.assertEqual((st["last_run"], st["list_done"], st["calls_left"]), (None, False, 19_000))
        self.assertFalse(os.path.exists(self.db))

    def test_older_schema_is_migrated_once(self):
        conn = sqlite3.connect(self.db)                         # a version-3 file: no currency, old views
        old_schema = "\n".join(line for line in store._SCHEMA.splitlines()
                               if not line.startswith(("  currency TEXT DEFAULT", "  avail_dt TEXT")))
        conn.executescript(old_schema)
        conn.execute("INSERT INTO sync_state(key, value) VALUES ('schema_version', '3')")
        conn.execute("INSERT INTO fin_std VALUES ('C1', 2025, 1, 'CFS', 'revenue', 5, 5, 0, 'r1', '2025-05-15')")
        conn.execute("INSERT INTO fin_raw(rcept_no, fs_div, sj_div, ord, currency) VALUES ('r1', 'CFS', 'IS', 1, 'USD')")
        conn.commit()
        conn.close()
        ro = store.open_readonly(self.db)
        self.addCleanup(ro.close)
        self.assertEqual(ro.execute("SELECT currency FROM fin_std").fetchone()[0], "USD")
        self.assertEqual(ro.execute("SELECT value FROM sync_state WHERE key = 'schema_version'").fetchone()[0],
                         str(store.SCHEMA_VERSION))
        self.assertEqual(ro.execute("SELECT currency, fs_div_changed FROM v_fin_growth").fetchone()[:], ("USD", 0))


def _table(rows, head="", cols="<TD>3개월</TD><TD>누적</TD><TD>3개월</TD><TD>누적</TD>", unit="백만원"):
    body = "".join("<TR>" + "".join(f"<TD>{c}</TD>" for c in r) + "</TR>" for r in rows)
    return (f'<DOCUMENT><TITLE>2-2. 연결 손익계산서</TITLE>{head}'
            f"<TABLE><TR><TD>(단위 : {unit})</TD></TR><TR><TD>과목</TD>{cols}</TR>{body}</TABLE>"
            "<TABLE><TR><TD>영업이익</TD><TD>9</TD><TD>9</TD><TD>9</TD><TD>9</TD></TR></TABLE></DOCUMENT>")


class TestReviewDocuments(_TmpMixin, unittest.TestCase):
    """H2: a blank current cell is a missing value, never the next column's number."""

    def _amounts(self, doc, reprt="11012"):
        got = docs.income_rows(doc, "20220816000001", reprt)
        return {r["account_id"]: (r["thstrm_amount"], r["thstrm_add_amount"]) for r in got[1]} if got else None

    def test_blank_three_month_cell_does_not_shift_columns(self):
        doc = _table([["영업이익", "-", "550", "(10)", "400"], ["당기순이익", "150", "", "5", "300"]])
        got = self._amounts(doc)
        self.assertEqual(got["dart_OperatingIncomeLoss"], (None, 550e6))     # was (550e6, -10e6)
        self.assertEqual(got["ifrs-full_ProfitLoss"], (150e6, None))

    def test_blank_current_period_is_not_the_prior_period(self):
        doc = _table([["영업이익", "-", "400"], ["당기순이익", "7", "300"]], cols="<TD>당기</TD><TD>전기</TD>")
        got = self._amounts(doc, "11013")
        self.assertNotIn("dart_OperatingIncomeLoss", got)                    # was 400e6 (last year's)
        self.assertEqual(got["ifrs-full_ProfitLoss"], (7e6, None))

    def test_annual_table_with_three_periods(self):
        doc = _table([["영업이익", "-", "20", "30"], ["당기순이익", "1", "2", "3"]],
                     cols="<TD>당기</TD><TD>전기</TD><TD>전전기</TD>")
        got = self._amounts(doc, "11011")
        self.assertNotIn("dart_OperatingIncomeLoss", got)
        self.assertEqual(got["ifrs-full_ProfitLoss"], (1e6, None))

    def test_statement_table_found_behind_two_heading_tables_or_none(self):
        two = _table([["영업이익", "1", "2", "3", "4"]],
                     head="<TABLE><TR><TD>연결 손익계산서</TD></TR></TABLE><TABLE><TR><TD>제 1 기</TD></TR></TABLE>")
        self.assertEqual(self._amounts(two)["dart_OperatingIncomeLoss"], (1e6, 2e6))
        none = _table([["영업이익", "1", "2", "3", "4"]])
        self.assertEqual(self._amounts(none)["dart_OperatingIncomeLoss"], (1e6, 2e6))   # not the next table's 9

    def test_lowercase_tags_and_foreign_currency_unit(self):
        doc = _table([["영업이익", "1", "2", "3", "4"]], unit="천USD").lower().replace("usd", "USD")
        fs_div, rows, *_ = docs.income_rows(doc, "20220816000001", "11012")
        self.assertEqual((rows[0]["thstrm_amount"], rows[0]["currency"]), (1e3, "USD"))

    def test_header_spans_give_the_columns(self):
        """The SK스퀘어 2022 layout: one period shown, "3개월 | 누적" each over a
        sub-amount / total column pair, the 과목 header spanning two rows -- and
        the tail of the balance sheet (its own "지배기업 소유주 ... 자본" line) in
        front of the statement."""
        doc = ('<DOCUMENT><TITLE>2. 연결재무제표</TITLE>'
               '<TABLE><TR><TH>과목</TH><TH>당기말</TH></TR>'
               '<TR><TD>Ⅰ.지배기업소유주에귀속되는자본</TD><TD>17,537,797</TD></TR>'
               '<TR><TD>Ⅱ.비지배지분</TD><TD>196,322</TD></TR></TABLE>'
               '<TABLE><TR><TD>(단위 : 백만원)</TD></TR></TABLE>'
               '<TABLE><TR><TH ROWSPAN="2">과목</TH><TH COLSPAN="4">제 2 기 반기</TH></TR>'
               '<TR><TH COLSPAN="2">3개월</TH><TH COLSPAN="2">누적</TH></TR>'
               '<TR><TD>Ⅰ.영업수익</TD><TD></TD><TD>1,529,031</TD><TD></TD><TD>2,850,101</TD></TR>'
               '<TR><TD>매출액</TD><TD>950,373</TD><TD></TD><TD>1,872,241</TD><TD></TD></TR>'
               '<TR><TD>Ⅲ.영업이익</TD><TD></TD><TD>-</TD><TD></TD><TD>889,478</TD></TR>'
               '<TR><TD>Ⅵ.반기연결순이익</TD><TD></TD><TD>493,830</TD><TD></TD><TD>958,838</TD></TR>'
               '<TR><TD>지배기업의 소유주지분</TD><TD></TD><TD>486,221</TD><TD></TD><TD>915,744</TD></TR>'
               '<TR><TD>비지배지분</TD><TD></TD><TD>7,609</TD><TD></TD><TD>43,094</TD></TR></TABLE></DOCUMENT>')
        got = self._amounts(doc)
        self.assertEqual(got["ifrs-full_Revenue"], (1529031e6, 2850101e6))
        self.assertEqual(got["dart_OperatingIncomeLoss"], (None, 889478e6))          # blank 3-month cell
        self.assertEqual(got["ifrs-full_ProfitLossAttributableToOwnersOfParent"], (486221e6, 915744e6))

    def test_four_periods_over_column_pairs(self):
        """The KB금융 2022 Q1 layout: four period headers, each over two columns."""
        head = "".join(f'<TH COLSPAN="2">{t}</TH>' for t in ("제15기 1분기", "제14기 1분기", "제14기", "제13기"))
        doc = ('<DOCUMENT><TITLE>2-2. 연결 손익계산서</TITLE><TABLE><TR><TD>(단위 : 백만원)</TD></TR></TABLE>'
               f'<TABLE><TR><TH>과목</TH>{head}</TR>'
               '<TR><TD>IX. 영업이익</TD><TD></TD><TD>-</TD><TD></TD><TD>1,746,618</TD><TD></TD><TD>6,097,000</TD>'
               '<TD></TD><TD>4,616,000</TD></TR>'
               '<TR><TD>XⅢ. 당기순이익</TD><TD></TD><TD>1,464,102</TD><TD></TD><TD>1,285,009</TD><TD></TD><TD>4,409,543</TD>'
               '<TD></TD><TD>3,515,566</TD></TR></TABLE></DOCUMENT>')
        got = self._amounts(doc, "11013")
        self.assertNotIn("dart_OperatingIncomeLoss", got)                  # not last year's 1,746,618
        self.assertEqual(got["ifrs-full_ProfitLoss"], (1464102e6, None))

    def test_numbered_account_names_from_the_api(self):
        """신영증권: "III. 영업이익" without the standard tag."""
        rows = [_fin_row("CIS", "-표준계정코드 미사용-", "III. 영업이익", 1, "7"),
                _fin_row("CIS", "-표준계정코드 미사용-", "Ⅵ. 당기순이익(주5)", 2, "5")]
        self.assertEqual(fin.find_metric(rows, "op_income")["thstrm_amount"], "7")
        self.assertEqual(fin.find_metric(rows, "net_income")["thstrm_amount"], "5")
        self.assertFalse(fin.is_owners_name("Ⅰ.지배기업소유주에귀속되는자본"))

    def test_section_cache_is_written_atomically(self):
        path = os.path.join(self.dir, "document", "2022", "r_is.xml")
        docs._write_section(path, ("CFS", "<TABLE></TABLE>"))
        self.assertEqual(os.listdir(os.path.dirname(path)), ["r_is.xml"])    # no temporary file left
        with open(path, encoding="utf-8") as f:
            self.assertTrue(f.read().startswith("<!--fs_div=CFS-->"))


class TestReviewSync(_TmpMixin, unittest.TestCase):
    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    # ---- H3 ----
    def test_one_failing_period_is_skipped_and_stays_pending(self):
        def hook(ep, params):
            if ep == "fnlttSinglAcntAll.json" and params["reprt_code"] == "11012":
                raise requests.ConnectionError("boom")
        self.fake.hook = hook
        res = self.sync("auto")
        # A request that failed is "partial", not "done" (review_codex 3 P2).
        self.assertEqual((res.status, res.pending_left, res.periods_failed), ("partial", 1, 1))
        self.assertIn("request failures", res.error)
        conn = self._conn()
        got = {r["fiscal_q"] for r in query.get_financials("005930", conn=conn)}
        self.assertEqual(got, {1, 3})                       # the quarters after the failing one still arrived
        self.fake.hook = None
        self.assertEqual(self.sync("auto").pending_left, 0)

    def test_consecutive_failures_stop_the_run(self):
        def hook(ep, params):
            if ep == "fnlttSinglAcntAll.json":
                raise requests.ConnectionError("down")
        self.fake.hook = hook
        with mock.patch.object(dsync, "MAX_CONSECUTIVE_ERRORS", 2):
            res = self.sync("auto")
        self.assertEqual(res.status, "failed")
        self.assertIn("network_error", res.error)

    def test_fatal_status_stops_at_once(self):
        def hook(ep, params):
            if ep == "fnlttSinglAcntAll.json":
                return FakeResp({"status": "010", "message": "unregistered key"})
        self.fake.hook = hook
        res = self.sync("auto")
        self.assertEqual(res.status, "failed")
        self.assertEqual(len([1 for ep, _ in self.fake.requests if ep == "fnlttSinglAcntAll.json"]), 1)

    def test_429_is_retried(self):
        seen = []

        def hook(ep, params):
            if ep == "list.json" and not seen:
                seen.append(1)
                return FakeResp({"status": "000"}, status_code=429)
        self.fake.hook = hook
        self.assertEqual(self.sync("auto").status, "done")

    def test_cancel_interrupts_a_backoff_wait(self):
        conn = self._conn()
        stop = threading.Event()
        c = DartClient(conn, "k", self.cache, session=self.fake, min_interval=0, backoff=30, cancel=stop)

        def hook(ep, params):
            stop.set()
            raise requests.ConnectionError("down")
        self.fake.hook = hook
        t0 = time.monotonic()
        with self.assertRaises(DartCancelled):
            c.get_json("list.json", {"bgn_de": "20250101", "end_de": "20250102"})
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_client_closes_only_its_own_session(self):
        conn = self._conn()
        own = DartClient(conn, "k", self.cache)
        with mock.patch.object(own.session, "close") as closed:
            own.close()
        closed.assert_called_once()
        given = mock.Mock()
        DartClient(conn, "k", self.cache, session=given).close()
        given.close.assert_not_called()

    # ---- H4 / M8 ----
    def _march_rows(self):
        lr = FakeDart._lr
        return [lr(MARCH, "067010", "새이름", "K", "사업보고서 (2021.03)", "20210610000002"),
                lr(MARCH, "067010", "옛이름", "K", "분기보고서 (2020.12)", "20210215000001")]

    def test_first_year_quarter_of_a_march_filer_is_kept_and_reclassified(self):
        conn = self._conn()
        rows = self._march_rows()
        fil.store_filings(conn, rows[1:], 2021)             # the Q3 report arrives a window earlier
        self.assertEqual(tuple(conn.execute("SELECT bsns_year, reprt_code FROM filing").fetchone()),
                         (2020, "11014"))                   # looks like FY2020 until the fiscal month is known
        fil.store_filings(conn, rows[:1], 2021)
        got = dict((r[0], (r[1], r[2])) for r in conn.execute("SELECT rcept_no, bsns_year, reprt_code FROM filing"))
        self.assertEqual(got, {"20210215000001": (2021, "11014"), "20210610000002": (2021, "11011")})

    def test_reclassify_drops_what_was_stored_under_the_old_period(self):
        conn = self._conn()
        rows = self._march_rows()
        fil.store_filings(conn, rows[1:], 2021)
        fin.store_period(conn, MARCH, 2020, "11014", "OFS", _is_rows("5", "1", "1", "", "20210215000001"))
        conn.execute("INSERT INTO fin_fetch(rcept_no, status, src_rcept_no) VALUES ('20210215000001', 'ok', "
                     "'20210215000001')")
        fil.store_filings(conn, rows[:1], 2021)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_std").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_fetch").fetchone()[0], 0)

    def test_company_name_comes_from_the_newest_report(self):
        conn = self._conn()
        fil.store_filings(conn, self._march_rows(), 2021)   # list.json order: newest first
        self.assertEqual(conn.execute("SELECT corp_name FROM company").fetchone()[0], "새이름")

    def test_list_rules_change_walks_the_windows_again(self):
        self.sync("auto")
        conn = self._conn()
        conn.execute("UPDATE sync_state SET value = '1' WHERE key = 'list_version'")
        conn.commit()
        n = len([1 for ep, _ in self.fake.requests if ep == "list.json"])
        self.sync("backfill")
        self.assertGreater(len([1 for ep, _ in self.fake.requests if ep == "list.json"]), n)
        self.assertEqual(dsync.get_state(conn, "list_version"), dsync.LIST_VERSION)

    @unittest.skipUnless(sys.platform == "win32", "Windows process check")
    def test_pid_alive_counts_an_unopenable_process_as_alive(self):
        self.assertTrue(pid_alive(4))                       # the System process: OpenProcess is denied
        self.assertTrue(pid_alive(os.getpid()))
        self.assertFalse(pid_alive(2 ** 22 + 12345))

    # ---- M1 ----
    def test_no_pending_means_no_priority_lookup(self):
        self.sync("auto")
        # The Universe file is read for the segment targets (a local file); the
        # KRX market-cap download must not happen without pending financials.
        with mock.patch.object(dsync, "market_cap_priority", side_effect=AssertionError("looked up")):
            res = run_sync("auto", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                           lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory())
        self.assertEqual(res.status, "done", res.error)

    def test_market_caps_only_when_a_pending_stock_is_outside_the_universe(self):
        with mock.patch.object(dsync, "universe_priority", return_value=["005930", "123450"]), \
                mock.patch.object(dsync, "market_cap_priority", side_effect=AssertionError("looked up")) as caps:
            res = run_sync("auto", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                           lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory())
        self.assertEqual(res.status, "done", res.error)
        caps.assert_not_called()

    def test_universe_rows_without_ticker_are_ignored(self):
        with open(paths.UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump([{"market": "KOSPI", "ticker": None, "market_cap": 9}, {"market": "KOSPI", "ticker": "5930",
                                                                           "market_cap": 5}], f)
        self.assertEqual(dsync.universe_priority(), ["005930"])

    # ---- M4 ----
    def test_recent_filing_without_data_stays_pending_and_uncached(self):
        self.fake.list_rows.append(FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", "[기재정정]분기보고서 (2025.09)",
                                                "20251125000030"))
        saved = self.fake.fin.pop((SAMSUNG, "2025", "11014", "CFS"))
        res = self.sync("auto")                              # 013 for a report received yesterday
        self.assertEqual((res.status, res.pending_left), ("done", 1))
        conn = self._conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20251125000030'").fetchone())
        # DART has loaded the corrected statements by the next run.
        self.fake.fin[(SAMSUNG, "2025", "11014", "CFS")] = [dict(r, rcept_no="20251125000030") for r in saved]
        self.assertEqual(self.sync("auto").pending_left, 0)
        self.assertIn(3, {r["fiscal_q"] for r in query.get_financials("005930", conn=conn)})

    # ---- M5 ----
    def test_reparse_replaces_unmapped_and_issue_lines(self):
        conn = self._conn()
        rows = [_fin_row("IS", "entity_Sales", "제품매출합계", 1, "5", rcept="20250515000001"),
                _fin_row("IS", "dart_OperatingIncomeLoss", "영업이익", 2, "1", rcept="20250515000001"),
                _fin_row("IS", "entity_NI", "순이익합계", 3, "1", rcept="20250515000001")]
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows)
        self.assertEqual(set(missing), {"revenue", "net_income", "net_income_owners"})
        amap = {SAMSUNG: {"revenue": ["entity_Sales"], "net_income": ["entity_NI"]}}
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows, amap)
        self.assertEqual(missing, [])                        # owners' profit falls back to the mapped net income
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_unmapped").fetchone()[0], 0)
        self.assertEqual([r[0] for r in conn.execute("SELECT kind FROM fin_issue")], ["owners_from_net_income"])

    def test_rows_without_receipt_use_the_fallback(self):
        conn = self._conn()
        rows = [dict(r, rcept_no="") for r in _is_rows("5", "1", "1", "1", "x")]
        with self.assertRaises(ValueError):
            fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows)
        src, _ = fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows, fallback_rcept="20250515000001")
        self.assertEqual(src, "20250515000001")

    # ---- M6 ----
    def test_currency_is_kept_and_shown(self):
        conn = self._conn()
        rows = [dict(r, currency="USD") for r in _is_rows("5,000,000", "1,000,000", "1", "1", "20250515000001")]
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", rows)
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES (?, '005930', 'x')", (SAMSUNG,))
        conn.commit()
        self.assertEqual(query.get_financials("005930", conn=conn)[0]["currency"], "USD")
        text = query.format_growth_table("005930", conn=conn)
        self.assertIn("백만 USD", text)
        self.assertNotIn("억원", text)

    def test_growth_is_not_computed_across_a_statement_change(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2024, "11013", "OFS", _is_rows("50", "5", "4", "", "20240515000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        row = [r for r in query.get_financials(SAMSUNG, conn=conn)
               if r["fiscal_year"] == 2025 and r["metric"] == "revenue"][0]
        self.assertEqual((row["yoy"], row["fs_div_changed"]), (None, True))
        v = conn.execute("SELECT yoy, turn, fs_div_changed FROM v_fin_growth WHERE fiscal_year = 2025 "
                         "AND metric = 'op_income'").fetchone()
        self.assertEqual(tuple(v), (None, None, 1))

    # ---- M7 ----
    def test_lookahead_q4_is_flagged_and_hidden_until_knowable(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2024, "11011", "CFS", _is_rows("130", "10", "-1", "1", "20250310000002"))
        fin.store_period(conn, SAMSUNG, 2024, "11014", "CFS",
                         _is_rows("30", "3", "2", "2", "20250620000009",
                                  add={"rev": "90", "op": "9", "ni": "6", "own": "5"}))
        early = [r for r in query.get_financials(SAMSUNG, as_of="2025-04-01", conn=conn) if r["metric"] == "revenue"][0]
        self.assertEqual((early["value_q"], early["value_ytd"], early["lookahead"]), (None, 130.0, False))
        late = [r for r in query.get_financials(SAMSUNG, as_of="2025-06-20", conn=conn)
                if r["metric"] == "revenue" and r["fiscal_q"] == 4][0]
        self.assertEqual((late["value_q"], late["rcept_dt"], late["lookahead"]), (40.0, "2025-06-20", True))

    def test_half_year_value_is_filled_when_q1_arrives_later(self):
        conn = self._conn()
        half = [dict(r, thstrm_amount=None) for r in
                _is_rows("", "", "", "", "20250814000002", add={"rev": "220", "op": "22", "ni": "17", "own": "15"})]
        fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS", half)            # cumulative only, no Q1 yet
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        h1 = [r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 2 and r["metric"] == "revenue"]
        self.assertEqual((h1[0]["value_q"], h1[0]["derived"]), (120.0, 1))

    # ---- M9 / L3 / L4 ----
    def test_pending_is_counted_in_periods(self):
        self.sync("init")
        res = self.sync("daily")
        st = dsync.collection_status(self.db, today=dt.date(2025, 11, 26))
        self.assertEqual(res.pending_left, st["periods_pending"])
        self.assertEqual(res.pending_left, 4)               # 5 filings: Samsung's H1 and its correction are one period

    def test_old_fetch_log_rows_are_pruned(self):
        conn = self._conn()
        conn.execute("INSERT INTO fetch_log(endpoint, fetched_at) VALUES ('old', '2020-01-01T00:00:00')")
        conn.commit()
        self.sync("auto")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fetch_log WHERE endpoint = 'old'").fetchone()[0], 0)
        self.assertGreater(conn.execute("SELECT COUNT(*) FROM fetch_log").fetchone()[0], 0)

    def test_daily_until_moves_only_after_the_new_filings_are_fetched(self):
        self.sync("init")
        conn = self._conn()
        conn.execute("UPDATE sync_state SET value = '2025-11-10' WHERE key = 'daily_until'")
        conn.commit()
        stop = threading.Event()
        res = self.sync("daily", cancel=stop,
                        on_progress=lambda d, t, m: stop.set() if m.startswith("new financials") else None)
        self.assertEqual(res.status, "partial")
        self.assertEqual(dsync.get_state(conn, "daily_until"), "2025-11-10")
        self.assertEqual(self.sync("daily").status, "done")
        self.assertEqual(dsync.get_state(conn, "daily_until"), "2025-11-26")
        self.assertIn(3, {r["fiscal_q"] for r in query.get_financials("005930", conn=conn)})

class TestReviewCodex(_TmpMixin, unittest.TestCase):
    """review_codex.md (2026-11-26)."""

    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _run(self, mode, today, **kw):
        return run_sync(mode, "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                        today=today, priority=[], client_factory=self.factory(), **kw)

    # ---- P1: a correction the API does not serve yet ----
    def _add_q3_correction(self, name="[기재정정]분기보고서 (2025.09)"):
        self.fake.list_rows.append(FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", name, "20251125000030"))

    def test_correction_not_served_yet_stays_pending_then_arrives(self):
        self._add_q3_correction()
        res = self.sync("auto")                       # the API still answers with the 11-14 original
        self.assertEqual((res.status, res.pending_left), ("done", 1))
        conn = self._conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20251125000030'").fetchone())
        q3 = [r for r in query.get_financials("005930", conn=conn) if r["fiscal_q"] == 3 and r["metric"] == "revenue"]
        self.assertEqual((q3[0]["value_q"], q3[0]["rcept_no"]), (130.0, "20251114000003"))
        # DART loads the corrected statements; nothing stale was cached.
        self.fake.fin[(SAMSUNG, "2025", "11014", "CFS")] = _is_rows(
            "131", "-5", "-3", "-2", "20251125000030", add={"rev": "351", "op": "17", "ni": "14", "own": "13"})
        res = self.sync("auto")
        self.assertEqual((res.status, res.pending_left, res.periods_fetched), ("done", 0, 1))

        def rev(as_of):
            return [r["value_q"] for r in query.get_financials("005930", as_of=as_of, conn=conn)
                    if r["fiscal_q"] == 3 and r["metric"] == "revenue"]
        self.assertEqual((rev("2025-11-13"), rev("2025-11-20"), rev("2025-11-26")), ([], [130.0], [131.0]))

    def test_attachment_only_correction_is_done_with_the_original(self):
        self._add_q3_correction("[첨부정정]분기보고서 (2025.09)")
        res = self.sync("auto")
        self.assertEqual((res.status, res.pending_left), ("done", 0))

    def test_correction_not_served_after_the_wait_is_retried_not_closed(self):
        """review_codex 3 P1: past the 7-day wait a correction the API still
        answers with the original for is kept as awaiting_correction (never
        closed with the older numbers) and asked again weekly, uncached."""
        self._add_q3_correction()
        self.assertEqual(self._run("auto", dt.date(2025, 11, 26)).pending_left, 1)
        res = self._run("auto", dt.date(2025, 12, 5))                  # 10 days after the correction
        self.assertEqual((res.status, res.pending_left), ("done", 0))  # waiting, not due again before 12-12
        conn = self._conn()
        row = conn.execute("SELECT status, src_rcept_no, retry_after FROM fin_fetch "
                           "WHERE rcept_no = '20251125000030'").fetchone()
        self.assertEqual(tuple(row), ("awaiting_correction", "20251114000003", "2025-12-12"))
        self.assertTrue(conn.execute("SELECT 1 FROM fin_issue WHERE rcept_no = '20251125000030' "
                                     "AND kind = 'correction_not_served'").fetchone())
        self.assertEqual(dsync.collection_status(self.db, today=dt.date(2025, 12, 5))["periods_pending"], 1)
        n = len(self.fake.requests)
        self._run("auto", dt.date(2025, 12, 8))                        # not due: no financial request for it
        self.assertFalse([1 for ep, prm in self.fake.requests[n:]
                          if ep == "fnlttSinglAcntAll.json" and prm["reprt_code"] == "11014"])
        self.fake.fin[(SAMSUNG, "2025", "11014", "CFS")] = _is_rows(
            "131", "-5", "-3", "-2", "20251125000030", add={"rev": "351", "op": "17", "ni": "14", "own": "13"})
        res = self._run("auto", dt.date(2025, 12, 12))                 # due: asked again, not from the cache
        self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20251125000030'")
                         .fetchone()[0], "ok")
        q3 = [r["value_q"] for r in query.get_financials("005930", conn=conn)
              if r["fiscal_q"] == 3 and r["metric"] == "revenue"]
        self.assertEqual(q3, [131.0])

    def test_migration_rechecks_a_correction_closed_with_older_numbers(self):
        """Schema 8 reopens corrections an older version closed with the
        original's numbers; an attachment-only one stays done."""
        self._add_q3_correction()
        self.fake.list_rows.append(FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", "[첨부정정]분기보고서 (2025.03)",
                                                "20251125000031"))
        self._run("auto", dt.date(2025, 12, 5))
        conn = self._conn()
        conn.execute("UPDATE fin_fetch SET status = 'ok', retry_after = NULL WHERE rcept_no = '20251125000030'")
        conn.execute("UPDATE sync_state SET value = '7' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        conn = self._conn()                                            # migrates to 8
        got = dict(conn.execute("SELECT rcept_no, status FROM fin_fetch "
                                "WHERE rcept_no IN ('20251125000030', '20251125000031')").fetchall())
        self.assertEqual(got, {"20251125000030": "awaiting_correction", "20251125000031": "ok"})
        self.assertEqual(self._run("auto", dt.date(2025, 12, 6)).periods_fetched, 1)   # due at once

    # ---- P1: a Q4 resting on a later Q3 ----
    def test_q4_is_available_from_the_day_its_q3_was_received(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS", _is_rows("1000", "100", "80", "70", "20260320000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",           # the Q3 correction of 2026-04-01
                         _is_rows("200", "20", "16", "14", "20260401000002",
                                  add={"rev": "600", "op": "60", "ni": "48", "own": "42"}))

        def q4(as_of=None):
            r = [x for x in query.get_financials(SAMSUNG, as_of=as_of, conn=conn)
                 if x["fiscal_q"] == 4 and x["metric"] == "revenue"][0]
            return r["value_q"], r["value_ytd"], r["lookahead"]
        self.assertEqual(q4("2026-03-21"), (None, 1000.0, False))       # only the annual total is known
        self.assertEqual(q4("2026-04-02"), (400.0, 1000.0, True))
        self.assertEqual(q4(), (400.0, 1000.0, True))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_issue WHERE kind = 'q4_uses_later_q3'").fetchone()[0],
                         4)
        # The original Q3 turns up later: the Q4 known on the annual's day uses it; the restated one stays.
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                         _is_rows("250", "25", "20", "18", "20251114000003",
                                  add={"rev": "650", "op": "65", "ni": "52", "own": "46"}))
        self.assertEqual(q4("2026-03-21"), (350.0, 1000.0, False))
        self.assertEqual(q4("2026-04-02"), (400.0, 1000.0, True))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM fin_issue WHERE kind = 'q4_uses_later_q3'").fetchone()[0],
                         4)

    def test_q4_does_not_depend_on_arrival_order(self):
        made = []

        def load(order):
            path = os.path.join(self.dir, f"order{len(made)}.db")
            made.append(path)
            conn = connect(path)
            self.addCleanup(conn.close)
            steps = {
                1: lambda: fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS",
                                            _is_rows("1000", "100", "80", "70", "20260320000001")),
                2: lambda: fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                                            _is_rows("200", "20", "16", "14", "20260401000002",
                                                     add={"rev": "600", "op": "60", "ni": "48", "own": "42"})),
                3: lambda: fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                                            _is_rows("250", "25", "20", "18", "20251114000003",
                                                     add={"rev": "650", "op": "65", "ni": "52", "own": "46"})),
            }
            for k in order:
                steps[k]()
            return [(day, r["fiscal_q"], r["metric"], r["value_q"], r["rcept_no"])
                    for day in ("2026-03-25", "2026-04-02", None)
                    for r in query.get_financials(SAMSUNG, as_of=day, conn=conn)]
        self.assertEqual(load((1, 2, 3)), load((3, 2, 1)))
        self.assertEqual(load((1, 2, 3)), load((2, 3, 1)))

    # ---- P1: reporting currency changes ----
    def test_amounts_of_different_currencies_are_not_combined(self):
        conn = self._conn()
        krw = _is_rows("700", "70", "56", "49", "20251114000003", add={"rev": "700", "op": "70", "ni": "56", "own": "49"})
        usd = [dict(r, currency="USD") for r in _is_rows("1000", "100", "80", "70", "20260320000001")]
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS", krw)
        fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS", usd)
        fin.store_period(conn, SAMSUNG, 2024, "11011", "CFS", _is_rows("900", "90", "72", "63", "20250320000001"))
        fin.store_period(conn, SAMSUNG, 2024, "11014", "CFS",
                         _is_rows("300", "30", "24", "21", "20241114000001",
                                  add={"rev": "600", "op": "60", "ni": "48", "own": "42"}))
        rows = {(r["fiscal_year"], r["fiscal_q"]): r for r in query.get_financials(SAMSUNG, conn=conn)
                if r["metric"] == "revenue"}
        self.assertEqual(rows[(2025, 4)]["value_q"], None)               # was 300 "USD" (1000 USD - 700 KRW)
        self.assertEqual((rows[(2025, 4)]["currency"], rows[(2025, 4)]["currency_changed"]), ("USD", True))
        self.assertEqual(rows[(2024, 4)]["value_q"], 300.0)
        view = conn.execute("SELECT yoy, currency_changed FROM v_fin_growth WHERE fiscal_year = 2025 "
                            "AND fiscal_q = 4 AND metric = 'revenue'").fetchone()
        self.assertEqual(tuple(view), (None, 1))
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES (?, '005930', 'x')", (SAMSUNG,))
        conn.commit()
        lines = query.format_growth_table("005930", conn=conn).splitlines()
        self.assertIn("백만 USD", lines[0])
        krw_line = next(x for x in lines if x.startswith("2024Q4"))
        self.assertIn("단위 억원", krw_line)                              # not shown in the dollar unit
        self.assertIn("통화 변경", next(x for x in lines if x.startswith("2025Q4")))

    # ---- P2: daily runs reach a report answered 013 for a few days ----
    def _bank_q3(self):
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "분기보고서 (2025.09)",
                                                "20251120000040"))

    def test_daily_runs_retry_a_recent_013_until_it_is_served(self):
        self._run("init", dt.date(2025, 11, 19))
        self._bank_q3()
        for day in (21, 22, 23, 24):
            self.assertEqual(self._run("daily", dt.date(2025, 11, day)).status, "done")
        conn = self._conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20251120000040'").fetchone())
        self.fake.fin[(BANK, "2025", "11014", "CFS")] = _is_rows(
            "9", "3", "2", "2", "20251120000040", add={"rev": "30", "op": "9", "ni": "6", "own": "6"})
        self._run("daily", dt.date(2025, 11, 25))                       # the list window starts 11-21, after the filing
        self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20251120000040'").fetchone()[0],
                         "ok")

    def test_daily_runs_fall_back_to_the_document_after_the_wait(self):
        self._run("init", dt.date(2025, 11, 19))
        self._bank_q3()
        self.fake.documents["20251120000040"] = _doc()
        for day in (21, 22, 23, 24, 25, 26, 27):
            self._run("daily", dt.date(2025, 11, day))
        conn = self._conn()
        self.assertIsNone(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20251120000040'").fetchone())
        self.assertEqual([p for ep, p in self.fake.requests if ep == "document.xml"], [])
        self._run("daily", dt.date(2025, 11, 28))                       # 8 days after the receipt
        self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20251120000040'").fetchone()[0],
                         "document")

    # ---- P2: --daily --tickers ----
    def test_daily_with_tickers_fetches_their_financials(self):
        res = self.sync("daily", tickers=["123450"])
        self.assertEqual((res.status, res.periods_fetched, res.pending_left), ("done", 1, 0))
        conn = self._conn()
        self.assertTrue(query.get_financials("123450", conn=conn))
        self.assertEqual(query.get_financials("005930", conn=conn), [])
        self.assertEqual(self.sync("init", tickers=["123450"]).periods_fetched, 0)      # lists only

    # ---- P2: a reader that must not migrate someone's DB ----
    def test_readonly_without_migration_leaves_an_old_db_untouched(self):
        conn = sqlite3.connect(self.db)
        conn.executescript(store._SCHEMA)
        conn.execute("INSERT INTO sync_state(key, value) VALUES ('schema_version', '2')")
        conn.execute("INSERT INTO fin_fetch(rcept_no, status) VALUES ('r1', 'unmapped')")
        conn.commit()
        conn.close()
        ro = store.open_readonly(self.db, migrate=False)
        self.assertFalse(store.schema_is_current(ro))
        ro.close()
        check = sqlite3.connect(self.db)
        self.addCleanup(check.close)
        self.assertEqual(check.execute("SELECT value FROM sync_state WHERE key = 'schema_version'").fetchone()[0], "2")
        self.assertEqual(check.execute("SELECT COUNT(*) FROM fin_fetch").fetchone()[0], 1)   # v3 would delete it

class TestReviewCodex2(_TmpMixin, unittest.TestCase):
    """review_codex.md, second round (2026-11-26, at commit 3a0a741)."""

    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    def _bank_half(self):
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "반기보고서 (2025.06)",
                                                "20250814000020"))

    # ---- P1: a document failure is not "no data" ----
    def test_document_error_keeps_the_period_pending_until_it_can_be_read(self):
        for failure in ("network", "800"):
            with self.subTest(failure=failure):
                self.setUp()
                self._bank_half()
                self.fake.documents["20250814000020"] = _doc()

                def hook(ep, params, failure=failure):
                    if ep == "document.xml":
                        if failure == "network":
                            raise requests.ConnectionError("down")
                        return FakeResp({"status": "800", "message": "maintenance"})
                self.fake.hook = hook
                res = self.sync("backfill", tickers=["105560"])
                conn = connect(self.db)
                self.assertIsNone(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20250814000020'").fetchone())
                self.assertEqual(res.pending_left, 1)
                self.assertEqual(res.status, "failed" if failure == "800" else "partial")   # 800 stops the run
                self.fake.hook = None
                res = self.sync("backfill", tickers=["105560"])                          # no cache to clear by hand
                self.assertEqual((res.status, res.pending_left), ("done", 0))
                self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20250814000020'")
                                 .fetchone()[0], "document")
                conn.close()

    def test_no_file_answer_is_still_remembered(self):
        self._bank_half()                              # no document registered: the fake answers 014
        self.sync("backfill", tickers=["105560"])
        conn = self._conn()
        self.assertEqual(conn.execute("SELECT status FROM fin_fetch WHERE rcept_no = '20250814000020'").fetchone()[0],
                         "no_data")
        n = len(self.fake.requests)
        conn.execute("DELETE FROM fin_fetch")
        conn.commit()
        self.sync("backfill", tickers=["105560"])
        self.assertEqual([ep for ep, _ in self.fake.requests[n:] if ep == "document.xml"], [])

    # ---- P1: a second Q3 correction must not rewrite an earlier as-of answer ----
    def test_each_q3_version_keeps_its_own_q4(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS", _is_rows("1000", "100", "80", "70", "20260320000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                         _is_rows("200", "20", "16", "14", "20260401000002",
                                  add={"rev": "600", "op": "60", "ni": "48", "own": "42"}))

        def q4(as_of):
            r = [x for x in query.get_financials(SAMSUNG, as_of=as_of, conn=conn)
                 if x["fiscal_q"] == 4 and x["metric"] == "revenue"][0]
            return r["value_q"]
        self.assertEqual((q4("2026-03-21"), q4("2026-04-02")), (None, 400.0))
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",           # a second correction, 2026-05-01
                         _is_rows("250", "25", "20", "18", "20260501000003",
                                  add={"rev": "650", "op": "65", "ni": "52", "own": "46"}))
        self.assertEqual((q4("2026-03-21"), q4("2026-04-02"), q4("2026-05-02"), q4(None)),
                         (None, 400.0, 350.0, 350.0))             # 04-02 used to turn into None
        view = conn.execute("SELECT value_q FROM v_fin_growth WHERE fiscal_year = 2025 AND fiscal_q = 4 "
                            "AND metric = 'revenue'").fetchall()
        self.assertEqual([r[0] for r in view], [350.0])

    def test_a_corrected_annual_report_restates_q4_too(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                         _is_rows("200", "20", "16", "14", "20251114000001",
                                  add={"rev": "600", "op": "60", "ni": "48", "own": "42"}))
        fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS", _is_rows("1000", "100", "80", "70", "20260320000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11014", "CFS",
                         _is_rows("200", "20", "16", "14", "20260401000002",
                                  add={"rev": "620", "op": "60", "ni": "48", "own": "42"}))
        fin.store_period(conn, SAMSUNG, 2025, "11011", "CFS", _is_rows("1010", "100", "80", "70", "20260601000003"))
        got = [(r["rcept_no"], r["value_q"], r["derived"]) for r in conn.execute(
            "SELECT rcept_no, value_q, derived FROM fin_std WHERE fiscal_q = 4 AND metric = 'revenue' ORDER BY rcept_no")]
        self.assertEqual(got, [("20260320000001", 400.0, 1),     # annual - the Q3 known that day
                               ("20260401000002", 380.0, 2),     # the corrected Q3 against that annual
                               ("20260601000003", 390.0, 1)])    # the corrected annual - the newest Q3

    # ---- P1: segments wait for the correction's own revenue ----
    def test_segments_wait_for_a_corrections_financials(self):
        fake = self.fake
        fake.list_rows.append(FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", "[기재정정]분기보고서 (2025.09)",
                                           "20251125000030"))
        table = ("<P>(단위 : 원)</P><TABLE><TR><TH>구분</TH><TH>매출액</TH></TR>"
                 "<TR><TD>가부문</TD><TD>100</TD></TR><TR><TD>나부문</TD><TD>51</TD></TR>"
                 "<TR><TD>합계</TD><TD>151</TD></TR></TABLE>")
        fake.documents["20251125000030"] = ('<DOCUMENT><TITLE>3. 연결재무제표 주석</TITLE><TITLE>4. 영업부문(연결)</TITLE>'
                                            + table + "<TITLE>4. 재무제표</TITLE></DOCUMENT>")
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory(),
                       tickers=["005930"])
        self.assertEqual(res.status, "done", res.error)
        conn = self._conn()
        # The API still serves the original (revenue 130): the correction's segments are not judged yet.
        self.assertIsNone(conn.execute("SELECT 1 FROM seg_fetch WHERE rcept_no = '20251125000030'").fetchone())
        fake.fin[(SAMSUNG, "2025", "11014", "CFS")] = _is_rows(
            "151", "-5", "-3", "-2", "20251125000030", add={"rev": "371", "op": "17", "ni": "14", "own": "13"})
        run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                 today=dt.date(2025, 11, 26), client_factory=self.factory(), tickers=["005930"])
        self.assertEqual(tuple(conn.execute("SELECT status, source FROM seg_fetch WHERE rcept_no = '20251125000030'")
                               .fetchone()), ("ok", "html_segment"))

    # ---- P2: amounts and status of the same receipt ----
    def test_segments_of_a_correction_without_a_table_do_not_show_the_original(self):
        conn = self._conn()
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES (?, '005930', 'x')", (SAMSUNG,))
        for rc, day in (("20250814000001", "2025-08-14"), ("20250901000002", "2025-09-01")):
            conn.execute("INSERT INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt) VALUES "
                         "(?, ?, '11012', 2025, ?)", (rc, SAMSUNG, day))
        c = seg.Candidate("html_segment", [("가", 60.0), ("나", 40.0)])
        seg.store_segments(conn, SAMSUNG, 2025, 2, "20250814000001", "2025-08-14", c, "q", 0.0, "ok", "html_segment")
        seg.record_fetch(conn, "20250814000001", "ok", "html_segment")
        seg.record_fetch(conn, "20250901000002", "review", None)          # the correction: no table found
        conn.commit()
        self.assertEqual(len(query.get_segments("005930", as_of="2025-08-20", conn=conn)), 2)
        self.assertEqual(query.get_segments("005930", as_of="2025-09-01", conn=conn), [])
        self.assertEqual(query.get_segments("005930", conn=conn), [])
        per = query.segment_periods("005930", conn=conn)[(2025, 2)]
        self.assertEqual((per["rcept_no"], per["status"]), ("20250901000002", "review"))
        data = query.company_financials("005930", conn=conn)
        self.assertEqual((data["segments"], data["segment_status"][(2025, 2)]), ([], ("review", None)))

    # ---- P2: a re-classified filing loses its segment data ----
    def test_reclassify_drops_segment_rows_and_marks(self):
        conn = self._conn()
        lr = FakeDart._lr
        q3 = lr(MARCH, "067010", "x", "K", "분기보고서 (2020.12)", "20210215000001")
        fil.store_filings(conn, [q3], 2021)
        c = seg.Candidate("html_segment", [("가", 60.0), ("나", 40.0)])
        seg.store_segments(conn, MARCH, 2020, 3, "20210215000001", "2021-02-15", c, "q", 0.0, "ok", "html_segment")
        seg.record_fetch(conn, "20210215000001", "ok", "html_segment")
        fil.store_filings(conn, [lr(MARCH, "067010", "x", "K", "사업보고서 (2021.03)", "20210610000002")], 2021)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM segment_rev").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)

# 2025 quarterly revenue / operating income (KRW, consolidated) of the plan-7
# verification stocks, checked on 2026-11-26 against each report's own income
# statement table (document.xml; Q4 = annual - Q3 cumulative of those tables).
EXPECTED_2025 = {
    "000660": {"revenue": [17639141000000, 22231952000000, 24448929000000, 32826653000000],
               "op_income": [7440504000000, 9212851000000, 11383390000000, 19169574000000]},
    "005380": {"revenue": [44407761000000, 48286677000000, 46721448000000, 46838586000000],
               "op_income": [3633609000000, 3601597000000, 2537266000000, 1695379000000]},
    "005930": {"revenue": [79140503000000, 74566317000000, 86061747000000, 93837371000000],
               "op_income": [6685272000000, 4676057000000, 12166062000000, 20073660000000]},
    "036930": {"revenue": [120846474023, 78792213478, 58773454725, 52280442586],
               "op_income": [33907650658, 6575447323, 3355821494, -12581107733]},
    "043260": {"revenue": [59265858353, 56551289696, 55603044857, 60156842864],
               "op_income": [1599388705, 1322860055, 539736800, 4089437148]},
}


class TestVerificationStocks(unittest.TestCase):
    """Plan 8 regression (optional): the collected dart.db against EXPECTED_2025.
    Runs only with a DART key and a dart.db holding these stocks (fill it with
    `tools/dart_sync.py --backfill --tickers 005930 000660 005380 043260 036930`);
    reads the DB only, through a read-only connection: no API calls, nothing
    written, and it works on a read-only copy (review_agy tests #1)."""

    def test_2025_quarters_match_report_tables(self):
        from data.dart.client import api_key_from_env
        if not api_key_from_env() or not os.path.exists(REAL_DART_DB):
            self.skipTest("no DART key or dart.db")
        conn = store.open_readonly(REAL_DART_DB, migrate=False)     # the user's data: never migrated by a test
        self.addCleanup(conn.close)
        if not store.schema_is_current(conn):
            self.skipTest("dart.db is at an older schema; run the collector once to migrate it")
        for code, metrics in EXPECTED_2025.items():
            rows = query.get_financials(code, conn=conn)
            if not rows:
                self.skipTest(f"{code} not collected yet")
            got = {(r["metric"], r["fiscal_q"]): r["value_q"] for r in rows if r["fiscal_year"] == 2025}
            for metric, values in metrics.items():
                for q, want in enumerate(values, start=1):
                    with self.subTest(code=code, metric=metric, q=q):
                        self.assertEqual(got.get((metric, q)), want)


if __name__ == "__main__":
    unittest.main()


class TestReviewCodex3(_TmpMixin, unittest.TestCase):
    """review_codex.md (2026-11-26, third review): P1 2, P2 3-5 (P1 1 is in TestReviewCodex)."""

    def _conn(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        return conn

    # ---- P1: a re-parsed receipt keeps no metric it no longer has ----
    def test_reparsed_receipt_drops_a_metric_it_no_longer_has(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS",
                         _is_rows("120", "12", "9", "8", "20250814000002",
                                  add={"rev": "220", "op": "22", "ni": "17", "own": "15"}))
        without_rev = [r for r in _is_rows("", "12", "9", "8", "20250814000002",
                                           add={"rev": "", "op": "22", "ni": "17", "own": "15"})
                       if r["account_id"] != "ifrs-full_Revenue"]
        _src, missing = fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS", without_rev)
        self.assertEqual(missing, ["revenue"])
        h1 = {r["metric"]: r for r in query.get_financials(SAMSUNG, conn=conn) if r["fiscal_q"] == 2}
        self.assertNotIn("revenue", h1)                       # no stale 120 next to the unmapped line
        self.assertEqual(h1["op_income"]["value_q"], 12.0)
        self.assertTrue(conn.execute("SELECT 1 FROM fin_unmapped WHERE rcept_no = '20250814000002' "
                                     "AND metric = 'revenue'").fetchone())

    def test_reparsed_q1_recomputes_the_quarter_derived_from_it(self):
        conn = self._conn()
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250515000001"))
        h1 = _is_rows("", "", "", "", "20250814000002", add={"rev": "220", "op": "22", "ni": "17", "own": "15"})
        fin.store_period(conn, SAMSUNG, 2025, "11012", "CFS", h1)         # only cumulatives: Q2 = 220 - 100
        rev_q2 = conn.execute("SELECT value_q FROM fin_std WHERE fiscal_q = 2 AND metric = 'revenue'").fetchone()[0]
        self.assertEqual(rev_q2, 120.0)
        q1 = [r for r in _is_rows("100", "10", "8", "7", "20250515000001") if r["account_id"] != "ifrs-full_Revenue"]
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", q1)         # Q1 revenue gone
        rev_q2 = conn.execute("SELECT value_q FROM fin_std WHERE fiscal_q = 2 AND metric = 'revenue'").fetchone()[0]
        self.assertIsNone(rev_q2)

    # ---- P2: request failures are reported ----
    def test_failed_segment_requests_make_the_run_partial(self):
        self.sync("auto")

        def hook(ep, params):
            if ep == "fnlttXbrl.xml":
                raise requests.ConnectionError("down")
        self.fake.hook = hook
        res = self.sync("segments", tickers=["005930"])
        self.assertEqual(res.status, "partial")
        self.assertGreater(res.segments_failed, 0)

    def test_a_report_dart_has_not_loaded_is_a_wait_not_a_failure(self):
        self.fake.list_rows.append(FakeDart._lr(BANK, "105560", "은행지주", "Y", "분기보고서 (2025.09)",
                                                "20251120000040"))
        res = self.sync("auto")                               # 013 for a 6-day-old report
        self.assertEqual((res.status, res.periods_failed), ("done", 0))
        self.assertGreater(res.pending_left, 0)

    # ---- P2: a stub filing is completed by the list ----
    def test_stub_filing_gets_its_name_and_correction_flag_from_the_list(self):
        conn = self._conn()
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name, fiscal_month) VALUES (?, '005930', 'x', 12)",
                     (SAMSUNG,))
        fil.ensure_filing(conn, "20250901000004", SAMSUNG, 2025, "11012")
        row = conn.execute("SELECT report_nm, is_correction FROM filing WHERE rcept_no = '20250901000004'").fetchone()
        self.assertEqual(tuple(row), (None, 0))
        fil.store_filings(conn, [FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", "[기재정정]반기보고서 (2025.06)",
                                              "20250901000004")], 2021)
        row = conn.execute("SELECT report_nm, is_correction, bsns_year, reprt_code FROM filing "
                           "WHERE rcept_no = '20250901000004'").fetchone()
        self.assertEqual(tuple(row), ("[기재정정]반기보고서 (2025.06)", 1, 2025, "11012"))

    def test_stub_with_the_wrong_period_drops_what_was_stored_under_it(self):
        conn = self._conn()
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name, fiscal_month) VALUES (?, '005930', 'x', 12)",
                     (SAMSUNG,))
        fin.store_period(conn, SAMSUNG, 2025, "11013", "CFS", _is_rows("100", "10", "8", "7", "20250814000077"))
        conn.execute("INSERT INTO fin_fetch(rcept_no, status, src_rcept_no) VALUES "
                     "('20250814000077', 'ok', '20250814000077')")
        fil.store_filings(conn, [FakeDart._lr(SAMSUNG, "005930", "삼성전자", "Y", "반기보고서 (2025.06)",
                                              "20250814000077")], 2021)
        self.assertEqual(tuple(conn.execute("SELECT bsns_year, reprt_code FROM filing "
                                            "WHERE rcept_no = '20250814000077'").fetchone()), (2025, "11012"))
        self.assertFalse(conn.execute("SELECT 1 FROM fin_std WHERE rcept_no = '20250814000077'").fetchone())
        self.assertFalse(conn.execute("SELECT 1 FROM fin_fetch WHERE rcept_no = '20250814000077'").fetchone())

    # ---- P2: a ticker run refreshes an old company list ----
    def test_ticker_run_refreshes_a_stale_company_list(self):
        self.sync("init")
        conn = self._conn()
        conn.execute("UPDATE sync_state SET value = '2025-08-01' WHERE key = 'corp_list_at'")
        conn.commit()
        n = len(self.fake.requests)
        res = self.sync("backfill", tickers=["123450", "999990"])
        self.assertEqual([ep for ep, _ in self.fake.requests[n:]].count("corpCode.xml"), 1)
        self.assertEqual(res.unknown_tickers, ("999990",))
        n = len(self.fake.requests)
        self.sync("backfill", tickers=["123450"])                   # fresh now: not asked again
        self.assertEqual([ep for ep, _ in self.fake.requests[n:]].count("corpCode.xml"), 0)


class TestUniverseOrder(_TmpMixin, unittest.TestCase):
    """User direction 2026-11-26: the auto backfill goes stock by stock in the
    Trading Universe's order -- a stock's financials, then its segments."""

    def test_stock_by_stock_in_the_given_order(self):
        res = run_sync("auto", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                       today=dt.date(2025, 11, 26), priority=["123450", "005930"], client_factory=self.factory())
        self.assertEqual(res.status, "done", res.error)
        steps = []
        for ep, prm in self.fake.requests:
            if ep == "fnlttSinglAcntAll.json":
                steps.append(("fin", prm["corp_code"]))
            elif ep == "fnlttXbrl.xml":
                steps.append(("seg", prm["rcept_no"][:8]))
        first_samsung = steps.index(("fin", SAMSUNG))
        self.assertTrue(all(s == ("fin", SMALL) for s in steps[:1]))
        self.assertIn(("seg", "20250515"), steps[:first_samsung])          # the small company's segments first
        self.assertTrue(all(s[0] != "fin" or s[1] == SAMSUNG for s in steps[first_samsung:]))

    def test_universe_order_is_the_tables(self):
        rows = [{"market": "Index", "ticker": "^KS11"},
                {"market": "KOSPI", "ticker": "5930", "market_cap": 5},
                {"market": "KOSPI", "ticker": "660", "market_cap": 9},
                {"market": "KOSDAQ", "ticker": "247540", "market_cap": 99},
                {"market": "KOSPI", "ticker": "5930", "market_cap": 5}]
        with open(paths.UNIVERSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        self.assertEqual(dsync.universe_priority(), ["005930", "000660", "247540"])
