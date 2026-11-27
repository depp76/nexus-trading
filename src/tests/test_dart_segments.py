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
        # A capital-management table that happens to add up (HD현대일렉트릭 2025) is no breakdown.
        cap = seg.Candidate("html_product", [("예금잔액(*1)", 96.9), ("차입금(*2)", 18.6), ("부채비율", 0.1)])
        self.assertEqual(seg.choose([cap], 115.6, None)[3], "review")
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


def _biz_doc(products: str, sales: str, note: str = "") -> str:
    """A document with the business section's two tables and a segment note after them."""
    return ('<DOCUMENT><TITLE ATOC="Y">II. 사업의 내용</TITLE><TITLE ATOC="Y">1. 사업의 개요</TITLE><P>...</P>'
            f'<TITLE ATOC="Y">2. 주요 제품 및 서비스</TITLE>{products}'
            '<TITLE ATOC="Y">3. 원재료 및 생산설비</TITLE><P>(단위 : 원)</P>'
            + _table([["원재료A", "1,000"], ["원재료B", "2,000"]], [["품목", "매입액"]])
            + f'<TITLE ATOC="Y">4. 매출 및 수주상황</TITLE>{sales}'
            '<TITLE ATOC="Y">III. 재무에 관한 사항</TITLE><TITLE ATOC="Y">3. 연결재무제표 주석</TITLE>'
            f'<TITLE ATOC="Y">4. 영업부문(연결)</TITLE>{note}<TITLE>4. 재무제표</TITLE></DOCUMENT>')


# Samsung 2026 Q3 (in 원 here): segments with the elimination under 기타, and a
# product table that lists the main product groups only.
_SAMSUNG_SALES = ("<P>(단위 : 원)</P>"
                  + _table([["DX부문", "제품", "TV,모니터,스마트폰 등", "150", "190"],
                            ["DS부문", "제품", "DRAM,NAND Flash", "250", "130"],
                            ["기타", ("부문간 내부거래 제거 등", 2), "(50)", "(30)"],
                            [("합 계", 3), "350", "290"]],
                           [["부문", "매출유형", "품목", "제58기 3분기", "제57기"]])
                  + "<P>(1) 주요 제품별 매출실적</P><P>(단위 : 원)</P>"
                  + _table([["TV, 모니터 등", "50", "60"], ["스마트폰", "90", "120"], ["메모리", "200", "110"]],
                           [["구분", "제58기 3분기", "제57기"]])
                  + "<P>(2) 매출유형별 매출실적</P><P>(단위 : 원)</P>"
                  + _table([["제ㆍ상품", "330", "280"], ["용역 및 기타매출", "20", "10"], ["계", "350", "290"]],
                           [["구분", "제58기 3분기", "제57기"]]))


class TestBusinessSection(unittest.TestCase):
    """User direction 2026-11-27: the business section's "주요 제품 및 서비스" /
    "매출 및 수주상황" tables, a product breakdown first."""

    def test_regions_are_the_two_sections_before_the_notes(self):
        doc = _biz_doc("<P>p</P>", "<P>s</P>", note="<P>매출 및 수주상황 (not here)</P>")
        regions = seg.biz_regions(doc)
        self.assertEqual(len(regions), 2)
        self.assertIn("주요 제품", regions[0])
        self.assertNotIn("원재료A", regions[0])

    def test_product_table_first_with_its_missing_lines_as_residual(self):
        cands = seg.biz_candidates(_biz_doc("", _SAMSUNG_SALES))
        self.assertFalse(any(c.sales_types() for c in cands))                 # 제ㆍ상품 / 용역 is no breakdown
        c, basis, residual = seg.choose_biz(cands, 130.0, 350.0)
        self.assertEqual((c.source, basis, residual), ("biz_product", "ytd", 10.0))
        self.assertEqual([n for n, _ in c.items], ["TV,모니터등", "스마트폰", "메모리"])
        segs = next(x for x in cands if x.source == "biz_segment" and x.label.endswith("제58기3분기"))
        self.assertEqual(segs.items, [("DX부문", 150.0), ("DS부문", 250.0)])
        self.assertEqual((segs.adjust, segs.total), ([("기타", -50.0)], 350.0))

    def test_segment_with_its_own_sub_rows_and_amount_with_share(self):
        """HD Hyundai Electric / LG Energy Solution layouts: 수출 | 내수 | 합계 under
        a segment, "2,173,111(64.7%)" cells, a segment column spanning rows."""
        products = ("<P>(단위 : 백만원)</P>"
                    + _table([[("전기전자부문", 1, 3), ("제품,기타", 1, 3), "전력기기", "2,173,111(64.7%)"],
                              ["회전기기", "518,398(15.4%)"], ["배전기기外", "666,667(19.9%)"],
                              [("합계", 3), "3,358,176(100.0%)"]],
                             [["사업부문", "매출유형", "품목", "매출액(비율)"]]))
        sales = ("<P>(단위 : 백만원)</P>"
                 + _table([[("에너지솔루션", 1, 3), "수출", "7,451,963"], ["내수", "16,306,686"], ["합계", "23,758,649"],
                           [("합계", 1, 3), "수출", "7,451,963"], ["내수", "16,306,686"], ["합계", "23,758,649"]],
                          [["사업부문", "구분", "제7기 3분기"]]))
        c, _b, residual = seg.choose_biz(seg.biz_candidates(_biz_doc(products, sales)), None, 3358176 * E)
        self.assertEqual((c.source, residual), ("biz_product", 0.0))
        self.assertEqual(c.items, [("전력기기", 2173111 * E), ("회전기기", 518398 * E), ("배전기기外", 666667 * E)])
        one = seg.biz_table_candidates(sales, E)
        self.assertFalse([x for x in one if len(x.items) >= 2 and not x.geographic()])   # one segment: no breakdown

    def test_kinds_channels_and_companies_are_not_business_lines(self):
        for names in (["제ㆍ상품", "용역및기타매출"], ["제품(화장품)", "상품(IT)"], ["직접판매", "간접판매"],
                      ["한전KPS㈜", "KEPCO KPS Ltd"]):
            c = seg.Candidate("biz_product", [(n, 10.0) for n in names])
            self.assertTrue(c.sales_types() or c.entities(), names)
        self.assertFalse(seg.Candidate("biz_product", [("메모리", 1.0), ("스마트폰", 1.0)]).sales_types())

    def test_choice_needs_the_revenue(self):
        cands = seg.biz_candidates(_biz_doc("", _SAMSUNG_SALES))
        self.assertIsNone(seg.choose_biz(cands, None, 500.0))                 # nothing adds up to 500
        self.assertIsNone(seg.choose_biz(cands, None, 450.0))                 # products 340: 24 % short is too far


class TestReviewCodex5(unittest.TestCase):
    """review_codex 5 (2026-11-27): adding up to revenue does not make a table a
    revenue breakdown -- what it lists, for which period, is checked first."""

    SALES_TABLE = _table([["변압기", "60"], ["차단기", "40"]], [["품목", "제10기"]])
    SALES = "<P>가. 매출실적</P><P>(단위 : 원)</P>" + SALES_TABLE

    def test_order_book_is_not_product_revenue(self):
        """P1: 수주총액 / 기납품액 / 수주잔고 columns and a table under a 수주
        caption are left out before any tolerance; the same amounts in the
        sales table are the products."""
        head = [[("품목", 1, 2), ("수주일자", 1, 2), ("수주총액", 2), ("기납품액", 2), ("수주잔고", 2)],
                ["수량", "금액", "수량", "금액", "수량", "금액"]]
        book = "<P>나. 수주상황</P><P>(단위 : 원)</P>" + _table(
            [["건설사업", "2025.01.02", "-", "50", "-", "50", "-", "50"],
             ["주택사업", "2025.03.04", "-", "30", "-", "30", "-", "30"],
             ["플랜트", "2025.05.06", "-", "20", "-", "20", "-", "20"]], head)
        cands = seg.biz_candidates(_biz_doc("", self.SALES + book))
        self.assertEqual([c.label for c in cands], ["biz 품목 / 제10기"])
        c, _basis, residual = seg.choose_biz(cands, None, 100.0)
        self.assertEqual((c.items, residual), ([("변압기", 60.0), ("차단기", 40.0)], 0.0))
        # The columns alone tell it (no caption), and so does the caption alone
        # ("품목 | 금액" under 수주현황) -- also when the book is a little below
        # revenue, the residual a product table may leave.
        self.assertEqual(seg.biz_table_candidates(book[book.index("<TABLE"):], 1.0), [])
        plain = "<P>(2) 수주현황</P><P>(단위 : 원)</P>" + _table(
            [["건설사업", "50"], ["주택사업", "30"], ["플랜트", "15"]], [["품목", "금액"]])
        self.assertEqual(seg.biz_candidates(_biz_doc("", plain)), [])
        # A sales table that also shows the backlog keeps its sales column only.
        both = "<P>가. 매출실적</P><P>(단위 : 원)</P>" + _table(
            [["변압기", "60", "500"], ["차단기", "40", "500"]], [["품목", "매출액", "수주잔고"]])
        self.assertEqual([c.label for c in seg.biz_candidates(_biz_doc("", both))], ["biz 품목 / 매출액"])
        # The section's own title ("매출 및 수주상황") is no 수주 caption.
        self.assertEqual(len(seg.biz_candidates(_biz_doc("", "<P>(단위 : 원)</P>" + self.SALES_TABLE))), 1)

    COSTS = _table([["인건비", "300", "280"], ["상품매출원가", "500", "450"], ["마케팅비용", "200", "190"]],
                   [["구분", "당기", "전기"]])
    PRODUCTS = _table([["백신", "700", "600"], ["위탁생산", "300", "250"], ["합계", "1,000", "850"]],
                      [["구분", "당기", "전기"]])

    def test_cost_table_after_the_single_segment_sentence(self):
        """P1 (SK바이오사이언스 2021): the single-segment sentence does not make
        every table of the region a product table."""
        single = "<P>회사는 단일 영업부문으로 구성되어 있습니다.</P>"
        for caption in ("<P>30. 비용의 성격별 분류</P><P>(단위 : 원)</P>", "<P>(단위 : 원)</P>"):
            region = seg.segment_region(_doc(single + caption + self.COSTS), True)
            cands, is_single = seg.document_candidates(region)
            self.assertTrue(is_single)
            self.assertEqual(seg.choose(cands, None, 1000.0)[3], "review", caption)      # costs add up to revenue
        doc = _doc(single + "<P>30. 비용의 성격별 분류</P><P>(단위 : 원)</P>" + self.COSTS
                   + "<P>31. 제품별 매출은 다음과 같습니다.</P><P>(단위 : 원)</P>" + self.PRODUCTS)
        cands, _single = seg.document_candidates(seg.segment_region(doc, True))
        c, _basis, _r, status = seg.choose(cands, None, 1000.0)
        self.assertEqual((status, c.source, [n for n, _ in c.items]), ("ok", "html_product", ["백신", "위탁생산"]))
        self.assertTrue(seg.is_product_breakdown(c))
        self.assertTrue(seg.Candidate("html_product", [("인건비", 1.0), ("감가상각비", 1.0), ("기타", 1.0)]).cost_items())
        self.assertFalse(seg.Candidate("html_product", [("백신", 1.0), ("위탁생산", 1.0)]).cost_items())

    def test_customer_table_is_not_a_product_breakdown(self):
        """P1: sales by customer reconcile with revenue exactly and are still
        no products."""
        rows = [["방위사업청", "600"], ["국방과학연구소", "300"], ["한화시스템", "100"]]
        by_head = "<P>(단위 : 원)</P>" + _table(rows, [["매출처", "매출액"]])
        self.assertEqual(seg.biz_candidates(_biz_doc("", by_head)), [])
        by_caption = "<P>라. 주요 매출처</P><P>(단위 : 원)</P>" + _table(rows, [["구분", "매출액"]])
        self.assertEqual(seg.biz_candidates(_biz_doc("", by_caption)), [])
        # "구분" says nothing: without a caption about products it is not taken ...
        bare = "<P>(단위 : 원)</P>" + _table(rows, [["구분", "매출액"]])
        self.assertEqual(seg.biz_candidates(_biz_doc("", bare)), [])
        # ... with one it is, and so is a table of the "주요 제품 및 서비스" section.
        named = "<P>(1) 주요 제품별 매출실적</P><P>(단위 : 원)</P>" + _table(rows, [["구분", "매출액"]])
        self.assertEqual(len(seg.biz_candidates(_biz_doc("", named))), 1)
        self.assertEqual(len(seg.biz_candidates(_biz_doc(bare, ""))), 1)
        # The notes' "주요 고객" table of a single-segment company likewise.
        note = ("<P>회사는 단일 영업부문입니다.</P><P>(2) 주요 고객에 대한 정보</P><P>(단위 : 원)</P>"
                + _table([["주요고객 A사", "600"], ["주요고객 B사", "400"]], [["구분", "당기"]]))
        cands, _single = seg.document_candidates(seg.segment_region(_doc(note), True))
        self.assertEqual(seg.choose(cands, None, 1000.0)[3], "review")
        self.assertTrue(seg.Candidate("html_product", [("주요고객A사", 1.0), ("기타고객(*)", 1.0)]).customers())

    def test_xbrl_period_decides_the_basis(self):
        """P1: a half-year context of 100 against Q2 99.5 and H1 100 is cumulative."""
        h1 = _xbrl_zip([("A", "가 부문", "60"), ("B", "나 부문", "40")], start="2025-01-01", end="2025-06-30")
        cands = seg.xbrl_candidates(h1, "CFS")
        self.assertEqual([c.months for c in cands], [6])
        self.assertEqual(seg.choose(cands, 99.5, 100.0, 2)[1:], ("ytd", 0.0, "ok"))
        self.assertEqual(seg.choose(cands, 99.5, 100.0)[1], "ytd")                    # the quarter not given
        self.assertEqual(seg.choose(cands, 100.0, 300.0, 3)[3], "review")             # 6 months in a Q3 report: not its period
        q2 = seg.xbrl_candidates(_xbrl_zip([("A", "가 부문", "60"), ("B", "나 부문", "40")],
                                           start="2025-04-01", end="2025-06-30"), "CFS")
        self.assertEqual(seg.choose(q2, 250.0, 100.0, 2)[3], "sum_mismatch")          # 3 months are not the half year
        self.assertEqual(seg.choose(q2, 100.0, 100.0, 1)[1], "q")                     # Q1: both, the quarter first
        # A result cached before the period was kept gets it from its label.
        old = [{"source": "xbrl", "items": [["가", 60.0], ["나", 40.0]], "adjust": [], "total": None,
                "label": "2025-01-01..2025-06-30 DisclosureOfOperatingSegmentsTableOf", "ops": {}}]
        self.assertEqual(seg._from_json(old)[0].months, 6)
        again = seg._from_json(seg._to_json(seg.table_candidates(
            _table([["가", "60", "70"], ["나", "40", "50"]], [["구분", ("매출액", 2)], ["", "당기", "전기"]]), 1.0, False)))
        self.assertEqual([(c.when, c.span) for c in again], [("cur", None), ("prev", None)])

    def test_closer_basis_and_prior_period_columns(self):
        plain = seg.Candidate("html_segment", [("A", 60.0), ("B", 40.0)])
        self.assertEqual(seg.choose([plain], 99.5, 100.0)[1], "ytd")                  # no period stated: the closer one
        self.assertEqual(seg.choose([plain], 100.0, 100.0)[1], "q")
        # P1: only the prior-period column matches -- not accepted, the current column is the mismatch.
        t = _table([["가", "70", "60"], ["나", "50", "40"]], [["구분", ("매출액", 2)], ["", "당기", "전기"]])
        c, basis, _r, status = seg.choose(seg.table_candidates(t, 1.0, False), None, 100.0)
        self.assertEqual((status, basis, c.items), ("sum_mismatch", None, [("가", 70.0), ("나", 50.0)]))
        for head in (["구분", "제19(전)기 매출액"], ["구분", "전분기 매출액"]):
            only_prev = seg.table_candidates(_table([["가", "60"], ["나", "40"]], [head]), 1.0, False)
            self.assertEqual(seg.choose(only_prev, None, 100.0)[3], "review", head)
        # A caption "(2) 전기" above a table without period columns.
        note = ("<P>(1) 당기</P><P>(단위 : 원)</P>" + _table([["가부문", "70"], ["나부문", "50"]], [["구분", "매출액"]])
                + "<P>(2) 전기</P><P>(단위 : 원)</P>" + _table([["가부문", "60"], ["나부문", "40"]], [["구분", "매출액"]]))
        cands, _single = seg.document_candidates(seg.segment_region(_doc(note), True))
        self.assertEqual([c.when for c in cands], ["cur", "prev"])
        self.assertEqual(seg.choose(cands, None, 100.0)[3], "sum_mismatch")
        # "3개월 | 누적" columns are checked against their own basis.
        t = _table([["가", "30", "60"], ["나", "20", "40"]], [["구분", ("매출액", 2)], ["", "3개월", "누적"]])
        self.assertEqual(seg.choose(seg.table_candidates(t, 1.0, False), 100.0, 250.0, 3)[3], "sum_mismatch")
        self.assertEqual(seg.choose(seg.table_candidates(t, 1.0, False), 50.0, 100.0, 3)[1], "q")
        # The business section: an older fiscal term (or year) is a prior period.
        biz = "<P>가. 매출실적</P><P>(단위 : 원)</P>" + _table([["변압기", "70", "60"], ["차단기", "50", "40"]],
                                                              [["품목", "제10기 3분기", "제9기"]])
        cands = seg.biz_candidates(_biz_doc("", biz))
        self.assertEqual([c.when for c in cands], [None, "prev"])
        self.assertIsNone(seg.choose_biz(cands, None, 100.0))
        self.assertIsNone(seg.choose_biz(cands, None, 110.0))                         # nor through a residual

    def test_a_product_of_several_segments_is_added_up(self):
        """P2: A·공통 40, B·공통 55, B·제품X 5."""
        t = _table([["A", "공통", "40"], [("B", 1, 2), "공통", "55"], ["제품X", "5"]], [["사업부문", "품목", "매출액"]])
        cands = {c.source: c for c in seg.biz_table_candidates(t, 1.0)}
        self.assertEqual(cands["biz_segment"].items, [("A", 40.0), ("B", 60.0)])
        self.assertEqual(cands["biz_product"].items, [("공통", 95.0), ("제품X", 5.0)])
        # An amount cell spanning rows is one amount.
        t = _table([[("에너지", 1, 2), "배터리", ("100", 1, 2)], ["ESS"], ["소재", "양극재", "50"]],
                   [["사업부문", "품목", "매출액"]])
        self.assertEqual(seg.biz_table_candidates(t, 1.0)[0].items, [("에너지", 100.0), ("소재", 50.0)])
        # Each block's own total is taken once ("수출 | 내수 | 합계" per segment).
        t = _table([[("A", 1, 3), ("공통", 1, 3), "수출", "10"], ["내수", "30"], ["합계", "40"],
                    [("B", 1, 3), ("공통", 1, 3), "수출", "50"], ["내수", "5"], ["합계", "55"],
                    [("합계", 2, 3), "수출", "60"], ["내수", "35"], ["합계", "95"]],
                   [["사업부문", "품목", "구분", "매출액"]])
        cands = seg.biz_table_candidates(t, 1.0)
        self.assertEqual((cands[0].items, cands[0].total), ([("A", 40.0), ("B", 55.0)], 95.0))
        # Sub-totals inside a segment ("제품 | 소계", "합계 | 수출") are not added to their rows.
        t = _table([["케미칼", "제품", "수출", "55"], ["케미칼", "제품", "내수", "9"], ["케미칼", "제품", "소계", "64"],
                    ["케미칼", "상품", "수출", "2"], ["케미칼", "상품", "소계", "2"],
                    ["케미칼", "합계", "수출", "57"], ["케미칼", "합계", "내수", "9"], ["케미칼", "합계", "소계", "66"],
                    ["장비", "제품", "내수", "34"], ["장비", "제품", "소계", "34"]],
                   [["사업부문", "매출유형", "구분", "매출액"]])
        self.assertEqual(seg.biz_table_candidates(t, 1.0)[0].items, [("케미칼", 66.0), ("장비", 34.0)])


class TestRegionTrees(unittest.TestCase):
    """기아 2025-2026 (2026-11-27): the region segments in an XBRL-styled note,
    each region shown as gross | its own eliminations | its own total."""

    def test_row_labels_over_several_columns(self):
        """2025 1-3Q: a level of the region tree per label column, with row
        spans; each region's "기업전체총계합계" row is its amount."""
        rows = [[("지역", 1, 9), ("본사소재지국가", 3, 3), ("기업전체총계", 1, 2), ("영업부문", 2), "160"],
                ["중요한조정사항", "부문간제거한금액", "(87)"],
                [("기업전체총계합계", 3), "73"],
                [("외국", 1, 6), ("북중미", 1, 3), ("KUS", 1, 3), ("기업전체총계", 1, 2), ("영업부문", 2), "98"],
                ["중요한조정사항", "부문간제거한금액", "0"],
                [("기업전체총계합계", 3), "98"],
                [("유럽", 2, 3), ("기업전체총계", 1, 2), ("영업부문", 2), "133"],
                ["중요한조정사항", "부문간제거한금액", "(69)"],
                [("기업전체총계합계", 3), "64"],
                [("지역합계", 7), "235"]]
        t = _table(rows, [["", "", "", "", "", "", "", "수익(매출액)"]])
        c, basis, residual, status = seg.choose(seg.table_candidates(t, E, False), 235 * E, 235 * E, 1)
        self.assertEqual((status, residual), ("ok", 0.0))
        self.assertEqual(c.items, [("본사소재지국가", 73 * E), ("KUS", 98 * E), ("유럽", 64 * E)])
        self.assertEqual((c.adjust, c.total), ([], 235 * E))

    def test_eliminations_inside_a_regions_own_total(self):
        """2025 annual and later: the same tree in the header; the elimination
        columns under a region are in its total, not adjustments of the table."""
        head = [["", ("지역", 6), ("지역합계", 1, 5)],
                ["", ("본사소재지국가", 3), ("유럽", 3)],
                ["", ("기업전체총계", 2), ("기업전체총계합계", 1, 3), ("기업전체총계", 2), ("기업전체총계합계", 1, 3)],
                ["", ("영업부문", 1, 2), "중요한조정사항", ("영업부문", 1, 2), "중요한조정사항"],
                ["", "부문간제거한금액", "부문간제거한금액"]]
        t = _table([["수익(매출액)", "651", "(351)", "300", "515", "(271)", "244", "544"]], head)
        c, _basis, residual, status = seg.choose(seg.table_candidates(t, E, False), None, 544 * E, 4)
        self.assertEqual((status, residual), ("ok", 0.0))
        self.assertEqual((c.items, c.adjust), ([("본사소재지국가", 300 * E), ("유럽", 244 * E)], []))

    def test_net_sales_row_before_the_gross_one(self):
        """2024 annual: "수익(매출액)" (gross, with the eliminations) and
        "순매출액" both reconcile; the net one keeps the series on one basis."""
        head = [["구분", "본사소재지국가", "유럽", "부문간제거한금액", "합계"]]
        t = _table([["수익(매출액)", "632", "492", "(580)", "544"], ["순매출액", "300", "244", "0", "544"]], head)
        c, _basis, _r, status = seg.choose(seg.table_candidates(t, E, False), None, 544 * E, 4)
        self.assertEqual((status, c.items), ("ok", [("본사소재지국가", 300 * E), ("유럽", 244 * E)]))


_CONS = "ifrs-full_ConsolidatedAndSeparateFinancialStatementsAxis_ifrs-full_ConsolidatedMember"
_GEO_AX = "ifrs-full_GeographicalAreasAxis"
_SCI = "ifrs-full_SegmentConsolidationItemsAxis"


def _te(value, period, *dims, code="ifrs-full_Revenue"):
    """A tagged value cell (document.xml's XBRL notes): ACODE / ACONTEXT."""
    ctx = "_".join((period, _CONS) + dims)
    return f'<TE ACODE="{code}" ACONTEXT="{ctx}" ADECIMAL="-6">\n<P>{value}</P></TE>'


def _tagged_regions(period="CFY2025dFQQ", unit="(단위 : 백만원)"):
    """기아 2025 annual and later, cut down: each region's gross and its own
    eliminations tagged, its net column untagged, the total untagged-member."""
    home = (_GEO_AX, "ifrs-full_CountryOfDomicileMember")
    kus = (_GEO_AX, "entity00106641_KusMmeberOfAmericaMemberOfDisclosureOfGeographicalAreasTableOfMember")
    gross, elim = (_SCI, "ifrs-full_OperatingSegmentsMember"), (_SCI, "ifrs-full_EliminationOfIntersegmentAmountsMember")
    head = ('<TR><TH></TH><TH COLSPAN="6">지역</TH><TH ROWSPAN="3">지역합계</TH></TR>'
            '<TR><TH></TH><TH COLSPAN="3">본사소재지국가</TH><TH COLSPAN="3">KUS</TH></TR>'
            '<TR><TH></TH><TH>영업부문</TH><TH>부문간제거한금액</TH><TH>기업전체총계합계</TH>'
            '<TH>영업부문</TH><TH>부문간제거한금액</TH><TH>기업전체총계합계</TH></TR>')
    row = ("<TR><TD>수익(매출액)</TD>" + _te("65", period, *home, *gross) + _te("(35)", period, *home, *elim)
           + "<TD>30</TD>" + _te("42", period, *kus, *gross) + _te("0", period, *kus, *elim) + "<TD>42</TD>"
           + _te("72", period) + "</TR>")
    return f"<P>{unit}</P><TABLE>{head}{row}</TABLE>"


class TestDocumentTags(unittest.TestCase):
    """The document's XBRL-tagged cells (2026-11-27, idea from dartlab): each
    cell says its account, period and members, whatever the table's layout."""

    def test_context(self):
        self.assertEqual(seg.parse_tag_context("CFY2025dHYA_" + _CONS + "_" + _GEO_AX + "_ifrs-full_CountryOfDomicileMember"),
                         ("cur", "ytd", {"ConsolidatedAndSeparateFinancialStatementsAxis": "ConsolidatedMember",
                                         "GeographicalAreasAxis": "CountryOfDomicileMember"}))
        self.assertEqual(seg.parse_tag_context("PFY2024dTQQ")[:2], ("prev", "q"))
        self.assertEqual(seg.parse_tag_context("CFY2025dFY")[:2], ("cur", "ytd"))
        self.assertIsNone(seg.parse_tag_context("CFY2025eFY"))                       # an instant
        # A filer's member token may hold "_" (SK가스: entity.._udf_NOTE_2023...Member).
        dims = seg.parse_tag_context("CFY2026dFQA_ifrs-full_ProductsAndServicesAxis_entity00144164_udf_NOTE_1Member")[2]
        self.assertEqual(dims, {"ProductsAndServicesAxis": "entity00144164_udf_NOTE_1Member"})

    def test_regions_gross_with_own_eliminations_named_from_the_table(self):
        doc = _doc(_tagged_regions())
        cands = seg.tag_candidates(doc)
        self.assertEqual([(c.source, c.when, c.span) for c in cands], [("tag_region", "cur", "q")])
        c = cands[0]
        self.assertEqual((c.items, c.adjust, c.total), ([("본사소재지국가", 30 * E), ("KUS", 42 * E)], [], 72 * E))
        self.assertEqual(seg.choose(cands, 72 * E, 72 * E, 1)[3], "ok")
        # No unit caption: ADECIMAL gives it.
        self.assertEqual(seg.tag_candidates(_doc(_tagged_regions(unit="")))[0].items[1], ("KUS", 42 * E))

    def test_parts_reconciling_members_and_kinds_of_sale(self):
        seg_ax, prod = "ifrs-full_SegmentsAxis", "ifrs-full_ProductsAndServicesAxis"
        p = "CFY2025dFY"
        head = "<TR><TH>구분</TH><TH>DX</TH><TH>DS</TH><TH>메모리</TH><TH>조정</TH><TH>합계</TH></TR>"
        row = ("<TR><TD>매출액</TD>" + _te("50", p, seg_ax, "entity1_DxMember") + _te("40", p, seg_ax, "entity1_DsMember")
               + _te("30", p, seg_ax, "entity1_MemoryMemberOfDsMember")
               + _te("(10)", p, seg_ax, "ifrs-full_MaterialReconcilingItemsMember") + _te("80", p) + "</TR>")
        kinds = ("<TR><TH>구분</TH><TH>재화의 판매</TH><TH>용역의 제공</TH><TH>합계</TH></TR><TR><TD>매출액</TD>"
                 + _te("70", p, prod, "entity1_SalesOfGoodsOfProductsAndServicesMemberOfXMember")
                 + _te("10", p, prod, "ifrs-full_RenderingOfServicesMember") + _te("80", p) + "</TR>")
        cands = seg.tag_candidates(f"<P>(단위 : 원)</P><TABLE>{head}{row}</TABLE><TABLE>{kinds}</TABLE>")
        self.assertEqual([(c.source, c.items, c.adjust) for c in cands],
                         [("tag_segment", [("DX", 50.0), ("DS", 40.0)], [("조정 항목", -10.0)])])


class TestLevelMigration(_TmpMixin, unittest.TestCase):
    def test_old_segment_rows_get_a_level_and_are_read_again(self):
        import sqlite3
        old = sqlite3.connect(self.db)
        old.executescript(
            "CREATE TABLE segment_rev (corp_code TEXT NOT NULL, fiscal_year INTEGER NOT NULL, fiscal_q INTEGER NOT NULL,"
            " segment_raw TEXT NOT NULL, segment_code TEXT, revenue REAL, op_income REAL, basis TEXT, source TEXT,"
            " check_status TEXT, rcept_no TEXT NOT NULL, rcept_dt TEXT NOT NULL,"
            " PRIMARY KEY (corp_code, fiscal_year, fiscal_q, segment_raw, rcept_no));"
            "CREATE INDEX ix_segment_rev_period ON segment_rev(corp_code, fiscal_year, fiscal_q);"
            "CREATE TABLE seg_fetch (rcept_no TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT, detail TEXT,"
            " fetched_at TEXT);"
            "CREATE TABLE sync_state (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);"
            "INSERT INTO sync_state VALUES ('schema_version', '10', '');"
            "INSERT INTO seg_fetch VALUES ('r1', 'ok', 'xbrl', '', '');"
            "INSERT INTO segment_rev VALUES ('C', 2025, 4, 'DX', 'DX', 1, NULL, 'ytd', 'xbrl', 'ok', 'r1', 'd');"
            "INSERT INTO segment_rev VALUES ('C', 2025, 4, '메모리', '메모리', 1, NULL, 'ytd', 'biz_product', 'ok', 'r1', 'd');")
        old.commit()
        old.close()
        conn = connect(self.db)
        self.assertEqual(sorted(tuple(r) for r in conn.execute("SELECT segment_raw, level FROM segment_rev")),
                         [("DX", "segment"), ("메모리", "product")])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)
        # Schema 12 (review_codex 5): a DB already at 11 has its periods read again once more.
        conn.execute("INSERT INTO seg_fetch VALUES ('r1', 'ok', 'xbrl', '', '')")
        conn.execute("UPDATE sync_state SET value = '11' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT value FROM sync_state WHERE key = 'schema_version'").fetchone()[0], "13")
        # Both levels may now hold the same name in one period.
        seg.store_segments(conn, "C", 2025, 4, "r1", "d", seg.Candidate("biz_product", [("DX", 2.0), ("DS", 1.0)]),
                           "ytd", 0.0, "ok", "biz_product", level="product")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM segment_rev WHERE segment_raw = 'DX'").fetchone()[0], 2)

    def test_schema_13_reads_every_period_again(self):
        """2026-11-27 (기아): the note tables' new layouts and the document's
        XBRL-tagged cells may change any period's segment or product level."""
        conn = connect(self.db)
        conn.executemany("INSERT INTO seg_fetch VALUES (?, ?, ?, '', '')",
                         [("x", "ok", "xbrl"), ("b", "ok", "biz_segment"), ("n", "na", None), ("h", "ok", "html_segment"),
                          ("p", "ok", "html_product"), ("s", "ok", "single"), ("r", "review", None),
                          ("m", "sum_mismatch", "xbrl")])
        conn.execute("UPDATE sync_state SET value = '12' WHERE key = 'schema_version'")
        conn.commit()
        conn.close()
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)


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
        # A single segment with a product table in its notes: the segment level
        # is the one segment, the table is the product level (review_codex 5 P1).
        small = query.get_segments("123450", conn=conn)
        self.assertEqual([(r["level"], r["segment_raw"], r["revenue"], r["source"]) for r in small],
                         [("segment", "단일부문", 1000.0, "single"),
                          ("product", "콘덴서", 600.0, "html_product"), ("product", "전원장치", 400.0, "html_product")])
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

    def test_tagged_regions_after_the_business_sections_segments(self):
        """The document's tagged regions fill a period without XBRL segments;
        the business section's business lines still come first (a region
        breakdown is the last resort, as in the notes)."""
        self.fake.documents["20250515000001"] = _doc(_tagged_regions(unit="(단위 : 원)").replace("72", "100")
                                                     .replace(">65<", ">93<"))
        self.assertEqual(self._seg(tickers=["005930"]).status, "done")
        conn = connect(self.db)
        q1 = [(r["segment_raw"], r["revenue"], r["source"]) for r in query.get_segments("005930", conn=conn)
              if (r["fiscal_year"], r["fiscal_q"]) == (2025, 1)]
        self.assertEqual(q1, [("본사소재지국가", 58.0, "tag_region"), ("KUS", 42.0, "tag_region")])
        conn.execute("DELETE FROM seg_fetch")
        conn.commit()
        conn.close()
        import shutil
        shutil.rmtree(os.path.join(self.cache, "segments"))      # the parsed document is cached per receipt,
        shutil.rmtree(os.path.join(self.cache, "seg_doc"))       # and the slice of the document it was read from
        sales = _table([["가사업", "70"], ["나사업", "30"], ["합계", "100"]], [["사업부문", "매출액"]])
        self.fake.documents["20250515000001"] = _biz_doc("", "<P>(단위 : 원)</P>" + sales,
                                                         _tagged_regions(unit="(단위 : 원)").replace("72", "100")
                                                         .replace(">65<", ">93<"))
        self._seg(tickers=["005930"])
        conn = connect(self.db)
        self.addCleanup(conn.close)
        q1 = [(r["segment_raw"], r["source"]) for r in query.get_segments("005930", conn=conn)
              if (r["fiscal_year"], r["fiscal_q"]) == (2025, 1)]
        self.assertEqual(q1, [("가사업", "biz_segment"), ("나사업", "biz_segment")])

    def test_business_section_products_come_before_xbrl(self):
        """User direction 2026-11-27: with a product table in the business
        section that adds up, it is stored instead of the XBRL segments."""
        self.fake.documents["20251114000003"] = _biz_doc("", _SAMSUNG_SALES)
        self.assertEqual(self._seg(tickers=["005930"]).status, "done")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        q3 = [r for r in query.get_segments("005930", include_adjustments=True, conn=conn)
              if (r["fiscal_year"], r["fiscal_q"]) == (2025, 3)]
        self.assertEqual({(r["level"], r["segment_raw"], r["revenue"], r["source"], r["basis"]) for r in q3},
                         {("product", "TV,모니터등", 50.0, "biz_product", "ytd"),
                          ("product", "스마트폰", 90.0, "biz_product", "ytd"),
                          ("product", "메모리", 200.0, "biz_product", "ytd"),
                          ("product", "기타·조정(차액)", 10.0, "biz_product", "ytd"),
                          # the segment level stays XBRL's
                          ("segment", "DX 부문", 80.0, "xbrl", "q"), ("segment", "DS 부문", 60.0, "xbrl", "q"),
                          ("segment", "부문간 내부거래 제거", -10.0, "xbrl", "q")})
        self.assertEqual(conn.execute("SELECT source FROM seg_fetch WHERE rcept_no = '20251114000003'").fetchone()[0],
                         "xbrl")
        # Without XBRL segments the business section's segment table is the segment level.
        self.fake.xbrl.clear()
        conn.execute("DELETE FROM seg_fetch")
        conn.commit()
        import shutil
        shutil.rmtree(os.path.join(self.cache, "segments"), ignore_errors=True)
        shutil.rmtree(os.path.join(self.cache, "seg_doc"), ignore_errors=True)
        self._seg(tickers=["005930"])
        q3 = [r for r in query.get_segments("005930", conn=conn)
              if (r["fiscal_year"], r["fiscal_q"]) == (2025, 3) and r["level"] == "segment"]
        self.assertEqual([(r["segment_raw"], r["source"]) for r in q3], [("DS부문", "biz_segment"), ("DX부문", "biz_segment")])

    def test_customer_and_order_tables_leave_the_product_level_empty(self):
        """review_codex 5 P1: a document whose only tables are sales by customer
        and an order book (both equal to revenue) gives no product data."""
        customers = "<P>라. 주요 매출처</P><P>(단위 : 원)</P>" + _table(
            [["방위사업청", "200"], ["국방과학연구소", "150"]], [["매출처", "매출액"]])
        book = "<P>마. 수주상황</P><P>(단위 : 원)</P>" + _table(
            [["건설사업", "250", "100", "350"], ["주택사업", "100", "250", "350"]],
            [["품목", "수주총액", "기납품액", "수주잔고"]])
        self.fake.documents["20251114000003"] = _biz_doc("", customers + book)
        self.assertEqual(self._seg(tickers=["005930"]).status, "done")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        q3 = [r for r in query.get_segments("005930", conn=conn) if (r["fiscal_year"], r["fiscal_q"]) == (2025, 3)]
        self.assertEqual({(r["level"], r["source"]) for r in q3}, {("segment", "xbrl")})

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

    def test_report_lists_companies_that_passed_then_failed(self):
        """2026-11-27 (기아): a pass followed by failures is listed with the last
        pass and the first failing receipt; a company that never passed, a
        financial filer's "na" and an older superseded receipt are not."""
        conn = connect(self.db)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES ('K', '000270', '기아')")
        conn.execute("INSERT INTO company(corp_code, stock_code, corp_name) VALUES ('N', '000001', '늘실패')")
        rows = [("K", 2024, "11011", "20250313000001", "ok", "html_segment", "row 순매출액"),
                ("K", 2025, "11013", "20250515000001", "ok", "html_segment", ""),       # superseded below
                ("K", 2025, "11013", "20250520000001", "review", None, ""),
                ("K", 2025, "11012", "20250814000001", "na", None, ""),
                ("K", 2025, "11011", "20260312000001", "sum_mismatch", "html_segment", "row 수익"),
                ("K", 2026, "11013", "20260515000001", "ok", "html_segment", ""),
                ("N", 2025, "11011", "20260312000002", "review", None, "")]
        for corp, fy, reprt, r, status, source, detail in rows:
            conn.execute("INSERT INTO filing(rcept_no, corp_code, reprt_code, bsns_year, rcept_dt) VALUES (?, ?, ?, ?, ?)",
                         (r, corp, reprt, fy, f"{r[:4]}-{r[4:6]}-{r[6:8]}"))
            seg.record_fetch(conn, r, status, source, detail)
        conn.commit()
        rep = query.segment_report(conn=conn)
        self.assertEqual(len(rep["breaks"]), 1)
        b = rep["breaks"][0]
        self.assertEqual((b["stock_code"], b["last_ok"]["period"], b["last_ok"]["detail"]),
                         ("000270", (2024, 4), "row 순매출액"))
        self.assertEqual([(f["period"], f["status"]) for f in b["failed"]],
                         [((2025, 1), "review"), ((2025, 4), "sum_mismatch")])
        self.assertEqual((b["failed"][0]["rcept_no"], b["passed_again"]), ("20250520000001", True))
        text = query.format_segment_report(conn=conn)
        self.assertIn("000270 기아: 마지막 통과 2024 연간 (원문 부문표 · row 순매출액) → 첫 실패 2025 1Q review "
                      "접수 20250520000001 · 이후 다시 통과한 기간 있음", text)
        self.assertIn("실패 기간: 2025 1Q review, 2025 연간 불일치", text)

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


class TestReviewAgy(_TmpMixin, unittest.TestCase):
    """review_agy.md (2026-11-27): the four accuracy bugs, the document slice,
    tagged operating income and units, operating income filled in from another
    reading, and the net reading before the gross one."""

    # ---- P0-1: "all other segments" is a segment --------------------------------

    @staticmethod
    def _with_other(z: bytes, value: str, op: str | None = None, start="2025-07-01", end="2025-09-30") -> bytes:
        """`z` (_xbrl_zip) plus a Revenue fact on ifrs-full:AllOtherSegmentsMember."""
        src = zipfile.ZipFile(io.BytesIO(z))
        files = {n: src.read(n).decode("utf-8") for n in src.namelist()}
        ctx = ('<xbrli:context id="other"><xbrli:entity/><xbrli:segment><xbrldi:explicitMember '
               'dimension="ifrs-full:SegmentsAxis">ifrs-full:AllOtherSegmentsMember</xbrldi:explicitMember>'
               f'</xbrli:segment><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>'
               f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>'
               f'<ifrs-full:Revenue contextRef="other" unitRef="KRW">{value}</ifrs-full:Revenue>')
        if op is not None:
            ctx += f'<dart:OperatingIncomeLoss contextRef="other" unitRef="KRW">{op}</dart:OperatingIncomeLoss>'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for n, text in files.items():
                zf.writestr(n, text.replace("</xbrli:xbrl>", ctx + "</xbrli:xbrl>") if n.endswith(".xbrl") else text)
        return buf.getvalue()

    def test_all_other_segments_member_is_a_segment_in_xbrl(self):
        """KT 2024-2026: the segments plus the standard "all other" member add
        up to revenue; without it every period was a sum_mismatch."""
        z = self._with_other(_xbrl_zip([("Ict", "ICT", "60"), ("Finance", "금융", "25")], adjust="-5"), "20", op="3")
        cands = seg.xbrl_candidates(z, "CFS")
        c, basis, residual, status = seg.choose(cands, 100.0, 300.0)
        self.assertEqual((status, basis, residual), ("ok", "q", 0.0))
        self.assertEqual(sorted(c.items), sorted([("기타", 20.0), ("ICT", 60.0), ("금융", 25.0)]))
        self.assertEqual(c.ops, {"기타": 3.0})
        self.assertEqual(c.adjust, [("부문간 내부거래 제거", -5.0)])
        # It may belong to another table: the reading without it is still there.
        self.assertEqual(seg.choose(cands, 80.0, None)[0].items, [("ICT", 60.0), ("금융", 25.0)])

    def test_all_other_segments_member_is_a_segment_in_tagged_cells(self):
        seg_ax, p = "ifrs-full_SegmentsAxis", "CFY2025dFY"
        head = "<TR><TH>구분</TH><TH>ICT</TH><TH>금융</TH><TH></TH><TH>합계</TH></TR>"
        row = ("<TR><TD>매출액</TD>" + _te("60", p, seg_ax, "entity1_IctMember") + _te("25", p, seg_ax, "entity1_FinanceMember")
               + _te("15", p, seg_ax, "ifrs-full_AllOtherSegmentsMember") + _te("100", p) + "</TR>")
        cands = seg.tag_candidates(f"<P>(단위 : 원)</P><TABLE>{head}{row}</TABLE>")
        self.assertEqual(cands[0].items, [("ICT", 60.0), ("금융", 25.0), ("기타", 15.0)])
        self.assertEqual(seg.choose(cands, None, 100.0, 4)[3], "ok")

    # ---- P0-2: accounts of a results summary are no products -----------------------

    def test_results_summary_is_not_a_product_breakdown(self):
        """현대차 2023: 영업이익 + 총자산 came within 0.5 % of the revenue."""
        summary = "<P>가. 주요 제품 등의 현황</P><P>(단위 : 원)</P>" + _table(
            [["매출액", "1,626"], ["영업이익", "137"], ["총자산", "1,481"]], [["구분", "2023년(제56기) 금액"]])
        cands = seg.biz_candidates(_biz_doc(summary, ""))
        self.assertTrue(all(c.not_revenue() for c in cands))
        self.assertIsNone(seg.choose_biz(cands, None, 1618.0, source="biz_product", fiscal_q=4))
        for name in ("영업이익", "총자산", "자산합계", "부채총계", "당기순이익", "법인세비용차감전순이익", "감가상각비", "EBITDA",
                     "CAPEX"):
            c = seg.Candidate("biz_product", [("차량", 60.0), (name, 40.0)])
            self.assertIsNone(c.check(None, 100.0), name)
        # Businesses whose names only look like accounts stay products.
        ok = seg.Candidate("biz_product", [("자산관리", 60.0), ("투자은행", 30.0), ("이익공유형 임대", 10.0)])
        self.assertEqual(ok.check(None, 100.0), ("ytd", 0.0))

    # ---- P0-3: balance accounts and instants are no revenue ------------------------

    def test_accrued_income_at_the_period_end_is_not_revenue(self):
        """에이치브이엠 2026 Q3: "row 미수수익 당분기말" was kept as the segments."""
        head = [["구분", ("당분기말", 2), ("전기말", 2)], ["", "첨단금속", "기타", "첨단금속", "기타"]]
        t = _table([["미수수익", "70", "30", "50", "20"], ["선수수익", "7", "3", "5", "2"]], head)
        self.assertEqual(seg.table_candidates(t, E, False), [])
        # A revenue row under a period-end header is a balance too, and so is a column of one.
        t = _table([["매출액", "70", "30", "50", "20"]], head)
        self.assertEqual(seg.table_candidates(t, E, False), [])
        t = _table([["첨단금속", "70", "700"], ["기타", "30", "300"]], [["구분", "당기말", "당기"]])
        cands = seg.table_candidates(t, E, True)
        self.assertEqual([(c.label, c.items[0][1]) for c in cands], [("col 당기", 700 * E)])
        for label in ("미수수익", "선수수익", "이연수익", "금융수익", "기타수익", "외환차익", "충당부채", "평가이익"):
            self.assertTrue(seg._NOT_REVENUE.search(label), label)
        # The revenue line of LG에너지솔루션, and the business section's "...분기말 매출액" period header.
        self.assertFalse(seg._NOT_REVENUE.search("매출액및기타수익"))
        sales = "<P>(단위 : 원)</P>" + _table([["가제품", "70"], ["나제품", "30"]],
                                              [["품목", "제16기 3분기말(2026년 9월말) 매출액"]])
        hit = seg.choose_biz(seg.biz_candidates(_biz_doc("", sales)), None, 100.0, fiscal_q=3)
        self.assertEqual(hit[0].items, [("가제품", 70.0), ("나제품", 30.0)])

    # ---- P0-4: the line break of DART documents -----------------------------------

    def test_dart_line_break_entity_is_whitespace(self):
        """CJ 2021: "식품및&cr;식품서비스" and "식품및식품서비스" were two codes."""
        self.assertEqual(seg._clean("식품 및&cr;식품서비스"), "식품및식품서비스")
        self.assertEqual(seg._clean("식품&amp;cr;서비스&#13;&#10;부문"), "식품서비스부문")
        self.assertEqual(seg.normalise("식품및&cr;식품서비스 부문"), seg.normalise("식품및식품서비스부문"))
        t = _table([["식품및&cr;식품서비스", "70"], ["물류", "30"], ["합계", "100"]], [["구분", "매출액"]])
        c = seg.choose(seg.table_candidates(t, 1.0, False), None, 100.0)[0]
        self.assertEqual(c.items, [("식품및식품서비스", 70.0), ("물류", 30.0)])

    # ---- P1-5: the saved slice of a document -----------------------------------------

    def _long_doc(self) -> str:
        """Business section, consolidated and separate notes with far more text
        than the readers use, a tagged table outside every region."""
        filler = "<P>" + "가나다라 " * 40 + "</P>"
        note = "<P>(단위 : 원)</P>" + _table([["DX부문", "80"], ["DS부문", "60"], ["연결조정", "(10)"], ["합계", "130"]],
                                              [["구분", "매출액"]])
        seg_ax, p = "ifrs-full_SegmentsAxis", "CFY2025dTQQ"
        tagged = ("<TABLE><TR><TH>구분</TH><TH>DX</TH><TH>DS</TH><TH>합계</TH></TR><TR><TD>매출액</TD>"
                  + _te("70", p, seg_ax, "entity1_DxMember") + _te("60", p, seg_ax, "entity1_DsMember")
                  + _te("130", p) + "</TR></TABLE>")
        return ('<DOCUMENT><TITLE ATOC="Y">II. 사업의 내용</TITLE><TITLE ATOC="Y">1. 사업의 개요</TITLE>' + filler * 300
                + f'<TITLE ATOC="Y">4. 매출 및 수주상황</TITLE>{_SAMSUNG_SALES}'
                + '<TITLE ATOC="Y">III. 재무에 관한 사항</TITLE><TITLE ATOC="Y">3. 연결재무제표 주석</TITLE>'
                + filler * 300 + "<P>29. 부문별 보고</P>" + note + filler * 1500
                + "<P>(단위 : 백만원)</P><P>" + "설명 " * 800 + "</P>" + tagged + filler * 300
                + '<TITLE ATOC="Y">5. 재무제표 주석</TITLE>' + filler * 200
                + "<P>30. 영업부문</P><P>회사는 단일 영업부문을 영위하고 있습니다.</P>" + filler * 300
                + '<TITLE ATOC="Y">6. 배당에 관한 사항</TITLE>' + filler * 300 + "</DOCUMENT>")

    @staticmethod
    def _read(doc: str, fs_div: str) -> tuple:
        cands, single = [], False
        for region in seg.segment_regions(doc, fs_div == "CFS"):
            found, one = seg.document_candidates(region)
            cands, single = cands + found, single or one
        return (seg._to_json(cands), single, seg._to_json(seg.biz_candidates(doc)),
                seg._to_json(seg.tag_candidates(doc, fs_div)))

    def test_slice_reads_as_the_document_does(self):
        doc = self._long_doc()
        cut = seg.slice_document(doc)
        self.assertLess(len(cut), len(doc) // 2)
        for fs_div in ("CFS", "OFS"):
            full = self._read(doc, fs_div)
            self.assertEqual(self._read(cut, fs_div), full, fs_div)
            self.assertEqual(self._read(seg.slice_document(cut), fs_div), full, fs_div)
        cands, single, biz, tags = self._read(cut, "CFS")
        self.assertTrue(cands and biz and tags)
        self.assertFalse(single)
        self.assertTrue(self._read(cut, "OFS")[1])                 # the separate notes' single-segment sentence
        self.assertEqual(tags[0]["items"], [("DX", 70 * E), ("DS", 60 * E)])        # its unit caption came along

    def test_a_new_parser_version_reads_the_slice_not_the_api(self):
        calls = []
        doc = self._long_doc()

        class Client:
            def get_bytes(self, endpoint, params):
                calls.append(endpoint)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w") as zf:
                    zf.writestr("20251114000003.xml", doc)
                return buf.getvalue()

        def plain(result):
            return [seg._to_json(x) if isinstance(x, list) else x for x in result]

        first = seg.fetch_document(Client(), "20251114000003", "CFS", self.cache)
        self.assertEqual(calls, ["document.xml"])
        self.assertTrue(os.path.exists(os.path.join(self.cache, "seg_doc", "2025", "20251114000003.xml.gz")))
        self.assertEqual(seg.load_slice(self.cache, "20251114000003"), seg.slice_document(doc))
        # The other statement kind, and a later parser version: no call.
        seg.fetch_document(Client(), "20251114000003", "OFS", self.cache)
        old, old_slice = seg.DOC_PARSER_VERSION, seg.SLICE_VERSION
        self.addCleanup(setattr, seg, "DOC_PARSER_VERSION", old)
        self.addCleanup(setattr, seg, "SLICE_VERSION", old_slice)
        seg.DOC_PARSER_VERSION = old + 1
        again = seg.fetch_document(Client(), "20251114000003", "CFS", self.cache)
        self.assertEqual(calls, ["document.xml"])
        self.assertEqual(plain(again), plain(first))
        # A slice of another SLICE_VERSION (the places read changed) is not used.
        seg.SLICE_VERSION = old_slice + 1
        seg.DOC_PARSER_VERSION = old + 2
        seg.fetch_document(Client(), "20251114000003", "CFS", self.cache)
        self.assertEqual(calls, ["document.xml", "document.xml"])

    def test_no_document_is_an_empty_slice(self):
        from data.dart.client import DartApiError
        calls = []

        class Client:
            def get_bytes(self, endpoint, params):
                calls.append(endpoint)
                raise DartApiError("014", "no file")

        self.assertEqual(seg.fetch_document(Client(), "20251114000004", "CFS", self.cache), ([], False, [], []))
        old = seg.DOC_PARSER_VERSION
        self.addCleanup(setattr, seg, "DOC_PARSER_VERSION", old)
        seg.DOC_PARSER_VERSION = old + 1
        self.assertEqual(seg.fetch_document(Client(), "20251114000004", "CFS", self.cache), ([], False, [], []))
        self.assertEqual(calls, ["document.xml"])

    def test_xbrl_result_of_the_version_before_is_kept_when_it_adds_up(self):
        calls = []
        package = self._with_other(_xbrl_zip([("Ict", "ICT", "60"), ("Finance", "금융", "25")]), "15")

        class Client:
            def get_bytes(self, endpoint, params):
                calls.append(params["rcept_no"])
                return package

        old = [{"source": "xbrl", "items": [["ICT", 60.0], ["금융", 25.0]], "adjust": [], "total": None,
                "label": "2025-07-01..2025-09-30 T", "ops": {}}]
        for rcept in ("r1", "r2"):
            seg._save(seg._cache_path(self.cache, "xbrl_CFS", rcept, seg.PREVIOUS_PARSER_VERSION), old)

        def accept(revenue):
            return lambda cands: not cands or seg.choose(cands, revenue, None, 3)[3] == "ok"

        kept = seg.fetch_xbrl(Client(), "r1", "11014", "CFS", self.cache, accept=accept(85.0))
        self.assertEqual((calls, kept[0].items), ([], [("ICT", 60.0), ("금융", 25.0)]))
        # The one that did not add up is asked for again -- and now it does.
        again = seg.fetch_xbrl(Client(), "r2", "11014", "CFS", self.cache, accept=accept(100.0))
        self.assertEqual(calls, ["r2"])
        self.assertEqual(sorted(seg.choose(again, 100.0, None, 3)[0].items),
                         sorted([("기타", 15.0), ("ICT", 60.0), ("금융", 25.0)]))
        seg.fetch_xbrl(Client(), "r1", "11014", "CFS", self.cache, accept=accept(85.0))
        self.assertEqual(calls, ["r2"])                            # settled as this version's

    # ---- P1-6: tagged operating income and units -----------------------------------

    def _tagged_segments(self, caption: str, inner: str = "") -> str:
        seg_ax, p = "ifrs-full_SegmentsAxis", "CFY2025dFY"
        op = "dart_OperatingIncomeLoss"
        head = f"<TR><TH>구분{inner}</TH><TH>DX</TH><TH>DS</TH><TH>합계</TH></TR>"
        rows = ("<TR><TD>매출액</TD>" + _te("60", p, seg_ax, "entity1_DxMember") + _te("40", p, seg_ax, "entity1_DsMember")
                + _te("100", p) + "</TR><TR><TD>영업이익</TD>" + _te("6", p, seg_ax, "entity1_DxMember", code=op)
                + _te("(4)", p, seg_ax, "entity1_DsMember", code=op) + _te("2", p, code=op) + "</TR>")
        return f"<P>{caption}</P><TABLE>{head}{rows}</TABLE>"

    def test_tagged_operating_income_goes_with_its_member(self):
        c = seg.tag_candidates(self._tagged_segments("(단위 : 백만원)"))[0]
        self.assertEqual(c.items, [("DX", 60 * E), ("DS", 40 * E)])
        self.assertEqual(c.ops, {"DX": 6 * E, "DS": -4 * E})

    def test_only_a_caption_of_amounts_sets_the_unit(self):
        for caption, inner, scale in (("(단위 : 백만원)", "", 1e6), ("(단위 : 천원)", "", 1e3), ("(단위: 원)", "", 1.0),
                                      ("(단위 : 억원)", "", 1e8), ("(단위 : 천USD)", "", 1e3),
                                      # no amounts: the cells' ADECIMAL (-6) stands
                                      ("(단위 : %)", "", 1e6), ("(단위 : 천주)", "", 1e6), ("", "", 1e6),
                                      ("영업단위를 근거로 하여 보고부문을 구분하고 있습니다.", "", 1e6),
                                      # the caption inside the table before the one above it
                                      ("(단위 : 천원)", " (단위: 백만원)", 1e6), ("(단위 : 천원)", " (단위: %)", 1e3)):
            c = seg.tag_candidates(self._tagged_segments(caption, inner))[0]
            self.assertEqual(c.items[0][1], 60 * scale, (caption, inner))

    # ---- P1-6: operating income from another reading --------------------------------

    def test_operating_income_is_filled_in_by_name_for_the_same_period(self):
        chosen = seg.Candidate("biz_segment", [("DX부문", 60.0), ("DS부문", 40.0)], span="ytd")
        note = seg.Candidate("html_segment", [("DX 부문", 75.0), ("DS 부문", 45.0)], [("연결조정", -20.0)],
                             ops={"DX 부문": 6.0, "DS 부문": -4.0}, when="cur", span="ytd")
        self.assertEqual(seg.enrich_ops(chosen, "ytd", [note], 30.0, 100.0, 2), {"DX부문": 6.0, "DS부문": -4.0})
        self.assertEqual(chosen.items, [("DX부문", 60.0), ("DS부문", 40.0)])          # the revenue choice stands
        # Not from a prior period, the other span, other segment names, or a
        # table of unknown period that neither reconciles nor shows the same revenue.
        for other in (seg.Candidate("html_segment", note.items, note.adjust, ops=note.ops, when="prev", span="ytd"),
                      seg.Candidate("html_segment", note.items, note.adjust, ops=note.ops, span="q"),
                      seg.Candidate("html_segment", [("가전", 75.0), ("반도체", 45.0)], note.adjust,
                                    ops={"가전": 6.0, "반도체": -4.0}, span="ytd"),
                      seg.Candidate("html_segment", [("DX 부문", 20.0), ("DS 부문", 15.0)], ops=note.ops)):
            self.assertEqual(seg.enrich_ops(chosen, "ytd", [other], 30.0, 100.0, 2), {}, (other.when, other.span))
        unknown = seg.Candidate("html_segment", [("DX 부문", 60.0), ("DS 부문", 40.0)], ops=note.ops)
        self.assertEqual(seg.enrich_ops(chosen, "ytd", [unknown], 30.0, 100.0, 2), {"DX부문": 6.0, "DS부문": -4.0})
        # Nothing to do for a reading that has its own, or for the single segment.
        self.assertEqual(seg.enrich_ops(note, "ytd", [note], 30.0, 100.0, 2), {})
        self.assertEqual(seg.enrich_ops(seg.Candidate("single", [("단일부문", 100.0)]), "ytd", [note], 30.0, 100.0, 2), {})

    def test_sync_fills_the_business_sections_segments_with_the_notes_operating_income(self):
        """Samsung 2025 Q3 without XBRL: revenue from the business section's
        segment table (cumulative 350), operating income from the segment note."""
        note = "<P>(단위 : 원)</P><P>&lt;당분기 누적&gt;</P>" + _table(
            [["매출액", "190", "280", "(120)", "350"], ["영업이익", "20", "45", "(5)", "60"]],
            [["구분", "DX 부문", "DS 부문", "내부거래 조정", "합계"]])
        self.fake.documents["20251114000003"] = _biz_doc("", _SAMSUNG_SALES, note)
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory(),
                       tickers=["005930"])
        self.assertEqual(res.status, "done")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        q3 = [(r["segment_raw"], r["revenue"], r["op_income"], r["source"], r["basis"])
              for r in query.get_segments("005930", conn=conn)
              if (r["fiscal_year"], r["fiscal_q"]) == (2025, 3) and r["level"] == "segment"]
        self.assertEqual(sorted(q3), [("DS부문", 250.0, 45.0, "biz_segment", "ytd"),
                                      ("DX부문", 150.0, 20.0, "biz_segment", "ytd")])

    # ---- P1-7 (minimal): the net reading before the gross one -----------------------

    def test_net_table_is_taken_whenever_it_is_there(self):
        """현대차: gross with eliminations in 2025, net in 2026 Q1, gross again
        in Q2 -- whichever table the package listed first."""
        gross = _xbrl_zip([("Vehicle", "차량", "117"), ("Finance", "금융", "19")], adjust="-41", family="DetailedTableOf")
        net = _xbrl_zip([("Vehicle", "차량", "76"), ("Finance", "금융", "19")], adjust="-41", family="NetRevenueTableOf")
        for order in ((gross, net), (net, gross)):
            cands = [c for z in order for c in seg.xbrl_candidates(z, "CFS")]
            c, _basis, residual, status = seg.choose(cands, 95.0, None)
            self.assertEqual((status, c.items, c.adjust, residual), ("ok", [("차량", 76.0), ("금융", 19.0)], [], 0.0))
        # Only the gross table: it is still the reading.
        c = seg.choose(seg.xbrl_candidates(gross, "CFS"), 95.0, None)[0]
        self.assertEqual((dict(c.items)["차량"], c.adjust), (117.0, [("부문간 내부거래 제거", -41.0)]))
        # Other segment names are another breakdown, not the net table of this one.
        other = _xbrl_zip([("Kr", "국내", "50"), ("Us", "미주", "45")], family="NetRevenueTableOf")
        c = seg.choose(seg.xbrl_candidates(gross, "CFS") + seg.xbrl_candidates(other, "CFS"), 95.0, None)[0]
        self.assertEqual(dict(c.items)["차량"], 117.0)

    def test_dropping_a_small_adjustment_is_not_a_net_table(self):
        """롯데쇼핑 2023: a 1.3 % reconciling item; with and without it the
        segments are within 1 % of revenue -- the exact reading stays."""
        z = _xbrl_zip([("Mart", "할인점", "9,935"), ("Super", "슈퍼", "200")], adjust="-135")
        c, _basis, _residual, status = seg.choose(seg.xbrl_candidates(z, "CFS"), 10_050.0, None)
        self.assertEqual((status, c.adjust), ("ok", [("부문간 내부거래 제거", -135.0)]))


_ = (SMALL, FakeDart, os)



class TestReviewAgy2(_TmpMixin, unittest.TestCase):
    """review_agy 2 (2026-11-27): what a breakdown lists is kept apart from
    where it was read, a table that names no period is not settled as the
    quarter's, and operating income is filled in segment by segment."""

    # ---- P1: the axis of a breakdown ----------------------------------------------

    def test_axis_from_the_names_then_the_source(self):
        def axis(source, names, **kw):
            return seg.Candidate(source, [(n, 1.0) for n in names], **kw).axis()
        self.assertEqual(axis("xbrl", ["DX 부문", "DS 부문"]), "segment")
        self.assertEqual(axis("html_segment", ["가부문", "나부문"]), "segment")
        self.assertEqual(axis("biz_product", ["메모리", "스마트폰"]), "product")
        self.assertEqual(axis("html_product", ["콘덴서", "전원장치"]), "product")
        self.assertEqual(axis("single", ["단일부문"]), "segment")
        # Regions and kinds of sale are what they are whatever lists them.
        self.assertEqual(axis("xbrl", ["국내", "해외"]), "region")
        self.assertEqual(axis("html_segment", ["한국", "기타"]), "region")              # "기타": the rest of the world
        self.assertEqual(axis("html_segment", ["아시아", "북아메리카", "남아메리카", "유럽", "기타"]), "region")
        self.assertEqual(axis("tag_region", ["본사소재지국가", "KUS"]), "region")
        self.assertEqual(axis("html_segment", ["본사소재지국가", "KUS", "KUS이외"], hint="region"), "region")
        self.assertEqual(axis("html_product", ["제품매출", "상품매출", "기타매출"]), "sales_type")
        self.assertEqual(axis("html_segment", ["상품판매", "용역수익", "로열티수익"]), "sales_type")
        self.assertEqual(axis("html_product", ["한 시점에 이전", "기간에 걸쳐 이전"]), "sales_type")
        self.assertEqual(axis("html_product", ["주요고객 A사", "주요고객 B사"]), "unknown")
        # A business line is not a kind of sale for holding one of its words.
        self.assertEqual(axis("html_segment", ["의료기기", "제품개발용역", "기타"]), "segment")

    def test_sales_type_tables_are_not_the_operating_segments(self):
        """The review's two receipts (the rows dart.db held for them at the
        segment level): 20261119000188 "제품매출 / 기타매출" and 20261118000236
        "재화의판매 / 용역의제공". Both add up to the revenue exactly."""
        first = ("<P>제품 및 용역에 대한 매출은 다음과 같습니다.</P><P>(단위 : 천원)</P>"
                 + _table([["제품매출", "14,097,653"], ["기타매출", "208,537"], ["합계", "14,306,190"]],
                          [["구분", "당분기 누적"]]))
        second = ("<P>재화 및 용역 유형별 수익의 구분은 다음과 같습니다.</P><P>(단위 : 원)</P>"
                  + _table([["재화의 판매", "7,851,173,220"], ["용역의 제공", "11,341,918"], ["합계", "7,862,515,138"]],
                           [["구분", "당기"]]))
        for note, total, q in ((first, 14_306_190_000.0, 3), (second, 7_862_515_138.0, 4)):
            cands, single = seg.document_candidates(seg.segment_region(_doc(note), True))
            c, basis, _res, status = seg.choose(cands, None, total, q)
            self.assertEqual((status, basis, c.axis(), single), ("ok", "ytd", "sales_type", False))
            self.assertFalse(seg.is_segment_reading(c, single))
            self.assertFalse(seg.is_product_breakdown(c))
        # Any other breakdown that adds up comes before the kinds of sale.
        both = "<P>(단위 : 천원)</P>" + _table(
            [["가부문", "10,000,000"], ["나부문", "4,306,190"], ["합계", "14,306,190"]], [["구분", "매출액"]]) + first
        cands, _single = seg.document_candidates(seg.segment_region(_doc(both), True))
        c = seg.choose(cands, None, 14_306_190_000.0, 3)[0]
        self.assertEqual((c.axis(), [n for n, _ in c.items]), ("segment", ["가부문", "나부문"]))

    def test_what_may_stand_as_the_segments(self):
        def cand(source, names, **kw):
            return seg.Candidate(source, [(n, 1.0) for n in names], **kw)
        ok = seg.is_segment_reading
        # The segment note's own regions are the company's segment disclosure
        # (기아), also beside "one reportable segment"; regions under a product
        # caption are a split of the sales.
        self.assertTrue(ok(cand("html_segment", ["본사소재지국가", "KUS", "KUS이외"], hint="region")))
        self.assertTrue(ok(cand("html_segment", ["국내", "북미", "유럽"]), single=True))
        self.assertTrue(ok(cand("tag_region", ["본사소재지국가", "KUS"])))
        self.assertTrue(ok(cand("xbrl", ["국내", "해외"])))                                # SegmentsAxis says so
        self.assertFalse(ok(cand("html_product", ["국내", "수출"])))
        # A product table stands in only without the single-segment statement.
        self.assertTrue(ok(cand("html_product", ["콘덴서", "전원장치"])))
        self.assertFalse(ok(cand("html_product", ["콘덴서", "전원장치"]), single=True))
        # Kinds of sale and customer lists never.
        self.assertFalse(ok(cand("html_segment", ["제품매출", "기타매출"])))
        self.assertFalse(ok(cand("html_product", ["재화의판매", "용역의제공"])))
        self.assertFalse(ok(cand("html_product", ["주요고객 A사", "주요고객 B사"])))

    def test_region_hint_from_the_tables_own_axis_label(self):
        # Items in columns under a 지역 header (기아's annual note).
        cols = _table([["순매출액", "600", "300", "100", "1,000"]],
                      [["", ("지역", 3), "지역합계"], ["", "본사소재지국가", "KUS", "KUS이외", ""]])
        c = seg.choose(seg.table_candidates(cols, 1.0, False), None, 1000.0)[0]
        self.assertEqual(([n for n, _ in c.items], c.hint, c.axis()),
                         (["본사소재지국가", "KUS", "KUS이외"], "region", "region"))
        # A 지역 header over the columns of a segments-by-region matrix says
        # nothing about the rows.
        matrix = _table([["가부문", "600"], ["나부문", "400"], ["합계", "1,000"]], [["구분", "지역"], ["", "매출액"]])
        c = seg.choose(seg.table_candidates(matrix, 1.0, False), None, 1000.0)[0]
        self.assertEqual((c.hint, c.axis()), (None, "segment"))
        # The caption: "지역별 매출" makes it the geographic table, not a product one.
        note = ("<P>제품 매출의 지역별 매출액 정보는 다음과 같습니다.</P><P>(단위 : 원)</P>"
                + _table([["A권역", "600"], ["B권역", "400"], ["합계", "1,000"]], [["구분", "당기"]]))
        cands, _single = seg.document_candidates(seg.segment_region(_doc(note), True))
        c = seg.choose(cands, None, 1000.0)[0]
        self.assertEqual((c.source, c.axis()), ("html_segment", "region"))
        # The hint survives the parsed-result cache.
        self.assertEqual(seg._from_json(seg._to_json([c]))[0].axis(), "region")

    def _small(self, note):
        """The small company's 2025 Q1 (revenue 1,000, no XBRL, no single-segment
        statement) read from a segment note; its rows by level."""
        self.fake.documents["20250515000009"] = _doc(note)
        res = run_sync("segments", "cli", api_key="k" * 40, db_path=self.db, cache_dir=self.cache,
                       lock_path=self.lock, today=dt.date(2025, 11, 26), client_factory=self.factory(),
                       tickers=["123450"])
        self.assertEqual(res.status, "done")
        conn = connect(self.db)
        self.addCleanup(conn.close)
        rows = [(r["level"], r["segment_raw"], r["revenue"], r["source"], r["axis"], r["check_status"])
                for r in query.get_segments("123450", conn=conn)]
        return rows, conn

    def test_sync_stores_a_sales_type_company_as_a_single_segment(self):
        rows, conn = self._small("<P>제품 및 용역에 대한 매출은 다음과 같습니다.</P><P>(단위 : 원)</P>"
                                 + _table([["제품매출", "985"], ["기타매출", "15"], ["합계", "1,000"]], [["구분", "당분기"]]))
        # Not its segments, and not its products either.
        self.assertEqual(rows, [("segment", "단일부문", 1000.0, "single", "segment", "ok")])
        self.assertEqual(tuple(conn.execute("SELECT status, source FROM seg_fetch").fetchone()), ("ok", "single"))

    def test_sync_keeps_regions_of_the_segment_note_as_segments_on_the_region_axis(self):
        """The contrast case (기아): the segment note's only table is by region."""
        rows, conn = self._small("<P>지역에 대한 공시</P><P>(단위 : 원)</P>" + _table(
            [["순매출액", "600", "300", "100", "1,000"]],
            [["", ("지역", 3), "지역합계"], ["", "본사소재지국가", "KUS", "KUS이외", ""]]))
        self.assertEqual(rows, [("segment", "본사소재지국가", 600.0, "html_segment", "region", "ok"),
                                ("segment", "KUS", 300.0, "html_segment", "region", "ok"),
                                ("segment", "KUS이외", 100.0, "html_segment", "region", "ok")])
        self.assertIn("axis region", conn.execute("SELECT detail FROM seg_fetch").fetchone()[0])

    def test_sync_keeps_a_product_table_as_segments_on_the_product_axis(self):
        rows, _conn = self._small("<P>주요 제품별 매출은 다음과 같습니다.</P><P>(단위 : 원)</P>"
                                  + _table([["콘덴서", "600"], ["전원장치", "400"], ["합계", "1,000"]], [["구분", "당분기"]]))
        self.assertEqual(sorted((r[0], r[1], r[3], r[4]) for r in rows),
                         [("product", "전원장치", "html_product", "product"), ("product", "콘덴서", "html_product", "product"),
                          ("segment", "전원장치", "html_product", "product"), ("segment", "콘덴서", "html_product", "product")])

    def test_stored_rows_get_their_axis_when_the_column_is_added(self):
        conn = connect(self.db)
        seg.store_segments(conn, "C", 2025, 4, "r1", "d", seg.Candidate("html_product", [("제품매출", 9.0), ("기타매출", 1.0)]),
                           "ytd", 0.0, "ok", "html_product")
        seg.store_segments(conn, "C", 2025, 4, "r2", "d", seg.Candidate("html_segment", [("국내", 6.0), ("해외", 5.0)],
                                                                       [("연결조정", -1.0)]),
                           "ytd", 0.0, "ok", "html_segment")
        seg.store_segments(conn, "C", 2025, 4, "r3", "d", seg.Candidate("xbrl", [("DX", 6.0), ("DS", 4.0)]),
                           "ytd", 0.0, "ok", "xbrl")
        self.assertEqual(dict(conn.execute("SELECT rcept_no, axis FROM segment_rev GROUP BY rcept_no")),
                         {"r1": "sales_type", "r2": "region", "r3": "segment"})
        # A DB of schema 12: no axis / op_source columns yet.
        conn.execute("ALTER TABLE segment_rev DROP COLUMN axis")
        conn.execute("ALTER TABLE segment_rev DROP COLUMN op_source")
        conn.execute("UPDATE sync_state SET value = '12' WHERE key = 'schema_version'")
        conn.execute("INSERT INTO seg_fetch VALUES ('r1', 'ok', 'html_product', '', '')")
        conn.commit()
        conn.close()
        conn = connect(self.db)
        self.addCleanup(conn.close)
        self.assertEqual(sorted(tuple(r) for r in conn.execute("SELECT DISTINCT rcept_no, axis FROM segment_rev")),
                         [("r1", "sales_type"), ("r2", "region"), ("r3", "segment")])      # adjustment rows too
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seg_fetch").fetchone()[0], 0)  # and every period is read again

    # ---- P1: a table that names no period ------------------------------------------

    def test_no_period_evidence_and_both_bases_fit_is_not_settled(self):
        """The review's repro: Q2, the quarter's and the cumulative revenue both 100."""
        plain = seg.Candidate("html_segment", [("A", 60.0), ("B", 40.0)])
        self.assertEqual(seg.choose([plain], 100.0, 100.0, 2)[1:], (None, 0.0, "ok"))
        self.assertEqual(seg.choose([plain], 99.5, 100.0, 3)[1:], (None, 0.0, "ok"))     # both within the tolerance
        # A stated period settles it.
        for span in ("q", "ytd"):
            marked = seg.Candidate("html_segment", plain.items, span=span)
            self.assertEqual(seg.choose([marked], 100.0, 100.0, 2)[1:], (span, 0.0, "ok"))
        for head, basis in (("3개월", "q"), ("누적", "ytd")):
            table = _table([["A", "60"], ["B", "40"], ["합계", "100"]], [["구분", ("매출액", 1)], ["", head]])
            c, got, _res, status = seg.choose(seg.table_candidates(table, 1.0, False), 100.0, 100.0, 2)
            self.assertEqual((status, got, [n for n, _ in c.items]), ("ok", basis, ["A", "B"]), head)
        bare = _table([["A", "60"], ["B", "40"], ["합계", "100"]], [["구분", "매출액"]])
        self.assertEqual(seg.choose(seg.table_candidates(bare, 1.0, False), 100.0, 100.0, 2)[1:], (None, 0.0, "ok"))
        # The same breakdown with its period stated, read later, settles the open one.
        marked = seg.Candidate("html_segment", [("A", 60.0), ("B", 40.0)], span="ytd", label="marked")
        c, basis, _res, _st = seg.choose([plain, marked], 100.0, 100.0, 2)
        self.assertEqual((c.label, basis), ("marked", "ytd"))
        # Only one basis fits: that one, as before. A first quarter, where the two
        # are one, and a caller that gives no quarter keep the closer basis.
        self.assertEqual(seg.choose([plain], 40.0, 100.0, 2)[1], "ytd")
        self.assertEqual(seg.choose([plain], 100.0, 250.0, 2)[1], "q")
        self.assertEqual(seg.choose([plain], 100.0, 100.0, 1)[1], "q")
        self.assertEqual(seg.choose([plain], 99.5, 100.0)[1], "ytd")
        # XBRL dates are evidence: never open.
        self.assertEqual(seg.choose([seg.Candidate("xbrl", plain.items, months=6)], 100.0, 100.0, 2)[1], "ytd")

    def test_an_open_basis_is_stored_and_nothing_is_filled_in_from_it(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        plain = seg.Candidate("html_segment", [("A", 60.0), ("B", 40.0)])
        c, basis, residual, status = seg.choose([plain], 100.0, 100.0, 2)
        seg.store_segments(conn, "C", 2025, 2, "r1", "d", c, basis, residual, status, c.source)
        self.assertEqual([tuple(r) for r in conn.execute("SELECT segment_raw, revenue, basis, check_status, axis "
                                                         "FROM segment_rev ORDER BY revenue DESC")],
                         [("A", 60.0, None, "ok", "segment"), ("B", 40.0, None, "ok", "segment")])
        other = seg.Candidate("html_segment", plain.items, ops={"A": 1.0, "B": 2.0}, span="ytd")
        self.assertEqual(seg.enrich_ops(plain, None, [other], 100.0, 100.0, 2), {})
        # The business section's choice prefers a table whose period is settled.
        open_biz = seg.Candidate("biz_segment", [("A", 60.0), ("B", 40.0)], label="open")
        settled = seg.Candidate("biz_segment", [("A", 60.0), ("B", 40.0)], span="ytd", label="settled")
        got = seg.choose_biz([open_biz, settled], 100.0, 100.0, source="biz_segment", fiscal_q=2)
        self.assertEqual((got[0].label, got[1]), ("settled", "ytd"))
        self.assertEqual(seg.choose_biz([open_biz], 100.0, 100.0, source="biz_segment", fiscal_q=2)[1], None)

    # ---- P2: operating income segment by segment -----------------------------------

    def test_only_the_missing_operating_income_is_filled_in(self):
        """A has an operating income of 0 (a real zero), B has none."""
        def chosen():
            return seg.Candidate("biz_segment", [("A부문", 60.0), ("B부문", 40.0)], ops={"A부문": 0.0}, span="ytd")
        note = seg.Candidate("html_segment", [("A 부문", 60.0), ("B 부문", 40.0)], ops={"A 부문": 0.0, "B 부문": 7.0},
                             span="ytd")
        self.assertEqual(seg.enrich_ops(chosen(), "ytd", [note], 30.0, 100.0, 2), {"B부문": 7.0})
        c = chosen()
        self.assertEqual(seg.fill_ops(c, "ytd", [note], 30.0, 100.0, 2), {"B부문": "html_segment"})
        self.assertEqual((c.ops, c.op_sources), ({"A부문": 0.0, "B부문": 7.0}, {"B부문": "html_segment"}))
        # A reading whose A disagrees shows another measure: A is not replaced
        # and B is not taken from it either.
        other = seg.Candidate("html_segment", note.items, ops={"A 부문": 5.0, "B 부문": 7.0}, span="ytd")
        c = chosen()
        self.assertEqual(seg.fill_ops(c, "ytd", [other], 30.0, 100.0, 2), {})
        self.assertEqual(c.ops, {"A부문": 0.0})
        # ... but the next reading that agrees completes it.
        c = chosen()
        seg.fill_ops(c, "ytd", [other, note], 30.0, 100.0, 2)
        self.assertEqual(c.ops, {"A부문": 0.0, "B부문": 7.0})
        # Names that do not map one to one, another axis: nothing.
        for bad in (seg.Candidate("html_segment", [("A 부문", 60.0), ("C 부문", 40.0)], ops={"C 부문": 7.0}, span="ytd"),
                    seg.Candidate("html_segment", [("A 부문", 60.0), ("B 부문", 30.0), ("B부문", 10.0)],
                                  ops={"B 부문": 7.0}, span="ytd")):
            self.assertEqual(seg.enrich_ops(chosen(), "ytd", [bad], 30.0, 100.0, 2), {})
        by_region = seg.Candidate("html_segment", note.items, ops=note.ops, span="ytd", hint="region")
        self.assertEqual(seg.enrich_ops(chosen(), "ytd", [by_region], 30.0, 100.0, 2), {})
        # Each missing one may come from a different reading; one nobody has stays empty.
        three = seg.Candidate("biz_segment", [("A", 50.0), ("B", 30.0), ("C", 20.0)], span="ytd")
        only_a = seg.Candidate("xbrl", three.items, ops={"A": 5.0}, months=6)
        only_b = seg.Candidate("html_segment", three.items, ops={"B": 3.0}, span="ytd")
        self.assertEqual(seg.fill_ops(three, "ytd", [only_a, only_b], 30.0, 100.0, 2), {"A": "xbrl", "B": "html_segment"})
        self.assertEqual(three.ops, {"A": 5.0, "B": 3.0})

    def test_the_source_of_each_operating_income_is_stored(self):
        conn = connect(self.db)
        self.addCleanup(conn.close)
        c = seg.Candidate("biz_segment", [("A", 60.0), ("B", 40.0), ("C", 5.0)], ops={"A": 0.0}, span="ytd")
        c.ops["B"], c.op_sources["B"] = 7.0, "html_segment"
        seg.store_segments(conn, "C", 2025, 2, "r1", "d", c, "ytd", 0.0, "ok", "biz_segment")
        self.assertEqual([tuple(r) for r in conn.execute("SELECT segment_raw, op_income, op_source FROM segment_rev "
                                                         "ORDER BY segment_raw")],
                         [("A", 0.0, "biz_segment"), ("B", 7.0, "html_segment"), ("C", None, None)])


if __name__ == "__main__":
    unittest.main()
