# Nexus Trading — 발전 로드맵

> 기준일: 2026-11-15 (최초 작성 2026-08-21)
> 현재 상태: PyQt6 단일 사용자 데스크톱 앱 (한국/미국 주식 포트폴리오 추적 + Trend Following 리서치) — git(PR 단위),
> GitHub Actions CI(ruff·pytest), pytest 853 통과, ruff(F) 0건, 화면 언어 한국어/영어
> 구조와 규칙은 `CLAUDE.md`, 화면 규칙은 `docs/ui.md`, 전략 스펙은 `src/strategy/trend_following/trend_following.md`
> **2026-11-15 정리**: 끝난 2·3·6·7장과 2026-08~10 변경 이력은 `docs/history/roadmap_2026-08_10.md`로 옮겼다
> (절 번호 그대로 — 코드 주석의 "roadmap 2-4", "roadmap 6-1a", "roadmap 7-2c" 등은 그 문서에 있다).

---

## 목차

1. [현황 요약](#1-현황-요약)
2. ~~단기 개선~~ · 3. ~~중기 개선~~ — 완료, `docs/history/roadmap_2026-08_10.md`
4. [장기 개선 (3개월+)](#4-장기-개선-3개월)
5. [우선순위 매트릭스](#5-우선순위-매트릭스)
6. ~~코드 분석 기반 개선 항목~~ · 7. ~~UI 개선 항목~~ — 완료, `docs/history/roadmap_2026-08_10.md`
8. [변경 이력](#변경-이력)

---

## 1. 현황 요약

### 현재 기능

| 탭 | 기능 |
|----|------|
| **Trading Universe** | KOSPI / KOSDAQ / NASDAQ 100 / S&P 500 시장 토글 워치리스트, 실시간 시세, MA 지표, 범위 필터·저장된 보기, 추세 점수 상위/하위 고정, 종목 직접 추가(NASDAQ·NYSE 포함) |
| **Trading History** | 거래 기록 수동 입력, SQLite 영속화 (`portfolio.db`), 오버라이드 지원 |
| **Total Assets** | 날짜별 자산 합계 테이블 + matplotlib 그래프, 환율·KOSPI 연동 |
| **Strategy** | Trend Following 리서치 백테스트(스펙 `trend_following.md` v06): 실행 도구 모음, KPI 카드·NAV 차트·결과 표, 리포트 저장 |
| (공통) | MA 차트 팝업(이평·거래량·이격도·RSI, 투자자 수급, 추세 점수), 화면 언어 한국어/영어(머리 오른쪽 국기) |

### 기술 스택

- **UI**: PyQt6 + matplotlib (QtAgg 백엔드)
- **데이터**: pykrx / FinanceDataReader / yfinance / yahooquery / Naver / KIS(한국투자증권) REST·WebSocket
- **연산**: polars (내부) → pandas (외부 라이브러리 경계)
- **DB**: SQLite (WAL 모드), `trade_db.py`
- **AI**: `google-genai` (`gemini_helper.py`로 종목 리포트에 실사용 중; 자연어 필터·포트폴리오 진단은 2026-09-19 삭제), `google-cloud-aiplatform` (requirements에 포함, 미사용)

### 소스 코드 구조 (2026-11-15 갱신)

> **구조가 바뀔 때마다(폴더/파일 신설·이동·삭제) 이 섹션을 최신 상태로 갱신할 것.** 모듈별 설명은 `CLAUDE.md`
> "Architecture"가 더 자세하다. 3-1의 "현재 파일 구조"(보관 문서)는 Phase 0~5 당시의 기록이다.

```
.github/workflows/ci.yml        # GitHub Actions: ruff + pytest (Windows, Qt offscreen)
.gitattributes                  # 줄바꿈 LF 통일
src/
├── main.py                # PyQt6 진입점 + MainWindow (헤더·국기 언어 선택·자동 갱신 타이머)
├── i18n.py                # tr("한국어", "English"), 화면 언어 저장/불러오기
├── paths.py                # 경로 상수 (cwd 무관)
├── data_fetcher.py          # data/ 재노출 파사드 (데이터 접근 전용)
├── trade_db.py               # SQLite (portfolio.db, WAL): trades, asset_records
├── gemini_helper.py            # Gemini 종목 리포트
├── data/                        # 순수 데이터 접근 계층 (cache·frames·collectors/·flows·rates·krx_calendar·
│                                #   raw_prices·price_crosscheck·listing·history·fx·indicators·market)
├── strategy/trend_following/     # 전략 패키지 + 스펙 trend_following.md (config·costs·signals·dataset·backtest·
│                                #   benchmarks·metrics·event_study·research·scoring·factor_ic·robustness·run 등)
├── ui/
│   ├── common.py / theme.py / colors.py # 공용 헬퍼·디자인 토큰·색 규칙
│   ├── language_switch.py / icons/      # 국기 언어 선택 버튼, SVG 아이콘
│   ├── universe_tab.py / universe_views.py / widgets.py / delegates.py
│   ├── history_tab.py / history_table.py / history_calc.py
│   ├── assets_tab.py / assets_report.py
│   ├── strategy_tab.py / trend_following_tab.py / strategy_widgets.py
│   ├── ma_chart.py / trend_score_section.py
│   └── dialogs/                         # index_ma·stock_ma·trade_edit·trade_history·assets_graph·holdings_summary·
│                                        #   assets_report·stock_report·variant_picker·trend_conditions·strategy_trades
├── threads/                       # fetch_threads.py, realtime.py, strategy_threads.py
└── tests/                          # pytest (네트워크 없음), tests/strategy/trend_following/
```

옛 전략 계층(rebalance·ma_cross·첫 trend_following)은 2026-09-28에 삭제했고(git `070f639` 이전), 지금의
`strategy/trend_following/`은 2026-09-28 이후 스펙에서 새로 만든 것이다.

---

## 4. 장기 개선 (3개월+)

### 4-1. 아키텍처 — 웹 앱 전환 검토

**현황**: PyQt6 데스크톱 앱 (단일 머신 의존)  
**방향 옵션**:

| 옵션 | 기술 | 장점 | 단점 |
|------|------|------|------|
| A. 유지 | PyQt6 | 현행 자산 재사용 | 멀티 디바이스 불가 |
| B. 웹 프론트 | FastAPI + React | 모바일 접근 가능 | 재작성 비용 큼 |
| C. 경량 웹 | Streamlit / Dash | Python 자산 재사용 | 커스터마이징 제한 |

> **권장**: 단기~중기는 PyQt6 유지. 장기적으로 FastAPI 백엔드 + React 프론트엔드 검토.

---

### 4-2. 데이터 — 클라우드 동기화

**현황**: `portfolio.db`가 로컬 파일  
**방향**:

- Google Cloud Storage에 `portfolio.db` 주기적 업로드 (기존 `tools/register_secret.py` 인프라 활용)
- 또는 Cloud Firestore/Supabase로 마이그레이션
- 다중 기기 접근, 자동 백업 목적

---

### 4-3. 알림 — 조건부 시세 알림

**현황**: 알림 기능 없음  
**방향**:

- 종목별 알림 조건 설정 (목표가 도달, 손절가 도달, 거래량 급증)
- Windows 알림 (`win10toast` 또는 `plyer`) + 사운드
- 60초 `global_auto_timer` 틱마다 조건 체크

---

### 4-4. 백테스트 — 전략 비교 UI

**현황**: `run_backtest_strategy()` 함수 존재하나 결과를 텍스트로만 표시  
**방향**:

- 백테스트 결과를 matplotlib으로 시각화 (진입·청산 시점 마킹)
- 여러 전략/파라미터 조합 비교 테이블
- 전략 파라미터를 UI에서 조정 가능 (슬라이더/스핀박스)

---

### 4-5. 섹터/업종 분석

**현황**: 종목 단위 데이터만 존재  
**방향**:

- KOSPI/KOSDAQ 업종 분류 데이터 (pykrx `get_market_sector_classifications()`) 연동
- Universe 탭에 섹터별 그룹 필터 추가
- Total Assets 탭에 섹터별 비중 파이차트 추가

---

## 5. 우선순위 매트릭스

> `상태` 컬럼은 2026-09-17 재조사 결과 반영, **2026-10-25 현행화**(3-4A·3-4B/C·3-6·4-4 행, UI 행 추가). 완료 항목은 우선순위 재산정 대상에서 제외.
> 6-x 행은 섹션 6(코드 분석 기반 개선 항목, `docs/history/roadmap_2026-08_10.md`) 참고.

| # | 항목 | 영향 | 난이도 | 우선순위 | 상태 |
|---|------|------|--------|----------|------|
| 2-1 | 로깅 도입 | 중 | 낮음 | — | ✅ 완료 |
| 2-2 | 캐시 효율 모니터링 | 낮음 | 낮음 | — | ✅ 완료 |
| 2-3 | 상태바 피드백 | 중 | 낮음 | — | ✅ 완료 |
| 2-4 | 자동 백업 | 높음 | 낮음 | — | ✅ 완료 |
| 2-5 | 입력 유효성 검사 | 중 | 낮음 | — | ✅ 완료 |
| 3-4B/C | AI 진단/자연어 필터 | 높음 | 중간 | — | 삭제 (2026-09-19, 사용자 요청) |
| 3-2 | 테스트 인프라 | 높음 | 중간 | — | ✅ 완료 |
| 3-1 | 모듈화 | 높음 | 높음 | — | ✅ 완료 (Phase 0~5) |
| 3-3 | pandas/polars 경계 최소화 | 낮음 | 낮음 | — | ✅ 완료 (재조사 결과 대부분 이미 최적) |
| 3-4A | AI 종목 리포트 | 높음 | 중간 | — | ✅ 완료 (2026-09-18, Universe 우클릭 메뉴) |
| 3-5 | 시세 fallback | 높음 | 중간 | — | ✅ 핵심 완료 (`fetch_kr_market_data` 리스팅 fallback) |
| 4-3 | 조건부 알림 | 높음 | 중간 | ⭐⭐ 중간 | 미착수 |
| 4-5 | 섹터 분석 | 중 | 중간 | ⭐⭐ 중간 | 미착수 |
| 3-6 | 다크 모드 | 낮음 | 낮음 | ⭐ 낮음 | 제외 (2026-08-29 구현 후 사용자 요청으로 철회) |
| 4-4 | 백테스트 UI | 중 | 높음 | ⭐ 낮음 | 부분 (2026-09-28 Strategy 탭 Trend Following 리서치: 스코어카드·NAV 차트·이벤트 스터디) |
| UI | UI 일관성·명세 (`docs/ui.md` 8장) | 중 | 중간 | — | ✅ 완료 (2026-10-25, 열 그룹 토글만 미정 — 2026-09-19 사용자 결정으로 삭제됐던 기능) |
| 4-1 | 웹 전환 | 높음 | 매우 높음 | ⭐ 낮음 | 미착수 |
| 4-2 | 클라우드 동기화 | 중 | 높음 | ⭐ 낮음 | 미착수 |
| 6-1 | 동작 버그 5건 (로깅·타이머 이중화·UI 프리즈·경로·키 생성) | 높음 | 낮음 | — | ✅ 완료 |
| 6-3a | CLAUDE.md / AGENTS.md 현행화 | 높음 | 낮음 | — | ✅ 완료 |
| 6-2a | 파사드 역참조 해소 | 중 | 중간 | — | ✅ 완료 |
| 6-2b~f | 죽은 코드·미사용 import·중복·의존성 정리 | 중 | 낮음 | — | ✅ 완료 (US 시장 경로는 유지) |
| 6-3b | 테스트 공백 보강 | 중 | 낮음 | — | ✅ 완료 |

---

## 변경 이력

2026-08 ~ 2026-10의 행은 `docs/history/roadmap_2026-08_10.md`로 옮겼다.

| 날짜 | 변경 내용 |
|------|-----------|
| 2026-11-04 | `docs/nexus/kis_api_review.md` 5장 2~3단계 구현. `data/krx_calendar.py`(KIS 휴장일 API, `cache/krx_calendar.json`), `strategy/trend_following/schedule.py`(주간 점검일), `raw_prices.get_adjusted_daily_prices`, `data/price_crosscheck.py`(KIS vs Naver 대조, 보고 전용). 실측: 2021~2024 개장일 983일이 KOSPI 일봉과 일치, 7종목 가격 일치, Naver 거래량은 자본 변동 이전 구간에서 실제 거래대금과 불일치(카카오 0.201배, 에코프로비엠 0.061배, 스펙 9-20). 엔진 데이터는 변경하지 않음. 4단계 모의 주문은 J1 폐기로 연결할 전략이 없고 모의 키도 없어 미구현. 검증: pytest 640, ruff 0건. |
| 2026-11-06 | `get_stock_listing`에서 FDR `KrxStockListing` 직접 호출 제거, `fdr.StockListing`만 사용. FDR 0.9.202가 http URL로 POST하면 301 리다이렉트에서 GET으로 바뀌어 HTML을 받고 `JSONDecodeError`로 항상 실패(10-29 이후 앱 시작마다 traceback 2건). 실제 데이터는 이미 fallback이 공급 중이었으므로 동작 변화 없음. |
| 2026-11-06 (2차) | FDR 일별 KRX 파일은 장 시작 전 판(영업일 아침)과 비거래일 판의 `Marcap`이 전 종목 NaN이라, Naver 실패 시 `_fetch_kr_listing_fdr_fallback`이 순위 없는(파일 순서) 유니버스를 경고 없이 만들 수 있었음. (1) 전부 비면 최근 10일 내 직전 파일의 시가총액으로 순위 산정(`_fetch_recent_marcaps`), (2) 빈 프레임·`Marcap` 전체 NaN 결과는 날짜 캐시 대신 30분 TTL(`_listing_is_provisional`), (3) 그래도 없으면 오류 로그 후 `[]`. 단일 종목 조회는 직전 값을 쓰지 않고 기존처럼 Naver 현재 값을 사용. |
| 2026-11-06 (3차) | `review_codex.md` 재검토 후속. **R1**: 자산 리포트(`build_report`)와 `AssetReportThread`가 시장을 `== "US"`로만 비교해 거래 입력창의 NASDAQ·NASDAQ 100·NYSE·AMEX 종목은 환율 조회 없이 달러 종가를 원화처럼 합산하고 `priced=True`로 표시하던 것을 `is_us_market()`으로 통일(현재 DB의 미국 거래는 `US` 라벨 2건뿐이라 실제 수치 영향 없음). 기존 비교가 대소문자를 무시했으므로 `is_us_market()`도 대문자로 정규화. **R3**: `upsert_trade()`가 생성 키를 커밋 후에만 호출자 dict에 기록. **R2**: 거래 레거시 마이그레이션 `_migrate_legacy_json`과 관련 경로 상수 삭제(사용자 확인: 어느 PC에도 `custom_history.json`·`trade_overrides.json` 없음, 실패 처리를 고치는 대신 제거). `trading_record.json` 가져오기는 유지. R4는 스펙 결정이 필요해 보류. 검증: pytest 621, ruff 0건. |
| 2026-11-13 | `review_codex.md` R4·Strategy UI 검토(S1·S3·S4·S8) 후속. **R4**: 6-4 위험지표(변동성·Sharpe·MDD·베타/알파·IR)도 시드 1.0 기준으로 첫 세션을 포함(`nav_returns(base)`, `max_drawdown(base)`, `session_rf`), 구간 표는 이전 구간 끝 NAV 기준. BM4 Sharpe ≈ 75(부동소수점 잡음끼리 나눈 값)도 0으로 수정. `dev_2021_2024_nex23` 실측: 전략 10개 Sharpe 차이 ≤ 0.001, BM1 −0.29 → −0.26, BM2 0.04 → 0.06, J1 결론 불변(스펙 9-21). **S1**: Investor flows가 꺼지면 A3~A5 체크박스 비활성화, 돌지 않은 변형은 `ResearchResult.skipped`로 리포트·상태·Warnings에 표시. **S3**: 탭 안 Warnings 패널. **S4**: 두 결과 표에 `install_table_keys`. **S8**: Run 툴팁에 C1~C3, End 라벨에 개발 구간 상한, 버튼 순서 Save·Stop·Run(primary 맨 끝). 검증: pytest 666, ruff 0건. |
| 2026-11-13 (2차) | Strategy 탭 변형 선택을 팝업으로: 체크박스 10개 줄 → **Variants...** 버튼 + 선택 요약. `VariantPickerDialog`(`ui/dialogs/variant_picker.py`)는 A/B/C 그룹 체크 목록과 강조한 행의 규칙 설명을 나란히 보여준다. 설명은 `strategy/trend_following/variant_info.py`가 `Variant` 플래그·`StrategyParams`(폼 값)로 조립(스펙 9-22). Investor flows가 꺼지면 A3~A5는 설명만 보이고 체크 불가, 선택은 기억. 설명·목록 문구는 사용자 지시로 한국어(CLAUDE.md 예외 추가). 검증: pytest 673, ruff 0건, 오프스크린 렌더링으로 목록 잘림 확인 후 수정. |
| 2026-11-13 (3차) | UI 개편(`docs/ui_redesign_2026-11.md`, 시안 `docs/ui_mockups/2026-11/`). **P0**: 배지 4종·KpiCard·칩 토큰과 QSS, `make_badge`/`KpiCard`. **P1**: Strategy 결과 화면을 실행 설정 칩 → 변형·비용 세그먼트 → KPI 카드 6장(Sharpe ≥ 1.5·\|MDD\| ≤ 15% 배지) → NAV+낙폭 차트·Target check 카드 → 결과 탭(Variant comparison·All metrics·Monthly returns·Trades·Event study·Warnings)으로 재구성, 새 모듈 `ui/strategy_widgets.py`. nex23 스냅샷 실행 화면 `impl_strategy.png`. **P2**: Universe Score 열, 이름 앞 순위 배지(T/B), 오른쪽 상세 패널(`ui/universe_detail.py`, 캐시 전용 미니 차트 `peek_recent_closes`), `scoring` 표시용 필드(`pct_*`, `Recommendation.scored`; 스펙 9-4). **P3**: `docs/ui.md` 8.10. 전략 계산 코드 변경 없음. 검증: pytest 696, ruff 0건. |
| 2026-11-13 (4차) | 사용자 검토: Universe의 Score 열·순위 배지·상세 패널이 표를 좁혀 원래 정보가 잘 안 보임 → 셋 다 제거하고 Universe 표를 개편 전으로 되돌림. 점수 구성(주간 평균·순위, pct 카드 3장, 게이트 Pass/Fail)은 MA 차트 팝업 오른쪽 패널 맨 위로 이동(`ui/trend_score_section.py`, `StockMaDialog(trend_rec=...)`, KOSPI/KOSDAQ만). 캐시 전용 미니 차트와 `peek_recent_closes`도 삭제. `scoring`의 표시용 필드는 팝업이 계속 사용. docs/ui.md 8.10·스펙 9-4 갱신. 검증: pytest 695, ruff 0건. |
| 2026-11-13 (5차) | 화면 문구 한글화 1·2순위(docs/ui.md 8.11): 우클릭 메뉴, 확인·경고 창, 다이얼로그 버튼·거래 입력 창, 보유/관심, 각 탭 툴바, Strategy 탭 라벨·결과 탭·판정 배지. 상위 탭 이름·지표 약어·좁은 숫자 열·메시지 박스 제목은 영어 유지. Qt 기본 대화상자는 `qtbase_ko` 번역 설치. 버튼 기호 검사를 기호·이모지 영역으로 좁혀 한글 허용. CLAUDE.md 언어 규칙 갱신. 검증: pytest 695, ruff 0건, 4개 탭·거래 입력 창·확인 창 렌더링 확인. |
| 2026-11-13 (6차) | Strategy 탭: 종료일 기본값·최댓값을 오늘로(`DEV_END` 이후면 `allow_holdout=True`, 스레드가 `load_dataset`에 전달, 폼·칩·경고 탭·리포트에 "홀드아웃 포함" 표시; 스펙 9-23). 실행 설정을 칩 줄 + 폼 4줄(총 5줄)에서 카드 안 2줄로(접힘 = 칩 한 줄), 2줄째는 버튼 아래까지 전체 폭. 변형 요약 라벨·칩 줄이 최소 폭을 넓히지 않게 해 최소 폭 1226 → 1200px(docs/ui.md 8.12). 검증: pytest 700, ruff 0건. |
| 2026-11-13 (7차) | Strategy 탭: 리서치 실행 뒤에도 설정 폼을 펼친 채로 둠(바로 다시 선택). 매매 내역 탭에 종목명 열(`dataset_info["names"]`, 스펙 9-24). 검증: pytest 700, ruff 0건. |
| 2026-11-13 (8차) | Strategy 유니버스 시장 선택(스펙 9-25): KOSPI·KOSDAQ·NASDAQ·NYSE 다중 선택, 선택 시장 전체에서 원화 환산 시총 상위 N. 미국 가격 원화 환산(일별 USD/KRW), 거래일 합집합 + 시장 휴장일은 직전 종가·거래량 0, L1·RS·이벤트 스터디는 종목별 자기 시장 지수, BM1/BM3은 선택 시장 지수 ETF 혼합, 미국 비용 0.25%+SEC 수수료. KOSPI 단일 선택은 이전 경로 그대로. 4개 시장 30종목 실데이터 확인(개발 구간). 테스트 `test_markets.py`, conftest가 matplotlib 글꼴 설정. 검증: pytest 713, ruff 0건. |
| 2026-11-13 (9차) | `review_codex.md`(2026-11-13) C1~C5와 UI 테스트 실패 2건 후속(스펙 9-26, docs/ui.md 8.12). **C1** BM2 첫 시가 후보는 전일까지의 유동성으로(전일이 없으면 첫 종가에 결정). **C2** 대기 중인 부분매도가 청산 검사를 막지 않고, 청산 신호는 부분매도를 대체하는 전량 주문 하나로. **C3** 다중 시장에서 ETF가 하나라도 없으면 BM1/BM3·상대지표 N/A + 누락 경고. **C4** 시가 체결의 유동성 슬리피지는 전일 ADV. **C5** 미지원(다중 시장) 스냅샷 저장 거부, 열 수 검증. **UI** 설정 폼을 `FlowLayout`(줄바꿈, 첫 줄만 버튼 자리 비움)으로 바꿔 최소 폭이 글꼴·플랫폼에 덜 의존(앱 테마 1170 → 811px), 배치 테스트는 size hint 기준. nex23 스냅샷 42건 수정 전후 지표 동일. 검증: pytest 742(Windows·offscreen 모두), ruff 0건. |
| 2026-11-13 (10차) | Strategy 매매 내역: 탭 표에 매입가·매도가 열, 진입일·청산일 가운데 정렬, 청산 사유 한국어 라벨, 표 위 요약 줄. 상세 팝업 `StrategyTradesDialog`(자세히 보기 / 행 더블클릭·Enter): 시장·수량·매입/매도 금액·손익·비용까지, 정렬·검색·사유 필터·Ctrl+C(docs/ui.md 8.13). `dataset_info["ticker_markets"]` 추가(스펙 9-24). 검증: pytest 748, ruff 0건, nex23 스냅샷 결과로 화면 확인. |
| 2026-11-13 (11차) | Strategy 결과 화면: 전략·BM 여러 개 선택(docs/ui.md 8.14). 세그먼트 다중 선택 + 기준(진한 테두리)이 카드·목표 점검·월별·매매 내역 기준, 비교표 Ctrl/Shift 다중 행 선택과 양방향 동기화, 차트는 선택한 실행 전부(전략 실선·BM 점선, `SERIES_PALETTE`), 낙폭은 기준 채움 + 나머지 선. 비용 전환 시 선택 유지. 검증: pytest 754, ruff 0건, nex23 스냅샷 결과로 화면 확인. |
| 2026-11-13 (12차) | Strategy 매매 내역: 탭의 표를 팝업과 같은 목록으로(`StrategyTradesView` 공유 — 15열, 검색·청산 사유 필터·정렬·요약 줄). 열 폭은 내용에 맞추고, 팝업은 모든 열이 가로 스크롤 없이 보이는 폭으로 연다(화면 폭 한도). 버튼 이름 "자세히 보기" → "크게 보기". `strategy_widgets.fill_trades`/`TRADE_HEADERS` 삭제. docs/ui.md 8.13. |
| 2026-11-13 (13차) | Strategy 탭에 목표 Sharpe 입력(기본 1.0, 이전 1.5 고정). 실행 시점의 값으로 KPI 배지·목표 점검·비교표 H 열/눈금을 판정(docs/ui.md 8.15). 겸사: `SegmentRow.set_keys`가 이전 버튼을 즉시 빼서 연속 호출 시 버튼이 겹치던 문제 수정. pytest 759, ruff 0건. |
| 2026-11-14 | UI Soft Slate 적용(`docs/ui_design_proposal_2026-11-14.md`, docs/ui.md 8.16). **공통**: 청회색 다크 테마 토큰·QSS 재작성, `app_palette()`·`apply_matplotlib_theme()`, 손익·상태·배지·MA·차트 계열색 다크용 명도(대비 4.5:1 계산 확인), primary만 채움 버튼, 약한 격자선, 스핀박스 포함 입력 통일(화살표 SVG), 글자 크기 위계(제목 17pt `NEXUS` + 부제, 본문 9pt, KPI 13pt), 차트 툴팁 흰 바탕 제거. **Strategy**: "표시 중인 결과" 기준 줄, 목표 점검 한 줄 요약 + 상세 토글, 차트 가로 전체 + 결과 탭과 스플리터. **History**: KPI 3개 강조 + 나머지 본문 크기. **Total Assets**: 입력 줄을 차트 위로, 차트·표 스플리터, 빈 상태 문구, 차트에 기간·통화 표기. 검증: pytest 759, ruff 0건, 오프스크린 렌더링(4개 탭, Strategy 결과·펼침). |
| 2026-11-14 (2차) | 제안서 6장(버튼 순서·배치·크기) 적용, docs/ui.md 8.17. `make_button` 기본 버튼 해제(Enter로 저장·실행 안 됨), `button_row`·`fit_to_screen`·`remember_splitter` 추가. 다이얼로그 하단 취소 → 저장/확인, 내보내기 → 닫기, 큰 팝업 초기 크기를 화면 가용 영역으로 제한, 거래 입력 창 최소 폭 400·라벨 정렬, 변형 선택 창 전체 선택/해제를 목록 위로. Universe 종목 추가 2행, History 툴바 2행, Total Assets 입력 줄(날짜 → 이번 주 → 총자산 \| 실시간 묶음 \| 기록 추가). Strategy: 설정 접기/펼치기를 제목 옆, 우측 중지 → 리서치 실행, 리포트 저장을 결과 줄로, 비교 대상(세그먼트)과 상세 기준(콤보) 분리, 실행 비용/표시 비용 구분, 폼 1행 기간·시장·목표 Sharpe, 매매 내역 요약 줄을 표 위로, 차트/표 스플리터 55:45 + 비율 기억. 검증: pytest 772, ruff 0건, 오프스크린 렌더링. |
| 2026-11-14 (3차) | Strategy 탭 버튼 배치 검토 후속(docs/ui.md 8.17 개정). 설정 접기 버튼을 체크 없는 일반 버튼으로(primary처럼 보이던 문제), 변형 선택 버튼 + 선택 요약을 한 묶음으로(줄바꿈으로 갈라지던 문제), 숫자 입력칸 고정 폭 90px(1440px에서 폼 3줄 → 2줄), 폼·칩 순서 통일(홀드아웃은 기간 옆, 목표 Sharpe는 입력 뒤·변형 앞), 비교 대상 세그먼트의 전략/BM 사이 14px 간격(`SegmentRow` `group_starts`). 검증: pytest 775, ruff 0건, 1440/1100px 렌더링. |
| 2026-11-14 (4차) | Strategy 실행 설정 카드 개편(`docs/ui_strategy_settings_2026-11-14.md`, docs/ui.md 8.18). 전역 체크 상태에서 ACCENT 테두리 제거(ACCENT_TEXT 글씨 + LINE_STRONG 테두리), 링크형 `QPushButton#link`. 카드 3열(제목 + 링크형 접기 │ 데이터·규칙/실행 두 줄 │ 중지 → 리서치 실행, 두 줄 높이 가운데), 줄마다 자기 흐름(`_SectionFlow`)과 묶음 구분선, `set_first_line_inset` 제거. 시장·실행 비용을 `SegmentRow`로, 수급 경고를 체크박스 옆 `#warn` 라벨로, 칩 순서 = 폼 순서. 1440px에서 데이터 줄은 2줄(홀드아웃·경고 표시, 8.18 실측). 검증: pytest 779, ruff 0건, offscreen 1440/1100/811px. |
| 2026-11-14 (5차) | Trading Universe 시장 토글: ALL/KOSPI/KOSDAQ 단일 필터 → KOSPI·KOSDAQ·NASDAQ·S&P500 다중 선택(최소 하나, 기본 KOSPI+KOSDAQ, `custom_settings.json` `markets`에 저장, docs/ui.md 2.1). 켜진 시장만 새로고침이 받아 오고(`AllDataFetchThread(markets, include_indices)`), 새로고침 뒤 켠 시장은 그 시장만 받아 합치며 끈 시장 행은 내린다. NASDAQ 100·S&P500 중복 종목은 한 행. 저장된 보기의 market은 목록(옛 단일 값은 1개 목록), 'ALL' 보기는 시장 유지. 시장 행이 바뀐 사이 끝난 가벼운 갱신은 버린다. 검증: pytest 789, ruff 0건, NASDAQ 100·S&P500 각 5종목 실수집, 툴바 렌더링. |
| 2026-11-14 (6차) | Universe 시장 토글 점검(헤드리스 시나리오 49개: 켜기/끄기 × 검색·상태·범위 필터·저장된 보기·새로고침 중 전환·연속 전환·실패·자동 갱신·추세 점수 고정·직접 추가 종목; 가짜 스레드와 실제 QThread 두 방식). 고친 것: ① NASDAQ 100∩S&P500 종목이 NASDAQ을 끄면 S&P500이 켜져 있어도 사라짐 → `also_in` 기록 후 남은 시장으로 이동(`_rows_for_markets`), ② 직접 추가한 종목 하나 때문에 다시 켠 시장을 받지 않음, ③ 수집 중 켠 시장이 finished_all이 isRunning보다 먼저 오면 영영 대기 → 끝난 수집 참조를 비움. 회귀 테스트 3개. pytest 792, ruff 0건. |
| 2026-11-14 (7차) | `review_codex.md` C10~C12 수정(Trading History). **C10**: 보유·매도 종목 시세 결과(`PositionPriceFetchThread`)를 목록 위치가 아니라 요청한 거래 기록에 적용 — 요청 번호를 스레드가 되돌려 주고(`prices_ready(results, request_id)`), 지난 요청·삭제/재적재된 기록·요청 뒤 회사명/티커가 바뀐 기록의 결과는 버림. 조회 중 삭제·매도 확정으로 다른 거래의 종목명·티커·가격이 덮어써지던 문제. 티커 편집 뒤 이름이 확인되면 새 이름으로 재조회. **C11**: 회사명·티커·매수 편집과 티커 이름 반영을 복사본 → 저장 성공 후 적용(실패 시 화면·후속 조회 없음, 매도 편집과 같은 방식). **C12**: 실시간 시세에서 한 시장만 실패해도 실패 유지(docs/ui.md 8.8). 테스트 `test_history_prices.py` 17개(수정 전 코드에서 실패 확인). pytest 809, ruff 0건. |
| 2026-11-15 | Strategy 실행 설정: "변형 선택" → "전략 선택"(버튼·팝업 제목·요약·칩), 처음에는 아무 전략도 고르지 않음(고르지 않고 실행하면 경고). 투자자 수급 체크박스·수급 경고 라벨 삭제 — 고른 전략에 A3~A5가 있을 때만 수급을 받음. 이벤트 스터디 체크박스와 결과 탭 삭제(UI는 `run_event_study=False`, 6-1은 `run.py`로만). 팝업의 수급 차단(회색·체크 불가) 제거. 스펙 9-22, docs/ui.md 8.18. 검증: pytest 808(기존 실패 1건 `test_rows_wrap_on_their_own_at_the_minimum_width`는 변경 전에도 실패, 화면 폭 800 제한), ruff 0건. |
| 2026-11-15 (2차) | Strategy 실행 카드를 `reports/strategy_run_settings_ui.md`대로 재구성(docs/ui.md 8.19): 머리(추세추종 실행 설정 │ 중지 → 백테스트 실행), 공통 입력 시장·시작일·종료일(필드명 위, 안내·오류 아래, 홀드아웃 + "개발 구간만"), 필수 "세부 규칙 선택...", 접히는 세부 조건 2행×3열 + 접힘 요약, 결과 머리(현재 결과 기준 · 다음 실행에 적용 · 리포트 저장). 입력 검사로 실행 버튼 활성화(경고 상자 제거), 실패해도 직전 결과 유지, 탭 순서 지정. `StrategyTab`은 `STRATEGY_TABS` 순회, 하위 탭명 "추세추종". 칩 줄·`_SectionFlow` 삭제, `_GroupFlow` 추가, `QLabel#fieldError`. 검증: pytest 815, ruff 0건, offscreen 600/900/1440px + Windows 글꼴 렌더링. |
| 2026-11-15 (3차) | 개정된 `reports/strategy_run_settings_ui.md`대로 Strategy 실행부를 한 줄 도구 모음으로(docs/ui.md 8.19 개정): 전략 선택기(하위 탭 대신, `StrategyTab` = `QStackedWidget`) · 시장 메뉴 버튼("KOSPI 외 1") · 기간 · 조건... · 중지/백테스트 실행, 좁으면 고정 두 줄. 상태 줄(요약 또는 해결 방법 + 관련 입력 강조, 오른쪽 홀드아웃·개발 구간만 자리). 세부 조건은 `TrendConditionsDialog`(규칙 목록 + 조건 2행×3열, 취소/적용)로, 탭 값은 `TrendConditions`. 실행 중 입력 잠금. 앞 차수의 카드·`_GroupFlow` 삭제. 검증: pytest 811, ruff 0건, Windows 글꼴 1280/700px·대화상자 렌더링. |
| 2026-11-15 (4차) | MA 차트 팝업 한글화(docs/ui.md 8.11): `StockMaDialog` 창 제목·기간 버튼(1개월~3년·전체, 내부 키 1M~All 유지)·표시 토글(종가·이격도·동일가중·거래량)·축/호버/끝 라벨·투자자별 순매수·선물 표와 머리글·기업 정보·Y 확대/축소, Trend Score 섹션(추세 점수·주간 평균·게이트·통과/미달), `IndexMaDialog` 제목·범례, 차트 로딩 상태 문구. MA5~MA50·RSI 약어, Top/Bottom 순위 태그는 영어 유지. 검증: pytest 811, ruff 0건. |
| 2026-11-15 (5차) | 화면 언어 선택(한국어 기본·영어, docs/ui.md 8.20). 머리 오른쪽 태극기·성조기 버튼(`ui/language_switch.py`, SVG 아이콘), 바꾸면 확인 후 재시작, `QSettings` `ui/language`에 저장. `src/i18n.py` `tr(한국어, English)`로 UI 문구 약 390곳(Universe·History·Total Assets·Strategy·MA 차트·Trend Score·`variant_info.py`)에 영어 짝을 붙임; 영어는 한글화 이전 커밋의 원래 문구를 우선 사용. 저장 값(보기 이름·상태 코드·DB)은 그대로, 표시만 번역(내장 보기 이름은 `view_display_name`). Qt `qtbase_ko`는 한국어일 때만. 검증: pytest 819, ruff 0건, 영어 모드 MainWindow 전체 문구 650개 중 한글 0(태극기 툴팁 '한국어' 제외), 화면 캡처. |
| 2026-11-15 (6차) | 연말 인출·입금 연도별 출력(`docs/yearly_flows_report_2026-11-15.md` 3장 1~5번, 스펙 9-27). 엔진이 연도별 시작/리셋 직전 평가액을 기록, `metrics.cash_flow_ledger` → `summarize_run['cash_flows_by_year']`, research 리포트 "Yearly cash flows (1x, KRW 만 원)"와 robustness 성과표 끝에 같은 표(BM4 제외). 트림 체결가-종가 차이는 엔진이 어디에도 넣지 않아 `fill_gap`으로 따로 기록(실데이터 1× 최대 990원/년). 리셋·비용·체결 불변: nex23 42건 기존 지표가 변경 전 코드와 비트 단위로 같음, 5장 기대값 13행 만 원 단위 일치. Strategy 탭 노출은 안 함. 검증: pytest 834, ruff 0건. |
| 2026-11-15 (7차) | Trading History: 거래 행을 클릭한 뒤 다른 곳을 눌러도 선택 강조가 남던 문제. `ui.common.clear_selection_on_outside_click(table, focus_root)` — 탭 바탕에 ClickFocus를 줘서 라벨·빈 바탕 클릭도 표의 포커스를 빼앗게 하고, `QApplication.focusChanged`에서 같은 창 안의 다른 위젯으로 포커스가 가면 선택 해제. 우클릭 메뉴·편집/삭제 확인 창(다른 창)과 앱 전환은 유지. 처음 쓴 앱 전역 이벤트 필터는 위젯 파괴 중 접근 위반으로 테스트가 죽어 버림. 검증: pytest 838, ruff 0건, 실제 탭에서 라벨·바탕·검색창·표 빈 곳 클릭 확인. |
| 2026-11-15 (8차) | 툴바 버튼·필터를 누르면 표 열 폭이 바뀌던 문제(Trading History, Trading Universe 둘 다). 두 표 모두 열 폭을 viewport 폭에서 계산하는데, 세로 스크롤바가 AsNeeded라 행 수가 바뀌면 스크롤바 폭만큼 viewport가 변해 History는 Company 열(270↔256px), Universe는 비율 열 전체(합 1460↔1474px)가 흔들렸고, History는 첫 표시 때 가로 스크롤바가 잠깐 생기기도 했다. 세로 스크롤바를 AlwaysOn으로(`history_tab._build_history_table`, `widgets.StockTable`). 회귀 테스트 `test_table_column_widths.py`(수정 전 코드에서 3건 모두 실패 확인). pytest 841, ruff 0건. |
| 2026-11-15 (9차) | MA 차트 기간 버튼을 표시 행으로(docs/ui.md 8.21, 검토 후 2안). 표시·기간·패널 세 묶음을 `FlowLayout`으로 — 넓으면 한 줄, 좁으면 묶음 단위 줄바꿈. 창 최소 폭 1163 → 902px(한 줄 고정안은 1385px). 기간 버튼 52px 고정 폭이 "3개월"·"6개월" 첫 숫자를 자르던 문제도 수정(가장 긴 라벨 폭으로 통일). 테스트 2개. pytest 843, ruff 0건, Windows 렌더링 1920/1280/1000px. |
| 2026-11-15 (10차) | MA 차트 y축 숫자 잘림: 좌우 여백이 고정 비율(7.5%·10%)이라 좁은 창에서 "100,000"·축 이름이 잘림. `StockMaDialog._fit_margins`가 y축 눈금·축 이름과 끝 값 라벨 폭을 재서 픽셀 단위로 여백 설정(토글·기간·크기 변경 때 재측정). 회귀 테스트 1개(수정 전 -39px로 실패 확인). pytest 844, ruff 0건, Windows 렌더링 1000/1280/1920px. |
| 2026-11-15 (11차) | MA 차트: 표시·패널 토글과 기간 변경에 가격 그래프가 움직이던 문제(가로: 끝 값 라벨 폭에 맞춰 여백이 바뀜, 세로: 보이는 패널 수로 재배치). 사용자 결정 "칸마다 고정 자리" — 패널마다 고정 칸(3:1:1:1), 끈 칸은 빈 자리. 여백은 시작·크기 변경 때 가장 넓은 경우로 재고 이후엔 잘릴 때만 넓힘(`_fit_margins(grow_only)`). 회귀 테스트 1개(수정 전 실패 확인). pytest 845, ruff 0건, Windows 렌더링. |
| 2026-11-15 (12차) | MA 차트 MA5 색이 종가와 구분 안 됨: MA_RAMP 첫 단계 `#ece6ff`가 종가 `#f0f2f6`와 CIE ΔE 11.7. 같은 보라 색상(HLS 252°)에 바탕 대비 6.2/4.9/3.9/3.1:1로 단계를 다시 잡음(`#d1c5ff`/`#bcabff`/`#a993ff`/`#977cff`): MA5↔종가 ΔE 32, 이웃 단계 ΔE ~16(MA20↔MA50은 8이던 것). 주요 지수 MA 창도 같은 램프. pytest·ruff 통과, Windows 렌더링 전후 비교. |
| 2026-11-15 (13차) | MA 차트: 기준일을 버튼 행 끝으로, 오른쪽 패널 "추세 점수" 제목을 첫 버튼 줄 높이로(패널 위 여백 = 머리 행 높이), 실행 전 안내 문구 삭제(`NOT_RUN_TEXT` 제거), 투자자 표 외국인·기관·개인 열 같은 폭(`_fit_side_table(equal_cols)` + Stretch). 테스트 1개 추가·3개 갱신. pytest 846, ruff 0건, Windows 렌더링 1920/1280px. |
| 2026-11-15 (14차) | MA 차트 오른쪽 패널: 표 제목(버튼 행 높이) → 표(차트 윗선 높이) → 추세 점수 → 기업 정보(사용자 결정 "추세 점수를 표 아래로"). `_align_right_panel`이 표시·크기·분할선 변경 때 실제 위치로 맞춤(1920/1280px, 추세 점수 실행 전후 모두 0px 차이). 하단 닫기 버튼 삭제. 테스트 2개(기존 1개 대체). pytest 847, ruff 0건. |
| 2026-11-15 (15차) | Trading Universe 종목 추가: "추가할 시장" 라벨 삭제, 시장 콤보에 NASDAQ·NYSE 추가(`ADD_TICKER_MARKETS`), 입력 안내가 시장에 따라 바뀜. NASDAQ·NYSE로 추가한 종목은 시장 토글이 없어 늘 보이게 `matches`가 토글 있는 시장만 거름(이전 그대로면 항상 숨겨짐). KO(NYSE)·AAPL(NASDAQ) 실조회 확인. 테스트 3개. pytest 850, ruff 0건. docs/ui.md 2.1. |
| 2026-11-15 (16차) | MA 차트 추세 점수 영역: 점수가 없어도 같은 행·카드·조건 줄을 "-"/중립 배지로 유지해 높이 고정(세 상태 211px), 채점 제외는 "채점 제외"+툴팁. 글자 크기 검토: 제목 8pt 흐림 → 표 제목과 같은 9pt 굵게, 핵심 점수(주간 평균) 9pt → 13pt, 카드 값 13pt → 12pt(전역 QSS가 setFont를 덮어쓰던 것 수정), 내용별 높이 차이는 `_lock_height`로 고정. 테스트 2개(기존 1개 대체), 3개 갱신. pytest 851, ruff 0건. |
| 2026-11-15 (17차) | 남은 영어 문구 한글화(검토 1번): 표·그룹 머리글, KPI 띠, 헤더 자동 갱신·툴팁, 상태바·푸터·진행 문구(스레드 14곳, 전략 패키지 진행 24곳), 빈 표 안내, 차트 축·범례, Strategy 면책 문구 등 약 300곳을 `tr()`로. 저장·비교되는 값은 그대로 두고 표시만 번역(`_part_label`, `_market_label`, `SUMMARY_LABELS`, `run_label`). 열 폭: Total Assets 'KOSPI 대비 (%p)' 90→96, Strategy 표는 머리글 폭에 맞춤(`_fit_headers`, 영어에서 잘리던 머리글도 해결). 검증: pytest 851, ruff 0건, 메인 창 348개 문구 — 영어 모드 한글 0, 한국어 모드 영어는 유지 목록(파일명·약어·고유명)만, Windows 렌더링. 같은 날: `.gitattributes`로 LF 통일, GitHub Actions CI(ruff·pytest), CI 첫 실행이 잡은 투자자 표 열 폭 버그 수정. |
| 2026-11-15 (18차) | 코드 검토 8·9·11번. **8**: `KpiCard.set()`이 값 라벨 스타일을 색 한 줄로 덮어써 카드별 값 크기를 줄 수 없던 것 — `KpiCard(value_pt=)`·`set_value_size()`, `set()`은 색과 크기를 함께 씀(추세 점수 카드의 우회 코드 제거). **9**: 둘 다 9pt이던 `FONT_SMALL`을 `FONT_BODY`로 합침(8개 파일 35곳, 화면 변화 없음). **11**: 메인 창 바탕(`central_widget`)에 ClickFocus — 헤더·빈 곳을 눌러도 Trading History 선택 해제. 테스트 2개. pytest 853, ruff 0건, 실제 메인 창에서 확인. |
| 2026-11-15 (19차) | 문서 정리(코드 검토 14번): roadmap.md 846→222줄 — 끝난 2·3·6·7장, 1장의 기존 최적화 내역, 2026-08~10 변경 이력을 `docs/history/roadmap_2026-08_10.md`로(절 번호 그대로, 코드 주석의 "roadmap 2-4" 등이 그대로 찾아짐). 머리말·현황 요약(Strategy 탭·소스 구조·CI·언어)을 현재 상태로 갱신. docs/ui.md 1029→844줄 — 옛 Strategy 탭(5장), Phase 0 제안 코드(6.1·6.2), 당시 적용 계획(7장)을 `docs/history/ui_archive_2026-09_10.md`로, 각 번호 자리에 한두 줄 요약과 안내. 옮기기 전 문서의 모든 줄이 새 문서들에 있는지 기계 대조(바꾼 머리말·목차 8줄 외 누락 0). |
| 2026-11-15 (20차) | 코드 검토 5~7번. **7**(PR #27): ruff 규칙 F → F·E7·E9·B·UP·PIE, 위반 113건 0으로(B905 zip strict는 동작 변화로 제외). **5**: `ui/widgets.py` 1,457→31줄(재노출) — `table_items`·`universe_columns`·`universe_table`·`filter_popups`·`table_headers`·`heat_legend`로, `stock_ma.py` 1,311→1,074줄(오른쪽 패널 `stock_ma_side.py`, 헬퍼 `stock_ma_helpers.py`), 거래 내역 창 `__init__` 240→50줄. 전후 화면 구조·픽셀 덤프 2,263줄 완전 일치. **6**: data·threads의 `except Exception` 117곳 중 59곳을 실제 예외로 좁힘(`PAYLOAD_ERRORS`, `KIS_ERRORS`), 58곳은 경계로 남기고 이유 주석 + 추적 로그(로그 없던 8곳 추가). 비현실적 예외를 넣던 테스트 3개 수정. 함께: 매칭 거래 없는 청산 종목의 거래 내역 창이 AttributeError로 안 열리던 버그 수정(테스트 추가). pytest 854, ruff 0건. |
| 2026-11-15 (21차) | 미국 시장 이름 정리(코드 검토 13번, 사용자 결정 "토글=지수명, 추가·입력=미국 하나"): Universe 토글 라벨 NASDAQ→NASDAQ 100, S&P500→S&P 500(키 유지, 버튼 폭 글자에 맞춤). 종목 추가와 거래 입력 창의 시장은 KOSPI·KOSDAQ·미국(저장 값 US) — 이전의 NASDAQ·NYSE(추가), NASDAQ·NASDAQ 100·NYSE·AMEX(거래 입력) 선택지 정리. 기존 저장 값은 그대로 읽힘. Strategy의 거래소 기준 NASDAQ·NYSE는 유지. 테스트 2개 추가·4개 갱신. pytest 856, ruff 0건. |
