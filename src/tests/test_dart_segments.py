"""Revenue by business segment (docs/dart_db_plan_2026-11-26.md 5, stage 4):
names, the XBRL reader, the table reader in its layouts, the 5-3 check, and a
collection run on a fake OpenDART."""
import datetime as dt
import io
import os
import zipfile
import unittest

from data.dart import query
from data.dart import segments as seg
from data.dart.store import connect
from data.dart.sync import run_sync
from tests.test_dart import SAMSUNG, SMALL, FakeDart, FakeResp, _TmpMixin

E = 1e6       # tables below are in 백만원


def _table(rows, head):
    """A <TABLE> from header rows (cells: text or (text, colspan, rowspan)) and body rows."""
    def cell(c, tag):
        if isinstance(c, tuple):
            text, cs, rs = (list(c) + [1, 1])[:3]
            return f'<{tag} COLSPAN="{cs}" ROWSPAN="{rs}">{text}</{tag}>'
        return f"<{tag}>{c}</{tag}>"
    h = "".join("<TR>" + "".join(cell(c, "TH") for c in r) + "</TR>" for r in head)
    b = "".join("<TR>" + "".join(cell(c, "TD") for c in r) + "</TR>" for r in rows)
    return f"<TABLE>{h}{b}</TABLE>"


class TestNames(unittest.TestCase):
    def test_normalise(self):
        for raw, code in (("CE부문", "CE"), ("DX 부문", "DX"), ("반도체사업부", "반도체"), ("기타, 영업부문", "기타"),
                          ("차량(*1)", "차량"), ("Harman", "Harman"), ("1. 담배사업부문", "담배")):
            self.assertEqual(seg.normalise(raw), code, raw)

    def test_totals(self):
        for t in ("합계", "계(*)", "합계합계", "제품과용역합계", "기업 전체 총계", "소계"):
            self.assertTrue(seg.is_total(t), t)
        self.assertFalse(seg.is_total("기타"))


class TestTables(unittest.TestCase):
    def _choose(self, table, rq, ry, product=False, unit=E):
        cands = seg.table_candidates(table, unit, product)
        return seg.choose(cands, rq, ry)

    def test_segments_in_rows_with_external_column(self):
        t = _table([["담배", "3,471", "(339)", "3,132"], ["인삼", "1,520", "(146)", "1,374"],
                    ["연결조정", "(485)", "485", "-"], ["합계", "4,506", "-", "4,506"]],
                   [["구분", "총부문수익", "부문간수익", "외부고객으로부터의수익"]])
        c, basis, residual, status = self._choose(t, None, 4506 * E)
        self.assertEqual((status, basis, residual), ("ok", "ytd", 0.0))
        self.assertEqual(c.items, [("담배", 3132 * E), ("인삼", 1374 * E)])          # the external column

    def test_segments_in_columns_with_a_parent_total(self):
        """Samsung 2021: DS shown as "계 | 반도체 | DP"; the table total is not
        the plain sum (segments gross) -- the difference is a residual."""
        head = [[("구분", 1, 2), ("CE부문", 1, 2), ("IM부문", 1, 2), ("DS부문", 3), ("Harman부문", 1, 2), ("계(*)", 1, 2)],
                ["계", "반도체", "DP"]]
        t = _table([["매출액", "55,832", "109,251", "125,089", "94,158", "31,712", "10,039", "279,604"],
                    ["영업이익", "3,645", "13,647", "33,734", "29,199", "4,457", "599", "51,633"]], head)
        c, basis, residual, status = self._choose(t, None, 279604 * E)
        self.assertEqual(status, "ok")
        self.assertEqual([n for n, _ in c.items], ["CE부문", "IM부문", "DS부문", "Harman부문"])
        self.assertEqual(dict(c.items)["DS부문"], 125089 * E)                    # its own total, not its parts
        self.assertAlmostEqual(residual, (279604 - 55832 - 109251 - 125089 - 10039) * E)
        self.assertEqual(c.ops["DS부문"], 33734 * E)

    def test_xbrl_styled_header_with_periods(self):
        """주성엔지니어링 2025 Q3: axis label, members, then 3개월 | 누적."""
        head = [["", ("제품과용역", 6), ("제품과용역합계", 2)],
                ["", ("반도체장비", 2), ("디스플레이장비", 2), ("Solarcell장비", 2)],
                ["", "3개월", "누적", "3개월", "누적", "3개월", "누적", "3개월", "누적"]]
        t = _table([["수익(매출액)", "55,608", "252,814", "3,164", "5,592", "1", "5", "58,773", "258,411"]], head)
        c, basis, _r, status = self._choose(t, 58773 * E, 258411 * E, product=True)
        self.assertEqual((status, basis), ("ok", "q"))
        self.assertEqual(c.items, [("반도체장비", 55608 * E), ("디스플레이장비", 3164 * E), ("Solarcell장비", 1 * E)])

    def test_axis_total_on_top_and_elimination_columns(self):
        """Hyundai 2023: "합계" is the axis caption above everything, and the
        "부문간 제거" columns are eliminations, not segments."""
        head = [["", ("합계", 8), "합계합계"],
                ["", ("영업부문", 4), ("부문간제거한금액", 4), ""],
                ["", ("구분", 3), "구분합계", ("구분", 3), "구분합계", ""],
                ["", "차량", "금융", "기타", "", "차량", "금융", "기타", "", ""]]
        t = _table([["순매출액", "130,149", "22,401", "10,112", "", "", "", "", "", ""],
                    ["매출액", "212,367", "22,688", "11,985", "", "", "", "", "(84,378)", ""]], head)
        c, _b, _r, status = self._choose(t, None, 162662 * E)
        self.assertEqual(status, "ok")
        self.assertEqual([n for n, _ in c.items], ["차량", "금융", "기타"])
        gross = seg.table_candidates(t, E, False)
        g = next(x for x in gross if x.label.startswith("row 매출액"))
        self.assertEqual(g.adjust, [("부문간 제거", -84378 * E)])

    def test_year_headers_are_periods_and_unit_caption_inside_the_table(self):
        """LG전자 2021 / KT&G 2021: "2021년" over the members; "(단위:백만원)" as the first row."""
        head = [[("(단위:백만원)", 6)], ["구분", ("2021년", 5)], ["", "H&A", "HE", "VS", "내부거래(*2)", "합계"]]
        t = _table([["외부고객매출", "27,077", "17,209", "7,193", "-", "51,479"]], head)
        c, _b, _r, status = self._choose(t, None, 51479 * E, unit=1.0)
        self.assertEqual(status, "ok")
        self.assertEqual([n for n, _ in c.items], ["H&A", "HE", "VS"])

    def test_check_rules(self):
        one = seg.Candidate("html_segment", [("수익", 100.0)])
        self.assertEqual(seg.choose([one], None, 100.0)[3], "review")             # one row tells nothing
        geo = seg.Candidate("html_segment", [("국내", 60.0), ("해외", 40.0)])
        ops = seg.Candidate("html_segment", [("차량", 70.0), ("금융", 30.0)])
        self.assertIs(seg.choose([geo, ops], None, 100.0)[0], ops)                # segments before regions
        self.assertIs(seg.choose([geo], None, 100.0)[0], geo)                     # regions when that is all
        off = seg.Candidate("html_segment", [("A", 50.0), ("B", 30.0)])
        self.assertEqual(seg.choose([off], None, 100.0)[3], "sum_mismatch")
        self.assertEqual(seg.choose([seg.Candidate("html_segment", [("A", 50.0), ("B", 49.5)])], None, 100.0)[3],
                         "ok")                                                    # within 1 %
        # A table total reached only through a large residual is a misread, not a reconciliation.
        big = seg.Candidate("html_segment", [("A", 57.0), ("B", 14.0), ("외부고객으로부터의수익", 59.0)], total=59.0)
        self.assertEqual(seg.choose([big], None, 59.0)[3], "sum_mismatch")
        small = seg.Candidate("html_segment", [("A", 55.0), ("B", 50.0)], total=100.0)
        self.assertEqual(seg.choose([small], None, 100.0)[:4][2:], (-5.0, "ok"))


def _doc(note: str, titled=True) -> str:
    head = '<TITLE ATOC="Y">3. 연결재무제표 주석</TITLE>'
    note_head = ('<TITLE ATOC="Y">4. 영업부문(연결)</TITLE>' if titled
                 else '<P><SPAN>26</SPAN><SPAN>. </SPAN><SPAN>부문별 정보</SPAN></P>')
    return f"<DOCUMENT>{head}<P>1. 회사의 개요</P>{note_head}{note}<TITLE>4. 재무제표</TITLE></DOCUMENT>"


class TestRegions(unittest.TestCase):
    NOTE = ("<P>(단위 : 백만원)</P>" + _table([["가부문", "60"], ["나부문", "40"], ["합계", "100"]],
                                             [["구분", "매출액"]]))

    def test_titled_and_split_span_headings(self):
        for titled in (True, False):
            cands, single = seg.document_candidates(seg.segment_region(_doc(self.NOTE, titled), True))
            c, _b, _r, status = seg.choose(cands, None, 100 * E)
            self.assertEqual((status, [n for n, _ in c.items]), ("ok", ["가부문", "나부문"]), titled)

    def test_single_segment_statement(self):
        doc = _doc("<P>연결회사는 반도체 제조 및 판매의 단일 영업부문으로 구성되어 있습니다.</P>")
        _c, single = seg.document_candidates(seg.segment_region(doc, True))
        self.assertTrue(single)


def _xbrl_zip(members, adjust=None, family="DisclosureOfOperatingSegmentsTableOf", end="2025-09-30",
              start="2025-07-01") -> bytes:
    """An XBRL package: Revenue facts for entity members (with Korean labels) and an elimination."""
    ctx, facts, locs = [], [], []
    for i, (mid, label, value) in enumerate(members):
        member = f"entity00126380:{mid}MemberOf{family}Member"
        ctx.append(f'<xbrli:context id="c{i}"><xbrli:entity/><xbrli:segment>'
                   '<xbrldi:explicitMember dimension="ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis">'
                   'ifrs-full:ConsolidatedMember</xbrldi:explicitMember>'
                   f'<xbrldi:explicitMember dimension="ifrs-full:SegmentsAxis">{member}</xbrldi:explicitMember>'
                   f'</xbrli:segment><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>'
                   f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')
        facts.append(f'<ifrs-full:Revenue contextRef="c{i}" unitRef="KRW" decimals="-6">{value}</ifrs-full:Revenue>')
        eid = member.replace(":", "_")
        locs.append(f'<link:loc xlink:label="loc{i}" xlink:href="x.xsd#{eid}" xlink:type="locator"/>'
                    f'<link:labelArc xlink:from="loc{i}" xlink:to="lab{i}"/>'
                    f'<link:label xml:lang="ko" xlink:label="lab{i}" xlink:role="http://www.xbrl.org/2003/role/label">'
                    f'{label} [구성요소]</link:label>')
    if adjust is not None:
        ctx.append('<xbrli:context id="adj"><xbrli:entity/><xbrli:segment><xbrldi:explicitMember '
                   'dimension="ifrs-full:SegmentConsolidationItemsAxis">ifrs-full:EliminationOfIntersegmentAmountsMember'
                   f'</xbrldi:explicitMember></xbrli:segment><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>'
                   f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')
        facts.append(f'<ifrs-full:Revenue contextRef="adj" unitRef="KRW">{adjust}</ifrs-full:Revenue>')
    inst = "<xbrli:xbrl>" + "".join(ctx) + "".join(facts) + "</xbrli:xbrl>"
    lab = "<link:linkbase><link:labelLink>" + "".join(locs) + "</link:labelLink></link:linkbase>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("entity00126380_2025-09-30.xbrl", inst)
        zf.writestr("entity00126380_2025-09-30_lab-ko.xml", lab)
    return buf.getvalue()


class TestXbrl(unittest.TestCase):
    def test_members_labels_and_elimination(self):
        z = _xbrl_zip([("DxDivision", "DX 부문", "60"), ("DsDivision", "DS 부문", "50")], adjust="-10")
        c, basis, _r, status = seg.choose(seg.xbrl_candidates(z, "CFS"), 100.0, 300.0)
        self.assertEqual((status, basis), ("ok", "q"))
        self.assertEqual(c.items, [("DX 부문", 60.0), ("DS 부문", 50.0)])
        self.assertEqual(c.adjust, [("부문간 내부거래 제거", -10.0)])

    def test_separate_member_tables_are_not_merged(self):
        """Hyundai 2026: a gross and a net revenue table with the same member names."""
        gross = _xbrl_zip([("Vehicle", "차량", "80"), ("Finance", "금융", "30")], family="GrossTableOf")
        net = _xbrl_zip([("Vehicle", "차량", "70"), ("Finance", "금융", "30")], family="NetTableOf")
        cands = seg.xbrl_candidates(gross, "CFS") + seg.xbrl_candidates(net, "CFS")
        c, _b, _r, status = seg.choose(cands, 100.0, None)
        self.assertEqual((status, dict(c.items)["차량"]), ("ok", 70.0))


class TestSegmentSync(_TmpMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        fake = self.fake
        # Samsung 2025 Q3: XBRL segments. The small company Q1: no XBRL segments,
        # a single segment with a product table in its document.
        fake.xbrl = {"20251114000003": _xbrl_zip([("DxDivision", "DX 부문", "80"), ("DsDivision", "DS 부문", "60")],
                                                 adjust="-10")}
        fake.documents["20250515000009"] = _doc(
            "<P>회사는 단일 영업부문을 영위하고 있습니다. 제품별 매출은 다음과 같습니다.</P><P>(단위 : 원)</P>"
            + _table([["콘덴서", "600"], ["전원장치", "400"], ["합계", "1,000"]], [["구분", "당기"]]), titled=True)
        orig_get = fake.get

        def get(url, params=None, timeout=None):
            ep = url.rsplit("/", 1)[-1]
            if ep == "fnlttXbrl.xml":
                fake.requests.append((ep, {k: v for k, v in (params or {}).items() if k != "crtfc_key"}))
                body = fake.xbrl.get(params["rcept_no"])
                return FakeResp(content=body) if body else FakeResp({"status": "014", "message": "no file"})
            return orig_get(url, params, timeout)
        fake.get = get

    def _seg(self, **kw):
        return run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                        lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory(), **kw)

    def test_segments_need_financials_then_are_read_and_mapped(self):
        self.assertEqual(self._seg(tickers=["005930", "123450"]).status, "done")     # lists + financials + segments
        conn = connect(self.db)
        self.addCleanup(conn.close)
        q3 = [r for r in query.get_segments("005930", conn=conn) if (r["fiscal_year"], r["fiscal_q"]) == (2025, 3)]
        self.assertEqual([(r["segment_raw"], r["segment_code"], r["revenue"], r["source"], r["check_status"], r["basis"])
                          for r in q3], [("DX 부문", "DX", 80.0, "xbrl", "ok", "q"), ("DS 부문", "DS", 60.0, "xbrl", "ok", "q")])
        with_adj = query.get_segments("005930", include_adjustments=True, conn=conn)
        self.assertIn(("부문간 내부거래 제거", "_adj"), {(r["segment_raw"], r["segment_code"]) for r in with_adj})
        small = query.get_segments("123450", conn=conn)
        self.assertEqual([(r["segment_raw"], r["revenue"], r["source"]) for r in small],
                         [("콘덴서", 600.0, "html_product"), ("전원장치", 400.0, "html_product")])
        draft = conn.execute("SELECT segment_raw, segment_code, note FROM segment_map WHERE corp_code = ? "
                             "ORDER BY segment_raw", (SAMSUNG,)).fetchall()
        self.assertEqual([tuple(r) for r in draft], [("DS 부문", "DS", "auto"), ("DX 부문", "DX", "auto")])
        # Q1 / H1 have no segment data anywhere: review, once.
        statuses = dict(conn.execute("SELECT f.reprt_code, s.status FROM seg_fetch s JOIN filing f "
                                     "ON f.rcept_no = s.rcept_no WHERE f.corp_code = ?", (SAMSUNG,)).fetchall())
        self.assertEqual(statuses, {"11013": "review", "11012": "review", "11014": "ok"})
        n = len(self.fake.requests)
        self._seg(tickers=["005930", "123450"])
        self.assertEqual([ep for ep, _ in self.fake.requests[n:] if ep in ("fnlttXbrl.xml", "document.xml")], [])

    def test_a_period_waits_for_its_financials(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        res = run_sync("init", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                       today=dt.date(2025, 11, 26), client_factory=self.factory(), priority=[])
        self.assertEqual(res.status, "done")
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory(),
                       priority=["005930"])
        self.assertEqual((res.status, res.segments_done), ("done", 0))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)

    def test_report_and_unmatched_names(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        for fy, names in ((2024, ("CE부문", "IM부문")), (2025, ("DX 부문",))):
            conn.execute("INSERT OR IGNORE INTO company(corp_code, stock_code, corp_name) VALUES (?, '005930', '삼성전자')",
                         (SAMSUNG,))
            r = f"{fy + 1}0310000001"
            conn.execute("INSERT INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt) VALUES "
                         "(?, ?, '11011', ?, ?)", (r, SAMSUNG, fy, f"{fy + 1}-03-10"))
            c = seg.Candidate("html_segment", [(n, 10.0) for n in names])
            seg.store_segments(conn, SAMSUNG, fy, 4, r, f"{fy + 1}-03-10", c, "ytd", 0.0, "ok", "html_segment")
            seg.record_fetch(conn, r, "ok", "html_segment")
        conn.commit()
        rep = query.segment_report(conn=conn)
        self.assertEqual((rep["companies"][0]["periods"], rep["companies"][0]["pass_rate"]), (2, 1.0))
        missing = {u["segment_code"]: u["missing"] for u in rep["unmatched"][SAMSUNG]}
        self.assertEqual(missing, {"CE": [2025], "IM": [2025], "DX": [2024]})
        text = query.format_segment_report(conn=conn)
        self.assertIn("통과율 100%", text)
        self.assertIn("'CE부문'", text)
        # A user's mapping (any note but 'auto') is kept: CE and IM become DX.
        conn.execute("UPDATE segment_map SET segment_code = 'DX', note = 'user' WHERE segment_raw IN ('CE부문', 'IM부문')")
        conn.execute("UPDATE segment_rev SET segment_code = 'DX' WHERE segment_raw IN ('CE부문', 'IM부문')")
        self.assertEqual(seg.unmatched_codes(conn, SAMSUNG), [])
        self.assertEqual(seg._segment_code(conn, SAMSUNG, "CE부문", 2026), "DX")

    def test_auto_reads_the_whole_backlog_stock_by_stock(self):
        """User direction 2026-11-26: the background run reads every pending
        segment period of the target stocks (it used to take only the reports
        of its daily window, plan 11-3-4); --segments then has nothing left."""
        res = run_sync("auto", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache, lock_path=self.lock,
                       today=dt.date(2025, 11, 20), client_factory=self.factory(), priority=["005930"])
        self.assertEqual(res.status, "done", res.error)
        conn = connect(self.db)
        self.addCleanup(conn.close)
        got = {r[0] for r in conn.execute("SELECT f.reprt_code FROM seg_fetch s JOIN filing f ON f.rcept_no = s.rcept_no")}
        self.assertEqual(got, {"11013", "11012", "11014"})
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2025, 11, 20), client_factory=self.factory(),
                       priority=["005930"])
        self.assertEqual(res.segments_done, 0)

    def test_financial_filer_is_not_applicable(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES ('B1', '105560', 'bank')")
        conn.execute("INSERT INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt) VALUES "
                     "('20260310000001', 'B1', '11011', 2025, '2026-03-10')")
        conn.execute("INSERT INTO fin_issue(rcept_no, corp_code, fiscal_year, fiscal_q, kind) VALUES "
                     "('20260310000001', 'B1', 2025, 4, 'revenue_na')")
        conn.execute("INSERT INTO fin_fetch(rcept_no, status, src_rcept_no) VALUES "
                     "('20260310000001', 'ok', '20260310000001')")          # its financials are in (no revenue line)
        conn.commit()
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2026, 11, 26), client_factory=self.factory(),
                       priority=["105560"])
        self.assertEqual(res.status, "done")
        self.assertEqual(conn.execute("SELECT status FROM seg_fetch").fetchone()[0], "na")
        self.assertEqual(self.fake.requests, [])                                  # no call for it


_ = (SMALL, FakeDart, os)


if __name__ == "__main__":
    unittest.main()
