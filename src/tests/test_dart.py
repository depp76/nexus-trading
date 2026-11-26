"""DART financial DB (docs/dart_db_plan_2026-11-26.md 8): parsing, quarter
maths, corrections, as-of queries, the client's limits and cache, and run_sync's
lock / cancel / budget / no-key behaviour -- all on a fake OpenDART."""
import datetime as dt
import io
import json
import os
import tempfile
import threading
import unittest
import zipfile

from data.dart import filings as fil
from data.dart import financials as fin
from data.dart import query
from data.dart.client import DartBudgetExceeded, DartClient, calls_today
from data.dart.lock import SyncLock
from data.dart.store import connect
from data.dart.sync import run_sync

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

    @staticmethod
    def _lr(corp, stock, name, cls, report, rcept):
        return {"corp_code": corp, "stock_code": stock, "corp_name": name, "corp_cls": cls,
                "report_nm": report, "rcept_no": rcept, "rcept_dt": rcept[:8]}

    def corp_zip(self):
        xml = ("<result><list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>"
               "<stock_code>005930</stock_code></list><list><corp_code>00999999</corp_code>"
               "<corp_name>작은회사</corp_name><stock_code>123450</stock_code></list>"
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
        res = self.sync("auto")
        self.assertEqual(res.status, "done", res.error)
        conn = connect(self.db)
        self.addCleanup(conn.close)
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
        done_before = conn.execute("SELECT COUNT(*) FROM fin_fetch").fetchone()[0]
        self.assertGreater(done_before, 0)               # the period in flight was committed
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
    reads the DB only, no API calls."""

    def test_2025_quarters_match_report_tables(self):
        import paths
        from data.dart.client import api_key_from_env
        if not api_key_from_env() or not os.path.exists(paths.DART_DB_FILE):
            self.skipTest("no DART key or dart.db")
        conn = connect(paths.DART_DB_FILE)
        self.addCleanup(conn.close)
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
