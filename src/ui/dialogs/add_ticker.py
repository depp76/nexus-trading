"""ui/dialogs/add_ticker.py — AddTickerDialog: the Trading Universe "종목 추가"
button's small form (market + code). It replaced the tab's own second toolbar
row on 2026-11-16 (user direction; docs/ui.md 8.17), so the table starts one
row higher. The dialog only collects the input; the tab runs the lookup and
reports the result in its footer as before.
"""
from __future__ import annotations

from PyQt6.QtWidgets import QComboBox, QDialog, QFormLayout, QLabel, QLineEdit, QVBoxLayout

from i18n import tr
from ui.common import button_row, make_button

# Markets a single name can be added from: (stored value, label). One US
# choice (user decision 2026-11-15): the US lookup does not depend on the
# exchange, and "NASDAQ"/"NYSE" next to the NASDAQ 100 / S&P 500 toggles read
# as the same thing. Rows added earlier as "NASDAQ"/"NYSE" still load (any
# market without a toggle always shows, universe_views.matches).
ADD_TICKER_MARKETS = (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ"), ("US", tr("미국", "US")))


def add_placeholder(market: str) -> str:
    if market in ("KOSPI", "KOSDAQ"):
        return tr("코드 예: 005930", "Ticker, e.g. 005930")
    return tr("티커 예: AAPL", "Ticker, e.g. AAPL")


class AddTickerDialog(QDialog):
    """`market` preselects the market combo. After `exec()` is accepted,
    `values()` is (market, ticker) with the ticker stripped and upper-cased."""

    def __init__(self, market: str = "KOSPI", parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("종목 추가", "Add Ticker"))
        self.setMinimumWidth(360)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        form = QFormLayout()
        self.market_combo = QComboBox()
        for value, label in ADD_TICKER_MARKETS:
            self.market_combo.addItem(label, value)
        self.market_combo.setToolTip(tr("추가할 종목의 시장(툴바의 시장 필터와 별개)",
                                        "Market of the ticker to add (separate from the toolbar's market filter)"))
        self.ticker_input = QLineEdit()
        self.ticker_input.returnPressed.connect(self._on_add)
        self.ticker_input.textChanged.connect(self._sync_add_enabled)
        self.market_combo.currentIndexChanged.connect(self._on_market_changed)
        form.addRow(tr("추가할 시장", "Add to market"), self.market_combo)
        form.addRow(tr("종목코드", "Ticker"), self.ticker_input)
        root.addLayout(form)

        hint = QLabel(tr("유니버스에 없는 종목을 하나 받아 와 표에 더합니다. 결과는 표 아래 상태 줄에 표시됩니다.",
                         "Fetches one name that is not in the universe and adds it to the table; "
                         "the result shows in the status line under the table."))
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.add_btn = make_button(tr("추가", "Add"), "primary", self._on_add)
        cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        root.addLayout(button_row(cancel_btn, self.add_btn))

        idx = self.market_combo.findData(market)
        self.market_combo.setCurrentIndex(max(idx, 0))
        self._on_market_changed()
        self._sync_add_enabled()
        self.ticker_input.setFocus()

    def _on_market_changed(self, _index: int = 0):
        self.ticker_input.setPlaceholderText(add_placeholder(self.market_combo.currentData()))

    def _sync_add_enabled(self, _text: str = ""):
        self.add_btn.setEnabled(bool(self.ticker_input.text().strip()))

    def _on_add(self):
        if self.ticker_input.text().strip():
            self.accept()

    def values(self) -> tuple[str, str]:
        return self.market_combo.currentData(), self.ticker_input.text().strip().upper()
