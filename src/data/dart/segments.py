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
(basis ``q``) or the cumulative one (``ytd``). The prior period's table or
column does not match the current revenue, so the check also picks the
period. A table whose total matches but whose segment rows do not add up
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
PARSER_VERSION = 12
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
_EXTERNAL = re.compile(r"외부")
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

    def __init__(self, source: str, items, adjust=(), total=None, label: str = "", ops=None):
        self.source = source
        self.items = [(n, v) for n, v in items if v is not None]
        self.adjust = [(n, v) for n, v in adjust if v is not None]
        self.total = total
        self.label = label
        self.ops = ops or {}

    def sums(self) -> tuple[float, float]:
        s = sum(v for _, v in self.items)
        return s, s + sum(v for _, v in self.adjust)

    def geographic(self) -> bool:
        """Whether most items are regions (a geographic breakdown, which the
        segment note often shows next to the operating segments)."""
        names = [n.replace(" ", "") for n, _ in self.items]
        geo = sum(1 for n in names if any(w in n for w in _GEO))
        return bool(names) and geo * 2 > len(names)

    def check(self, revenue_q, revenue_ytd):
        """(basis, residual) when the candidate reconciles with revenue, else None.
        A breakdown needs two items at least: a one-row table equal to the
        revenue tells nothing (the single-segment case is decided apart)."""
        if self.source != "single" and len(self.items) < 2:
            return None
        _seg, with_adj = self.sums()
        for basis, rev in (("q", revenue_q), ("ytd", revenue_ytd)):
            if not rev:
                continue
            if abs(with_adj - rev) <= abs(rev) * TOLERANCE:
                return basis, 0.0
            if self.total is not None and abs(self.total - rev) <= abs(rev) * TOLERANCE and len(self.items) >= 2                     and abs(self.total - with_adj) <= abs(rev) * MAX_RESIDUAL_SHARE:
                return basis, self.total - with_adj
        return None


def choose(candidates: list[Candidate], revenue_q, revenue_ytd):
    """(candidate, basis, residual, status) by plan 5-3; (None, None, 0, 'review')
    when there is nothing to choose from."""
    # Operating segments before a geographic breakdown of the same note, then
    # the order read (segment tables before product tables, the current period
    # first). A filer whose segments are regions still gets them when nothing
    # else reconciles.
    candidates = sorted(candidates, key=lambda c: (c.geographic(), c.source == "html_product"))
    for c in candidates:
        hit = c.check(revenue_q, revenue_ytd)
        if hit:
            return c, hit[0], hit[1], "ok"
    usable = [c for c in candidates if len(c.items) >= 2]
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
                                 label=f"{start}..{end} {family or ''}".strip(), ops=slot["op"]))
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


def table_candidates(table: str, unit: float, product: bool) -> list[Candidate]:
    """Candidates of one table in both orientations (see the module docstring).
    A unit caption inside the table's own header wins over `unit` (the last
    one before the table)."""
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
    for c, lab in rev_cols:
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
            out.append(Candidate(source, items, adjust, total, label=f"col {lab}", ops=ops))

    # Orientation B: items are columns; one candidate per revenue-like row and
    # period. A column's path of header labels ends with its period ("3개월",
    # "누적", "당기"...) when the table shows several; the label before it is
    # the item. A total label at the top ("계(*)", "제품과용역합계") is the
    # table's total; a total label under a parent ("DS부문 > 계") is that
    # parent's own amount, and the parent's other sub-columns (반도체, DP) are
    # its parts, not segments.
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
    def is_axis(n):
        return n.replace(" ", "") in _AXIS_WORDS or is_total(n)

    def real_parents(names):
        """The segment-like labels above a column's leaf (axis words, totals
        and elimination groups are not segments)."""
        return [n for n in names[:-1] if not is_axis(n) and not _ADJ.search(n)]

    # A segment shown with its own total and parts ("DS부문 > 계 | 반도체 | DP").
    parent_totals = set()
    for _c, names, _p in columns:
        if is_total(names[-1]) and not any(_ADJ.search(n) for n in names):
            rp = real_parents(names)
            if rp:
                parent_totals.add(rp[-1])
    rev_rows = [(n, r) for n, r in rows if _REVENUE_LABEL.search(n) and not _NOT_REVENUE.search(n)
                and not _INTERNAL.search(n)]
    rev_rows.sort(key=lambda nr: 0 if _EXTERNAL.search(nr[0]) else 1)
    op_row = next((r for n, r in rows if _OP_LABEL.search(n.replace(" ", ""))), None)
    periods = sorted({p for _c, _n, p in columns}, key=lambda p: [x for x in p])
    for rname, r in rev_rows:
        for period in periods:
            items, adjust, adj_total, total, ops = [], [], None, None, {}
            for c, names, p in columns:
                if p != period:
                    continue
                v = val(r, c)
                if v is None:
                    continue
                leaf = names[-1]
                in_adj = any(_ADJ.search(n) for n in names)
                rp = real_parents(names)
                if is_total(leaf):
                    if in_adj:
                        adj_total = v                      # an elimination group's own total
                    elif rp:
                        items.append((rp[-1], v))          # a segment's own total
                        if op_row is not None and val(op_row, c) is not None:
                            ops[rp[-1]] = val(op_row, c)
                    else:
                        total = v                          # the last total column: the grand total
                    continue
                if in_adj:
                    adjust.append((f"{leaf} 제거" if not _ADJ.search(leaf) else leaf, v))
                    continue
                if any(p in parent_totals for p in rp):
                    continue                               # part of a segment shown with its total
                if _is_revenue_caption(leaf):
                    continue                               # "외부고객으로부터의 수익" over the columns, not a segment
                items.append((leaf, v))
                if op_row is not None:
                    ov = val(op_row, c)
                    if ov is not None:
                        ops[leaf] = ov
            if adj_total is not None and not adjust:
                adjust = [("부문간 제거", adj_total)]
            if items:
                out.append(Candidate(source, items, adjust, total, label=f"row {rname} {' '.join(period)}".strip(),
                                     ops=ops))
    return out


def document_candidates(region: str) -> tuple[list[Candidate], bool]:  # one region
    """(candidates of the segment note's tables, the note says a single segment)."""
    if not region:
        return [], False
    text = _SPLIT.sub("", docs._text(region[:20_000]))
    single = bool(_SINGLE.search(text))
    out = []
    for tm in docs._TABLE.finditer(region):
        before = _SPLIT.sub("", docs._text(region[max(0, tm.start() - 600):tm.start()]))
        product = single or bool(_PRODUCT_CONTEXT.search(before[-150:]))
        out.extend(table_candidates(tm.group(0), _unit_before(region, tm.start()), product))
    # Segment tables before product tables: plan 5-1 order.
    out.sort(key=lambda c: 0 if c.source == "html_segment" else 1)
    return out, single


# ---- fetching ---------------------------------------------------------------------

def _cache_path(cache_dir: str, kind: str, rcept_no: str) -> str:
    return os.path.join(cache_dir, "segments", rcept_no[:4], f"{rcept_no}_{kind}_v{PARSER_VERSION}.json")


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
             "ops": c.ops} for c in cands]


def _from_json(data):
    return [Candidate(d["source"], [tuple(x) for x in d["items"]], [tuple(x) for x in d["adjust"]], d["total"],
                      d.get("label", ""), d.get("ops") or {}) for d in data]


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


def fetch_document(client, rcept_no: str, fs_div: str, cache_dir: str) -> tuple[list[Candidate], bool]:
    """Document candidates of a filing and the single-segment flag; the parsed
    result is cached, not the document."""
    path = _cache_path(cache_dir, f"doc_{fs_div}", rcept_no)
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
            data = {"cands": _to_json(cands), "single": single}
        except DartApiError as e:
            if not is_no_file(e):
                raise
            data = {"cands": [], "single": False}
        except (zipfile.BadZipFile, IndexError, KeyError) as e:
            raise DartApiError("bad_reply", f"document.xml of {rcept_no} is not a readable zip") from e
        _save(path, data)
    return _from_json(data["cands"]), bool(data["single"])


# ---- storing ----------------------------------------------------------------------

def store_segments(conn, corp_code: str, fiscal_year: int, fiscal_q: int, rcept_no: str, rcept_dt: str,
                   chosen: Candidate | None, basis: str | None, residual: float, status: str, source: str) -> int:
    """Replace the period's rows of this receipt; returns segments stored. Not committed."""
    conn.execute("DELETE FROM segment_rev WHERE corp_code = ? AND fiscal_year = ? AND fiscal_q = ? AND rcept_no = ?",
                 (corp_code, fiscal_year, fiscal_q, rcept_no))
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
            "INSERT OR REPLACE INTO segment_rev(corp_code, fiscal_year, fiscal_q, segment_raw, segment_code, revenue, "
            "op_income, basis, source, check_status, rcept_no, rcept_dt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (corp_code, fiscal_year, fiscal_q, key, code, rev, op, basis, source, status, rcept_no, rcept_dt))
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
        "SELECT segment_code, MIN(segment_raw) AS raw, GROUP_CONCAT(DISTINCT fiscal_year) AS years FROM segment_rev "
        "WHERE corp_code = ? AND segment_code NOT LIKE '\\_%' ESCAPE '\\' AND check_status IN ('ok', 'sum_mismatch') "
        "AND source <> 'single' GROUP BY segment_code", (corp_code,)).fetchall()
    all_years = {int(y) for r in rows for y in str(r["years"]).split(",")}
    if len(all_years) < 2:
        return []
    out = []
    for r in rows:
        years = {int(y) for y in str(r["years"]).split(",")}
        if years != all_years:
            out.append({"segment_code": r["segment_code"], "segment_raw": r["raw"], "years": sorted(years),
                        "missing": sorted(all_years - years)})
    return out


def single_segment(revenue_q, revenue_ytd, reprt_code: str) -> tuple[Candidate, str]:
    """The one-segment reading of a company with a single operating segment."""
    if reprt_code != "11011" and revenue_q is not None:
        return Candidate("single", [("단일부문", revenue_q)]), "q"
    return Candidate("single", [("단일부문", revenue_ytd)]), "ytd"


def record_fetch(conn, rcept_no: str, status: str, source: str | None, detail: str = "") -> None:
    conn.execute("INSERT OR REPLACE INTO seg_fetch(rcept_no, status, source, detail, fetched_at) VALUES (?, ?, ?, ?, ?)",
                 (rcept_no, status, source, detail, now_iso()))
