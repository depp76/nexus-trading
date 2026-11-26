# DART 재무 DB 구축 — 설계·구현 지시서 (2026-11-26)

대상: 데이터 계층 `src/data/dart/` (새 패키지), DB 파일 `dart.db` (SQLite, 프로젝트 루트)
범위: 1차는 **매출·손익(분기·연간)과 증감**, 2차는 **사업부별 매출**. 전략 규칙·백테스트 코드는 바꾸지 않는다.
수집 기간: **최근 5개 사업연도 + 진행 연도**(2026-11 기준 2021~2025 연간 + 2026 1~3분기). 설정값 `DART_YEARS = 5`로 매년 1월에 창이 한 해씩 밀린다. 창 밖으로 나간 해의 행은 지우지 않고 갱신만 멈춘다.
수집 시점(2026-11-26 사용자 결정): **앱을 켜면 백그라운드에서 자동 수집(11장)을 먼저 구현**한다. Windows 작업 스케줄러 실행(12장)은 나중에 붙일 수 있게 구조만 맞춰 둔다.

## 1. 목적

- 상장사(코스피·코스닥)의 사업보고서·반기보고서·분기보고서에서 매출, 영업이익, 순이익을 분기 단위로 정리하고 전년 동기(YoY)·직전 분기(QoQ) 증감을 본다.
- 관심 종목부터 사업부(영업부문)별 매출의 증감을 본다.
- 나중에 전략·백테스트에서 쓸 수 있게 **공시 접수일(rcept_dt) 기준**으로 저장한다(미래정보 차단, 체크리스트 A).

## 2. 데이터 출처 (OpenDART)

| 목적 | 엔드포인트 | 비고 |
|---|---|---|
| 회사 목록 | `corpCode.xml` (zip) | 종목코드 6자리 → DART 고유번호 8자리. 주 1회 갱신 |
| 보고서 목록·접수일 | `list.json` | `pblntf_ty=A`(정기공시). 접수번호·접수일·보고서명(정정 여부) |
| 매출·손익 | `fnlttSinglAcntAll.json` | 단일회사 전체 재무제표. `reprt_code` 11011 사업 / 11012 반기 / 11013 1분기 / 11014 3분기, `fs_div` CFS(연결) / OFS(별도) |
| 사업부별 매출 | `fnlttXbrl.xml` (XBRL 원본 zip), `document.xml` (원문 zip) | 주석 '영업부문', '사업의 내용 – 주요 제품 매출' 표 |

- 인증키: OpenDART에서 발급, `.env`의 `DART_API_KEY`로 읽는다(코드·로그에 키를 남기지 않음).
- 요청 한도: 키당 하루 약 20,000건에서 응답 코드 `020`(한도 초과). 상장사 약 2,700개 × 연 4개 보고서 ≈ 하루 11,000건이므로, 과거치 최초 수집은 며칠로 나눈다.
- 응답 `013`(데이터 없음)은 오류가 아니라 정상 상태로 처리한다(연결재무제표가 없는 회사 등).

## 3. DB 구조 (`dart.db`)

```sql
-- 회사 기준표
CREATE TABLE company (
  corp_code TEXT PRIMARY KEY,        -- DART 고유번호 8자리
  stock_code TEXT,                   -- 6자리, 비상장은 NULL
  corp_name TEXT NOT NULL,
  market TEXT,                       -- KOSPI / KOSDAQ / NULL
  fiscal_month INTEGER,              -- 결산월(12월 외 결산 처리용)
  updated_at TEXT
);

-- 보고서 1건 = 1행 (정정 포함)
CREATE TABLE filing (
  rcept_no TEXT PRIMARY KEY,         -- 접수번호 14자리
  corp_code TEXT NOT NULL REFERENCES company,
  reprt_code TEXT NOT NULL,          -- 11011/11012/11013/11014
  bsns_year INTEGER NOT NULL,
  rcept_dt TEXT NOT NULL,            -- 접수일 YYYY-MM-DD (전략에서 쓸 수 있는 첫날)
  report_nm TEXT,
  is_correction INTEGER DEFAULT 0,   -- 보고서명에 [기재정정] 등
  superseded_by TEXT                 -- 같은 기간 최신 정정 접수번호
);

-- API 응답 원본 (재가공용, 지우지 않음)
CREATE TABLE fin_raw (
  rcept_no TEXT NOT NULL REFERENCES filing,
  fs_div TEXT NOT NULL,              -- CFS / OFS
  sj_div TEXT NOT NULL,              -- BS / IS / CIS / CF / SCE
  account_id TEXT, account_nm TEXT, ord INTEGER,
  thstrm_amount REAL, thstrm_add_amount REAL,   -- 당기(3개월) / 당기누적
  frmtrm_amount REAL, frmtrm_add_amount REAL,   -- 전기 / 전기누적
  currency TEXT,
  PRIMARY KEY (rcept_no, fs_div, sj_div, ord)
);

-- 표준화한 손익 (분기 단위)
CREATE TABLE fin_std (
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_q INTEGER NOT NULL,         -- 1~4
  fs_div TEXT NOT NULL,              -- 실제 사용한 구분(CFS 우선, 없으면 OFS)
  metric TEXT NOT NULL,              -- revenue / op_income / net_income / net_income_owners
  value_q REAL,                      -- 분기(3개월) 값
  value_ytd REAL,                    -- 누적 값
  derived INTEGER DEFAULT 0,         -- 4분기 등 계산값이면 1
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, metric, rcept_no)
);

-- 사업부별 매출 (2차)
CREATE TABLE segment_rev (
  corp_code TEXT NOT NULL,
  fiscal_year INTEGER NOT NULL,
  fiscal_q INTEGER NOT NULL,
  segment_raw TEXT NOT NULL,         -- 보고서 원문 부문명
  segment_code TEXT,                 -- segment_map으로 정규화한 이름
  revenue REAL,                      -- 외부 매출 기준(내부거래 제거 후)
  op_income REAL,
  basis TEXT,                        -- 'ytd' 또는 'q'
  source TEXT,                       -- 'xbrl' / 'html_segment' / 'html_product'
  check_status TEXT,                 -- 'ok' / 'sum_mismatch' / 'review'
  rcept_no TEXT NOT NULL,
  rcept_dt TEXT NOT NULL,
  PRIMARY KEY (corp_code, fiscal_year, fiscal_q, segment_raw, rcept_no)
);

-- 회사별 부문명 매핑 (개편·명칭 변경 흡수)
CREATE TABLE segment_map (
  corp_code TEXT NOT NULL,
  segment_raw TEXT NOT NULL,
  segment_code TEXT NOT NULL,
  valid_from INTEGER, valid_to INTEGER,   -- 적용 사업연도 범위
  note TEXT,
  PRIMARY KEY (corp_code, segment_raw)
);

-- 수집 기록 (재시도·중복 방지)
CREATE TABLE fetch_log (
  endpoint TEXT, params TEXT, status TEXT, fetched_at TEXT, error TEXT,
  run_id TEXT                        -- 어느 실행(앱/CLI/스케줄러)에서 부른 것인지
);

-- 일일 호출 수 (앱·CLI·스케줄러가 같은 키의 한도를 나눠 씀)
CREATE TABLE api_usage (
  kst_date TEXT PRIMARY KEY,         -- 한국 날짜 YYYY-MM-DD
  calls INTEGER NOT NULL DEFAULT 0
);

-- 수집 진행 상태 (중간에 꺼져도 이어서 받기)
CREATE TABLE sync_state (
  key TEXT PRIMARY KEY,              -- 'corp_list_at' / 'daily_until' / 'backfill_cursor' / 'last_run' ...
  value TEXT,
  updated_at TEXT
);

-- 실행 기록 (화면 상태 표시·실패 추적)
CREATE TABLE sync_run (
  run_id TEXT PRIMARY KEY,
  trigger TEXT NOT NULL,             -- 'app' / 'cli' / 'scheduler'
  mode TEXT NOT NULL,                -- 'daily' / 'backfill' / 'segments'
  started_at TEXT, finished_at TEXT,
  status TEXT,                       -- 'running' / 'done' / 'partial' (한도·종료로 멈춤) / 'failed'
  calls INTEGER, filings_added INTEGER, error TEXT
);
```

- SQLite는 WAL 모드(`PRAGMA journal_mode=WAL`)로 연다. 백그라운드 수집이 쓰는 동안 화면이 읽어도 막히지 않는다.
- 보고서 1건(접수번호) 단위로 커밋한다. 어디서 끊겨도 다음 실행이 남은 것부터 이어 받는다.

구현 메모 (2026-11-26, 1~3단계):
- 표 두 개를 더했다. `fin_fetch(rcept_no, status, src_rcept_no, fs_div, fetched_at)`: 재무를 요청한 보고서. 이어 받기는 `sync_state.backfill_cursor` 대신 이 표에 없는 보고서를 찾는 방식이다. `fin_unmapped(rcept_no, corp_code, metric)`: 4-1의 '매핑되지 않은 회사 목록'.
- 뷰 `v_fin_latest`: 기간·지표별 최신 접수번호 행(`v_fin_growth`의 바탕).
- 보고서 목록은 회사별이 아니라 시장 전체(`corp_cls=Y`·`K`)를 달력 분기 단위로 조회한다(회사 코드 없이 조회하면 기간이 3개월로 제한됨). 6년치 목록이 수백 건 호출로 끝나고, 시장 구분도 이 응답에서 얻는다. `--tickers`는 회사별로 조회한다.
- `fnlttSinglAcntAll`은 기간별로 **최신 정정본 숫자만** 돌려준다. 그래서 숫자는 응답에 적힌 접수번호(정정본) 아래 저장하고, 같은 기간의 원본 보고서는 '받음'으로만 표시한다. 정정 전 원본 숫자는 이 API로 얻을 수 없어, `as_of`가 정정 접수일 이전이면 그 기간 값이 비어 나온다(미래정보 대신 결측).
- 순이익 계정명은 기간·연결 여부가 붙는다("연결분기순이익(손실)" 등, 현대차 2021 3분기) → 이름 규칙을 정규식으로 둔다.
- 검증(2026-11-26): 검증 종목 5개의 2025년 분기 매출·영업이익 40개 값이 각 보고서 원문(`document.xml`) 연결 손익계산서 표 값(4분기는 원문 연간 − 원문 3분기 누적)과 모두 일치. 기대값은 `src/tests/test_dart.py` `EXPECTED_2025`(키와 dart.db가 있을 때만 실행). 시장 전체 보고서 목록 첫 수집은 호출 681건·약 11분(보고서 60,604건, KOSPI 825·KOSDAQ 1,834개사), 이어진 `--daily`는 호출 2건.
- 3A단계(2026-11-26): `src/ui/dart_sync_thread.py`, docs/ui.md 8.33. 11-2의 `last_run`은 한국 시각(+09:00)으로 저장한다. 취소는 보고서 기간과 목록 페이지마다 확인한다. 앱이 2초 안에 못 끝낸 실행(늦은 응답 하나를 기다리는 중)은 프로세스와 함께 끝나고, 다음 실행이 잠금을 회수하며 남은 `sync_run.status='running'`을 `partial`로 닫는다. 실측: 첫 화면 시간 변경 전·후 모두 평균 2.47초, 닫기 0.39~0.67초(늦은 응답 1회 2.02초), 닫았다 켤 때마다 이어 받음(재무 요청 547건 중 같은 기간 중복 0).
- 결산월은 사업보고서 이름의 기간(예: `(2025.03)`)에서 얻는다. ~~12월이 아닌 회사는 API 사업연도를 결산 연도와 그 전 해 순서로 시도~~ → 아래 4.

미구현 점검 후속 (2026-11-26, 점검 항목 1~7):
1. **금융업 매출**(사용자 결정: 매출 없음으로 표시): 금융지주·은행·보험사는 손익계산서에 매출 합계 줄이 없다. 이자수익·보험수익·수수료수익 같은 금융업 계정이 있으면 `revenue`를 만들지 않고 `fin_issue.kind='revenue_na'`로 남긴다(미매핑 아님). 연결재무제표에 비지배지분 줄이 아예 없으면 지배주주순이익 = 순이익(`derived=1`, `owners_from_net_income`). 지배주주순이익 이름에 '지배회사'·'지배주주'를, 순이익 이름에 '분기연결순이익'·'연결당반기순이익'·'당(분)기연결순이익'을 더했다. 포괄이익 귀속 줄은 제외한다.
2. **013 기간**: 금융업(과 SK스퀘어)은 2023년 반기까지 `fnlttSinglAcntAll`·`fnlttSinglAcnt` 모두 013이다. 이 기간은 보고서 원문(`document.xml`)의 손익계산서 표에서 읽는다(`documents.py`, `fin_fetch.status='document'`, `fin_issue.kind='from_document'`, `fin_raw`에는 넣지 않음). 연결 손익계산서 제목(없으면 '2. 연결재무제표' 절에서 영업이익·순이익 줄이 있는 첫 표), 단위(원·천원·백만원·억원), 주석 열, '3개월/누적' 열을 읽는다. 첨부만 정정한 보고서는 표가 없어 같은 기간의 이전 접수본을 차례로 본다. utf-8로 선언했지만 실제로는 cp949인 옛 문서도 읽는다. 손익계산서 부분만 `cache/dart/document/`에 저장한다. 검증: API 값이 있는 기간 288개 값(분기·누적)이 원문 파서 값과 모두 일치. 금융 5개사 2021~2023 반기 47개 기간이 채워졌다(예: 신한지주 2022 순이익 4조 7,322억).
   - 4분기 계산에 필요한 3분기가 사업보고서보다 뒤에 접수된 정정본으로만 있으면(API가 최신본만 줌, KB금융 2023 3분기는 2024-12 정정) 그 3분기를 쓰고 `q4_uses_later_q3`로 남긴다. 이 4분기 값은 사업보고서 접수일 시점에는 몰랐던 숫자에 기대므로, 백테스트에서는 이 목록을 보고 판단한다.
3. **4-7 달력 분기**: `v_fin_latest`·`v_fin_growth`와 `query.get_financials`에 `period_end`(분기 말 'YYYY-MM')·`cal_year`·`cal_q`를 더했다(`query.calendar_period`). 결산월을 모르는 회사는 12월로 본다.
4. **12월 외 결산 API 연도**: 실데이터 확인(3월·6월 결산 5개사, 121개 기간) 결과 DART의 `bsns_year`는 **보고서 기간 말의 연도**다(2027-03 결산 연도의 '분기보고서 (2026.06)' → 2026). 보고서 이름에서 바로 쓰고(`sync.api_years`), 이름이 없을 때만 결산 연도·전년을 시도한다.
5. **증감표**: `query.growth_table` / `format_growth_table`, CLI `--show 005930 [--as-of 2025-11-10] [--quarters 12]`.
6. **점검 목록**: 표 `fin_issue(rcept_no, corp_code, fiscal_year, fiscal_q, kind, detail)` — `revenue_na`, `owners_from_net_income`, `fs_div_change`(4-4 경고도 여기 기록), `from_document`, `no_data`, `q4_uses_later_q3`. `query.issues` / `format_issues`, CLI `--issues [--limit N]`(미매핑 목록 포함).
7. **다른 실행 반영**: 앱 상태 표시는 할 일이 없을 때(30분 확인 포함) dart.db의 가장 최근 실행(앱·CLI·스케줄러)을 보여 주고 툴팁에 실행 주체를 적는다. 잠금 때문에 건너뛰면 5분 뒤 다시 확인한다(docs/ui.md 8.33).
- 스키마 3: 2는 `no_data` 기간을, 3은 미매핑 기간을 다시 처리하게 한다(재무 응답은 캐시에서 읽어 호출 없음).

뷰 `v_fin_growth`: `fin_std`에서 기간별 최신 정정본만 골라 YoY·QoQ 증감률, 흑자전환·적자전환·적자지속 플래그를 계산.

## 4. 매출·손익 처리 규칙

1. **계정 표준화**: 계정명이 아니라 `account_id`(IFRS 태그) 우선. 기본 매핑:
   - 매출 `ifrs-full_Revenue` (없으면 `ifrs-full_RevenueFromContractsWithCustomers`, 금융업은 영업수익 계열)
   - 영업이익 `dart_OperatingIncomeLoss`
   - 당기순이익 `ifrs-full_ProfitLoss`, 지배주주순이익 `ifrs-full_ProfitLossAttributableToOwnersOfParent`
   - 회사 고유 확장 태그(`entity…`)는 `account_map` 설정 파일로 보완하고, 매핑되지 않은 회사는 목록으로 남긴다.
2. **손익계산서 위치**: IS가 없고 CIS(포괄손익계산서)에만 있는 회사가 있다 → IS 우선, 없으면 CIS.
3. **분기값**: 1분기는 `thstrm_amount`, 반기·3분기는 3개월값 `thstrm_amount`와 누적 `thstrm_add_amount`를 함께 저장. **4분기 = 사업보고서 연간값 − 3분기 누적**, `derived = 1`.
4. **연결 우선**: CFS 조회 → `013`이면 OFS. 실제 사용한 구분을 `fs_div`에 기록하고, 같은 회사에서 기간마다 구분이 바뀌면 경고.
5. **정정보고서**: 새 접수번호로 저장하고 이전 행은 지우지 않는다. `filing.superseded_by`로 최신본을 가리키고, 뷰는 최신본만, 백테스트 조회는 '그날까지 접수된 최신본'을 쓴다.
6. **단위·통화**: API 금액은 원 단위. 외화 표시 회사는 `currency`를 보존하고 증감률만 계산(원화 환산은 하지 않음).
7. **결산월**: 12월 외 결산 회사는 `fiscal_year`/`fiscal_q`를 회사 회계연도 기준으로 두고, 달력 분기와의 대응은 뷰에서 처리.

## 5. 사업부별 매출 처리 규칙 (2차)

1. **추출 순서**: XBRL 주석의 영업부문 태그 → 원문 '영업부문 정보' 표 → '사업의 내용 – 주요 제품 등의 매출' 표.
2. **표 파싱**: 단위(원·천원·백만원) 감지, '내부거래'·'연결조정'·'합계'·'기타' 행 구분, 누적 기준인지 분기 기준인지 `basis`에 기록.
3. **자동 검산**: 부문 외부 매출 합계(+ 연결조정)와 `fin_std` 매출의 차이가 1% 이내면 `ok`, 아니면 `sum_mismatch`. 표를 못 찾으면 `review`.
4. **부문 매핑**: 사업부 개편·명칭 변경은 `segment_map`으로 묶는다. 매핑이 없는 새 부문명은 목록으로 보고.
5. **대상**: 처음에는 관심 종목만(Universe 194개 + 별도 지정 목록). 전 종목 확대는 검산 통과율을 보고 결정.

## 6. 코드 구조

```
src/data/dart/
  client.py      # HTTP, 키, 재시도(지수 백오프), 일일 호출 수 집계·한도 전 정지, 응답 캐시(cache/dart/)
  corp.py        # corpCode.xml → company
  filings.py     # list.json → filing (정정 연결)
  financials.py  # fnlttSinglAcntAll → fin_raw → fin_std
  segments.py    # XBRL/원문 → segment_rev (2차)
  store.py       # SQLite 스키마·마이그레이션·조회 함수
  query.py       # get_financials(corp, as_of=날짜) — 그날까지 접수된 값만 반환
  sync.py        # run_sync(mode, trigger, budget, cancel) — 앱·CLI·스케줄러가 모두 이 함수 하나를 부른다
  lock.py        # 프로세스 간 잠금(cache/dart/sync.lock) — 둘이 동시에 수집하지 않게
tools/dart_sync.py  # CLI: --init / --backfill / --daily / --segments --tickers ... / --auto (12장 스케줄러용)
src/ui/dart_sync_thread.py   # QThread 래퍼: run_sync(trigger='app') 호출, 진행·완료·실패 시그널
```

- 수집 로직은 `sync.run_sync()` 한 곳에만 둔다. 앱 스레드·CLI·스케줄러는 실행 방법만 다르고 같은 함수를 호출한다(나중에 12장을 붙여도 수집 코드는 그대로).
- `run_sync`는 Qt를 import하지 않는다. 진행 상황은 콜백 `on_progress(done, total, msg)`, 중단은 `cancel()` 함수(또는 `threading.Event`)로 받는다.

- 화면·전략 코드는 `query.py`만 호출한다(전략 패키지가 데이터 패키지를 직접 뒤지지 않는 기존 구조 규칙 유지).
- 원본 응답 zip/json은 `cache/dart/`에 보관해 재파싱 시 API를 다시 부르지 않는다. 캐시는 `.gitignore`.

## 7. 단계

| 단계 | 내용 | 완료 기준 |
|---|---|---|
| 1 | `client`·`corp`·`filings`, 스키마, 한도·재시도·캐시 | 상장사 전체 최근 5개 사업연도 보고서 목록 적재, 호출 수 로그 |
| 2 | `financials`: fin_raw → fin_std (분기·누적·4분기 계산, 연결 우선, 정정 처리) | 검증 종목 5개를 DART 화면 값과 대조해 전부 일치 |
| 3 | `v_fin_growth`(YoY·QoQ·전환 플래그), `query.get_financials(as_of)`, `sync.run_sync`·`lock`·`api_usage`·`sync_run` | 증감표 출력, as_of 테스트 통과, CLI `--daily` 두 번째 실행은 새 공시가 없으면 호출 수십 건 이내 |
| 3A | **앱 시작 시 자동 수집**(11장): `DartSyncThread`, 상태 표시, 종료 처리 | 앱 시작이 느려지지 않음(첫 화면 표시 시간 차이 0.5초 이내), 앱을 중간에 닫았다 켜도 이어서 받음, 화면에 진행·완료·실패 표시 |
| 4 | `segments` (관심 종목) + 검산 + `segment_map`, 자동 수집에 포함 | 대상 종목별 추출 성공·검산 통과 비율 보고 |
| 5 | 앱 화면: MA 차트 팝업에 **재무** 탭(13장 시안) | 시안 승인 후 별도 UI 지시 |

검증 종목(2단계): 삼성전자(005930), SK하이닉스(000660), 현대차(005380), 성호전자(043260), 주성엔지니어링(036930) — 대형·중소형, 코스피·코스닥 혼합. 대조 항목: 2025년 4개 분기 매출·영업이익(4분기는 계산값).

## 8. 테스트

- 단위(가짜 응답): 4분기 계산, CFS→OFS 대체, 정정본 선택, IS/CIS 대체, 013·020 처리, 단위 변환.
- `as_of`: 2025-11-10 기준 조회 시 2025년 3분기 값(접수 11월 중순)이 **나오지 않아야** 한다.
- 회귀(선택, 키가 있을 때만): 검증 종목 5개 기대값 일치. 키가 없으면 skip.
- 수집(가짜 클라이언트): 잠금이 잡혀 있으면 두 번째 `run_sync`는 바로 `skipped` 반환 / 오래된 잠금(프로세스 없음·2시간 경과)은 회수 / `cancel()` 후 다음 보고서 경계에서 멈추고 `partial` 기록, 재실행 시 이어 받음 / `api_usage`가 한도에 닿으면 `partial` / 키가 없으면 호출 없이 `no_key`.
- 앱 스레드(pytest-qt): 시작 시그널 → 완료 시그널, 창을 닫으면 2초 안에 스레드 종료.
- 기존 테스트 전부 통과, `ruff check src` 통과.

## 9. 하지 않는 것

- 전략 규칙·백테스트에 재무 지표를 넣는 것(별도 가설 등록 후).
- 원화 환산, 컨센서스·추정치 수집.
- 재무 화면(5단계)은 13장 시안 승인 전에는 만들지 않는다. 3A단계에서는 수집 상태 표시만 넣는다.
- Universe 표에 재무 열 추가(표 폭 문제로 8.10에서 철회한 것과 같은 이유).
- Windows 작업 스케줄러 등록(12장, 구조만 맞춰 둠).

## 10. CLI 프롬프트

1~3단계:
```
docs/dart_db_plan_2026-11-26.md 1~3단계를 구현해줘.
src/data/dart/ 패키지와 tools/dart_sync.py, dart.db 스키마(3장)를 만들고, 4장 규칙대로 매출·영업이익·순이익을 분기 단위로 적재해줘.
API 키는 .env의 DART_API_KEY에서 읽고, 일일 호출 수를 api_usage 테이블에 세서 19,000건에서 멈추게 해줘. 원본 응답은 cache/dart/에 캐시.
수집 로직은 6장대로 src/data/dart/sync.py의 run_sync() 하나에 두고(Qt import 금지), lock.py 잠금과 sync_run 기록을 붙여줘. CLI는 run_sync를 호출만 한다.
수집 기간은 DART_YEARS = 5(2021~2025 + 2026 진행 분기).
8장 테스트를 추가하고, 7장 검증 종목 5개의 2025년 분기 매출·영업이익을 DART 화면 값과 대조한 표를 보여줘.
pytest·ruff 결과를 보여준 뒤 커밋해줘. 전략 코드와 화면은 건드리지 마.
```

4단계(1~3단계 커밋 후):
```
docs/dart_db_plan_2026-11-26.md 4단계(사업부별 매출)를 구현해줘.
대상은 우선 검증 종목 5개와 Universe 종목. 5장 순서대로 추출하고 합계 검산 결과(ok/sum_mismatch/review)를 종목별로 표로 보여줘.
segment_map 초안은 자동 생성하되, 매핑 안 된 부문명 목록을 따로 출력해줘.
```

3A단계(1~3단계 커밋 후):
```
docs/dart_db_plan_2026-11-26.md 3A단계(11장 앱 시작 시 자동 수집)를 구현해줘.
src/ui/dart_sync_thread.py에 run_sync(trigger='app')를 부르는 QThread를 만들고, 11장 시작 시점·하루 한 번 규칙·종료 처리·상태 표시를 그대로 따라줘.
상태 표시는 상위 탭 줄 오른쪽의 갱신 상태 옆에만 넣고, 재무 화면(13장)은 만들지 마.
docs/ui.md 8장에 새 절로 기록하고, 8장 테스트(수집·앱 스레드)를 추가해줘.
앱을 켜서 첫 화면이 뜨는 시간이 전과 같은지, 수집 중에 앱을 닫았다 다시 켰을 때 이어 받는지 확인한 결과를 보여준 뒤 커밋해줘.
```

## 11. 앱 시작 시 자동 수집 (3A단계, 먼저 구현)

1. **시작 시점**: 메인 창이 뜬 뒤 Universe 첫 새로고침이 끝나거나 10초가 지나면(둘 중 늦은 쪽) `DartSyncThread`를 시작한다. 앱 시작과 시세 갱신을 늦추지 않기 위해서다.
2. **하루 한 번**: `sync_state.last_run`이 오늘(한국 시각) 19:00 이후에 끝났으면 건너뛴다. 앱을 계속 켜 두면 30분마다 이 조건만 확인해 19:00 이후 한 번 더 돈다. 정기보고서는 장중~18시 전후에 접수되므로 19시 이후 한 번이면 그날 공시를 모두 받는다.
3. **작업 순서**(한 번의 실행 안에서):
   1. 회사 목록(`corpCode.xml`) — 7일 지났을 때만.
   2. 증분: `daily_until − 3일`부터 오늘까지 정기공시 목록 → 새 접수번호(정정 포함)만 재무 수집. 3일 겹치는 이유는 늦게 반영되는 공시 대비.
   3. 초기 수집이 안 끝났으면 남은 한도 안에서 이어 받기(`backfill_cursor`). **Universe 종목 → 나머지는 시가총액 큰 순서**로 받아, 첫날부터 보는 종목은 쓸 수 있게 한다.
   4. 사업부 매출(4단계 이후): 새로 들어온 관심 종목 보고서만.
4. **한도·속도**: 하루 19,000건(`api_usage`를 CLI·스케줄러와 공유), 초당 5건 이하로 보수적으로 호출. 초기 수집은 상장사 약 2,700개 × 5년치라 앱을 매일 켜면 **3~4일**에 끝난다. 이후 평소 하루 호출은 수십~수백 건, 분기보고서 마감 주간(5·8·11월 중순)과 사업보고서 마감 주간(3월 말)에는 수천 건.
5. **동시 실행 방지**: `cache/dart/sync.lock`을 잡지 못하면(CLI나 스케줄러가 수집 중) 이번 실행은 `skipped`로 넘어간다.
6. **종료**: 창을 닫으면 `cancel()`을 보내고 최대 2초 기다린다. 보고서 단위로 커밋하므로 다음 실행이 이어서 받는다. 창 닫기를 막지 않는다.
7. **키가 없을 때**: 호출하지 않고 상태를 "DART 키 없음"으로 둔다(툴팁: `.env`에 `DART_API_KEY`를 넣으면 다음 실행부터 수집).
8. **상태 표시**(ui.md 8.4·8.7 규칙): 상위 탭 줄 오른쪽의 갱신 상태 옆에 짧게.
   - 수집 중: `DART 수집 중 1,234건` (초기 수집 중이면 `DART 초기 수집 43%`)
   - 완료: `DART 11-26 19:42` — 툴팁에 새 공시 n건, 호출 수, 남은 한도
   - 한도·종료로 멈춤: `DART 일부 수집` (WARN색), 실패: `DART 실패` (DANGER색) — 툴팁에 오류 한 줄
   - 완료 시 상태바 `status_message`로 "DART 새 공시 12건 반영"을 잠깐 띄운다.
9. **끄기**: `custom_settings.json`의 `dart_auto_sync`(기본 `true`). 화면의 토글은 5단계에서 다룬다.

## 12. Windows 작업 스케줄러 (나중에 붙일 수 있게 구조만)

- 실행 명령: `python tools/dart_sync.py --auto` → `run_sync(mode='auto', trigger='scheduler')`. 11장 3번과 같은 작업 순서다.
- 앱과 같은 잠금·`api_usage`·`sync_state`를 쓰므로 앱이 켜져 있을 때 스케줄러가 돌아도 한쪽은 `skipped`로 넘어가고 한도는 함께 센다.
- 등록 예(실제 등록은 이 단계에서 하지 않음):
  `schtasks /Create /TN "NexusTrading\DART" /SC DAILY /ST 19:30 /TR "<프로젝트>\.venv\Scripts\python.exe <프로젝트>\tools\dart_sync.py --auto"`
  PC가 꺼져 있던 날은 '예약된 시작 시간을 놓치면 가능한 빨리 실행' 옵션으로 다음 부팅 때 실행.
- 붙일 때 할 일: 등록·해제 스크립트(`tools/register_dart_task.ps1`), 로그 파일(`cache/dart/logs/`), 앱 상태 표시가 `sync_run.trigger='scheduler'` 실행도 보여 주는지 확인.

## 13. 화면 시안 (5단계, 승인 대기)

시안 파일: `docs/ui_mockups/dart_financials_mockup.html` (가상 회사·가상 수치).

- 위치: Universe에서 종목을 열면 뜨는 **MA 차트 팝업**(`StockMaDialog`)에 `차트 | 재무` 탭을 둔다. Universe 표 폭은 건드리지 않는다.
- 머리줄: 종목명·코드·시장, 기준 보고서(접수일·연결/별도), 마지막 수집 시각. `분기 | 연간` 전환.
- KPI 카드 4장: 최근 분기 매출 · 영업이익(영업이익률) · 지배주주순이익 · 최근 4분기 합계 매출. 각 카드에 YoY·QoQ.
- 차트: 최근 5년 분기 매출 막대 + 영업이익률 선. 4분기(계산값)는 빗금, 적자 분기는 빨강.
- 분기 표: 매출·YoY·영업이익·영업이익률·지배순이익·접수일, 흑자전환·적자전환 배지, 계산값 †, 정정 반영 표시.
- 사업부 매출: 연도별 누적 막대(부문 비중) + 부문별 매출·YoY 표, 검산 배지(ok / 합계 불일치 / 확인 필요), 부문 개편 메모(`segment_map`).
- 색: 앱 규칙 그대로 파랑 = 증가·이익, 빨강 = 감소·손실(ui.md 1.1, 8.16 Soft Slate).

