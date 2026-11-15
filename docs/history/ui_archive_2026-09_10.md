# 화면 설계 보관본 (docs/ui.md에서 옮김, 2026-11-15)

`docs/ui.md`에서 옮긴 지난 기록이다. 절 번호는 원문 그대로이며, `docs/ui.md`의 같은 번호 자리에는 한두 줄 요약과 이 문서로의
안내가 남아 있다. 지금 규칙은 `docs/ui.md`와 `src/ui/theme.py`가 기준이다.

---

## 5. 옛 Strategy 탭 (기록용)

2026-09-28에 옛 전략 계층과 함께 제거된 화면의 명세다. 지금의 Strategy 탭은 Trend Following 리서치 화면
(`ui/trend_following_tab.py`, 8.4·8.6)이라 아래 구조와 다르다. 고지 배너(5.6)와 상태·오류 분리(5.2)의 원칙만 지금 화면에도 적용돼 있다.

### 5.1 구조

```
헤더:      앱명 · 탭 · 계산 시각/소요 시간 · [시그널 다시 계산]
시그널 카드: 매수 후보 · 매도 후보 · 추세 보유 · 관망  (4장)
커버리지:   경고 배너 — 검사 범위와 그 이유
서브탭:     Auto Trading · Trend Following · MA Cross(준비중, 비활성)
표:        단일 랭킹 표 11열
고지:      투자 유의 배너 (본문 크기)
```

### 5.2 시그널 카드

현재 한 줄 f-string(`_summary_lbl`)을 카드 4장으로 분해한다.

| 카드 | 값 | 보조 |
| --- | --- | --- |
| 매수 후보 | 5 종목 | 리밸런스 밴드 상단 진입 |
| 매도 후보 | 3 종목 | 시장별 순위 25위 밖으로 이탈 |
| 추세 보유 | 12 / 30 | 20일 채널 상단 돌파 유지 |
| 관망 | 18 / 30 | 오류 0건 · 400일 워밍업 |

- 상태(`computing...`)와 오류(`computation failed (…)`)는 **카드와 다른 영역**에 표시한다. 결과 라벨을 덮어쓰지 않는다.
- 헤더에 계산 시각과 소요 시간. 일정 시간이 지나면 "오래됨" 표시.

### 5.3 커버리지 배너

`_TF_SUMMARY_MAX_TICKERS = 30` 제약을 결과 문자열 괄호가 아니라 **배너로** 노출한다.

> 추세추종 시그널은 시총 상위 30종목만 검사합니다(전체 312종목 중 9.6%). 조회 비용 때문이며, 나머지 282종목의 신호는 계산되지 않았습니다. **[범위 넓히기 →]**

범위 확장 버튼은 예상 소요 시간을 함께 안내한다.

### 5.4 단일 랭킹 표

Buy 후보 표 + Sell 후보 표 + Full Ranking 13열 = 같은 결과의 3중 복제를 **표 하나 + 상태 필터**로 합친다.

| 그룹 | span | 컬럼 |
| --- | --- | --- |
| 종목 | 1 | Rank · 종목명 · `티커 · 시장` |
| 시그널 | 2 | Action 배지 · Score (바 + 수치) |
| 점수 기여 요인 | 6 | PER · MA20Div · MA50Div · 52wHigh% · Ret20D% · Ret60D% |
| 추세추종 | 2 | 상태(보유 중/관망) · 보유 |

- **기여 요인 셀은 점수 기여도에 비례한 accent 농도**로 칠한다. 왜 그 종목이 후보인지 같은 줄에서 읽힌다.
- Action 배지: 매수(초록 아웃라인) · 매도(PROFIT 아웃라인) · 보유(accent) · `—`.
- 🟢🔴 이모지를 쓰지 않는다.
- 컬럼 폭은 최소폭+비율로 고정한다(`ResizeToContents` 금지 — 계산할 때마다 열이 움직인다).

### 5.5 미구현 탭

MA Cross는 **비활성 탭 + `준비중` 배지**로 명시한다. 회색 안내 문구 한 줄짜리 빈 탭이 다른 두 전략과 같은 무게를 갖지 않게 한다.

### 5.6 고지

"리서치·백테스트용 신호 생성기이며 투자 자문이 아닙니다"를 8pt 회색(`create_font(8)`, `#888`)에서 **표 하단 고정 배너, 본문 크기(12px), 경고 톤**으로 올린다.

---

### 6.1 현재 전역 스타일시트가 강제하는 것

`src/main.py` 하단 `app.setStyleSheet(...)`:

| 규칙 | 현재 값 | 결과 |
| --- | --- | --- |
| `QMainWindow` | `background-color: #f0f0f0` | 창 전체가 회색. 목업의 `#fbfbfd` 바탕이 안 나온다 |
| `QPushButton` | `background: #0078d4; color: white; padding: 8px 16px` | **앱의 모든 버튼이 파란 채움.** "주 동작만 강조" 규칙(§1.6)이 원천 차단된다 |
| `QHeaderView::section` | `background: #e0e0e0; border: 1px solid #d0d0d0; padding: 4px; bold` | 헤더가 회색 상자. 목업의 연보라 헤더·2단 그룹 규칙이 묻힌다 |
| `QTabBar::tab` | `background: #e0e0e0; padding: 10px 30px; bold` | 탭이 크고 무겁다. 목업의 컴팩트 pill과 다르다 |
| `QTableWidget` | `alternate-background-color: #f9f9f9; gridline-color: #d0d0d0` | 지브라·격자가 목업(`#fafbfe` / `#f0f1f8`)보다 어둡고 진하다 |
| `QLineEdit` / `QComboBox` | `padding: 5px; border: 1px solid #c0c0c0` | 입력창이 두껍고, 읽기 전용 값도 같은 모양 |

즉 **목업과 앱의 차이는 대부분 탭 코드가 아니라 이 40줄에서 발생합니다.**

### 6.2 Phase 0 — 테마 모듈 분리

전역 스타일시트를 `src/ui/theme.py`로 옮기고 §1의 토큰에서 생성합니다.

```python
# src/ui/theme.py
from ui.colors import PROFIT, LOSS, FLAT

BG          = "#fbfbfd"   # 앱 바탕
SURFACE     = "#ffffff"   # 카드·표 본문
ZEBRA       = "#fafbfe"   # 짝수 행
HDR_BG      = "#f3f5fe"   # 표 헤더
GRP_BG      = "#f8f9fe"   # 그룹 헤더
TEXT        = "#1c1e2c"
TEXT_SUB    = "#595d6c"
TEXT_MUTED  = "#75798c"
TEXT_FAINT  = "#9397ab"
TEXT_EMPTY  = "#c3c6d4"   # 미해당 —
LINE_STRONG = "#cfd3e5"
LINE        = "#e4e7f5"
LINE_SOFT   = "#f0f1f8"
ACCENT      = "#9184d9"
ACCENT_TEXT = "#5d5294"
ACCENT_BG   = "#f5f4ff"

def app_qss(font_css: str) -> str:
    return f"""
    QMainWindow, QWidget#Page {{ background-color: {BG}; }}

    /* 버튼: 기본은 중립 아웃라인. 강조는 objectName 으로만. */
    QPushButton {{
        background: {SURFACE}; color: {TEXT_SUB};
        border: 1px solid {LINE_STRONG}; border-radius: 6px;
        padding: 5px 12px; font-weight: 400; {font_css}
    }}
    QPushButton:hover    {{ background: {HDR_BG}; }}
    QPushButton:pressed  {{ background: {LINE}; }}
    QPushButton:disabled {{ color: {TEXT_EMPTY}; border-color: {LINE_SOFT}; }}
    QPushButton#primary {{
        background: {SURFACE}; color: {ACCENT_TEXT}; border: 1px solid {ACCENT};
    }}
    QPushButton#primary:hover {{ background: {ACCENT_BG}; }}
    QPushButton#danger  {{ color: {PROFIT}; border-color: #f0c4c1; }}
    QPushButton#danger:hover {{ background: #fdf0ef; }}

    QTableWidget, QTableView {{
        background-color: {SURFACE};
        alternate-background-color: {ZEBRA};
        gridline-color: {LINE_SOFT};
        selection-background-color: {ACCENT_BG};
        selection-color: {TEXT};
        border: 1px solid {LINE}; border-radius: 8px;
        {font_css}
    }}
    QTableWidget::item, QTableView::item {{ padding: 2px 8px; }}
    QTableWidget::item:hover {{ background: {ACCENT_BG}; }}

    QHeaderView::section {{
        background-color: {HDR_BG}; color: {TEXT_MUTED};
        border: none; border-bottom: 1px solid {LINE_STRONG};
        border-right: 1px solid {LINE};
        padding: 5px 8px; font-weight: 600; {font_css}
    }}

    QTabWidget::pane {{ border: 1px solid {LINE}; background: {SURFACE}; border-radius: 8px; }}
    QTabBar::tab {{
        background: transparent; color: {TEXT_MUTED};
        border: 1px solid transparent; border-radius: 6px;
        padding: 5px 12px; margin-right: 2px; font-weight: 400; {font_css}
    }}
    QTabBar::tab:selected {{ background: {ACCENT_BG}; color: {ACCENT_TEXT}; border-color: #c9c2f3; }}
    QTabBar::tab:hover:!selected {{ background: {HDR_BG}; }}

    QLineEdit, QComboBox {{
        background: {SURFACE}; color: {TEXT};
        border: 1px solid {LINE_STRONG}; border-radius: 6px;
        padding: 4px 9px; {font_css}
    }}
    QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
    QLineEdit[readOnly="true"] {{ background: transparent; border: none; color: {TEXT}; }}

    QLabel#kpiLabel {{ color: {TEXT_FAINT}; font-size: 10px; font-weight: 600; }}
    QLabel#kpiValue {{ color: {TEXT}; font-size: 17px; }}
    QLabel#kpiSub   {{ color: {TEXT_FAINT}; font-size: 10px; }}
    """
```

`main.py`는 이것만 호출합니다:

```python
from ui.theme import app_qss
app.setStyleSheet(app_qss(FONT_FAMILY_CSS))
```

그리고 **위젯별 `setStyleSheet` 하드코딩을 전부 제거**합니다. 현재 위반:

- `history_tab.py` — `_BTN_BLUE` `_BTN_ORANGE` `_BTN_GREY` `_BTN_INSIGHT` `_INPUT_STYLE` `_COMBO_STYLE` `_CARD_STYLE` `_POS_TABLE_STYLE`
- `assets_tab.py` — `add_btn` `del_btn` `today_btn` `graph_btn` `export_btn` 각각의 인라인 QSS
- `widgets.py` — `btn_tg` `btn_ma`(보라 `#7d3c98`) `btn_del`(빨강 `#e74c3c`), `tg_filter_btn`(`#87CEEB`)

→ 전역 QSS + `setObjectName("primary" | "danger")` 두 가지로 대체합니다.

## 7. 적용 순서 (당시 계획)

→ 현재: Phase 0~4와 6이 적용됐다(2026-09 ~ 2026-10, 8장). 2.5 열 그룹 토글은 2026-09-19 사용자 결정으로 삭제된 상태,
6의 "지수/원자재 분리"는 2.4에서 폐기됐다.

| Phase | 내용 | 대상 파일 | 규모 |
| --- | --- | --- | --- |
| **0** | **전역 스타일시트를 `ui/theme.py`로 분리 · 위젯별 인라인 QSS 제거 · Fusion 스타일** | `main.py` `app.setStyleSheet` · `history_tab.py` `_BTN_*` · `assets_tab.py` · `widgets.py` | **1–2일** |
| 1 | 색 상수 전역 통일 · 숫자 tabular · 히트맵 임계값 상수화 | `widgets.py` `_C_*` `_populate_row` · `history_table.py` `col_red/col_blue` | 1–2일 |
| 2 | 정렬·갱신 정합성 (ticker 매핑 또는 Model/Proxy 전환) | `widgets.py` `load_data` `update_changed_rows` | 3–5일 |
| 3 | 컬럼 폭 체계 (최소폭+비율) · 고정 열 · 컬럼 그룹 · 밀도 | `widgets.py` `_stretch_columns` · `assets_tab.py` `widths` | 3–4일 |
| 4 | 서브 헤더 라벨 · 통화 토글 · 인라인 차트 | `assets_tab.py` `_COLS_SUB` `_show_graph` | 3–4일 |
| ~~5~~ | 시그널 카드 · 커버리지 배너 · 단일 랭킹 표 | (Strategy 탭 2026-09-28 제거, 재도입 시 재검토) | — |
| 6 | 지수/원자재 분리 · 저장된 뷰 · 행 선택·복사 · 월 요약 그룹 헤더 | `universe_tab.py` · `history_calc.py` · `history_table.py` | 1주+ |

**Phase 0을 먼저 하지 않으면 이후 작업의 결과가 화면에 나타나지 않습니다** — `main.py`의 전역 QSS가 버튼·헤더·탭·표 색을 전부 덮어쓰기 때문입니다. Phase 1–2는 코드 변경량 대비 체감이 가장 크고, Phase 2는 실제 데이터 오염 가능성을 막는 수정이므로 우선순위가 높습니다.

---
