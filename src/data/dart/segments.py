"""Revenue by business segment (docs/dart_db_plan_2026-11-26.md 5, stage 4).

Sources, in plan 5-1 order:

1. ``xbrl``: the report's XBRL package (``fnlttXbrl.xml``). Revenue facts on an
   entity member of ``SegmentsAxis`` / ``SegmentConsolidationItemsAxis`` are
   segments; IFRS reconciling members (intersegment elimination, unallocated,
   material reconciling items) are adjustments. Member names come from the
   Korean label linkbase. Large filers tag these from about 2023 on.
2. ``html_segment``: the segment note of the report's document ("영업부문",
   "부문별 정보"), consolidated notes for a consolidated filer.
3. ``html_product``: a revenue table by product in that note -- what a filer
   with a single operating segment (SK하이닉스, 주성엔지니어링) discloses there.
   ``single``: a single segment and no usable breakdown: one segment equal to
   the company's revenue.

Tables have no fixed layout (segments as rows or columns, sub-columns under a
segment, one table per period or "3개월 | 누적" columns, totals, eliminations),
so every table near the note heading is read into *candidates* -- one per
plausible revenue row / column -- and the plan 5-3 check chooses: a candidate
is accepted when its segments plus adjustments (or the table's own total)
come within 1 % of the period's revenue in fin_std, the 3-month value
(basis ``q``) or the cumulative one (``ytd``). Adding up is a check of the
amounts only, so what a table lists and for which period is settled first
(review_codex 5, 2026-11-27): an order book, an expense table and a customer
list are told by the caption before the table, the header path and the item
names and never become candidates, and a candidate keeps its period (당기 /
전기, 3개월 / 누적, the XBRL dates) and is checked against that period's
revenue only. A table whose total matches but whose segment rows do not add up
(segments reported gross, "기타" not shown) gets the difference as a residual
adjustment row. Nothing matching: the first segment candidate is stored as
``sum_mismatch``; nothing read at all: ``review``.

Adjustment rows are stored with ``segment_code`` "_adj" (and "_residual" for a
computed difference), so the segments alone are ``segment_code NOT LIKE '_%'``.
``segment_map`` gets a draft row per new segment name (``note = 'auto'``,
normalised name as the code, the fiscal years it was seen); rows a user edits
(any other note) are left alone (plan 5-4).
"""
from __future__ import annotations

import html
import io
import json
import logging
import os
import re
import zipfile

from data.dart import documents as docs
from data.dart.client import DartApiError, is_no_file
from data.dart.store import now_iso

logger = logging.getLogger(__name__)

TOLERANCE = 0.01                 # plan 5-3
# A table total may reconcile through a residual (segments shown gross, "기타"
# not shown: Samsung 2021, 7 %), but not a large one -- that is a misread
# (a revenue caption taken for a segment, cancelled by the residual).
MAX_RESIDUAL_SHARE = 0.15
# Bumped whenever reading changes: cached results of another version are read again.
# XBRL and documents apart, so a change in document reading does not ask for the
# XBRL packages again (13: the business section, 2026-11-27; 14: review_codex 5 --
# order / cost / customer tables left out, the period of a column kept, a product
# sold by several segments added up). The XBRL result gained the period's length,
# which an older cached result gives through its label (_from_json): no bump.
PARSER_VERSION = 12
DOC_PARSER_VERSION = 15
_GEO = ("국내", "한국", "대한민국", "내수", "수출", "해외", "북미", "미국", "미주", "유럽", "구주", "아시아", "중국",
        "일본", "베트남", "인도", "중남미", "남미", "중동", "아프리카", "오세아니아", "호주", "캐나다", "멕시코",
        "브라질", "독일", "러시아", "기타지역", "기타국가", "아태", "동남아")
_ADJ_MEMBERS = ("EliminationOfIntersegmentAmountsMember", "MaterialReconcilingItemsMember",
                "UnallocatedAmountsMember", "CorporateMember", "ReconcilingItemsMember")
_SUBTOTAL_MEMBERS = ("OperatingSegmentsMember", "ReportableSegmentsMember", "AllOtherSegmentsMember")

_TOTAL = re.compile(r"^((합계|총계|소계|계|연결|연결계|총합계|연결합계|매출합계|부문합계|조정후금액|연결재무제표금액|"
                    r"(기업)?전체총계)(\(\*?\d*\))?)+$")
_PERIOD = re.compile(r"^(3개월|6개월|9개월|12개월|누적|당기|전기|당분기|전분기|당반기|전반기|당기말|전기말|당분기말|전분기말|"
                     r"누적기간|제\d+(\(당\)|\(전\))?기.*|\d{4}년.*|\d{4}[.\-/]\d{1,2}([.\-/]\d{1,2})?\.?|\d{4}|"
                     r"FY\d{2,4}|\d{1,2}Q\d{2,4})$")
_ADJ_NAMES = {"EliminationOfIntersegmentAmountsMember": "부문간 내부거래 제거",
              "MaterialReconcilingItemsMember": "조정 항목", "UnallocatedAmountsMember": "미배분",
              "CorporateMember": "본사·공통", "ReconcilingItemsMember": "조정 항목"}


def _is_revenue_caption(name: str) -> bool:
    n = (name or "").replace(" ", "")
    return n in ("매출액", "매출", "수익", "영업수익", "수익(매출액)", "순매출액", "총매출액") or         bool(_EXTERNAL.search(n) and _REVENUE_LABEL.search(n))


def is_total(name: str) -> bool:
    """A total row / column: "합계", "계(*)", "제품과용역합계", "기업 전체 총계"..."""
    n = (name or "").replace(" ", "")
    return bool(_TOTAL.match(n)) or n.endswith(("합계", "총계"))
_ADJ = re.compile(r"(연결조정|조정|내부거래|제거|상계|부문간|내부매출|연결제거)")
_EXTERNAL = re.compile(r"(외부|순매출)")         # the external-sales row or column first ("외부매출", 기아 "순매출액")
_INTERNAL = re.compile(r"(내부|부문간|부문사이)")
_REVENUE_LABEL = re.compile(r"(매출|수익|영업수익)")
_NOT_REVENUE = re.compile(r"(원가|비율|구성비|%|이익|손익|손실|비용|자산|부채|상각|법인세|이자|취득|투자|감가|채권|증감)")
_OP_LABEL = re.compile(r"^(영업이익|영업손익|영업이익\(손실\)|영업손실|부문(영업)?(이익|손익))")
_SINGLE = re.compile(r"(단일\s*(의\s*)?(영업|보고|사업)?\s*부문|하나의\s*(영업|보고|사업)?\s*부문|보고\s*부문은\s*단일|"
                     r"단일\s*사업\s*부문|단일\s*영업\s*단위)")
_HEADING = re.compile(r"(영업\s*부문|부문\s*별\s*(정보|보고|공시)|부문\s*정보|보고\s*부문)")
_BLOCK = re.compile(r"<(TD|TE|TH|P|TITLE)\b[^>]*>(.*?)</\1>", re.S | re.I)
_RAW_HEADING = re.compile(r"(?:(?<=[>\s])\d{1,2}(?:\.\d{1,2})?\s*(?:<[^>]+>\s*)*\.?\s*(?:<[^>]+>\s*)*"
                          r"|\(\d\)\s*)(?:영업\s*부문|부문\s*별|부문\s*정보|보고\s*부문)(?!\s*별\s*(재고|보유))")
_AXIS_WORDS = {"영업부문", "보고부문", "부문", "구분", "지역", "제품과용역", "제품", "고객", "영업부문합계", "부문정보",
               "보고부문합계", "기업전체", "연결"}
_PRODUCT_CONTEXT = re.compile(r"(제품|품목|매출유형|상품|용역)[^.。]{0,15}(매출|수익)")
_NOTE_HEADING = re.compile(r"^(\d{1,2}(\.\d{1,2})?|[가-하]|\(\d\))[.)]?(영업부문|부문별|부문정보|보고부문|사업부문)")
_PRODUCT_WORDS = ("제품", "품목", "매출유형", "용역", "상품", "서비스", "구분별", "유형별")
_SPLIT = re.compile(r"\s+")

# What a table is about, told by its item names, its header and the caption or
# sentence before it (review_codex 5: adding up to revenue does not make a table
# a revenue breakdown -- an order book, a customer list and an expense table do too).
_COST_NAME = re.compile(r"(원가|비용|인건비|급여|상여|퇴직|복리후생|상각비|임차료|판매비|관리비|연구개발비|경상개발비|"
                        r"재고자산의변동|원재료|소모품)")
_ORDER_NAME = re.compile(r"(수주잔고|수주잔량|수주총액|수주액|기납품|계약잔액)")
_ROLL_NAME = re.compile(r"^(기초|기말|수주|증가|감소|증감)")
_CUSTOMER_NAME = re.compile(r"(주요고객|기타고객|고객[A-Z\d]|[A-Z]고객|매출처|거래처)")
_REVENUE_NAME = re.compile(r"(매출(?!원가|채권)|수익|판매)")
_ORDER_HEAD = re.compile(r"(수주|잔고|잔량|잔액|납품|납기|도급|완성공사)")
_CUSTOMER_HEAD = re.compile(r"(매출처|거래처|고객|판매처|납품처|발주처|수요처|공급처)")
_PRODUCT_HEAD = re.compile(r"(품목|제품|상품|서비스|용역|품명|유형|모델|차종|품종|공사|종류)")
# A text column describing the row's item, not naming items of its own.
_DESCRIPTION_HEAD = re.compile(r"(용도|특징|설명|비고|내용|상표|브랜드|기능|목표시장|수요|판매경로|소재지|회사)")
_SUBTOTAL = re.compile(r"(소계|부문계)$")
_CUSTOMER_AXIS = re.compile(r"(고객|매출처|거래처)(합계|총계)")
_CTX_SECTION = re.compile(r"매출(실적)?및수주(상황|현황)?|&cr;")
_CTX_ORDER = re.compile(r"(수주|잔고|잔량|납기|도급)")
_CTX_REVENUE = re.compile(r"(매출(?!처|원가|채권)|수익|판매실적)")
_CTX_CUSTOMER = re.compile(r"(주요\d*대?(매출처|거래처|고객|판매처|납품처)|(매출처|거래처|판매처|납품처|수요처)(별|명)|고객별)")
_CTX_COST = re.compile(r"(원가|비용|판매비|관리비|상각|급여|인건비|매입|원재료|생산실적|생산능력|가동률|가격|단가)")
_CTX_PRODUCT = re.compile(r"(제품|품목|상품|서비스|용역|사업|부문|유형|브랜드|상표|품종|차종)")
_FOOTNOTE = re.compile(r"^(※|\*|\(\*|주\d*\)|\(주\d*\)|\[주)")
# Period marks of a header label or a short caption ("전기", "제19(전)기", "당분기 3개월").
# ("전기" also starts 전기전자 / 전기차: it needs a period-like continuation.)
_PREV_MARK = re.compile(r"\(전\)|(?:^|[^가-힣]|\d기)전(?:분기|반기|기(?=말|누적|누계|매출|금액|수익|실적|$|[^가-힣]))"
                        r"|전년(?:동기|도)?|직전")
_CUR_MARK = re.compile(r"\(당\)|(?:^|[^가-힣]|\d기)당(?:분기|반기|기(?=말|누적|누계|매출|금액|수익|실적|$|[^가-힣]))")
_TERM = re.compile(r"제(\d+)(?:\([당전]\))?기")
_YEAR = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")


# ---- names ------------------------------------------------------------------------

def normalise(name: str) -> str:
    """The draft segment_code of a reported name: no spaces, item numbers,
    note marks, a trailing "부문" / "사업부문" / "사업부" / "사업" (so "CE부문",
    "CE 부문" and "CE사업부" agree) or parenthesised remarks."""
    n = _SPLIT.sub("", html.unescape(name or ""))
    n = re.sub(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩIVX\d]+[.)]", "", n)
    n = re.sub(r"\((주|주석|\*|Note)[^)]*\)|\(\*\d*\)|\*\d*$", "", n)
    n = re.sub(r"\([^)]*\)$", "", n)
    n = re.sub(r",(영업|보고)?부문$", "", n)          # "기타, 영업부문" (an XBRL label) is "기타"
    for suffix in ("사업부문", "사업본부", "사업부", "부문", "사업"):
        if len(n) > len(suffix) and n.endswith(suffix):
            n = n[: -len(suffix)]
            break
    return n or (name or "").strip()


def _clean(name: str) -> str:
    return docs._text(name)


# ---- candidates and the check -----------------------------------------------------

class Candidate:
    """One way of reading a table (or the XBRL facts): segments, adjustments,
    the table's total, and where it came from."""

    def __init__(self, source: str, items, adjust=(), total=None, label: str = "", ops=None,
                 when: str | None = None, span: str | None = None, months: int | None = None):
        self.source = source
        self.items = [(n, v) for n, v in items if v is not None]
        self.adjust = [(n, v) for n, v in adjust if v is not None]
        self.total = total
        self.label = label
        self.ops = ops or {}
        # The period the amounts were disclosed for (review_codex 5 P1): `when`
        # "cur" / "prev" (a 전기 column or table), `span` "q" (3 months) / "ytd"
        # (cumulative), `months` the length of an XBRL context. None: not stated.
        self.when = when
        self.span = span
        self.months = months

    def bases(self, fiscal_q: int | None = None) -> tuple:
        """The revenue bases ("q", "ytd") the candidate may be checked against:
        its own period's only. A prior-period column has none; a 3-month figure
        is the cumulative one as well in the first quarter."""
        if self.when == "prev":
            return ()
        if self.months is not None:
            if self.months == 3:
                return ("q", "ytd") if fiscal_q == 1 else ("q",)
            return ("ytd",) if fiscal_q is None or self.months == 3 * fiscal_q else ()
        if self.span == "q":
            return ("q", "ytd") if fiscal_q == 1 else ("q",)
        if self.span == "ytd":
            return ("ytd",)
        return ("q", "ytd")

    def sums(self) -> tuple[float, float]:
        s = sum(v for _, v in self.items)
        return s, s + sum(v for _, v in self.adjust)

    def geographic(self) -> bool:
        """Whether most items are regions (a geographic breakdown, which the
        segment note often shows next to the operating segments)."""
        names = [n.replace(" ", "") for n, _ in self.items]
        geo = sum(1 for n in names if any(w in n for w in _GEO))
        return bool(names) and geo * 2 > len(names)

    def balance_items(self) -> bool:
        """Whether an item is a balance-sheet figure ("예금잔액", "차입금",
        "부채비율": a capital-management table that happened to add up to the
        quarter's revenue, HD현대일렉트릭 2025)."""
        return any(_BALANCE.search(n.replace(" ", "")) for n, _ in self.items)

    def cost_items(self) -> bool:
        """Whether half the items or more are costs ("인건비", "상품매출원가",
        "감가상각비": an expenses-by-nature table, SK바이오사이언스 2021)."""
        costs = sum(1 for n, _ in self.items if _COST_NAME.search(n.replace(" ", "")))
        return bool(self.items) and costs * 2 >= len(self.items)

    def order_items(self) -> bool:
        """Whether the items are an order book ("수주잔고", "기초 / 수주 / 기말")."""
        names = [n.replace(" ", "") for n, _ in self.items]
        return any(_ORDER_NAME.search(n) for n in names) or \
            (bool(names) and sum(1 for n in names if _ROLL_NAME.match(n)) * 2 >= len(names))

    def customers(self) -> bool:
        """Whether half the items or more are customers ("주요고객 A사", "매출처 B")."""
        hits = sum(1 for n, _ in self.items if _CUSTOMER_NAME.search(n.replace(" ", "")))
        return bool(self.items) and hits * 2 >= len(self.items)

    def revenue_named(self) -> bool:
        """Whether most items are named as revenue ("제품매출", "용역수익")."""
        hits = sum(1 for n, _ in self.items if _REVENUE_NAME.search(n.replace(" ", "")))
        return bool(self.items) and hits * 2 > len(self.items)

    def not_revenue(self) -> bool:
        """Read from a table that is no revenue breakdown at all, whatever it adds up to."""
        return self.balance_items() or self.cost_items() or self.order_items()

    def entities(self) -> bool:
        """Whether most items are companies (a breakdown by subsidiary)."""
        ents = sum(1 for n, _ in self.items if _ENTITY.search(n))
        return bool(self.items) and ents * 2 > len(self.items)

    def sales_types(self) -> bool:
        """Whether most items are kinds of sale (제품 / 상품 / 용역 / 기타), not
        business lines -- a breakdown the business section shows next to them."""
        def is_kind(name: str) -> bool:
            n = re.sub(r"\([^)]*\)", "", re.sub(r"\s", "", name))
            return bool(n) and (n in _SALES_KINDS or not _KIND_WORDS.sub("", n))
        kinds = sum(1 for n, _ in self.items if is_kind(n))
        return bool(self.items) and kinds * 2 > len(self.items)

    def check(self, revenue_q, revenue_ytd, fiscal_q: int | None = None):
        """(basis, residual) when the candidate reconciles with revenue, else None.
        A breakdown needs two items at least: a one-row table equal to the
        revenue tells nothing (the single-segment case is decided apart).
        Only the bases of the candidate's own period are tried (`bases`); where
        both are open, the closer one wins -- a half-year table of 100 against
        a quarter of 99.5 and a half year of 100 is cumulative (review_codex 5
        P1) -- and a plain sum before one that needs a residual."""
        if self.source != "single" and len(self.items) < 2:
            return None
        if self.not_revenue():
            return None
        _seg, with_adj = self.sums()
        hits = []
        for order, basis in enumerate(self.bases(fiscal_q)):
            rev = revenue_q if basis == "q" else revenue_ytd
            if not rev:
                continue
            if abs(with_adj - rev) <= abs(rev) * TOLERANCE:
                hits.append((0, abs(with_adj - rev) / abs(rev), order, basis, 0.0))
            elif self.total is not None and abs(self.total - rev) <= abs(rev) * TOLERANCE and len(self.items) >= 2 \
                    and abs(self.total - with_adj) <= abs(rev) * MAX_RESIDUAL_SHARE:
                hits.append((1, abs(self.total - rev) / abs(rev), order, basis, self.total - with_adj))
        if not hits:
            return None
        best = min(hits)
        return best[3], best[4]


def choose(candidates: list[Candidate], revenue_q, revenue_ytd, fiscal_q: int | None = None):
    """(candidate, basis, residual, status) by plan 5-3; (None, None, 0, 'review')
    when there is nothing to choose from. `fiscal_q` (1-4) is the report's
    quarter: it tells which XBRL period is the cumulative one."""
    # Operating segments before a geographic breakdown of the same note, then
    # the order read (segment tables before product tables, the current period
    # first). A filer whose segments are regions still gets them when nothing
    # else reconciles.
    candidates = sorted(candidates, key=lambda c: (c.geographic(), c.source == "html_product"))
    for c in candidates:
        hit = c.check(revenue_q, revenue_ytd, fiscal_q)
        if hit:
            return c, hit[0], hit[1], "ok"
    # Nothing reconciles: the first reading of the current period is kept as a
    # mismatch. A prior-period column, a customer list or a cost table is not
    # the period's segments even as a mismatch.
    usable = [c for c in candidates if len(c.items) >= 2 and not c.not_revenue() and not c.customers()
              and c.bases(fiscal_q)]
    if usable:
        return usable[0], None, 0.0, "sum_mismatch"
    return None, None, 0.0, "review"


# ---- XBRL -------------------------------------------------------------------------

_CTX = re.compile(r"<xbrli:context id=\"([^\"]+)\">(.*?)</xbrli:context>", re.S)
_DIM = re.compile(r"<xbrldi:explicitMember dimension=\"([^\"]+)\">([^<]+)</xbrldi:explicitMember>")
_PER = re.compile(r"<xbrli:(startDate|endDate|instant)>([^<]+)<")
_FACT = re.compile(r"<(ifrs-full:Revenue|ifrs-full:RevenueFromContractsWithCustomers|dart:OperatingIncomeLoss)\b"
                   r"([^>]*)>([^<]*)<")


_ATTR = re.compile(r"([\w:]+)=\"([^\"]*)\"")
_LABEL_DATES = re.compile(r"(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})")


def _attrs(tag: str) -> dict:
    return dict(_ATTR.findall(tag))


def _labels(lab: str) -> dict:
    """{element id: Korean standard label} from a label linkbase (attributes
    in any order), without the "[구성요소]" / "[member]" mark of a member."""
    locs, arcs, labels = {}, {}, {}
    for m in re.finditer(r"<link:loc\b([^>]*)/?>", lab):
        a = _attrs(m.group(1))
        if "xlink:label" in a and "xlink:href" in a:
            locs[a["xlink:label"]] = a["xlink:href"].split("#")[-1]
    for m in re.finditer(r"<link:labelArc\b([^>]*)/?>", lab):
        a = _attrs(m.group(1))
        if "xlink:from" in a and "xlink:to" in a:
            arcs.setdefault(a["xlink:to"], a["xlink:from"])
    for m in re.finditer(r"<link:label\b([^>]*)>([^<]*)</link:label>", lab):
        a = _attrs(m.group(1))
        if not a.get("xlink:role", "").endswith("/label"):
            continue
        loc = arcs.get(a.get("xlink:label", ""))
        if loc in locs:
            text = re.sub(r"\s*\[[^\]]*\]$", "", html.unescape(m.group(2))).strip()
            labels.setdefault(locs[loc], text)
    return labels


def _months(start: str, end: str) -> int | None:
    """Length of an XBRL duration in whole months ("2025-01-01", "2025-06-30": 6)."""
    try:
        (y1, m1, d1), (y2, m2, d2) = ([int(x) for x in d[:10].split("-")] for d in (start, end))
    except ValueError:
        return None
    n = (y2 - y1) * 12 + (m2 - m1) + (1 if d2 >= d1 else 0)
    return n if n > 0 else None


def xbrl_candidates(zip_bytes: bytes, fs_div: str) -> list[Candidate]:
    """Segment candidates of an XBRL package: one per period (3-month and
    cumulative) ending on the report's period end."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        inst = next((n for n in names if n.endswith(".xbrl")), None)
        if inst is None:
            return []
        x = zf.read(inst).decode("utf-8", errors="replace")
        lab_name = next((n for n in names if n.endswith("lab-ko.xml")), None)
        labels = _labels(zf.read(lab_name).decode("utf-8", errors="replace")) if lab_name else {}
    want = "ConsolidatedMember" if fs_div == "CFS" else "SeparateMember"
    contexts = {}
    for m in _CTX.finditer(x):
        dims = dict(_DIM.findall(m.group(2)))
        per = dict(_PER.findall(m.group(2)))
        if "startDate" not in per:
            continue
        cs = dims.pop("ifrs-full:ConsolidatedAndSeparateFinancialStatementsAxis", None)
        if cs and not cs.endswith(want):
            continue
        seg_axes = {k: v for k, v in dims.items()
                    if k in ("ifrs-full:SegmentsAxis", "ifrs-full:SegmentConsolidationItemsAxis")}
        if len(seg_axes) != len(dims) or not seg_axes:
            continue                               # another axis (products, regions...) or no segment at all
        entity = [v for v in seg_axes.values() if not v.startswith("ifrs-full:")]
        ifrs = [v.split(":")[1] for v in seg_axes.values() if v.startswith("ifrs-full:")]
        if entity:
            kind, member = "segment", entity[0]
            fam = re.search(r"Of([A-Za-z0-9]+?TableOf)Member$", member.split(":")[-1])
            family = fam.group(1) if fam else ""
        elif any(i in _ADJ_MEMBERS for i in ifrs):
            kind, member = "adjust", next(i for i in ifrs if i in _ADJ_MEMBERS)
            family = None                          # an adjustment belongs to whichever table it reconciles
        else:
            continue                               # the segments' own subtotal
        contexts[m.group(1)] = (kind, member, per["startDate"], per["endDate"], family)
    by_period: dict = {}
    adjust: dict = {}
    for m in _FACT.finditer(x):
        ref = re.search(r"contextRef=\"([^\"]+)\"", m.group(2))
        if not ref or ref.group(1) not in contexts:
            continue
        kind, member, start, end, family = contexts[ref.group(1)]
        val = docs._amount(m.group(3))
        if val is None:
            continue
        if re.search(r"sign=\"-\"", m.group(2)):
            val = -val
        if kind == "adjust":
            name = _ADJ_NAMES.get(member, member)
            if m.group(1) != "dart:OperatingIncomeLoss":
                adjust.setdefault((start, end), {})[name] = val
            continue
        slot = by_period.setdefault((start, end, family), {"rev": {}, "op": {}})
        name = labels.get(member.replace(":", "_"), member.split(":")[-1])
        if m.group(1) == "dart:OperatingIncomeLoss":
            slot["op"][name] = val
        elif name not in slot["rev"] or m.group(1) == "ifrs-full:Revenue":
            slot["rev"][name] = val              # Revenue wins over RevenueFromContractsWithCustomers
    if not by_period:
        return []
    last_end = max(end for _s, end, _f in by_period)
    out = []
    for (start, end, family), slot in sorted(by_period.items(), key=lambda kv: kv[0][0], reverse=True):
        if end != last_end or not slot["rev"]:
            continue
        adj = list(adjust.get((start, end), {}).items())
        # One table of segments (Hyundai shows gross and net revenue tables with
        # the same member names): each table on its own, with and without the
        # reconciling items; the check keeps the one that adds up.
        for with_adj in ((True, False) if adj else (False,)):
            out.append(Candidate("xbrl", list(slot["rev"].items()), adj if with_adj else [],
                                 label=f"{start}..{end} {family or ''}".strip(), ops=slot["op"],
                                 months=_months(start, end)))
    return out


# ---- documents --------------------------------------------------------------------

def _notes_range(doc: str, consolidated: bool) -> tuple[int, int]:
    """(start, end) of the (consolidated or separate) notes in a document;
    the other kind when the wanted one is not there."""
    found = _notes_range_of(doc, consolidated)
    return found if found[1] > found[0] else _notes_range_of(doc, not consolidated)


def _notes_range_of(doc: str, consolidated: bool) -> tuple[int, int]:
    titles = [(m.start(), _SPLIT.sub("", docs._text(m.group(1)))) for m in docs._TITLE.finditer(doc)]
    key = "연결재무제표주석" if consolidated else "재무제표주석"
    start = None
    for i, (pos, t) in enumerate(titles):
        if t.endswith(key) and (consolidated or "연결" not in t):
            start = pos
            # The notes end at the next top-level item ("4.재무제표", "IV.이사의...").
            for pos2, t2 in titles[i + 1:]:
                if re.match(r"^(\d+|[IVX]+)\.", t2) and not re.search(r"\((연결|별도)\)$", t2) \
                        and not re.match(r"^\d+\.\d", t2) and ("주석" in t2 or "재무제표" in t2 or t2[:2] in ("IV", "V.")):
                    return start, pos2
            return start, len(doc)
    return 0, 0


MAX_REGIONS = 4


def segment_regions(doc: str, consolidated: bool) -> list[str]:
    """Places that may hold the segment note, most likely first: note titles
    ("4.영업부문(연결)"), headings inside the notes text ("29.부문별보고",
    also split over spans), and as a last resort the text around a statement
    that there is a single segment. Each is the heading plus the tables that
    follow (~80k characters at most). An accounting-policy item about segments
    ("저. 영업부문") also looks like a heading, so several places are kept and
    their tables all become candidates -- the check picks."""
    start, end = _notes_range(doc, consolidated)
    if end <= start:
        return []
    starts: list[int] = []
    for m in docs._TITLE.finditer(doc, start, end):
        t = _SPLIT.sub("", docs._text(m.group(1)))
        if _HEADING.search(t) and len(t) < 40:
            nxt = docs._TITLE.search(doc, m.end(), end)
            return [doc[m.start():nxt.start() if nxt else min(end, m.end() + 80_000)]]
    for m in _BLOCK.finditer(doc, start, end):
        if _NOTE_HEADING.match(_SPLIT.sub("", docs._text(m.group(2)))):   # "29.부문별보고:..." may run on
            starts.append(m.start())
    starts += [m.start() for m in _RAW_HEADING.finditer(doc, start, end)]
    out, last = [], -1
    for pos in sorted(set(starts)):
        if pos < last:
            continue                          # inside a region already taken
        out.append(doc[pos:min(end, pos + 80_000)])
        last = pos + 80_000
        if len(out) >= MAX_REGIONS:
            break
    notes = doc[start:end]
    for m in re.finditer(r"단일|하나의", notes):
        around = _SPLIT.sub("", docs._text(notes[m.start():m.start() + 400]))
        if _SINGLE.search(around):
            out.append(notes[max(0, m.start() - 200):m.start() + 2000])
            break
    return out


def segment_region(doc: str, consolidated: bool) -> str:
    """The most likely place of the segment note (see segment_regions)."""
    regions = segment_regions(doc, consolidated)
    return regions[0] if regions else ""


def _unit_before(region: str, pos: int) -> float:
    found = re.findall(r"단위[:：]?([^)\]]{1,15})", _SPLIT.sub("", docs._text(region[max(0, pos - 3000):pos])))
    u = found[-1] if found else ""
    return 1e8 if "억" in u else 1e6 if "백만" in u else 1e3 if "천" in u else 1.0


def _header_split(grid):
    first = next((i for i, row in enumerate(grid)
                  if any(docs._amount(b) is not None for a, _e, b in row if a > 0)), len(grid))
    return grid[:first], grid[first:]


def _column_labels(header, ncols):
    """Per column: the path of header labels above it. A unit caption
    ("(단위:백만원)") or a remark row inside the header is not a label."""
    paths = [[] for _ in range(ncols)]
    for row in header:
        for a, b, body in row:
            t = _clean(body)
            if not t or "단위" in t or t.startswith("(*"):
                continue
            for c in range(a, min(b, ncols - 1) + 1):
                paths[c].append(t)
    return paths


def _data_tables(region: str):
    """(table match, context) of a region's tables. The context is the last two
    captions or sentences before the table (their last 80 characters, without
    unit captions and footnotes): "가. 매출실적", "(2) 수주현황", "<전기>". A
    one-row table is a caption itself (DART wraps "(단위: 백만원)" in one)."""
    prev_end = 0
    for tm in docs._TABLE.finditer(region):
        if len(docs._TR.findall(tm.group(0))) < 2:
            continue
        blocks = [docs._text(b) for _tag, b in _BLOCK.findall(region[prev_end:tm.start()])]
        keep = [b for b in blocks if b and not ("단위" in b and len(b) < 40) and not _FOOTNOTE.match(b)]
        yield tm, [_CTX_SECTION.sub("", b)[-80:] for b in keep[-2:]]
        prev_end = tm.end()


def _context_kind(context: list[str]) -> str | None:
    """What the text before a table says it is: "customer", "order", "cost"
    (expenses, purchases, prices), "product" or "revenue"; None when it says
    nothing. The nearest caption that says anything decides."""
    for t in reversed(context):
        if _CTX_CUSTOMER.search(t):
            return "customer"
        revenue = bool(_CTX_REVENUE.search(t))
        if _CTX_ORDER.search(t) and not revenue:
            return "order"
        if _CTX_COST.search(t) and not revenue:
            return "cost"
        if _CTX_PRODUCT.search(t):
            return "product"
        if revenue:
            return "revenue"
    return None


def _marks(labels) -> tuple:
    """(when, span, term, year) of a column's header labels: "prev" / "cur"
    from 전기 / 당기 marks, "q" / "ytd" from "3개월" / "누적", the fiscal term
    ("제58기") and the calendar year named."""
    when = span = term = year = None
    for raw in labels:
        t = (raw or "").replace(" ", "")
        if _PREV_MARK.search(t):
            when = "prev"
        elif _CUR_MARK.search(t) and when is None:
            when = "cur"
        if "3개월" in t:
            span = "q"
        elif re.search(r"(누적|누계|[69]개월|12개월)", t) and span is None:
            span = "ytd"
        m = _TERM.search(t)
        if m and term is None:
            term = int(m.group(1))
        m = _YEAR.search(t)
        if m and year is None:
            year = int(m.group(1))
    return when, span, term, year


def _periods(marks: dict, context=(None, None)) -> dict:
    """{column: (when, span)} from each column's marks: a column of an older
    fiscal term or year than another column of the table is a prior period,
    and a column that says nothing takes the caption's ("<전기>" above the table)."""
    terms = [m[2] for m in marks.values() if m[2] is not None]
    years = [m[3] for m in marks.values() if m[3] is not None]
    out = {}
    for c, (when, span, term, year) in marks.items():
        if when is None:
            if term is not None and term < max(terms):
                when = "prev"
            elif term is None and year is not None and year < max(years):
                when = "prev"
        out[c] = (when or context[0], span or context[1])
    return out


def _context_period(context: list[str]) -> tuple:
    """(when, span) a short caption right before a table gives it: "(2) 전기",
    "[제79기 3분기 누계]", "<당분기 3개월>"."""
    if not context or len(context[-1]) > 25:
        return None, None
    when, span, _term, _year = _marks([context[-1]])
    return when, span


def table_candidates(table: str, unit: float, product: bool, caption=(None, None)) -> list[Candidate]:
    """Candidates of one table in both orientations (see the module docstring).
    A unit caption inside the table's own header wins over `unit` (the last
    one before the table). `caption`: the (when, span) the table's caption
    gives; a column's own header marks win over it."""
    grid = docs._grid(table)
    if len(grid) < 2:
        return []
    header, body = _header_split(grid)
    if not body:
        return []
    inner = re.findall(r"단위[:：]?([^)\]]{1,15})", _SPLIT.sub("", " ".join(docs._text(x) for row in header
                                                                          for _a, _b, x in row)))
    if inner:
        u = inner[-1]
        unit = 1e8 if "억" in u else 1e6 if "백만" in u else 1e3 if "천" in u else 1.0
    ncols = max((b for row in grid for _a, b, _x in row), default=0) + 1
    paths = _column_labels(header, ncols)
    source = "html_product" if product else "html_segment"
    out: list[Candidate] = []

    def cell(row, col):
        for a, b, x in row:
            if a <= col <= b:
                return x
        return None

    def val(row, col):
        x = cell(row, col)
        v = docs._amount(x) if x is not None else None
        return None if v is None else v * unit

    def row_name(row):
        return _clean(row[0][2]) if row and row[0][0] == 0 else ""

    # Orientation A: items are rows, one candidate per numeric column (revenue-like
    # header first; for a product table every amount column is a candidate).
    rows = [(row_name(r), r) for r in body if row_name(r)]
    cols = []
    for c in range(1, ncols):
        label = " ".join(paths[c])
        if any(val(r, c) is not None for _, r in rows):
            cols.append((c, label))
    rev_cols = [(c, lab) for c, lab in cols if _REVENUE_LABEL.search(lab) and not _NOT_REVENUE.search(lab)
                and not _INTERNAL.search(lab)]
    rev_cols.sort(key=lambda cl: 0 if _EXTERNAL.search(cl[1]) else 1)
    if not rev_cols and (product or any(t in " ".join(" ".join(p) for p in paths) for t in _PRODUCT_WORDS)):
        rev_cols = [(c, lab) for c, lab in cols if not _NOT_REVENUE.search(lab)]
    op_col = next((c for c, lab in cols if _OP_LABEL.search(lab.replace(" ", ""))), None)
    col_period = _periods({c: _marks(paths[c]) for c, _lab in cols}, caption)
    first_value = min((a for row in body for a, _b, x in row if a > 0 and docs._amount(x) is not None),
                      default=ncols)
    tree = _row_paths(body, first_value)
    for c, lab in rev_cols if tree else ():
        entries = [(names, val(r, c), None if op_col is None else val(r, op_col)) for names, r in tree]
        items, adjust, total, ops = _tree_items([e for e in entries if e[1] is not None])
        if items:
            out.append(Candidate(source, items, adjust, total, label=f"col {lab}", ops=ops,
                                 when=col_period[c][0], span=col_period[c][1]))
    for c, lab in rev_cols if not tree else ():
        items, adjust, total = [], [], None
        ops = {}
        for name, r in rows:
            v = val(r, c)
            if v is None:
                continue
            if is_total(name):
                total = v if total is None else total
            elif _ADJ.search(name):
                adjust.append((name, v))
            else:
                items.append((name, v))
                if op_col is not None:
                    ov = val(r, op_col)
                    if ov is not None:
                        ops[name] = ov
        if items:
            out.append(Candidate(source, items, adjust, total, label=f"col {lab}", ops=ops,
                                 when=col_period[c][0], span=col_period[c][1]))

    # Orientation B: items are columns; one candidate per revenue-like row and
    # period. A column's path of header labels ends with its period ("3개월",
    # "누적", "당기"...) when the table shows several; the label before it is
    # the item (see _tree_items for totals and parts).
    columns = []
    for c in range(1, ncols):
        path = [p for p in paths[c] if p]
        if not path:
            continue
        period = tuple(p for p in path if _PERIOD.match(p.replace(" ", "")))
        names = [p for p in path if not _PERIOD.match(p.replace(" ", ""))]
        if not names:
            continue
        columns.append((c, names, period))
    rev_rows = [(n, r) for n, r in rows if _REVENUE_LABEL.search(n) and not _NOT_REVENUE.search(n)
                and not _INTERNAL.search(n)]
    rev_rows.sort(key=lambda nr: 0 if _EXTERNAL.search(nr[0]) else 1)
    op_row = next((r for n, r in rows if _OP_LABEL.search(n.replace(" ", ""))), None)
    periods = sorted({p for _c, _n, p in columns}, key=lambda p: [x for x in p])
    if any(_CUSTOMER_AXIS.search(n.replace(" ", "")) for _c, names, _p in columns for n in names):
        periods = []                               # columns are customers ("... | 고객합계"), not segments
    row_period = _periods({p: _marks(p) for p in periods}, caption)
    for rname, r in rev_rows:
        for period in periods:
            entries = [(names, val(r, c), None if op_row is None else val(op_row, c))
                       for c, names, p in columns if p == period]
            items, adjust, total, ops = _tree_items([e for e in entries if e[1] is not None])
            if items:
                out.append(Candidate(source, items, adjust, total, label=f"row {rname} {' '.join(period)}".strip(),
                                     ops=ops, when=row_period[period][0], span=row_period[period][1]))
    return out


def _is_axis(name: str) -> bool:
    return name.replace(" ", "") in _AXIS_WORDS or is_total(name)


def _real_parents(names) -> list[str]:
    """The segment-like labels above a path's leaf (axis words, totals and
    elimination groups are not segments)."""
    return [n for n in names[:-1] if not _is_axis(n) and not _ADJ.search(n)]


def _tree_items(entries) -> tuple[list, list, float | None, dict]:
    """(items, adjust, total, ops) of one period's amounts, each given with the
    path of labels that names it -- a column's header path, or a row's labels
    across the table's label columns (_row_paths). A total at the top ("계(*)",
    "지역합계") is the table's total; a total under a parent ("DS부문 > 계",
    "KUS > 기업전체총계합계") is that parent's own amount, and the parent's other
    entries -- its parts (반도체, DP), its gross amount and its own eliminations
    (기아: "KUS > 기업전체총계 > 영업부문 | 중요한조정사항 > 부문간제거한금액") --
    are already in it, not segments or adjustments of the table."""
    parent_totals = set()
    for names, _v, _o in entries:
        if is_total(names[-1]) and not any(_ADJ.search(n) for n in names):
            rp = _real_parents(names)
            if rp:
                parent_totals.add(rp[-1])
    items, adjust, adj_total, total, ops = [], [], None, None, {}
    for names, v, ov in entries:
        leaf = names[-1]
        in_adj = any(_ADJ.search(n) for n in names)
        rp = _real_parents(names)
        if is_total(leaf):
            if in_adj:
                adj_total = v                      # an elimination group's own total
            elif rp:
                items.append((rp[-1], v))          # a segment's own total
                if ov is not None:
                    ops[rp[-1]] = ov
            else:
                total = v                          # the last total: the grand total
            continue
        if any(p in parent_totals for p in rp):
            continue                               # part of a segment shown with its total
        if in_adj:
            adjust.append((f"{leaf} 제거" if not _ADJ.search(leaf) else leaf, v))
            continue
        if _is_revenue_caption(leaf):
            continue                               # "외부고객으로부터의 수익" over the columns, not a segment
        name = rp[-1] if _is_axis(leaf) and rp else leaf     # "KUS > 영업부문": the segment is KUS
        items.append((name, v))
        if ov is not None:
            ops[name] = ov
    if adj_total is not None and not adjust:
        adjust = [("부문간 제거", adj_total)]
    return items, adjust, total, ops


def _row_paths(body, first_value: int) -> list:
    """(labels, row) of each body row when the row labels run over several
    columns, a level per column with row spans (기아's segment note 2025 1-3Q:
    "지역 | 외국 | 북중미 | KUS | 기업전체총계 | 영업부문 | amount"); a label is
    carried down to the rows it spans and a new label at a level clears the
    deeper ones. None when the labels sit in the first column only."""
    stack: dict[int, str] = {}
    out, deep = [], False
    for row in body:
        labels = [(a, b, _clean(x)) for a, b, x in row if b < first_value and _clean(x)]
        for a, _b, _t in labels:
            for k in [k for k in stack if k >= a]:
                del stack[k]
        for a, _b, t in labels:
            stack[a] = t
        deep = deep or any(a > 0 for a, _b, _t in labels) or len(labels) > 1
        if stack:
            out.append(([stack[k] for k in sorted(stack)], row))
    return out if deep else None


def document_candidates(region: str) -> tuple[list[Candidate], bool]:  # one region
    """(candidates of the segment note's tables, the note says a single segment)."""
    if not region:
        return [], False
    text = _SPLIT.sub("", docs._text(region[:20_000]))
    single = bool(_SINGLE.search(text))
    out = []
    for tm, context in _data_tables(region):
        # The single-segment sentence tells how many segments the company has,
        # not what a table is (review_codex 5 P1: the region runs on into the
        # next notes, and the expenses-by-nature table was read as products).
        # Each table is judged by its own caption and item names.
        kind = _context_kind(context)
        if kind in ("customer", "order", "cost"):
            continue
        before = _SPLIT.sub("", docs._text(region[max(0, tm.start() - 600):tm.start()]))
        product = bool(_PRODUCT_CONTEXT.search(before[-150:])) or (single and kind in ("product", "revenue"))
        unit, caption = _unit_before(region, tm.start()), _context_period(context)
        found = table_candidates(tm.group(0), unit, product, caption)
        if single and not product:
            # No caption to go by: a table whose rows are named as revenue
            # ("제품매출 | 상품매출 | 용역매출") is still the single segment's breakdown.
            have = {c.label for c in found}
            found += [c for c in table_candidates(tm.group(0), unit, True, caption)
                      if c.label not in have and c.revenue_named()]
        out.extend(c for c in found if not c.customers())
    # Segment tables before product tables: plan 5-1 order.
    out.sort(key=lambda c: 0 if c.source == "html_segment" else 1)
    return out, single


# ---- business section ("II. 사업의 내용") ----------------------------------------------
#
# Plan 5-1's third source, made the first choice for a product breakdown (user
# direction 2026-11-27): "2. 주요 제품 및 서비스" and "4. 매출 및 수주상황" list
# revenue by business segment and often by product line, as the company itself
# divides its business -- where the notes have a single segment (SK hynix, HD
# Hyundai Electric) or the reader took a geographic table for segments. The
# tables have label columns on the left (사업부문 | 매출유형 | 품목 | 수출/내수)
# with row spans, and amount columns per period (the current one cumulative).
# Every label column gives a candidate (items keyed by that column), and the
# usual check against the income statement's revenue decides.

_BIZ_TITLE = re.compile(r"(주요제품|매출및수주|매출에관한사항|매출실적)")
_SEGMENT_HEAD = re.compile(r"(부문|사업)")
# Kinds of sale ("제ㆍ상품", "용역 및 기타매출", "제품(화장품)") and sales channels
# ("직접판매 / 간접판매"): a breakdown by kind, not by business line. Names are
# split on , ㆍ · / 및 first, a parenthesised remark dropped.
# A name made of these words only ("상품제품기타", "용역및기타수수료", "배당금수익") is a kind.
_KIND_WORDS = re.compile(r"(제품|상품|용역|기타|서비스|매출액|매출|수익|공사|임대료|임대|로열티|수수료|배당금|배당|"
                         r"브랜드사용료|사용료|이자|제|상|등|외|및|[,ㆍ·/&])")
_SALES_KINDS = {"제", "상", "제품", "상품", "용역", "기타", "서비스", "공사", "임대", "로열티", "수수료", "제품상품",
                "제품상품기타", "제품기타", "상품기타", "제상품", "제품외", "상품외", "직접판매", "간접판매", "직판",
                "대리점", "위탁판매", "온라인", "오프라인", "도매", "소매", "직수출", "로컬", "로컬수출"}
_BALANCE = re.compile(r"(잔액|차입금|부채비율|예금|현금성자산|사채|순부채|자본총계|부채총계|자산총계)")
_ENTITY = re.compile(r"(㈜|\(주\)|주식회사|Co\.|Ltd|Inc\.?$|LLC|Corp|GmbH|B\.V\.|S\.A\.)", re.I)
_LEADING_MARKS = re.compile(r"^[-–·•ㆍ\s]+")
_PCT_TAIL = re.compile(r"\(\s*-?[\d.,]+\s*%\s*\)$")        # "(64.7%)" after an amount; "(50)" is a negative


def biz_regions(doc: str) -> list[str]:
    """The business section's "주요 제품 및 서비스" and "매출 및 수주상황"
    sections (title to the next title), before the financial notes."""
    titles = list(docs._TITLE.finditer(doc))
    notes_at = min((s for s in (_notes_range_of(doc, True)[0], _notes_range_of(doc, False)[0]) if s > 0),
                   default=len(doc))
    out = []
    for i, m in enumerate(titles):
        if m.start() >= notes_at:
            break
        t = _SPLIT.sub("", docs._text(m.group(1)))
        if _BIZ_TITLE.search(t) and len(t) < 40:
            end = titles[i + 1].start() if i + 1 < len(titles) else min(len(doc), m.end() + 80_000)
            out.append(doc[m.start():min(end, m.start() + 120_000)])
    return out


def _matrix(table: str) -> list[list[str]]:
    """The table as a full text matrix: a cell spanning rows or columns is
    repeated in every position it covers (so a row knows its segment)."""
    return [[body for body, _origin in row] for row in _matrix_cells(table)]


def _matrix_cells(table: str) -> list[list[tuple]]:
    """_matrix with every position as (cell html, origin): the origin (row,
    column of the source cell) is the same in all the positions one spanning
    cell covers, so an amount cell spanning rows can be counted once."""
    rows, carry = [], {}                 # carry: column -> [cell, rows left]
    for tr in docs._TR.findall(table):
        row: dict = {}
        col = 0
        for attrs, body in docs._CELL_TAG.findall(tr):
            while col in carry:
                row[col] = carry[col][0]
                carry[col][1] -= 1
                if carry[col][1] <= 0:
                    del carry[col]
                col += 1
            spans = {k.upper(): int(v) for k, v in docs._SPAN.findall(attrs)}
            width, height = max(1, spans.get("COLSPAN", 1)), max(1, spans.get("ROWSPAN", 1))
            cell = (body, (len(rows), col))
            for c in range(col, col + width):
                row[c] = cell
                if height > 1:
                    carry[c] = [cell, height - 1]
            col += width
        for c in sorted(k for k in carry if k >= col):
            row[c] = carry[c][0]
            carry[c][1] -= 1
            if carry[c][1] <= 0:
                del carry[c]
        n = max(row) + 1 if row else 0
        rows.append([row.get(c, ("", None)) for c in range(n)])
    width = max((len(r) for r in rows), default=0)
    return [r + [("", None)] * (width - len(r)) for r in rows]


def _biz_amount(cell: str):
    """An amount, also "2,173,111(64.7%)" (amount with its share); a share
    alone ("30.5%") is not an amount."""
    t = docs._text(cell)
    if "%" in t and not _PCT_TAIL.search(t):
        return None
    return docs._amount(_PCT_TAIL.sub("", t))


def _biz_name(text: str) -> str:
    return _LEADING_MARKS.sub("", docs._text(text))


def _block_value(block: dict) -> float:
    """Revenue of one block of rows of an item: its own total row, else its
    sub-totals (the outermost ones) plus the rows no sub-total covers."""
    if block["total"] is not None:
        return block["total"]
    subs = block["subs"]
    tops = [p for p in subs if not any(q != p and p[:len(q)] == q for q in subs)]
    return sum(subs[p] for p in tops) + sum(v for sub, v in block["leaves"]
                                            if not any(sub[:len(p)] == p for p in tops))


def biz_table_candidates(table: str, unit: float, context=(), section_title: str = "") -> list[Candidate]:
    """Candidates of one business-section table: per label column (the item
    level) and amount column (the period).

    What is left out before any check against revenue (review_codex 5 P1):
    an order-book table or column (수주총액 / 기납품액 / 수주잔고, told by the
    column's whole header path or the caption before the table), a price or
    purchase table, a customer table (caption "주요 매출처", name column headed
    매출처 / 고객 / 거래처) and a name column with a header that says nothing
    ("구분") unless the caption, the section title or another label column
    says the table is about products, services or business lines. An amount
    column of an older fiscal term or year than another one is a prior period.

    An item's revenue is the sum over its blocks of rows (review_codex 5 P2:
    one product sold by two segments is two blocks); in a block the item's own
    total row is taken ("수출 | 내수 | 합계"), else its rows are added up, an
    amount cell spanning rows once."""
    cells = _matrix_cells(table)
    m = [[body for body, _origin in row] for row in cells]
    if len(m) < 2:
        return []
    first = next((i for i, r in enumerate(m) if any(_biz_amount(x) is not None for x in r[1:])), None)
    if first is None:
        return []
    kind = _context_kind(list(context))
    if kind in ("customer", "order", "cost"):
        return []
    header, body, body_cells = m[:first], m[first:], cells[first:]
    inner = re.findall(r"단위[:：]?([^)\]]{1,15})", _SPLIT.sub("", " ".join(docs._text(x) for r in header for x in r)))
    if inner:
        u = inner[-1]
        unit = 1e8 if "억" in u else 1e6 if "백만" in u else 1e3 if "천" in u else 1.0
    ncols = len(m[0])
    head_labels = [list(dict.fromkeys(_clean(r[c]) for r in header if _clean(r[c]) and "단위" not in _clean(r[c])))
                   for c in range(ncols)]
    heads = [" ".join(labels) for labels in head_labels]
    amount_cols = [c for c in range(ncols)
                   if sum(_biz_amount(r[c]) is not None for r in body) * 2 >= len(body)
                   and not re.search(r"(단가|가격|수량)", heads[c])
                   and not _ORDER_HEAD.search(heads[c])
                   and not (re.search(r"(비율|비중|구성비|%)", heads[c]) and not re.search(r"(매출|금액)", heads[c]))]
    if not amount_cols:
        return []
    label_cols = [c for c in range(min(amount_cols))]
    period = _periods({c: _marks(head_labels[c]) for c in amount_cols}, _context_period(list(context)))
    about_products = kind == "product" or bool(_CTX_PRODUCT.search(_SPLIT.sub("", section_title)))
    out: list[Candidate] = []
    n_segments: dict = {}                          # amount column -> items of the segment reading

    def is_sum(name: str) -> bool:
        return is_total(name) or bool(_SUBTOTAL.search(name))

    for k in label_cols:
        if _CUSTOMER_HEAD.search(heads[k]) or _DESCRIPTION_HEAD.search(heads[k]):
            continue                               # sales by customer, or a description of the row
        segment_level = bool(_SEGMENT_HEAD.search(heads[k]))
        if not segment_level and not _PRODUCT_HEAD.search(heads[k]) and not about_products \
                and not any(_PRODUCT_HEAD.search(heads[x]) or _SEGMENT_HEAD.search(heads[x])
                            for x in label_cols if x != k and not _CUSTOMER_HEAD.search(heads[x])):
            continue                               # "구분" and nothing saying what is listed
        source = "biz_segment" if segment_level else "biz_product"
        deeper = [c for c in label_cols if c > k]
        upper = [c for c in label_cols if c < k]
        for c in amount_cols:
            groups: dict = {}                      # item -> {labels left of it: block}
            total = None
            adjust: list = []
            counted = set()
            for r, rc in zip(body, body_cells):
                key = _biz_name(r[k])
                v = _biz_amount(r[c])
                if not key or v is None:
                    continue
                v *= unit
                sub = [s for s in (_biz_name(r[x]) for x in deeper) if s and s != key]
                if _SUBTOTAL.search(key) or not re.sub(r"\([^)]*\)", "", key):
                    continue                       # a subtotal (its lines are rows of their own) or a mark "(가)"
                if is_total(key) or _is_revenue_caption(re.sub(r"^연결(기준)?", "", key)):
                    if total is None and (not sub or is_total(sub[-1])):
                        total = v                  # "합계 | 수출" is a part of the total, "합계 | 합계" the total
                    continue
                if any(_ADJ.search(p) for p in [key, *sub]):
                    adjust.append((key, v))
                    continue
                origin = rc[c][1]
                if origin in counted:
                    continue                       # one amount cell spanning several rows
                counted.add(origin)
                parent = tuple(_biz_name(r[x]) for x in upper)
                block = groups.setdefault(key, {}).setdefault(parent, {"total": None, "subs": {}, "leaves": []})
                cut = next((i for i, name in enumerate(sub) if is_sum(name)), None)
                if cut is None:
                    block["leaves"].append((tuple(sub), v))
                elif not all(is_sum(name) for name in sub[cut:]):
                    continue                       # "합계 | 수출": a part of a sub-total, its rows are counted
                elif cut == 0:
                    if block["total"] is None:
                        block["total"] = v
                else:
                    block["subs"].setdefault(tuple(sub[:cut]), v)
            items = []
            for name, blocks in groups.items():
                if name.replace(" ", "") in _GEO:
                    continue                       # "국내 / 해외" rows among products: a split of a line
                v = sum(_block_value(b) for b in blocks.values())
                # A negative line ("기타" under the segments: consolidation adjustments) is no business.
                (adjust if v < 0 else items).append((name, v))
            if segment_level:
                n_segments[c] = max(n_segments.get(c, 0), len(items))
            elif "주요" in heads[k] and len(items) < n_segments.get(c, 0):
                continue                           # "주요제품" beside the segments: fewer names than segments,
                #                                    a description shared by some of them, not a finer breakdown
            if len(items) >= 2:
                out.append(Candidate(source, items, adjust, total, label=f"biz {heads[k]} / {heads[c]}".strip(),
                                     when=period[c][0], span=period[c][1]))
    # The same breakdown read through a second label column (품목 next to its
    # 부문, one product list per segment) is the segment reading again.
    seen, unique = set(), []
    for cand in out:
        sig = (tuple(round(v) for _n, v in cand.items), cand.label.split(" / ")[-1])
        if sig not in seen:
            seen.add(sig)
            unique.append(cand)
    return unique


def biz_candidates(doc: str) -> list[Candidate]:
    """Candidates of the business section's tables, a product breakdown
    (item column not headed 부문 / 사업) before a segment one."""
    out = []
    for region in biz_regions(doc):
        title = docs._TITLE.match(region)
        section = docs._text(title.group(1)) if title else ""
        for tm, context in _data_tables(region):
            out.extend(biz_table_candidates(tm.group(0), _unit_before(region, tm.start()), context, section))
    return [c for c in out if is_product_breakdown(c)]


def is_product_breakdown(c: Candidate) -> bool:
    """Whether a candidate's items are business lines or products: not regions,
    kinds of sale, companies, customers, or rows of a non-revenue table."""
    return not (c.geographic() or c.sales_types() or c.entities() or c.customers() or c.not_revenue())


def choose_biz(candidates: list[Candidate], revenue_q, revenue_ytd, source: str | None = None,
               fiscal_q: int | None = None):
    """(candidate, basis, residual) of the finest business-section breakdown
    that reconciles with revenue -- a product table first, then the one with
    more items -- or None. A product table may leave out minor lines
    (Samsung lists four product groups): their sum below revenue by at most
    MAX_RESIDUAL_SHARE is kept with the difference as a residual row."""
    best = None
    for c in candidates:
        if source is not None and c.source != source:
            continue
        hit = c.check(revenue_q, revenue_ytd, fiscal_q)
        if hit is None and c.source == "biz_product" and not c.adjust and c.total is None \
                and not c.not_revenue():
            seg = c.sums()[0]
            for basis in c.bases(fiscal_q):          # the candidate's own period only
                rev = revenue_q if basis == "q" else revenue_ytd
                if rev and rev > 0 and 0 < rev - seg <= rev * MAX_RESIDUAL_SHARE:
                    hit = (basis, rev - seg)
                    break
        if hit is None:
            continue
        rank = (c.source == "biz_product", hit[1] == 0, len(c.items))
        if best is None or rank > best[0]:
            best = (rank, c, hit)
    return (best[1], best[2][0], best[2][1]) if best else None


# ---- fetching ---------------------------------------------------------------------

def _cache_path(cache_dir: str, kind: str, rcept_no: str, version: int = PARSER_VERSION) -> str:
    return os.path.join(cache_dir, "segments", rcept_no[:4], f"{rcept_no}_{kind}_v{version}.json")


def _cached(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(path, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        logger.warning("Could not cache DART segment data %s", path, exc_info=True)


def _to_json(cands):
    return [{"source": c.source, "items": c.items, "adjust": c.adjust, "total": c.total, "label": c.label,
             "ops": c.ops, "when": c.when, "span": c.span, "months": c.months} for c in cands]


def _from_json(data):
    out = []
    for d in data:
        months = d.get("months")
        if months is None and d["source"] == "xbrl":
            # A result cached before the period was kept (PARSER_VERSION is not
            # bumped for it, so no package is asked for again): the label has the dates.
            m = _LABEL_DATES.match(d.get("label", ""))
            months = _months(m.group(1), m.group(2)) if m else None
        out.append(Candidate(d["source"], [tuple(x) for x in d["items"]], [tuple(x) for x in d["adjust"]], d["total"],
                             d.get("label", ""), d.get("ops") or {}, d.get("when"), d.get("span"), months))
    return out


def fetch_xbrl(client, rcept_no: str, reprt_code: str, fs_div: str, cache_dir: str) -> list[Candidate]:
    """XBRL candidates of a filing; only the parsed result is cached (the
    package itself is up to ~1 MB). DART's "no file" answer counts as none."""
    path = _cache_path(cache_dir, f"xbrl_{fs_div}", rcept_no)
    data = _cached(path)
    if data is None:
        try:
            body = client.get_bytes("fnlttXbrl.xml", {"rcept_no": rcept_no, "reprt_code": reprt_code})
            data = _to_json(xbrl_candidates(body, fs_div))
        except DartApiError as e:
            if not is_no_file(e):
                raise                      # not an answer about the file: nothing cached, the period stays pending
            data = []
        except zipfile.BadZipFile as e:
            raise DartApiError("bad_reply", f"fnlttXbrl.xml of {rcept_no} is not a readable zip") from e
        _save(path, data)
    return _from_json(data)


def fetch_document(client, rcept_no: str, fs_div: str, cache_dir: str) -> tuple[list[Candidate], bool, list[Candidate]]:
    """Document candidates of a filing: the segment note's, the single-segment
    flag and the business section's (biz_candidates); the parsed result is
    cached, not the document."""
    path = _cache_path(cache_dir, f"doc_{fs_div}", rcept_no, DOC_PARSER_VERSION)
    data = _cached(path)
    if data is None:
        try:
            body = client.get_bytes("document.xml", {"rcept_no": rcept_no})
            with zipfile.ZipFile(io.BytesIO(body)) as zf:
                names = zf.namelist()
                main = next((n for n in names if n == f"{rcept_no}.xml"), names[0])
                doc = docs.decode_document(zf.read(main))
            cands, single = [], False
            for region in segment_regions(doc, fs_div == "CFS"):
                rc, rs = document_candidates(region)
                cands += rc
                single = single or rs
            data = {"cands": _to_json(cands), "single": single, "biz": _to_json(biz_candidates(doc))}
        except DartApiError as e:
            if not is_no_file(e):
                raise
            data = {"cands": [], "single": False, "biz": []}
        except (zipfile.BadZipFile, IndexError, KeyError) as e:
            raise DartApiError("bad_reply", f"document.xml of {rcept_no} is not a readable zip") from e
        _save(path, data)
    return _from_json(data["cands"]), bool(data["single"]), _from_json(data.get("biz") or [])


# ---- storing ----------------------------------------------------------------------

def store_segments(conn, corp_code: str, fiscal_year: int, fiscal_q: int, rcept_no: str, rcept_dt: str,
                   chosen: Candidate | None, basis: str | None, residual: float, status: str, source: str,
                   level: str = "segment") -> int:
    """Replace the period's rows of this receipt at `level` ("segment", or
    "product" for the business section's product breakdown); returns the
    items stored. Not committed."""
    conn.execute("DELETE FROM segment_rev WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = ? AND rcept_no = ? "
                 "AND level = ?", (corp_code, fiscal_year, fiscal_q, rcept_no, level))
    if chosen is None:
        return 0
    rows = []
    for name, v in chosen.items:
        rows.append((name, _segment_code(conn, corp_code, name, fiscal_year), v, chosen.ops.get(name)))
    for name, v in chosen.adjust:
        rows.append((name, "_adj", v, None))
    if residual and abs(residual) > 0:
        rows.append(("기타·조정(차액)", "_residual", residual, None))
    seen = set()
    for raw, code, rev, op in rows:
        key = raw
        n = 2
        while key in seen:                  # the same name twice in one table (sub-rows)
            key, n = f"{raw} ({n})", n + 1
        seen.add(key)
        conn.execute(
            "INSERT OR REPLACE INTO segment_rev(corp_code, fiscal_year, fiscal_q, level, segment_raw, segment_code, "
            "revenue, op_income, basis, source, check_status, rcept_no, rcept_dt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (corp_code, fiscal_year, fiscal_q, level, key, code, rev, op, basis, source, status, rcept_no, rcept_dt))
    return len(chosen.items)


def _segment_code(conn, corp_code: str, raw: str, fiscal_year: int) -> str:
    """segment_map's code for a reported name; a new name gets a draft row
    (normalised name, note 'auto'), an auto row widens its year range."""
    row = conn.execute("SELECT segment_code, valid_from, valid_to, note FROM segment_map WHERE corp_code = ? "
                       "AND segment_raw = ?", (corp_code, raw)).fetchone()
    if row is None:
        code = normalise(raw)
        conn.execute("INSERT INTO segment_map(corp_code, segment_raw, segment_code, valid_from, valid_to, note) "
                     "VALUES (?, ?, ?, ?, ?, 'auto')", (corp_code, raw, code, fiscal_year, fiscal_year))
        return code
    if row["note"] == "auto":
        conn.execute("UPDATE segment_map SET valid_from = MIN(valid_from, ?), valid_to = MAX(valid_to, ?) "
                     "WHERE corp_code = ? AND segment_raw = ?", (fiscal_year, fiscal_year, corp_code, raw))
    return row["segment_code"]


def unmatched_codes(conn, corp_code: str) -> list[dict]:
    """Segment codes of a company that do not run through its years: present
    in some fiscal years of its segment data but missing in others between its
    first and last year (a reorganisation or renaming to map by hand, plan 5-4)."""
    rows = conn.execute(
        "SELECT level, segment_code, MIN(segment_raw) AS raw, GROUP_CONCAT(DISTINCT fiscal_year) AS years "
        "FROM segment_rev WHERE corp_code = ? AND segment_code NOT LIKE '\\_%' ESCAPE '\\' "
        "AND check_status IN ('ok', 'sum_mismatch') AND source <> 'single' GROUP BY level, segment_code",
        (corp_code,)).fetchall()
    out = []
    for level in sorted({r["level"] for r in rows}, reverse=True):          # segment first
        mine = [r for r in rows if r["level"] == level]
        all_years = {int(y) for r in mine for y in str(r["years"]).split(",")}
        if len(all_years) < 2:
            continue
        for r in mine:
            years = {int(y) for y in str(r["years"]).split(",")}
            if years != all_years:
                out.append({"segment_code": r["segment_code"], "segment_raw": r["raw"], "years": sorted(years),
                            "missing": sorted(all_years - years), "level": level})
    return out


def single_segment(revenue_q, revenue_ytd, reprt_code: str) -> tuple[Candidate, str]:
    """The one-segment reading of a company with a single operating segment."""
    if reprt_code != "11011" and revenue_q is not None:
        return Candidate("single", [("단일부문", revenue_q)]), "q"
    return Candidate("single", [("단일부문", revenue_ytd)]), "ytd"


def record_fetch(conn, rcept_no: str, status: str, source: str | None, detail: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO seg_fetch(rcept_no, status, source, detail, fetched_at) VALUES (?, ?, ?, ?, ?)",
                 (rcept_no, status, source, detail, now_iso()))
