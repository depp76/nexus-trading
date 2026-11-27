# Nexus Trading — 발전 로드맵

> 기준일: 2026-11-15 (최초 작성 2026-08-21)
> 현재 상태: PyQt6 단일 사용자 데스크톱 앱 (한국/미국 주식 포트폴리오 추적 + Trend Following 리서치) — git(PR 단위),
> GitHub Actions CI(ruff·pytest), pytest 853 통과, ruff(F) 0건, 화면 언어 한국어/영어
> 구조와 규칙은 `CLAUDE.md`, 화면 규칙은 `docs/ui.md`, 전략 스펙은 `src/strategy/trend_following/trend_following.md`
> **2026-11-15 정리**: 끝난 2·3·6·7장과 2026-08~10 변경 이력은 `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)로 옮겼다
> (절 번호 그대로 — 코드 주석의 "roadmap 2-4", "roadmap 6-1a", "roadmap 7-2c" 등은 그 문서에 있다).

---

## 목차

1. [현황 요약](#1-현황-요약)
2. ~~단기 개선~~ · 3. ~~중기 개선~~ — 완료, `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)
4. [장기 개선 (3개월+)](#4-장기-개선-3개월)
5. [우선순위 매트릭스](#5-우선순위-매트릭스)
6. ~~코드 분석 기반 개선 항목~~ · 7. ~~UI 개선 항목~~ — 완료, `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)
8. [변경 이력](#변경-이력)

---

## 1. 현황 요약

### 현재 기능

| 탭 | 기능 |
|----|------|
| **Trading Universe** | KOSPI / KOSDAQ / NASDAQ 100 / S&P 500 시장 토글 워치리스트, 실시간 시세, MA 지표, 범위 필터·저장된 보기, 추세 점수 상위/하위 고정, 종목 직접 추가(NASDAQ·NYSE 포함) |
| **Trading History** | 거래 기록 수동 입력, SQLite 영속화 (`portfolio.db`), 오버라이드 지원 |
| **Total Assets** | 날짜별 자산 합계 테이블 + matplotlib 그래프, 환율·KOSPI 연동 |
| **Strategy** | Trend Following 리서치 백테스트(스펙 `trend_following.md` **v07** — 1차 검증 대상·개발 기준, v06은 중간 단계; 기준선 10-14): 실행 도구 모음, KPI 카드·NAV 차트·결과 표, 리포트 저장 |
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
│   ├── ma_chart.py / strategy_check_section.py
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
> 6-x 행은 섹션 6(코드 분석 기반 개선 항목, `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)) 참고.

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

2026-08 ~ 2026-10의 행은 `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)로 옮겼다.

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
| 2026-11-15 (19차) | 문서 정리(코드 검토 14번): roadmap.md 846→222줄 — 끝난 2·3·6·7장, 1장의 기존 최적화 내역, 2026-08~10 변경 이력을 `docs/history/roadmap_2026-08_10.md`(삭제됨, `git show cc28828:docs/history/roadmap_2026-08_10.md`)로(절 번호 그대로, 코드 주석의 "roadmap 2-4" 등이 그대로 찾아짐). 머리말·현황 요약(Strategy 탭·소스 구조·CI·언어)을 현재 상태로 갱신. docs/ui.md 1029→844줄 — 옛 Strategy 탭(5장), Phase 0 제안 코드(6.1·6.2), 당시 적용 계획(7장)을 `docs/history/ui_archive_2026-09_10.md`(삭제됨, `git show cc28828:docs/history/ui_archive_2026-09_10.md`)로, 각 번호 자리에 한두 줄 요약과 안내. 옮기기 전 문서의 모든 줄이 새 문서들에 있는지 기계 대조(바꾼 머리말·목차 8줄 외 누락 0). |
| 2026-11-15 (20차) | 코드 검토 5~7번. **7**(PR #27): ruff 규칙 F → F·E7·E9·B·UP·PIE, 위반 113건 0으로(B905 zip strict는 동작 변화로 제외). **5**: `ui/widgets.py` 1,457→31줄(재노출) — `table_items`·`universe_columns`·`universe_table`·`filter_popups`·`table_headers`·`heat_legend`로, `stock_ma.py` 1,311→1,074줄(오른쪽 패널 `stock_ma_side.py`, 헬퍼 `stock_ma_helpers.py`), 거래 내역 창 `__init__` 240→50줄. 전후 화면 구조·픽셀 덤프 2,263줄 완전 일치. **6**: data·threads의 `except Exception` 117곳 중 59곳을 실제 예외로 좁힘(`PAYLOAD_ERRORS`, `KIS_ERRORS`), 58곳은 경계로 남기고 이유 주석 + 추적 로그(로그 없던 8곳 추가). 비현실적 예외를 넣던 테스트 3개 수정. 함께: 매칭 거래 없는 청산 종목의 거래 내역 창이 AttributeError로 안 열리던 버그 수정(테스트 추가). pytest 854, ruff 0건. |
| 2026-11-15 (21차) | 미국 시장 이름 정리(코드 검토 13번, 사용자 결정 "토글=지수명, 추가·입력=미국 하나"): Universe 토글 라벨 NASDAQ→NASDAQ 100, S&P500→S&P 500(키 유지, 버튼 폭 글자에 맞춤). 종목 추가와 거래 입력 창의 시장은 KOSPI·KOSDAQ·미국(저장 값 US) — 이전의 NASDAQ·NYSE(추가), NASDAQ·NASDAQ 100·NYSE·AMEX(거래 입력) 선택지 정리. 기존 저장 값은 그대로 읽힘. Strategy의 거래소 기준 NASDAQ·NYSE는 유지. 테스트 2개 추가·4개 갱신. pytest 856, ruff 0건. |
| 2026-11-15 (22차) | 연말 트림 체결가-종가 차이를 그해 결과에 포함(코드 검토 12번, 스펙 9-28): `_year_end_reset`의 그해 결과를 트림 직후 실제 평가액으로 — 수수료·세금만 빼서 체결가 차이(실데이터 최대 990원/년)가 NAV·인출 어디에도 없던 것. `fill_gap` 폐지(Σ연도별 손익 = 총손익 정확). nex23 42건 중 8건만 변화, 최대 Sharpe −0.0001·CAGR −0.002%p·MDD −0.005%p(표시 자릿수 아래, 9-19 판정 불변). 회귀 테스트(수정 전 993원 차이로 실패). |
| 2026-11-15 (23차) | 언어 전환을 재시작 없이(코드 검토 10번): `main.switch_language`가 언어를 저장하고 `ui`·`threads` 모듈을 다시 불러온 뒤 메인 창을 새로 만들어 바꿔 끼움(Qt 번역기도 교체). 각 탭 `export_state()`/`import_state()` — Universe 표·필터·추세 점수 고정, History 기간·묶음·오래된 시세, Total Assets 통화, Strategy 조건·시장·기간·마지막 결과와 선택(새 언어로 다시 그림). 진행 중인 백테스트만 확인을 묻고, 진행 중이던 새로고침은 새 창에서 다시 시작. 닫기 처리는 `stop_background_work()`로 분리. 테스트: 별도 프로세스에서 한→영→한 전환(임시 DB·설정). |
| 2026-11-15 (24차) | 남은 긴 함수 나누기(동작 변화 없음): `stock_ma.py` 1,074→약 380줄 — 그리기·마우스 오버·이동/확대/기간을 믹스인(`stock_ma_plot`·`stock_ma_hover`·`stock_ma_view`)과 상수 모듈로, 전후 화면 구조·픽셀 덤프 2,321줄 일치. `yahoo.fetch_us_stock_data_bulk` 218→30줄(모듈 헬퍼 11개, 공유 상태 `_UsBulkRun`), `PositionPriceFetchThread.run` 193→13줄(메서드 8개); 외부 호출을 가짜로 바꾼 17개 시나리오의 결과·신호 순서·로그 564줄 일치(일부러 넣은 변경 2곳을 잡아냄 확인). pytest 858, ruff 0건. |
| 2026-11-15 (25차) | 남은 긴 함수 5개 나누기(동작 변화 없음): `StockTable._populate_row` 174→20, `fill_table_rows` 148→28, `TradingRecordTab._build_ui` 174→13(세 탭 화면 구조·셀 역할/색/툴팁·열 폭·픽셀 덤프 3,906줄 일치), `theme.app_qss` 189→19(영역별 빌더 9개, 결과 문자열 동일), `research.render_markdown` 146→16(절별 헬퍼 11개, 합성·2시장·홀드아웃·nex23 스냅샷 등 23개 출력 바이트 일치). 이제 150줄 넘는 함수 없음(최장 141줄). pytest 858, ruff 0건. |
| 2026-11-15 (26차) | 100줄 넘던 함수 14개 나누기(동작 변화 없음): 화면 6개(MainWindow 생성 141→26, 거래 기록 수정 140→26, 종목별 요약 128→29, Universe 화면 126→18, Total Assets 지표 119→33, 자산 그래프 102→27; 언어 전환 포함 덤프 3,326줄 일치), 전략 4개(다중 시장 불러오기 134→30, 불러오기 130→54, 데이터셋 만들기 124→42 — 시세·수급·CD91 중복 코드 공유, 규칙 설명 103→18; 스냅샷 리포트 포함 덤프 일치), 데이터 4개(종목 조회 132→10, 분기 재무 120→13, 등락률 101→45, KIS 실시간 101→46; 135개 시나리오 일치, 변이 검사). 함께: `test_language_switch.py`가 Windows에서 실제 레지스트리 ui/language에 쓰던 문제 수정(QSettings 클래스를 임시 INI로 교체, 전체 테스트 전후 레지스트리 동일 확인). pytest 858, ruff 0건. |
| 2026-11-15 (27차) | 80줄 넘던 함수 9개 나누기(동작 변화 없음): MA 차트 창 생성 87→28, 보유 요약 계산 86→46, 조건 창 86→23, History 도구 줄 84→29, 추세 점수 영역 83→11, Strategy 결과 탭 80→11(한·영 덤프 4,768줄 일치, 무작위 포지션 400세트 포함), 국내 시세 수집 85→23, 강건성 실행 82→30, 신호 계산 81→43(42개 시나리오·nex23 스냅샷 일치, 함수별 변이 검사). 이제 80줄 넘는 함수 없음(최장 79줄). 레지스트리 전후 동일. pytest 858, ruff 0건. |
| 2026-11-16 | Trading Universe 툴바 정리(docs/ui.md 2.1·8.17): 툴바 버튼 9개를 한 폭으로(가장 긴 라벨, 상태 라벨 3개·"필터 (n)" 포함 — 라벨이 바뀌어도 폭 불변), 묶음 사이 12px. 상태 순환 버튼 라벨을 종목(전체)/종목(보유)/종목(관심)으로("전체"가 모든 시장으로 읽힘; 버튼 묶음안은 철회). 종목 추가 2행을 없애고 종목 추가 버튼이 `AddTickerDialog`를 열도록(표가 한 줄 위로). NASDAQ 100 토글 글자를 "NASDAQ"으로(툴팁·보기 이름은 그대로) — 한 폭 규칙에서 탭 최소 폭 1558 → 1240px(버튼 82px). pytest 861, ruff 0건. |
| 2026-11-16 | Trading History 툴바를 Universe 방식 한 줄로(docs/ui.md 8.17): 실시간 검색(종목명·코드, 보유·매도 완료 모두; 검색 버튼 삭제), 보유 종목 콤보 삭제 → 종목 거래 이력은 표 우클릭 메뉴로, 기간·보기·30일 규칙을 순환 버튼(기간(전체/1개월/3개월/올해)·보기(상태별/월별)·시세(30일 숨김/모두 표시))으로, 버튼 7개 한 폭(110px)·묶음 간격 12px, 예수금 조회는 그대로. `tests/test_history_toolbar.py` 7건. pytest 868, ruff 0건. |
| 2026-11-16 | Total Assets 툴바 한 줄(docs/ui.md 8.17): 통화 순환 버튼 통화(KRW/USD), 현재 총자산(실시간)·주간 손익을 툴바로, 입력 줄(날짜·이번 주·총자산)은 `AddRecordDialog`로(자동 채움·manual 규칙 그대로, 자정 갱신 타이머 삭제). Universe·History·Total Assets 툴바 버튼 **세 탭 공용 폭**(`ui.common.toolbar_button_width`, 실측 94px); History 시세 라벨은 시세(30일)/시세(전체)로 줄여 Universe 1348px. pytest 876, ruff 0건. |
| 2026-11-16 | Total Assets 내보내기 삭제(사용자 결정): 툴바 버튼, `_on_export_clicked`(KRW+USD 전체 Excel/CSV), 그것만 쓰던 `openpyxl`(requirements.txt). 그래프 창 리포트의 CSV 내보내기는 그대로. |
| 2026-11-16 | Strategy 실행 툴바를 다른 탭 방식으로(docs/ui.md 8.17): 시장 메뉴 버튼 → Universe 같은 토글 4개(KOSPI/KOSDAQ/NASDAQ/NYSE), 시장·조건·중지·실행·리포트 저장을 네 탭 공용 폭(94px)으로, 실행 라벨 "백테스트 실행" → "백테스트"(102px이 Universe를 1440px 창 밖으로 밀어서). pytest 877, ruff 0건. |
| 2026-11-16 | Strategy 결과 **초기화**(docs/ui.md 8.19 결과 머리): 리포트 저장 앞 버튼, 확인 후 `clear_result()`로 실행 전 화면(결과·표·차트·경고 비움, 폼 유지), 결과 있을 때만·실행 중 잠금. `TestResetAfterRun` 4건, pytest 881. |
| 2026-11-16 | 상위 탭 4개 한 폭(`EqualWidthTabBar`), Strategy "조건..." → "매매전략..."(버튼·대화상자 제목·오류 문구), 백테스트 뒤 아래 영역 고정: 결과 머리·선택 줄 자리 유지(`retainSizeWhenHidden`, BTN_H 한 줄), 결과 기준 한 줄+툴팁, KpiCard 배지 자리 유지 (docs/ui.md 8.17·8.19). pytest 884. |
| 2026-11-16 | **속도 개선**(실측 기준; 측정 스크립트는 일회용). ① HTTPS 연결마다 certifi 묶음을 다시 읽던 것(requests 2.32.3+: 새 연결당 ~0.1초, GIL로 직렬화 — 30연결이면 3초)을 `data.cache.SharedTlsAdapter`로 프로세스당 1회로(Naver·Yahoo·MOF·KIS 세션; 검증은 그대로 — self-signed·wrong-host·expired 거부 확인). ② JP10Y: 매 새로고침 1.2MB CSV 재다운로드(2.6초) → 30분 창 + 이번 달 CSV만 받아 캐시에 덧씌움(0.09초, 월이 바뀌면 전체). ③ 지수 단계를 시장과 동시에(`AllDataFetchThread`). ④ 투자자 수급 동시 2 → 8(`FLOW_FETCH_CONCURRENCY`; 16종목×1,300행 19.3초 → 5.1초, 행 수 동일). ⑤ FinanceDataReader(plotly 포함)·yahooquery 지연 import(`LazyModule`). ⑥ Total Assets 추이 차트는 보일 때만 그림. 결과: 첫 새로고침 12~14초 → 3.7~4.0초, 이후 새로고침 ~3초 → 1.3~1.5초, 60초 틱 1.4~1.5초 → 1.0~1.2초, 창이 뜨기까지 ~2.7초 → ~2.1초(import 1.25 → 0.94초). 손대지 않은 것: 표 채우기·필터(보유 데이터 규모에서 50ms 이하), 백테스트 계산(7변형×3비용 1.2초), Qt 글꼴 DB 첫 조회(0.2~0.5초, 글꼴 목록을 바꿔도 앱 전체에서는 줄지 않음). `tests/test_speed_2026_11.py` 14건, pytest 898. |
| 2026-11-16 | Total Assets 탭 안 추이 차트 삭제(사용자 결정: 그래프는 툴바의 그래프 버튼 창으로): 차트·차트/표 스플리터·차트 더블클릭 제거, 주간 표가 남는 높이 전부(docs/ui.md 4.4). 같은 날 속도 개선의 '보일 때만 그림'도 함께 없어짐. pytest 898. |
| 2026-11-16 | Total Assets 그래프 창 수치 겹침 해소(docs/ui.md 4.4.1): 점마다 수치(46주×2계열) 삭제 → 오른쪽 끝 최신값(겹치면 벌림)·최고점 1개·마우스 세로선과 한 줄 읽기값(mplcursors 대체)·월 눈금·억 단위·총자산/KOSPI 사이 음영. `tests/test_assets_graph.py` 7건, pytest 905. |
| 2026-11-16 | Total Assets 그래프 주별 마커(docs/ui.md 4.4.1 #8): 세 선의 매주 점(총자산 찬 점·총자산($) 빈 점·KOSPI 작은 점). 마우스가 마커 위(10px)에 있을 때만 그 마커를 키우고 옆에 값 상자(계열·날짜·수익률·금액); 세로선과 위쪽 한 줄 읽기값은 삭제(사용자 결정). `tests/test_assets_graph.py` 9건. |
| 2026-11-16 | Total Assets 그래프: 어느 마커에 올려도 그 주의 세 값(총자산·총자산($)·KOSPI)을 한 상자에(줄마다 계열 색, `AnnotationBbox`), 그 주의 세 마커를 함께 키움(docs/ui.md 4.4.1 #8). `tests/test_assets_graph.py` 9건. |
| 2026-11-16 | MA 차트 표시 행(docs/ui.md 8.21 개정): 버튼 한 폭, 묶음 제목, [이동평균 MA10·MA20·MA50(·동일가중)] [보조지표 이격도·거래량·RSI + 기간 한 묶음]. 종가 버튼 삭제(종가 선 항상 표시), MA5 삭제(`_MA_KEYS`). |
| 2026-11-16 | MA 차트 RSI 패널을 기본으로 켬(거래량·이격도·RSI 모두 켠 채로 열림; docs/ui.md MA 차트 패널 토글). |
| 2026-11-16 | 앱 제목 NEXUS → Nexus Trading. Strategy 매매 내역 탭: 검색·청산 사유 필터 삭제, 요약 줄 오른쪽 끝에 '자세히'(이전 '크게 보기'); 팝업은 필터 유지(`StrategyTradesView(filters=False)`). |
| 2026-11-16 | 갱신 상태(자동 갱신·최근 갱신)와 국기 버튼을 제목 줄에서 상위 탭 줄 오른쪽 끝으로(Trading Universe 탭과 같은 높이, `QTabWidget.setCornerWidget`; docs/ui.md 8.17 헤더 갱신 상태). |
| 2026-11-16 | Trading History KPI: 총자산 = 평가액 + 예수금 + 출금(원금이 얼마가 됐는지), 바로 오른쪽에 원금 입력칸, 총자산 아래 '원금의 n배'; '누적 자산' 칸 삭제(같은 값). 같은 날 키움 명세서 대조로 `portfolio.db` 거래 기록 정정(백업 archive/manual_before_kiwoom_fix_20261116_153310). |
| 2026-11-16 | Trading History KPI 오른쪽 묶음: 매입 원가 · 평가액 · 평가 손익(운용 자금 = 예수금 + 매입 원가 대비 %, `history_calc` `capital`/`pos_pl_pct_capital`) · 예수금 · 출금. 값 아래 설명 문구 삭제(툴팁으로), 입력칸 세 개에 점선 밑줄. 재입금은 출금(순출금)에서 뺌. |
| 2026-11-16 | Universe '추세 점수' → **'전략 신호'**(메뉴로 A0~C3 선택, `strategy.trend_following.universe_signals`; 스펙 9-29): C는 기존 주간 추천, A는 오늘 진입 신호 / 추세 이탈 신호, B는 52주 고가 근접 순 / 추세 이탈 신호; A3~A5는 수급 로드. 종목 추가 버튼을 검색창 오른쪽으로. `tests/strategy/trend_following/test_universe_signals.py` 8건 등, pytest 918. |
| 2026-11-16 | MA 차트 오른쪽 칸 '추세 점수' → **'전략 점검'**(전략 콤보 A0~C3; A·B는 이 종목만 즉시 점검 `check_stock`, C는 유니버스 결과 또는 칸 안 '계산'; Universe와 결과 공유). 전략 신호 접점 **`strategy/signal_api.py`**(등록부 + 결과 타입)를 새로 두어 Universe·MA 차트가 전략 패키지를 직접 부르지 않게 함 — 새 전략은 `PROVIDER` 하나로 두 화면에 붙음. 스펙 9-29, docs/ui.md 8.22, pytest 928. |
| 2026-11-16 | 전략 연결 구조 정리(사용자 지시 1~7): 단일 전략 등록부 `strategy/registry.py`(Strategy 탭 페이지 + 신호 제공자), 공용 입력 `strategy/common/`(`PriceBook` 이동, `load_daily_inputs`), 제공자가 데이터를 직접 로드(`load`)·적용 시장(`markets`)·목록 문구(`labels`)를 선언, 종목 점검을 공통 `StockCheck`(점수·지표 카드 포함)로 그려 `trend_score_section.py` 삭제, `SignalCache`(계산한 날만 유효), 이름 정리(`universe_signals` → `signal_provider`, `TrendScoreThread` → `SignalPicksThread`, `trend_score_btn` → `signal_btn` 등). pytest 931. |
| 2026-11-16 | MA 차트 '전략 점검' 크기 고정: 전략을 바꿔도 칸 높이·폭 불변(제공자 `templates`로 가장 큰 모양을 재서 본문 높이·최소 폭 고정, docs/ui.md 8.22). 전략 선택을 콤보 → 체크박스 메뉴(Universe와 같은 방식), 칸은 세로 고정이라 남는 높이는 투자자 표가 갖고 칸은 '기업 정보' 위로. 전략을 바꿔도 '전략 점검' 제목 줄이 움직이지 않게 본문 고정 높이 적용 버그와 기준일 줄 높이 수정. pytest 933. |
| 2026-11-17 | MA 차트에 **시장지수** 토글(MA50 오른쪽, 기본 꺼짐): 종목 시장(KOSPI/KOSDAQ/NASDAQ 100/S&P500)의 지수를 가격축에 겹쳐 그림 — 보이는 구간 첫날 종가에 맞춰 다시 스케일(기간·팬·줌마다), 끝 라벨·호버는 지수 값 자체. 데이터는 `data.market.attach_market_index`(`StockMaThread`가 `MarketIndex` 열 조인, 실패 시 열 없음 → 버튼 없음). pytest 936, ruff 0건. |
| 2026-11-18 | MA 차트 버튼 행을 **한 줄**로: 이동평균은 체크박스 드롭다운(MA10/20/50·동일가중·시장지수 여러 개 선택, 버튼에 켠 선 이름 "MA10 외 2"), 기간은 콤보박스(이동평균 버튼과 같은 폭), 보조지표(이격도·거래량·RSI)는 토글 버튼 유지 — `FlowLayout` → `QHBoxLayout`, `_period_buttons` → `_period_combo`. docs/ui.md 8.21, pytest 939, ruff 0건. |
| 2026-11-18 | 추세 이평 60일 통일 1단계 **[1a] 완료** (MA 차트·주요 지수 이평 창, `docs/ma60_unification_2026-11-18.md`): 커밋 `7335695`. pytest 955, ruff 0건. |
| 2026-11-18 | 추세 이평 60일 통일 1단계 **[1b] 완료** (돌파 채널·KOSPI/KOSDAQ 국면 음영, signal_api 1.7): 커밋 `0cec010`. pytest 976, ruff 0건. |
| 2026-11-18 | 추세 이평 60일 통일 2단계: v07 가설 카드(C 계열 `score_ma_long` 50→60)를 `trend_following.md` 10장에 **등록만**(미시행, v06 규칙·결과 불변, 백테스트 없음). 등록표 기본 9회 + E2 N6·N7 6회, 누락·모순 8건과 홀드아웃 개봉 확인 결과 기록(10-6·10-7). |
| 2026-11-18 | v07 등록 확정(10-6의 1~4·10-7 추천안): H 전체 구간만, E1 포함, 현금 이자 0 참고 3회, 50일 0·2배 인용 → 시행 30회. **홀드아웃 개봉 기록 파일** `holdout_ledger.jsonl`(Strategy 탭 실행이 DEV_END를 넘으면 데이터 로드 전 한 줄, `record_holdout_run`). [D] 구현 지시서 10-9. |
| 2026-11-18 | v07 [D] 구현(스펙 10-9·10-10): `V07_VARIANTS` C1L~C3L(UI 목록 밖, 헤드리스만), E2 N6·N7(`score_ma_long` 50/80)과 주변값이 변형 override에 덮이지 않게 적용 순서 수정, 현금 이자 0 참고 실행(`--cash-interest-ref`), E5 30회 계획, 라벨 `MA{n}Div`. 백테스트 미실행, v06 규칙·결과 불변. pytest 990, ruff 0건. |
| 2026-11-18 | v07 [V] 개발 구간 30회 실행(스펙 10-11): C1L/C2L/C3L 비용 1배 Sharpe 0.44/0.41/0.41, MDD −13.8/−15.3/−17.8% → **H 미달, 채택 후보 아님**(E2는 모두 통과). N6이 v06 C1~C3(0.51/0.29/0.40)을 그대로 재현. 화면은 MA50 유지. 리포트 E2 정의 표의 기준값 표시 오류 수정. |
| 2026-11-18 | **스펙 v07**: C 계열 `score_ma_long` 50→60(사용자 결정, 스펙 10-13; 성과 판정 10-12 그대로·실거래 후보 아님). 재현 ID C1S~C3S(50일), C1L~C3L = C1~C3, E2 N6·N7을 C1~C3에도(매트릭스 E2 31→37), MA50 고정 문구 일반화(`ma50_exit` 키·필드 이름 유지). 실데이터 백테스트 미실행(v06 회귀 테스트는 C1S~C3S로 통과). pytest 992, ruff 0건. |
| 2026-11-18 | MA 차트 이동평균 메뉴에서 **MA50 제외**(사용자 결정, 스펙 v07): MA20·MA60·MA200만, 차트 전용 SMA50 미계산, 색 램프 3단계. 이격도(50)·Universe MA50 Div는 2단계 나머지. pytest 992, ruff 0건. |
| 2026-11-18 | **MA50 → MA60 전체 전환**(사용자 지시): Universe `MA60 Div` 열·필터(키 `ma60div`, 저장 보기의 `ma50div` 이전·한 번 안내), 데이터 `ma60_div`·`MA60_Div`, MA 차트 이격도(60), AI 리포트 입력. 전략 필드 이름·`ma50_exit` 키는 유지. |
| 2026-11-18 | 지시서 `ma60_default` 4장 결과 기록(`docs/ui.md` 8.23 결정 요약), Universe 캡처로 MA60 Div 확인(삼성전자 79.2% = MA 차트 이격도(60)). **테스트가 실제 `universe_cache.json`을 덮어쓰던 문제 수정**(`test_universe_views`의 탭 헬퍼가 표를 그린 뒤 캐시 저장): 캐시 경로를 호출 시점에 `paths`에서 읽고 `conftest`가 임시 파일로 격리. pytest 999, ruff 0건. |
| 2026-11-18 | **v07 기준선**(스펙 10-14): C1L~C3L 삭제·N6·N7 대상 C1~C3·현재 버전 문구 v07(`df53e12`) 후 A0~C3 전체 매트릭스 1회 실행(117회, 기준선 재현 실행). 3장 확인 항목 모두 일치(C1~C3 = 10-11, A·B = 9-19, E2·연도별 흐름 일치). 판정 불변: 10개 모두 H 미달. v06 = v07 검증을 위한 중간 단계, 이후 개발 기준 v07. 리포트 `cache/dev_reports/robustness_v07_a0de8fb82a53_20261118_131134.md`. |
| 2026-11-18 | Trading History **'KIS 조회'**(예수금 조회 대체, 사용자 결정): 예수금 + 새 체결 내역을 한 번에, 미리보기 확인 후 반영 — 매수는 새 보유, 매도는 먼저 산 보유부터 청산(일부 매도 분할), 주문별 누적 체결을 `kis_imports`에 기록해 차이만, 보유보다 많은 매도는 건너뛰고 경고, KIS 잔고와 수량 비교. '다시 읽기' 버튼 삭제(Universe 새로 고침이 같은 일). docs/ui.md 8.27·8.28. |
| 2026-11-18 | 코드 점검(`review_codex.md`, 기준 `5cbb2ca`) 4건 수정: **스냅샷 불변**(임시 폴더에 쓰고 검증 후 발행, 기존 이름 거부), **언어 전환이 작업을 기다리거나 강제 종료하지 않음**(취소 요청 후 새 창, 이전 창은 마지막 작업 뒤 폐기), **신호 캐시 세대**(새로 고침 전에 시작한 계산 결과 거부), **보고서 같은 초 충돌 방지**(배타 생성 + 접미사). 전략 수치 불변. 스펙 9-30, docs/ui.md 8.29. |
| 2026-11-18 | Total Assets 표 재배치(사용자 지시): 주차·날짜 분리, 총자산(KRW)·KOSPI 대비(%)를 두 줄 병합 머리글로, KOSPI 대비를 총자산 오른쪽으로. `GroupedHeaderView`에 병합 열(`merged`) 추가. docs/ui.md 8.30. |
| 2026-11-18 | Universe 미국 종목 시총을 달러로 표시(`$3,410.0B`·`$512.3B`처럼 $B 통일, 툴팁 원화 환산). 시총 정렬은 한국(원화 순) → 미국(달러 순) 묶음, 범위 필터는 원화(억) 유지, 데이터에 `usd_market_cap` 추가. docs/ui.md 8.31. |
| 2026-11-18 | Universe 'NASDAQ 100' 표시를 **NASDAQ**으로, 토글 버튼을 **NASDAQ 150**으로(목록이 지수 구성이 아니라 거래소 시총 상위 150·ADR 포함임을 확인: 위키백과 구성 표가 사라져 FDR 거래소 목록으로 조용히 대체). 저장 키 `NASDAQ 100` 유지, 세 탭 툴바 공용 폭 94→101px. docs/ui.md 8.32. |
| 2026-11-19 | Universe '저장된 보기' 목록 구성 고정(사용자 지시): 전체 보기·현재 필터 저장·보기 삭제 항목을 항상 표시하고 쓸 수 없을 때는 비활성화 — 뷰를 고르면 항목이 생기던 문제. docs/ui.md 8.9. |
| 2026-11-19 | Universe '저장된 보기'를 **체크 박스 메뉴**로(사용자 지시, 콤보 상자 대체): 여러 뷰 체크 = 조건 모두 적용(AND), 해제 = 그 조건만 제거, 체크 상태는 현재 필터에서 계산. docs/ui.md 8.9. |
| 2026-11-19 | Universe '저장된 보기' 메뉴에서 **체크 박스 외 항목 삭제**(사용자 지시): 전체 보기·현재 필터 저장·보기 삭제와 저장·삭제 코드 제거. docs/ui.md 8.9. |
| 2026-11-19 | Universe **필터와 저장된 보기 통합**(사용자 결정, 검토안 A): '저장된 보기' 버튼 삭제, 기본 보기 2개(저PER·52주 신고가 근접)를 필터 패널 맨 위 **빠른 선택** 체크 박스로(건수 표시, 체크 = 칸 채우고 즉시 적용, 칸 값이 같으면 체크). 사용자 뷰·MA50 이전 코드 제거, 옛 `views` 설정은 읽을 때 삭제. docs/ui.md 8.9. |
| 2026-11-19 | Universe **필터 → 신호 포착**(사용자 지시): 빠른 선택(저PER·52주 신고가 근접) 삭제, 범위 조건마다 체크 박스 — 값을 넣으면 체크된 조건으로 추가, 체크한 조건만 거르고 해제하면 범위는 남긴 채 끔(`_ranges_off`), 체크/해제는 즉시 적용. 버튼 이름 `신호 포착` / `Signal Scan`. `FILTER_PRESETS`·프리셋 함수 제거. docs/ui.md 8.9. |
| 2026-11-19 | Universe 신호 포착에 **신호 추가 팝업**(사용자 지시): 조건 입력 칸(최소/최대)을 `SignalConditionsDialog`로 옮기고, 패널(`SignalScanPopup`, `RangeFilterPopup` 대체)은 추가한 신호만 체크 박스로(건수, × 지우기; 모두 지우기 버튼은 같은 날 삭제, 신호 추가 버튼은 제목 줄 오른쪽 끝의 `추가`(글자·크기 80%)로). 새로/고친 신호는 체크된 채 추가, 신호는 `custom_settings.json` `signals`에 저장. 신호가 없을 때 패널이 뜨자마자 닫히던 문제(표시 전 자식 위젯에 명시적 show → Windows가 Qt.Popup을 닫음) 수정. docs/ui.md 8.9. |
| 2026-11-20 | Universe 보유/관심 버튼 클릭이 표에 포커스를 주고 묵은 눌림 위치로 행을 끌어 선택하던 문제 수정(포커스는 이전 위젯으로 되돌림). tPER/fPER **음수 그대로 표시**: 네이버 추정PER 부호 유지, 네이버 PER 'N/A'(적자)·Yahoo trailingPE 없음은 현재가/EPS로 계산(`data.cache.per_from_eps`), 음수 tPER은 빨강. docs/ui.md 2 표. |
| 2026-11-20 | Universe **60초 자동 갱신 때 tPER/fPER도 가격 따라 재계산**: 전체 새로고침이 EPS(네이버 4분기·추정 EPS, Yahoo `epsTrailingTwelveMonths`/`epsForward`)를 행에 `eps_trailing`/`eps_forward`로 저장하고 `data.cache.refresh_row_per`가 가격 ÷ EPS로 갱신(옛 캐시 행은 다음 전체 새로고침부터). 같은 경로의 미국 종목 버그 수정: 달러 시세를 원화 `price`에 써서 화면 가격(`usd_price`)이 자동 갱신되지 않던 문제. |
| 2026-11-20 | 코드 리뷰(`review_codex.md`·`review_agy.md`, 기준 `4ace769`) 반영 — **History/KIS**: 건너뛴 매도를 `kis_pending`에 기록해 다음 조회가 가장 이른 보류일부터, 매도마다 매수일 순 정렬(선입선출), 예수금 필드 없음 = 기존 값 유지, 잔고 연속 페이지 수집·장부 단독 국내 종목 대조(페이지 중단 시 판정 보류), KIS 401/만료 토큰 1회 재발급, 검증 중 Esc 무시, 디바운스 설정 저장 보장, 매도 칸 입력 검증, 중복 주문 행 병합, 청산 거래 매도 수량 축소 시 분할, 월 요약 라벨 손익. |
| 2026-11-20 | 같은 리뷰 — **데이터 계층**: 콜드스타트 환율 1450 영구 고정 해소(최대 1분 간격 재시도), 수급 부분 수집을 완전 커버로 기록하던 문제·동시 저장, Yahoo 일괄 캐시 동시성/누수/시장 간 삭제, crumb 오류 캐싱·중복 재발급, CD91 커버리지 판정·API 키 로그 마스킹, history/listing single-flight 락 경합·stale 폴백, KRX 달력 당일 갱신 누락, 파싱 방어(null Close, regex=False, 페이지 단위 시총), 보합 RSI 100 -> 50. |
| 2026-11-20 | 같은 리뷰 — **스레드/UI**: `retire_thread` 좀비 검사, 은퇴 스레드의 대기 시그널 무시(`stale_sender`), 4개 스레드 run() 경계 처리, 전체 새로고침이 60초 틱·시장 수집 은퇴 + 틱은 행 제자리 갱신, 이평 차트 닫힘/언어 전환 시 전략 점검 스레드 정리, 언어 전환 중복 클릭, 로깅 `setup_logging()`(5MB x3 회전), 종료 시 백업 스레드 강제 종료 금지, Total Assets 환율 결측·저장 실패 시 화면 불일치. |
| 2026-11-20 | 종목 MA 차트 기간에 **10년** 추가(국내·미국 종목, 지수·금리·VIX·WTI 행 모두 같은 차트; VKOSPI·KR3YT는 API 차단으로 캐시 구간만). |
| 2026-11-20 | 종목 MA 차트 기간에 **5년** 추가, 받아 오는 일봉을 5년(1825일) -> **10년(3650일)** 으로 늘려 "전체"가 약 10년(`fetch_stock_ma_multi` 기본 `days`). docs/ui.md 기간 버튼. |
| 2026-11-20 | 같은 리뷰 — **전략(결과 불변만, 사용자 결정)**: 홀드아웃 기록을 로드 계층으로(직접 호출도 기록), 스냅샷 가장자리, robustness/factor IC 중복 연산 캐시, 수치 방어, Universe 전략 신호의 저유동성 이탈 표시·수급 시작일. v07 기준선 바이트 동일 확인. 결과가 바뀌는 H10~H13·M9는 spec 10-16에 미반영으로 기록. trend_following.md 10-15, 10-16. |
| 2026-11-23 | Trading History -> Total Assets 총자산 점검 — **KIS 예수금을 D+2(`prvs_rcdl_excc_amt`) 우선**으로(D+0 `dnca_tot_amt`는 최근 2영업일 매수를 보유·예수금에 이중 계산, 매도 대금은 누락), **첫 시세 도착 전에는 `total_asset_updated` 보류**(원가 평가 총자산이 주간 기록 자동입력에 들어가던 문제; 실패 응답도 해제), 시세 실패(0)가 기존 현재가를 지우지 않음. 남은 한계: 리포트 현금 역산의 수수료·세금 미반영, 추가 입금이 주간 수익으로 잡힘, 달러 예수금 없음. |
| 2026-11-24 | 키움 거래내역 PDF(2025·2026) 대조로 `portfolio.db` 매매일 13건(결제일 역산·휴장일 확인)과 금액 8행 수정(백업 `archive/manual_before_kiwoom_recon_20261124_093942.db`). **KIS 체결 반영을 기록 원칙에 맞춤**(사용자 지시): 같은 날도 가격이 다르면 다른 행(평균 금지), 금액은 KIS 기간별매매손익(`TTTC8715R`)의 실제 수수료·세금 포함(미정산은 보류 후 재조회), 자동 갱신 중 30분마다 **확인 없이 자동 반영**(`auto_kis_sync_tick`, 예수금 칸은 그대로). docs/ui.md 8.28. |
| 2026-11-26 | Trading Universe 국내 종목 수 확대(사용자 지시): `UNIVERSE_MARKET_SIZES` KOSPI 350 -> 500, KOSDAQ 300 -> 700(상장 보통주 약 828·1,835 중). Naver 시가총액 목록은 100개씩 페이지로 받으므로 코드 변경은 상수뿐; 새로고침 시간과 요청 수는 약 1.8배. docs/ui.md 시장 토글 항목. |
| 2026-11-26 | Universe NASDAQ 시장 저장 키 `"NASDAQ 100"` -> `"NASDAQ 150"`(사용자 지시, 종목 수에 맞춤). 설정·캐시의 옛 값은 로드 때 `migrate_market_keys`로 변환, MA 차트 시장지수는 `MARKET_INDEX_LABELS`로 ^NDX 유지, 쓰이지 않던 위키백과 Nasdaq-100 표 경로 제거. docs/ui.md 8.32. |
| 2026-11-26 | **DART 재무 DB 1~3단계**(`docs/dart_db_plan_2026-11-26.md`): `src/data/dart/`(client·corp·filings·financials·store·query·lock·sync), `dart.db` 스키마(3장 + `fin_fetch`·`fin_unmapped`), 매출·영업이익·순이익 분기 적재(연결 우선, IS→CIS, 4분기 계산, 정정 연결), `query.get_financials(as_of)`, `v_fin_growth`, `run_sync` 한 곳 + 잠금·`sync_run`·`api_usage`(19,000건), CLI `tools/dart_sync.py`. 테스트 `test_dart.py`(가짜 OpenDART + 키가 있을 때 검증 종목 회귀). 검증 종목 5개 2025년 분기 매출·영업이익 40개 값이 보고서 원문 표와 전부 일치, 두 번째 `--daily` 호출 2건. 화면·전략 미변경. |
| 2026-11-26 | **DART 3A단계: 앱 시작 시 자동 수집**(`docs/dart_db_plan_2026-11-26.md` 11장, docs/ui.md 8.33): `ui/dart_sync_thread.py` `DartSyncThread`·`DartAutoSync`, Universe 첫 새로고침 끝 + 10초 뒤 시작, 19시 기준 하루 한 번(30분 확인), 갱신 상태 옆 `DART …` 한 칸, 닫을 때 cancel + 2초. `run_sync`에 목록 페이지 단위 취소·남은 running 기록 정리·`is_due`/`collection_status`. 실측 첫 화면 2.47초 → 2.47초, 닫았다 켜면 이어 받음(중복 0). 테스트 `test_dart_auto_sync.py`, conftest가 테스트의 dart.db·키를 격리. |
| 2026-11-26 | **DART 계획서 미구현 점검 1~7 처리**: (1) 금융업 매출은 만들지 않고 revenue_na로 표시(사용자 결정), 비지배지분 없는 연결은 지배주주순이익=순이익, 계정 이름 규칙 보완; (2) API 013 기간(금융업 2023 반기까지)은 보고서 원문 손익계산서 표에서 읽음(documents.py, API 값과 288개 일치), 정정 3분기 뒤늦은 경우 Q4 처리·표시; (3) 달력 분기(period_end·cal_year·cal_q); (4) 12월 외 결산 API 연도 = 보고서 기간 말 연도(실데이터 5개사 확인); (5) CLI --show 증감표; (6) fin_issue 표·--issues; (7) 앱 상태 표시에 CLI·스케줄러 실행 반영, 건너뛰면 5분 뒤 재확인. 계획서 구현 메모, docs/ui.md 8.33. |
| 2026-11-26 | **DART 코드 리뷰(`review_agy.md`) 반영**: 조회는 읽기 전용 연결·스키마는 버전이 다를 때만(H1), 원문 표 파서를 머리글 열 격자 기준으로 재작성 — 빈 칸 열 밀림 방지, 재무상태표 줄을 지배주주순이익으로 읽던 기존 오류 정정(7개사 60개 값; H2), 기간 단위 장애 격리·429 재시도·취소 가능한 대기(H3), 12월 외 결산 첫 해 보고서 보존·재분류 시 재표준화(H4), Windows 잠금 판정(H5), M1~M9·L1~L4(우선순위 조회 절약, 19시 이후 취소 재개, 초기 수집 %, 최근 013 미캐시, 통화 열, 연결/별도 변경 시 증감률 보호, lookahead 표시, Ctrl+C, fetch_log 정리 등). 테스트 41개 추가, 리뷰가 지적한 두 실패 (읽기 전용 DB, 네트워크 의존) 해소. 실데이터: 원문 파서 vs API 588개 값 불일치 0. 계획서 "코드 리뷰 반영". |
| 2026-11-26 | **DART 코드 리뷰(`review_codex.md`) 반영**: API에 아직 안 올라온 정정공시를 완료 처리하지 않음(7일 대기, 그동안 응답 미캐시), 4분기 계산값의 가용일 `fin_std.avail_dt`(스키마 5; 늦게 접수된 3분기에 기댄 값은 그날부터 `as_of` 조회에 나옴, 수집 순서 무관), 통화가 다른 기간끼리는 차감·증감률 계산 안 함·증감표 행별 단위, 일일 재무 창을 8일 앞당김, `--daily --tickers`가 재무를 받음, 검증 테스트가 실제 DB를 이관하지 않음. 테스트 11개 추가. 계획서 "코드 리뷰 반영". |
| 2026-11-26 | **DART 4단계: 사업부별 매출**(`docs/dart_db_plan_2026-11-26.md` 5장, 구현 메모): `data/dart/segments.py` — XBRL 부문 구성원 → 원문 영업부문 주석 표 → 제품별 매출표 → 단일부문, 표는 머리글 격자(축 이름·기간·부문 소계·부문간 제거)로 후보를 만들고 매출 대비 1% 검산이 고름. `seg_fetch`(스키마 6), `segment_map` 초안, CLI `--segments`/`--segment-report`, 앱 자동 수집은 새 보고서만. 검증 종목 5개 55기간 전부 ok, Universe 표본 25개사 275기간 통과율 92%. 테스트 17개. |
| 2026-11-26 | **DART 5단계: MA 차트 팝업 재무 탭**(`docs/dart_db_plan_2026-11-26.md` 13장 시안, docs/ui.md 8.34): KOSPI·KOSDAQ 종목의 `StockMaDialog`에 `차트 | 재무` 탭 — 머리줄·분기/연간 전환, KPI 4장, 분기 매출·영업이익률 차트(4분기 빗금, 적자 빨강), 실적 표(전환·정정·계산값 표시), 사업부 비중 막대와 부문 × 연도 표(검산 배지, 총액/외부매출 기준이 다른 해는 증감 없음). `ui/dart_financials_model.py`(순수 계산), `threads/dart_threads.py`, `query.company_financials`. 테스트 8개. |
| 2026-11-26 | **DART 코드 리뷰 2차(`review_codex.md`) 반영**: 원문·XBRL 요청 실패를 자료 없음으로 확정하지 않음(013·014만 음성 캐시), 4분기를 3분기 버전마다 한 행으로 보존(스키마 7, 과거 기준일 답 불변), 정정본 부문 검증은 그 재무가 들어온 뒤 해당 매출로, 부문 조회는 처리된 최신 접수번호 기준, 재무 탭 TTM·전년비·연평균의 연속성·통화·연결 기준 검사, 재분류 시 부문 데이터 정리. 테스트 10개 추가. 실데이터 사본에서 검증 종목 40개 값 불변. |
| 2026-11-26 | MA 차트 팝업 '기업 정보' 버튼(네이버 금융/Google Finance 링크) 삭제 — 재무 탭이 대체(사용자 지시, docs/ui.md 8.34). 오른쪽 패널은 투자자/선물 표나 전략 점검 칸이 있을 때만 생성(버튼만 남는 빈 패널 방지). pytest 1323, ruff 0건. |
| 2026-11-26 | **MA 차트 조작 개선**(`docs/history/ma_chart_ux_review_2026-11-26.md`(삭제됨, `git show cc28828:docs/history/ma_chart_ux_review_2026-11-26.md`) 우선 개선 1~3, docs/ui.md 8.35): 기간 콤보를 `activated`에 연결해 같은 기간 재선택 시 최근 구간 복귀, 이동 여유가 없으면 스크롤바 비활성; '이동평균' → '가격선', 메뉴를 이동평균·비교선·채널로 묶고 툴팁에 켠 선 전체; 오른쪽 상세 패널 '상세 접기/펼치기'(분할 폭 복원, 상태 유지). 고배율 실기 확인은 미완. |
| 2026-11-26 | 끝난 문서 삭제(사용자 지시): `docs/history/` 5개(옛 roadmap 장·이력, ui.md 대체 절, 2026-08 최적화 기록·점검 목록, MA 차트 리뷰)와 구현이 끝난 지시서 6개: `docs/ui_redesign_2026-11.md`·`ui_design_proposal_2026-11-14.md`·`ui_strategy_settings_2026-11-14.md`·`yearly_flows_report_2026-11-15.md`·`ma60_unification_2026-11-18.md`·`ma60_default_2026-11-18.md`. 결정은 docs/ui.md·스펙에 있고, 코드·문서의 인용(절 번호)은 그대로 — 원문은 `git show cc28828:docs/<이름>.md`. |
| 2026-11-26 | 저장소 정리(사용자 지시, 구조 점검 후): 테스트만 쓰던 재수출 모듈 `src/ui/widgets.py` 삭제(테스트 5개는 실제 모듈에서 import), CLAUDE.md와 어긋난 낡은 규칙 `.agents/rules/optimize-code.md` 삭제(AGENTS.md가 CLAUDE.md를 가리킴), 구현이 끝난 시안·스크린샷 `docs/ui_mockups/2026-11/` 10개 삭제(`git show cc28828:…`). `docs/nexus/`는 Paperclip 운영 문서라 유지. |
| 2026-11-26 | **DART 코드 리뷰 3차(review_codex) 반영**: (P1) 7일이 지나도 API가 옛 접수 수치로 답하는 기재정정을 원본 수치로 완료 처리하지 않음 — `fin_fetch.status = awaiting_correction`, 7일마다 캐시 없이 재조회(접수 후 365일까지), `correction_not_served` 이슈; 스키마 8이 옛 버전에서 완료로 닫힌 기재정정을 한 번 재검사(첨부정정 제외), 수치를 바꾸는 정정의 응답은 캐시하지 않음. (P1) 같은 접수 재처리 시 그 기간의 지표 집합을 통째로 교체하고, 그 값으로 계산한 반기·3분기 값도 다시 계산. (P2) 요청 실패가 있으면 실행 결과 `partial` + `periods_failed`/`segments_failed`(자료 대기는 실패가 아님). (P2) 재무 응답에서 먼저 생긴 임시 공시 행을 목록이 오면 보고서명·정정 여부·기간으로 채우고, 기간이 달랐으면 저장분을 지워 다시 받음. (P2) 종목 지정 수집도 오래된 회사 목록을 갱신, 찾지 못한 종목은 CLI에 표시. 테스트 9개(각각 이전 코드에서 실패 확인). |
| 2026-11-26 | **DART 앱 연속 수집**(사용자 지시, docs/ui.md 8.33): 앱이 켜져 있으면 한 실행이 무언가를 받은 동안 1분 간격으로 계속 수집. `auto`의 백필을 Trading Universe 표 순서(KOSPI → KOSDAQ, `universe_priority`가 캐시 순서 유지)로 종목마다 재무 → 사업부별 매출(`_Job.backfill_by_stock`; 밀린 사업부 기간도 앱이 받음, 전에는 `--segments`만), 그다음 Universe 밖 종목 재무. 일일 한도 소진 시 `DART 한도 소진` + 상태 표시줄 영구 문구, 자정(KST) 1분 뒤 자동 재개(`DartAutoSync._reset_timer`), 시작 시 한도 0이면 실행 안 함. 테스트 7개 추가. |
| 2026-11-27 | **사업부별 매출: 원문 XBRL 태그 판독**(사용자 지시, GitHub 오픈소스 조사 후, `docs/dart_db_plan_2026-11-26.md` 5장 메모): `document.xml` 값 칸의 `ACODE`/`ACONTEXT`로 표 배치와 무관하게 부문·지역·제품 매출을 읽는 `segments.tag_candidates`(멤버 이름은 표 이름에서, 판매 종류 멤버 제외, 단위 문구 없으면 `ADECIMAL`). 순서: 부문 XBRL → 태그 부문 → 사업의 내용 부문표 → 주석 → 태그 지역 → 단일, 제품은 사업의 내용 → 태그 → 주석. 표본 58건 중 8건 변경(SK가스 운영 부문, 휴젤 제품, 단일·review 2곳 지역). 스키마 13은 `seg_fetch` 전체 재판독, 원문 파서 16. 테스트 4개. |
| 2026-11-27 | **사업부별 매출 점검 목록**(사용자 지시): `tools/dart_sync.py --segment-report`에 '통과하다 실패로 바뀐 회사' 절 — `query.segment_breaks`가 회사별로 마지막 통과 기간(출처·표 이름), 첫 실패 기간·접수번호, 실패 기간 목록, 이후 재통과 여부를 낸다(보고서 표 배치 변화를 찾는 출발점, `docs/dart_db_plan_2026-11-26.md` 5장 메모). 실제 DB 91개사. 테스트 1개. |
| 2026-11-27 | **사업부별 매출: 지역 부문 트리 판독**(기아 확인 요청, `docs/dart_db_plan_2026-11-26.md` 5장 메모): 기아 2025 연간~2026 3Q가 지역 자기 합계 아래의 부문간 제거 열을 표 조정으로 더해 `sum_mismatch`, 2025 1~3Q는 행 이름이 여러 열에 걸친 트리라 `review`. 머리글·행 경로를 같은 규칙(`segments._tree_items`, `_row_paths`)으로 읽고, '순매출액' 행을 총액 행보다 먼저(기준 일관). 표본 58건 비교: 기아 4건 + 3건 새로 `ok`. 스키마 13(실패·주석·단일 판정 기간만 다시 읽기, 약 2,200건), 원문 파서 15. 테스트 4개. |
| 2026-11-27 | **기업 정보 탭 정리**(사용자 지시, docs/ui.md 8.34): 사업부별 절의 보기 설명 캡션과 차트 아래 메모 삭제(자료 없는 보기는 차트 가운데 안내). `분기 | 연간`·`제품별 | 부문별` 전환 때 위치 고정 — 두 차트 높이·그림 영역 픽셀 여백 고정(`tight_layout` 대신 `_pin_margins`), 두 표 높이·이름 열·기간 열 고정 폭, 세로 스크롤바 항상 표시, 픽셀 단위 가로 스크롤. 테스트 1개(전환 4번 동안 위치·크기 불변). |
| 2026-11-27 | **사업부·제품별 매출: 사업의 내용 표와 분기 보기**(사용자 지시, `docs/dart_db_plan_2026-11-26.md` 5장 구현 메모, docs/ui.md 8.34): 차트를 비중 누적에서 매출액 묶음 막대로. `segments.biz_candidates` — 'II. 사업의 내용'의 '주요 제품 및 서비스'·'매출 및 수주상황' 표(병합 셀 격자, 수출/내수/합계 하위 행, 금액(비율) 셀, 매출유형·지역·판매경로·회사명 표 제외), 같은 검산. `segment_rev.level`로 부문 기준(XBRL → 사업의 내용 부문표 → 주석 → 단일)과 제품 기준을 함께 저장(스키마 11: 테이블 재구성 + 전 기간 재수집, 원문 파서 13). 탭: 제품별 기본 + `제품별 | 부문별` 전환, 사업부 카드가 `분기 | 연간`을 따름(분기 = 누적 차이), 셀 아래 줄 매출 비중(손익계산서 매출 대비) · 증감. 자본관리 표가 우연히 검산을 통과해 부문으로 저장되던 문제 수정. 실제 3종목 재수집·화면 확인(삼성전자 메모리 분기 급증, HD현대일렉트릭 전력기기, NAVER 2026 개편). 같은 날 사업부 블록을 실적 카드와 같은 구조로(카드 하나, 차트 5 : 표 4, 고정 세로 머리글·최신으로 열림·전용 스크롤바 `_PinnedScroll` 공용, 같은 분기 x축, 호버 상자·열 선택), 이어 실적 카드 안으로 합침(사용자 결정: 기간 칸을 실적과 한 칸씩 맞추고 빈 기간은 빈칸, 두 차트 그림 영역 정렬, 호버 하나로 두 차트·두 표 표시; 연간의 진행 중인 해는 누적). 테스트 13개. |
| 2026-11-27 | **DART 코드 리뷰 5차(review_codex, 사업부·제품별 매출) 반영**(`docs/dart_db_plan_2026-11-26.md` 5장 메모, docs/ui.md 8.34): 합계가 매출과 맞아도 표의 정체·기간을 먼저 확인. (P1) 수주잔고·수주총액·기납품액 열과 '수주상황' 설명의 표를 잔차 허용 전에 제외(다단 머리글 경로 + 표 앞 설명 `_data_tables`/`_context_kind`). (P1) 주석의 '단일 부문' 문장이 영역의 모든 표를 제품표로 만들지 않음 — 표마다 설명·항목명으로 판정, 원가·비용 항목 표 제외; 단일 부문 회사의 주석 제품표는 부문 기준 `단일부문` + 제품 기준(`html_product`)으로 저장. (P1) 매출처·고객·거래처 표는 제품이 아님, '구분' 머리글은 제품·서비스 근거가 있어야 함. (P1) 후보가 기간을 가짐(`Candidate.when/span/months`: 당기·전기, 3개월·누적, 제N기, XBRL 시작·종료일) — 자기 기간의 매출과만 대사, 표시가 없으면 더 가까운 기준(반기 100 vs 2분기 99.5 → `ytd`), 전기 열만 맞는 문서는 채택 안 함. (P1) `segment_view`의 분기 환산·YoY·CAGR은 검산 `ok` + 기준 확인된 기간만 사용, 미검증 금액은 자기 칸에만 표시. (P2) 여러 부문이 같은 제품을 팔면 합산(행 병합 셀·소계는 한 번만). 스키마 12(`seg_fetch` 비움 → 전 기간 재판독), 원문 파서 14, XBRL 캐시는 호환(라벨의 날짜). 테스트 9개 추가. |
| 2026-11-26 | **DART 코드 리뷰 4차(review_codex, 기준 e4518f7) 반영**: (P1) 분기마다 기준일에 알려진 최신 재무제표 접수 하나의 계정만 조회 — 정정본에서 빠진 계정에 원본 금액이 섞이지 않음(`get_financials`와 `v_fin_latest`/`v_fin_version`, 뒤늦은 3분기 정정의 4분기 재계산은 유지). (P1) 공개 전 공시(013)는 수집으로 세지 않고(`periods_waiting`) 같은 실행에서 다시 고르지 않음 → 앱이 1분 뒤 실행을 부르지 않음. (P1) 재무가 `awaiting_correction`이면 사업부문 검증 보류. (P2) 합산으로 구한 누적액에 `fin_std.ytd_derived`, 앞 분기 재저장 시 재계산. (P2) 수집 종료 사유를 `SyncResult.stop_reason`(daily_limit/server_limit/budget/cancelled)으로 전달, 서버 `020`이면 오늘 사용량을 한도로 채워 앱이 자정까지 대기. 스키마 9(실제 DB 복사본: 즉시 마이그레이션, 조회 결과 변화 0, 회귀 통과). 테스트 6개(5개는 이전 코드에서 실패 확인). |
