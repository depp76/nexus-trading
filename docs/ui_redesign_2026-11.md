# UI 개편 구현 계획 (2026-11) — Strategy · Trading Universe · 토큰

CLI(Claude Code) 세션이 이 문서만 보고 구현할 수 있도록 정리한 작업 지시서다. 2026-11-13 시안(디자인 캔버스
"Nexus 앱 UI 시안", 원본 마크업은 `docs/ui_mockups/2026-11/`)을 실제 코드 구조(`src/ui/`, `src/strategy/trend_following/`)에
맞춰 옮겼다. **`docs/ui.md` 8장 규칙이 이 문서보다 우선**한다. 충돌하는 곳은 0.3에 정리했다.

---

## 0. 공통 전제

### 0.1 범위

| 단계 | 화면 | 주요 파일 |
|---|---|---|
| P0 | 토큰·QSS 추가 | `ui/colors.py`, `ui/theme.py`, `ui/common.py` |
| P1 | Strategy › Trend Following 결과 화면 | `ui/trend_following_tab.py`, 신규 `ui/strategy_widgets.py`(이름 자유) |
| P2 | Trading Universe 상세 패널·Score 열 | `ui/universe_tab.py`, `ui/widgets.py`, `ui/delegates.py`, `strategy/trend_following/scoring.py` |
| P3 | 문서·테스트 | `docs/ui.md` 8.10 신설, `tests/` |

단계마다 커밋을 나누고, 각 단계 끝에 `pytest`와 `ruff check src`를 통과시킨다.

### 0.2 바꾸지 않는 것

- 전략 규칙·파라미터·백테스트 엔진·성과 계산(`backtest.py`, `metrics.py`의 계산식, `research.py`의 실행 흐름).
  P2의 `scoring.py` 변경은 **표시용 필드 추가만** 허용(점수·게이트 계산 불변, 기존 테스트 그대로 통과).
- 색 규칙: **파랑 = 상승/이익(PROFIT), 빨강 = 하락/손실(LOSS)** (ui.md 1.1, 2026-09-22 결정). 판정은 OK/WARN/DANGER(8.3).
- 글꼴: 맑은 고딕 Semilight 하나(ui.md 1.3). 숫자 전용 글꼴을 따로 쓰지 않는다.
- 버튼은 `make_button()`, 표는 `setup_table()`, 탭당 primary 하나(Strategy = Run Research), 탭 안 제목 없음(8.4·8.5).
- 홀드아웃 가드(`DEV_END` 이후 날짜 차단)와 면책 배너.

### 0.3 시안과 ui.md가 다른 곳 — ui.md를 따른다

| 시안 | 구현 |
|---|---|
| 상단 앱 헤더·메인 탭 모양 | 기존 MainWindow 셸 그대로(시안은 위치 참고용) |
| 비교표의 옅은 행 구분선 | `setup_table()` 기본(격자선 `LINE_GROUP` 1px, 24px 행) |
| 숫자 tabular 표기 | 사용하지 않음(1.3) |
| 판정 배지의 기호(✓ ! ✕ –) | 배지는 버튼이 아니므로 기호 허용. 버튼 라벨에는 기호 금지(8.1) |
| 샘플 종목·예시 곡선 | 실제 데이터로 대체. 시안의 표 수치(NEX-59 1×)만 실제 값 |

---

## P0. 토큰과 QSS

### P0-1 `ui/colors.py`에 추가

```python
# Badge fills for the status channel (docs/ui.md 8.3). Text colors are one step
# darker than OK/WARN/DANGER so a 11px badge label keeps 4.5:1 contrast.
OK_BG, OK_TEXT = "#e8f3ee", "#2e7d5b"
WARN_BG, WARN_TEXT = "#fff6e8", "#8a4f08"
FAIL_BG, FAIL_TEXT = DANGER_BG, "#b3362e"
NA_BG, NA_TEXT = "#f0f1f8", "#595d6c"

# Chart series roles (strategy charts)
SERIES_SELECTED = "#9184d9"   # = theme.ACCENT
SERIES_BENCH = "#9397ab"      # = theme.TEXT_FAINT, dashed
SERIES_TARGET = WARN          # target lines (Sharpe 1.5, -15% drawdown), dashed
DRAWDOWN_ALPHA = 0.18         # LOSS fill under the drawdown curve
```

`theme.py`에서 import하는 방향(colors → theme)은 기존과 같게 유지해 순환 import를 만들지 않는다.

### P0-2 `ui/theme.py` `app_qss()`에 추가

```css
QLabel#badgeOk   { color: OK_TEXT;   background: OK_BG;   border-radius: 9px; padding: 1px 8px; font-weight: 600; }
QLabel#badgeWarn { color: WARN_TEXT; background: WARN_BG; ... }
QLabel#badgeFail { color: FAIL_TEXT; background: FAIL_BG; ... }
QLabel#badgeNa   { color: NA_TEXT;   background: NA_BG;   ... }
QFrame#KpiCard   { background: SURFACE; border: 1px solid LINE; border-radius: 8px; }
QLabel#kpiHelp   { color: TEXT_MUTED; font-size: 8pt; }      /* 한 줄 한국어 풀이 */
QLabel#settingsChip { background: HDR_BG; border-radius: 4px; padding: 2px 8px; }
```

(f-string 안에서 실제 상수로 치환. 크기는 pt 단위, `FONT_*` 체계와 맞춘다.)

### P0-3 `ui/common.py` 헬퍼

- `make_badge(kind: Literal["ok","warn","fail","na"], text: str) -> QLabel` — objectName만 바꾸는 얇은 팩토리.
- `KpiCard(QFrame)`: 라벨(대문자, `kpiLabel`), 값(`kpiValue`, FONT_KPI보다 한 단계 큰 값 허용 여부는 8.5에 맞춰 `FONT_TITLE` 사용),
  우상단 배지 자리, 아래 한 줄 풀이(`kpiHelp`). `set(value, color=None, badge=None, help=None)`.
  기존 `KpiStrip`과 역할이 겹치므로 KpiStrip은 그대로 두고, 카드형이 필요한 곳(P1)만 KpiCard를 쓴다.

**완료 기준**: 기존 화면 변화 없음, `tests/test_ux_consistency.py` 통과, 새 헬퍼 단위 테스트(objectName, 배지 종류별).

---

## P1. Strategy › Trend Following 결과 화면

### P1-1 배치 (위 → 아래)

1. **실행 설정 한 줄** (`QFrame#DashboardCard`): 현재 폼 값을 칩(`settingsChip`)으로 요약
   — `2021-01-01 → 2024-12-30` · `KOSPI Top 200` · `Liquidity ≥ 100억` · `Max 5 positions` · `Check: Mon` · `Costs 0x/1x/2x` · `All 10 variants`
   — 오른쪽 끝에 `Edit settings`(접기/펼치기, 펼치면 기존 3×3 폼과 Variants... 버튼이 그 자리에 나타남), 그다음 `Save Report`, `Stop`, `Run Research`(primary, 맨 끝).
   기본은 접힘. 실행 전 첫 화면만 펼친 상태로 시작해도 된다(구현자 판단, ui.md에 기록).
2. **변형 선택 줄**: 결과에 있는 ID 순서대로 세그먼트 버튼(A0 … C3, BM1). 고정 폭 세그먼트(8.1 예외 허용 대상).
   오른쪽에 비용 세그먼트 `0x | 1x | 2x`(결과에 있는 배수만 활성). 기본 선택: 1x, 그리고 비용 1×에서 Sharpe가 가장 높은 전략 변형.
   ※ "가장 높은"은 선택 초기값일 뿐 순위·채택 표시가 아니다(배지·문구로 강조하지 않는다).
3. **KPI 카드 6장** (`KpiCard`, 한 줄, 창이 좁으면 2줄로 감김 — `QGridLayout` 3열×2행 대체 가능):

   | 카드 | 값 (`r.metrics`) | 배지 | 풀이 문구 |
   |---|---|---|---|
   | SHARPE | `sharpe` | ≥ 1.5 → Ok "Target met" / 미만 → Fail "Below 1.5" | 위험 1단위당 초과수익 · 목표 ≥ 1.5 |
   | MAX DRAWDOWN | `mdd` (음수, LOSS 색) | `abs(mdd) ≤ 0.15` → Ok "Within 15%" / Fail "Over 15%" | 고점 대비 최대 하락 · 목표 \|MDD\| ≤ 15% |
   | CAGR | `cagr` (`fg_for`) | – | 연평균 수익률 |
   | VS BM1 | `bm1_excess_cagr` → `+24.5%p` 형식 (`fg_for`) | – | 시장(KODEX 200TR) 대비 CAGR 차이 |
   | VOLATILITY | `vol` | – | 수익률 출렁임(연환산) |
   | TRADES | `n_trades` | – | 기간 중 매매 횟수 |

   목표값 1.5·0.15는 상수 하나(`TARGET_SHARPE`, `TARGET_MDD`)로 두고 툴팁에 출처(사용자 기준, Nexus README)를 적는다.
   벤치마크(BM1~BM4)를 선택하면 배지는 숨긴다.
4. **차트 + 판정 카드** (가로 분할, 차트 쪽 stretch 2 : 카드 1)
   - 차트 하나의 Figure에 축 2개를 `sharex=True`, 높이 비 3:1.
     - 위: NAV(100 기준). **선택 변형 1개 + BM1만** 그린다(현재는 전부 그림 → 범례 혼잡). 선택 = `SERIES_SELECTED` 실선 2.2, BM1 = `SERIES_BENCH` 점선.
       "Show all" 체크박스를 두어 켜면 기존처럼 전부 그린다(나머지 변형은 `TEXT_EMPTY` 계열 얇은 선).
     - 아래: 낙폭 `nav / cummax(nav) - 1`을 `fill_between(…, color=LOSS, alpha=DRAWDOWN_ALPHA)`, −15% 수평선 `SERIES_TARGET` 점선.
     - 기존 마우스 오버 readout·crosshair 유지(두 축에 같은 x).
   - 판정 카드(`QFrame#DashboardCard`, 제목 "Target check"): 앱이 **직접 계산할 수 있는 것만** 보여 준다.
     - 행 1 Sharpe ≥ 1.5, 행 2 |MDD| ≤ 15% (전체 구간), 행 3 두 기준 동시 충족(=H 기준) — 각 행 오른쪽 배지.
     - 행 4 "Robustness (E2) · Look-ahead (A5)": 앱 안에서는 계산하지 않으므로 `badgeNa` "Not run here" + 툴팁
       "독립 검증은 Validation 리포트(NEX-59 등)에서 확인". 시안의 NEX-59 문구·J1 판정은 **넣지 않는다**(실행 결과와 무관한 고정 문구가 되므로).
5. **하단 탭** (`QTabWidget`, 결과 영역의 남는 높이)
   - `Variant comparison`(기본): 비용 배수 하나(위 세그먼트와 연동)의 전략+BM1~BM4 행. 열:
     ID · Entry(설명, `describe_variant(vid).title` 재사용) · CAGR · Vol · **Sharpe(막대)** · MDD · Trades · vs BM1(%p) · Target(H).
     - Sharpe 열은 새 `SharpeBarDelegate`: 0~1.6 범위 막대(ACCENT, 벤치마크는 `TEXT_EMPTY`), 1.5 위치 세로선, 숫자는 막대 왼쪽.
     - MDD 글자색: |MDD| ≤ 15% → OK, 초과 → `FAIL_TEXT`(손실 방향이 아니라 기준 판정이므로 상태 채널).
     - 행 클릭 = 위 변형 세그먼트 선택과 동기화.
     - **기존 전체 열(Calmar, Win, Hold, Turnover, Cost/yr, Cash, IR, Beta, Alpha, Up/Down cap, 구간 수익률) 표는 `All metrics` 탭으로 그대로 이동**(정보 손실 없음, Ctrl+C TSV 유지).
   - `Monthly returns`: 선택 변형의 `metrics.monthly_returns(nav, dates)` 연×월 표, 셀 배경 `heatmap_bg(v*100, scale=10)`, 글자 `fg_for`, 마지막 열 연간 합(`yearly_returns`).
   - `Trades`: 선택 변형 `RunSummary.trades`(전략만). 열은 거래 객체 필드에 맞춰(진입일·청산일·종목·수익률·사유). BM 선택 시 빈 상태 문구.
   - `Event study`: 기존 표 이동.
   - `Warnings (n)`: 기존 Warnings 토글 패널을 탭으로 이동. n > 0이면 탭 글자색 WARN.
6. **푸터·배너**: 기존 그대로.

### P1-2 상태·동작 규칙

- 결과 없을 때: KPI 카드 값 `-`, 차트 영역 기존 빈 상태 문구, 비교표 `setup_table(empty_text=…)`.
- 변형 세그먼트·비용 세그먼트 변경은 **재실행 없이** 이미 받은 `ResearchResult`에서 다시 그린다.
- `_chart_series`/readout 로직은 선택 변형 기준으로 재사용.
- 새 위젯은 `trend_following_tab.py`가 너무 커지지 않도록 `ui/strategy_widgets.py`로 분리(KpiRow, SharpeBarDelegate, MonthlyTable 등).

### P1-3 완료 기준

- 시안 ①과 같은 배치·색 역할(차이는 0.3 표의 항목만).
- 기존 `test_strategy_tab.py` 통과 + 신규 테스트: 변형 선택 시 KPI 값·배지 갱신, 비용 세그먼트 전환, Sharpe/MDD 목표 배지 경계값(1.5, 0.15 포함), 낙폭 계산(`cummax`) 단위 테스트, BM 선택 시 배지 숨김, `All metrics` 탭에 기존 `SUMMARY_COLUMNS` 전 열 존재.
- 실제 실행 화면 스크린샷을 `docs/ui_mockups/2026-11/impl_strategy.png`로 남긴다(검토용).

---

## P2. Trading Universe

### P2-1 Score 열과 이름 배지

- `widgets.COLUMNS`의 `trend` 열 왼쪽 또는 맨 끝에 `ColSpec("score", "Score", 54, 0.55, "momentum", None)` 추가.
  상수(`COL_*`)와 import 시 assertion, `GROUP_START_COLS`, `HeatLegend` 문구 영향 범위를 함께 고친다.
  값: 마지막 Trend Score 실행의 주간 평균 총점(`week_avg`, −1~2, 소수 2자리). 실행 전·미채점 종목은 `-`(TEXT_EMPTY).
- 이름 셀(`IdentityDelegate`)의 현재 `· Top n` 텍스트를 **작은 배지**로: Top = PROFIT 계열 옅은 배경(`#e9eefa`/`#2d5aa8`), Bottom = LOSS 계열(`#fbecea`/`#b3362e`).
  배지 위치는 이름 앞. 새 hex는 `colors.py`에 상수로(`RANK_TOP_BG` 등).

### P2-2 오른쪽 상세 패널

- 표와 패널을 `QSplitter(Horizontal)`로, 패널 기본 폭 320px, 접기 가능(splitter 핸들 더블클릭 또는 툴바 토글 — 툴바에 버튼을 늘리지 않으려면 핸들 방식 권장).
- 선택한 행(현재 셀 변경 신호)에 따라 갱신:
  1. 이름·티커·시장, 현재가·Chg(`fg_for`)
  2. 가격 미니 차트: 기존 `TrendDelegate`가 쓰는 시계열 재사용(새 다운로드 없음), 60~120일.
  3. **Trend Score 구성**: `pct(MA50Div)`, `pct(Range52)`, `pct(R3)`와 주간 평균 총점. 세 칸 카드에 "추세 방향 +", "추세 위치 +", "최근 3일 −" 풀이.
  4. **Gate**: 유동성, MA50Div > 0, Range52 ≥ 0.70, 과열 아님 — 각 Pass/Fail 배지.
  5. 버튼: `MA Chart`, `AI Report`(기존 우클릭 메뉴 동작 재사용).
  Trend Score를 아직 실행하지 않았으면 3·4 대신 "Run Trend Score to see the score breakdown" 한 줄.
- 데이터 공급(필요한 최소 변경):
  - `scoring.ScoredName`에 `pct_ma50`, `pct_range52`, `pct_r3`(최신 세션 퍼센타일, NaN 허용) 추가 — `ScoreBook.pct`에서 읽기만.
  - `Recommendation`에 `scored: dict[str, ScoredName]`(채점된 전 종목) 추가. `top`/`bottom` 계산은 그대로.
  - 스펙 문서 `trend_following.md` 9-4절에 "표시용 필드 추가, 점수 계산 불변" 한 줄 기록.
  - `TrendScoreThread`가 넘기는 객체를 Universe 탭이 보관(`self._last_recommendation`).

### P2-3 완료 기준

- 행 높이 28px 유지(8.2 예외), 열 추가 후 `RangeBarDelegate.content_height()` 테스트 통과.
- 신규 테스트: Score 열 값/미채점 `-`, Top/Bottom 배지 종류, 패널이 선택 행을 따라감, 미실행 시 안내 문구, `ScoredName` 새 필드가 `ScoreBook.pct`와 일치.
- `scoring` 기존 테스트 전부 통과(점수·게이트 값 불변).

---

## P3. 문서·정리

- `docs/ui.md`에 **8.10 "Strategy 결과 화면·Universe 상세 패널 (2026-11)"** 신설: 이 문서의 결정 요약(배치, 배지 4종, 차트 계열 색, KPI 카드, 판정 카드는 앱 계산 항목만, Score 열, 상세 패널). 1~7장은 건드리지 않는다.
- `docs/ui.md` 상단 mockup 표에 `docs/ui_mockups/2026-11/Main.dc.html`, `Universe.dc.html`, `Tokens.dc.html` 행 추가.
- `roadmap.md`의 소스 코드 구조 섹션에 새 파일(`ui/strategy_widgets.py`) 반영(관례).

---

## 체크리스트 (CLI가 단계마다 확인)

- [ ] `ui/theme.py`·`ui/colors.py` 밖에 새 hex 없음(8.3)
- [ ] 버튼은 `make_button`, 라벨에 기호·이모지 없음
- [ ] 표는 `setup_table` + `install_table_keys`
- [ ] 글꼴 크기는 `FONT_*` 상수만
- [ ] 정보성 메시지는 모달이 아니라 푸터·상태바
- [ ] 전략·백테스트 계산 코드 diff 없음(P2의 표시용 필드 제외)
- [ ] `pytest` 전부 통과, `ruff check src` 0건
- [ ] 단계별 커밋, 커밋 메시지에 이 문서 절 번호(P0-1 등)

## CLI 시작 프롬프트 (붙여넣기용)

```
docs/ui_redesign_2026-11.md를 읽고 P0부터 순서대로 구현해 줘.
docs/ui.md 8장이 우선이고, 시안 마크업은 docs/ui_mockups/2026-11/에 있어.
전략 계산 코드는 바꾸지 말고, 단계마다 pytest와 ruff를 통과시킨 뒤 커밋해.
P1을 끝내면 실행 화면 스크린샷을 docs/ui_mockups/2026-11/impl_strategy.png로 남겨 줘.
```
