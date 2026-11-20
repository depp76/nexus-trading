"""ui/dialogs/trade_edit.py — Trade entry/edit dialogs for TradingHistoryTab (buy side, sell side, new trade).

Split out of the former single ui/dialogs.py (2026-09-17)."""
from i18n import tr
import logging
from datetime import datetime

from PyQt6.QtWidgets import (
    QDialog, QFormLayout,
    QLineEdit, QComboBox, QMessageBox,
    QApplication,
)
from PyQt6.QtCore import Qt


logger = logging.getLogger(__name__)

from ui.common import (
    make_button,
    button_row,
    _fmt_num_edit,
    _fmt_num_value,
    _validate_date_str,
    _normalize_date_str,
    _validate_positive_number,
    _mk_field_validator,
    _set_field_error,
)
from ui.history_calc import trade_conflict
from threads.fetch_threads import TickerValidateThread

# Message boxes carry the owning tab's name (docs/ui.md 8.5).
_TITLE = "Trading History"

# Edit dialogs (docs/ui.md 8.17): 380px wide at least so dates and amounts
# are never clipped, labels right-aligned on one column, fields growing.
_DIALOG_MIN_W = 400

# Field-validator tooltips (red border + this text on bad input).
_MSG_DATE = tr("날짜 형식이 올바르지 않습니다 (YYYY-MM-DD)", "Invalid date format (YYYY-MM-DD)")
_MSG_POSITIVE = tr("0보다 큰 숫자를 입력하세요", "Enter a number greater than 0")
_MSG_SELL_REQUIRED = tr("매도를 기록하려면 0보다 큰 숫자가 필요합니다", "A number greater than 0 is required to record a sell")
_MSG_SELL_DATE_REQUIRED = tr("매도 값을 입력하면 매도일(YYYY-MM-DD)이 필요합니다",
                             "A sell date (YYYY-MM-DD) is required once a sell value is entered")
_MSG_AMOUNT = tr("비워 두거나 0보다 큰 숫자를 입력하세요", "Leave empty or enter a number greater than 0")


def _validate_optional_amount(text: str) -> bool:
    """An amount field: empty (auto-calculated) or a positive finite number;
    "inf" used to pass and fail only at the DB save (review_agy M12-6)."""
    if not text.replace(",", "").strip():
        return True
    return _validate_positive_number(text)


def _form_layout(dialog: QDialog) -> QFormLayout:
    layout = QFormLayout(dialog)
    layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    layout.setHorizontalSpacing(10)
    layout.setVerticalSpacing(8)
    return layout


# ---------------------------------------------------------------------------
# BuyEditDialog — Edit buy details in TradingHistoryTab
# ---------------------------------------------------------------------------
class BuyEditDialog(QDialog):
    """Dialog for editing buy details in TradingHistoryTab."""
    def __init__(self, current_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("매수 수정", "Edit Buy"))
        self.setMinimumWidth(_DIALOG_MIN_W)
        self.result_data = None
        self._sell_date_str = current_data.get("sell_date", "") or ""
        self._sell_qty = float(current_data.get("sell_qty", 0.0) or 0.0)

        layout = _form_layout(self)

        self.buy_date_edit = QLineEdit(current_data.get("buy_date", ""))
        self.buy_date_edit.setPlaceholderText("YYYY-MM-DD")

        # Pre-fill at stored precision (review_codex.md #4): the old ",.2f"/",.0f"
        # display strings were parsed back on save and rounded the record.
        self.buy_price_edit = QLineEdit(_fmt_num_value(current_data.get("buy_price", 0.0)))
        self.buy_qty_edit = QLineEdit(_fmt_num_value(current_data.get("qty", 0.0)))
        self.buy_amount_edit = QLineEdit(_fmt_num_value(current_data.get("buy_amount", 0.0)))
        self.buy_amount_edit.setPlaceholderText(tr("비우면 자동 계산", "Auto-calculated if empty"))

        layout.addRow(tr("매수일:", "Buy Date:"), self.buy_date_edit)
        layout.addRow(tr("매수가:", "Buy Price:"), self.buy_price_edit)
        layout.addRow(tr("수량:", "Quantity:"), self.buy_qty_edit)
        layout.addRow(tr("매수 금액:", "Buy Amount:"), self.buy_amount_edit)

        # 취소 → 저장, right-aligned; no default button, so Enter in a
        # field never saves (docs/ui.md 8.17).
        save_btn = make_button(tr("저장", "Save"), "primary", self.on_save)
        cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        btn_box = button_row(cancel_btn, save_btn)

        # ---1,000-separator auto-format ---
        self.buy_price_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.buy_price_edit, t, decimal=True))
        self.buy_qty_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.buy_qty_edit, t))
        self.buy_amount_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.buy_amount_edit, t, decimal=True))

        # ---Real-time validation: red border + tooltip on bad input (roadmap 2-5) ---
        self._val_date = _mk_field_validator(
            self.buy_date_edit, _validate_date_str, _MSG_DATE)
        self._val_price = _mk_field_validator(
            self.buy_price_edit, _validate_positive_number, _MSG_POSITIVE)
        self._val_qty = _mk_field_validator(
            self.buy_qty_edit, _validate_positive_number, _MSG_POSITIVE)
        self._val_amount = _mk_field_validator(
            self.buy_amount_edit, _validate_optional_amount, _MSG_AMOUNT)
        self.buy_date_edit.textChanged.connect(self._val_date)
        self.buy_price_edit.textChanged.connect(self._val_price)
        self.buy_qty_edit.textChanged.connect(self._val_qty)
        self.buy_amount_edit.textChanged.connect(self._val_amount)
        # Validate pre-filled values immediately so existing bad data is flagged on open.
        self._val_date()
        self._val_price()
        self._val_qty()
        self._val_amount()

        layout.addRow(btn_box)

    def on_save(self):
        date_ok = self._val_date()
        price_ok = self._val_price()
        qty_ok = self._val_qty()
        amount_ok = self._val_amount()
        if not (date_ok and price_ok and qty_ok and amount_ok):
            QMessageBox.warning(self, _TITLE, tr("빨간 테두리로 표시된 칸을 확인하세요.", "Please check the highlighted fields (shown with a red border)."))
            return

        def to_f(val):
            try:
                return float(val.replace(',', '').replace('%', '').strip())
            except Exception:
                return 0.0

        b_price = to_f(self.buy_price_edit.text())
        b_qty = to_f(self.buy_qty_edit.text())
        b_amt = to_f(self.buy_amount_edit.text())
        buy_date_str = _normalize_date_str(self.buy_date_edit.text())

        # The sell side already recorded on this lot still has to hold after
        # the buy side moves (review_codex.md #5).
        conflict = trade_conflict(buy_date_str, b_qty, self._sell_date_str, self._sell_qty)
        if conflict:
            QMessageBox.warning(self, _TITLE, conflict)
            return

        self.result_data = {
            "buy_date": buy_date_str,
            "buy_price": b_price,
            "qty": b_qty,
            "buy_amount": b_amt if b_amt > 0 else b_price * b_qty,
        }
        self.accept()


# ---------------------------------------------------------------------------
# SellEditDialog — Edit sell details in TradingHistoryTab
# ---------------------------------------------------------------------------
class SellEditDialog(QDialog):
    """Dialog for editing sell details in TradingHistoryTab."""
    def __init__(self, current_data, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("매도 수정", "Edit Sell"))
        self.setMinimumWidth(_DIALOG_MIN_W)
        self.result_data = None
        self._buy_date_str = current_data.get("buy_date", "")
        self._orig_qty = float(current_data.get("qty", 0.0))

        layout = _form_layout(self)

        self.sell_date_edit = QLineEdit(current_data.get("sell_date", ""))
        self.sell_date_edit.setPlaceholderText(tr("YYYY-MM-DD (보유 중이면 비움)", "YYYY-MM-DD (leave empty if still open)"))

        self.sell_price_edit = QLineEdit(_fmt_num_value(current_data.get("sell_price", 0.0)))
        self.sell_qty_edit = QLineEdit(_fmt_num_value(current_data.get("sell_qty", 0.0)))
        self.sell_amount_edit = QLineEdit(_fmt_num_value(current_data.get("sell_amount", 0.0)))
        self.sell_amount_edit.setPlaceholderText(tr("비우면 자동 계산", "Auto-calculated if empty"))

        layout.addRow(tr("매도일:", "Sell Date:"), self.sell_date_edit)
        layout.addRow(tr("매도가:", "Sell Price:"), self.sell_price_edit)
        layout.addRow(tr("매도 수량:", "Sell Quantity:"), self.sell_qty_edit)
        layout.addRow(tr("매도 금액:", "Sell Amount:"), self.sell_amount_edit)

        # 취소 → 저장, right-aligned; no default button, so Enter in a
        # field never saves (docs/ui.md 8.17).
        save_btn = make_button(tr("저장", "Save"), "primary", self.on_save)
        cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        btn_box = button_row(cancel_btn, save_btn)

        # ---1,000-separator auto-format ---
        # Price/amount accept decimals like the buy side (US sell prices were
        # truncated to an integer as they were typed); quantities stay whole
        # shares, matching TradeEntryDialog.
        self.sell_price_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.sell_price_edit, t, decimal=True))
        self.sell_qty_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.sell_qty_edit, t))
        self.sell_amount_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.sell_amount_edit, t, decimal=True))

        # ---Real-time validation (roadmap 2-5) ---
        # All sell fields empty keeps (or puts back) the position open. Once
        # any of them is filled, the sell date, price and quantity are all
        # required: a sell price alone used to save a closed trade with no
        # date and sell_qty 0 (review_agy H18).
        def _validate_sell_amount_field(text: str) -> bool:
            text = text.replace(",", "").replace("%", "").strip()
            if not text:
                return not self._sell_entered()
            return _validate_positive_number(text)

        self._val_sell_price = _mk_field_validator(
            self.sell_price_edit, _validate_sell_amount_field, _MSG_SELL_REQUIRED)
        self._val_sell_qty = _mk_field_validator(
            self.sell_qty_edit, _validate_sell_amount_field, _MSG_SELL_REQUIRED)
        self._val_sell_amount = _mk_field_validator(
            self.sell_amount_edit, _validate_optional_amount, _MSG_AMOUNT)
        for edit in (self.sell_date_edit, self.sell_price_edit, self.sell_qty_edit, self.sell_amount_edit):
            for check in (self._val_sell_date, self._val_sell_price, self._val_sell_qty, self._val_sell_amount):
                edit.textChanged.connect(check)
        self._val_sell_date()
        self._val_sell_price()
        self._val_sell_qty()
        self._val_sell_amount()

        layout.addRow(btn_box)

    def _sell_entered(self) -> bool:
        """True when any sell field has text (the dialog then records a sell)."""
        return any(e.text().replace(",", "").strip()
                   for e in (self.sell_date_edit, self.sell_price_edit, self.sell_qty_edit, self.sell_amount_edit))

    def _val_sell_date(self, _ignored=None) -> bool:
        """Empty only while no other sell field is filled; else a valid date."""
        text = self.sell_date_edit.text().strip()
        if text:
            ok, msg = _validate_date_str(text), _MSG_DATE
        else:
            ok, msg = not self._sell_entered(), _MSG_SELL_DATE_REQUIRED
        _set_field_error(self.sell_date_edit, "" if ok else msg)
        return ok

    def on_save(self):
        date_ok = self._val_sell_date()
        price_ok = self._val_sell_price()
        qty_ok = self._val_sell_qty()
        amount_ok = self._val_sell_amount()
        if not (date_ok and price_ok and qty_ok and amount_ok):
            QMessageBox.warning(self, _TITLE, tr("빨간 테두리로 표시된 칸을 확인하세요.", "Please check the highlighted fields (shown with a red border)."))
            return

        sell_date_str = _normalize_date_str(self.sell_date_edit.text())

        def to_f(val):
            try:
                return float(val.replace(',', '').replace('%', '').strip())
            except Exception:
                return 0.0

        s_price = to_f(self.sell_price_edit.text())
        s_qty = to_f(self.sell_qty_edit.text())
        s_amt = to_f(self.sell_amount_edit.text())

        conflict = trade_conflict(self._buy_date_str, self._orig_qty, sell_date_str, s_qty)
        if conflict:
            QMessageBox.warning(self, _TITLE, conflict)
            return

        self.result_data = {
            "sell_date": sell_date_str,
            "sell_price": s_price,
            "sell_qty": s_qty,
            "sell_amount": s_amt if s_amt > 0 else s_price * s_qty,
        }
        self.accept()


# ---------------------------------------------------------------------------
# TradeEntryDialog — Enter a new trade into TradingHistoryTab
# ---------------------------------------------------------------------------
class TradeEntryDialog(QDialog):
    """Dialog for entering a new trade into TradingHistoryTab."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("거래 추가", "Add New Trade"))
        self.setMinimumWidth(_DIALOG_MIN_W)
        self.result_data = None
        self._validate_thread = None
        self._pending = None  # input snapshot of the in-flight ticker check

        layout = _form_layout(self)

        self.market_combo = QComboBox()
        # One US choice (user decision 2026-11-15): the stored value is "US";
        # trades saved earlier as NASDAQ / NASDAQ 100 / NYSE / AMEX keep their
        # value and are still US everywhere (data.cache.is_us_market).
        for value, label in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ"), ("US", tr("미국", "US"))):
            self.market_combo.addItem(label, value)

        self.ticker_edit = QLineEdit()
        self.ticker_edit.setPlaceholderText(tr("종목코드 (예: 005930, QQQM)", "Ticker (e.g. QQQM or 005930)"))

        self.buy_date_edit = QLineEdit(datetime.now().strftime("%Y-%m-%d"))
        self.buy_date_edit.setPlaceholderText("YYYY-MM-DD")
        self.buy_price_edit = QLineEdit()
        self.qty_edit = QLineEdit()
        self.buy_amount_edit = QLineEdit()
        self.buy_amount_edit.setPlaceholderText(tr("비우면 자동 계산", "Auto-calculated if empty"))

        layout.addRow(tr("시장:", "Market:"), self.market_combo)
        layout.addRow(tr("종목코드:", "Ticker:"), self.ticker_edit)
        layout.addRow(tr("매수일:", "Buy Date:"), self.buy_date_edit)
        layout.addRow(tr("매수가:", "Buy Price:"), self.buy_price_edit)
        layout.addRow(tr("매수 수량:", "Buy Quantity:"), self.qty_edit)
        layout.addRow(tr("매수 금액:", "Buy Amount:"), self.buy_amount_edit)

        # ---1,000-separator auto-format ---
        self.buy_price_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.buy_price_edit, t, decimal=True))
        self.qty_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.qty_edit, t))
        self.buy_amount_edit.textEdited.connect(
            lambda t: _fmt_num_edit(self.buy_amount_edit, t, decimal=True))

        # ---Real-time validation (roadmap 2-5) ---
        self._val_date = _mk_field_validator(
            self.buy_date_edit, _validate_date_str, _MSG_DATE)
        self._val_price = _mk_field_validator(
            self.buy_price_edit, _validate_positive_number, _MSG_POSITIVE)
        self._val_qty = _mk_field_validator(
            self.qty_edit, _validate_positive_number, _MSG_POSITIVE)
        self._val_amount = _mk_field_validator(
            self.buy_amount_edit, _validate_optional_amount, _MSG_AMOUNT)
        self.buy_date_edit.textChanged.connect(self._val_date)
        self.buy_price_edit.textChanged.connect(self._val_price)
        self.qty_edit.textChanged.connect(self._val_qty)
        self.buy_amount_edit.textChanged.connect(self._val_amount)
        self._val_date()
        self._val_price()
        self._val_qty()
        self._val_amount()

        # 취소 → 저장, right-aligned; no default button (docs/ui.md 8.17).
        self.save_btn = make_button(tr("저장", "Save"), "primary", self.on_save)
        self.cancel_btn = make_button(tr("취소", "Cancel"), on_click=self.reject)
        btn_box = button_row(self.cancel_btn, self.save_btn)

        layout.addRow(btn_box)

    def on_save(self):
        market = self.market_combo.currentData()
        ticker = self.ticker_edit.text().strip().upper()
        if not ticker:
            QMessageBox.warning(self, _TITLE, tr("종목코드를 입력하세요.", "Ticker is required."))
            return

        date_ok = self._val_date()
        price_ok = self._val_price()
        qty_ok = self._val_qty()
        amount_ok = self._val_amount()
        if not (date_ok and price_ok and qty_ok and amount_ok):
            QMessageBox.warning(self, _TITLE, tr("빨간 테두리로 표시된 칸을 확인하세요.", "Please check the highlighted fields (shown with a red border)."))
            return

        def to_f(val):
            try:
                return float(val.replace(',', '').replace('%', '').strip())
            except Exception:
                return 0.0

        # Everything the record is built from is frozen here, and the inputs
        # are locked until the reply lands: the reply validates *this* market/
        # ticker, so reading the widgets again in _on_ticker_validated() could
        # pair one stock's company name with another ticker, or save numbers
        # typed after the check (review_codex.md #3).
        b_price = to_f(self.buy_price_edit.text())
        qty = to_f(self.qty_edit.text())
        b_amt = to_f(self.buy_amount_edit.text())
        self._pending = {
            "market": market,
            "ticker": ticker,
            "buy_date": _normalize_date_str(self.buy_date_edit.text()),
            "buy_price": b_price,
            "qty": qty,
            "buy_amount": b_amt if b_amt > 0 else b_price * qty,
        }

        # Ticker validation hits the network (fetch_single_stock -> yahooquery ->
        # Naver fallback chain), so it runs on TickerValidateThread instead of
        # blocking the UI thread here. Buttons stay disabled and closeEvent()
        # ignores close requests until _on_ticker_validated() fires, so the
        # dialog can't be destroyed while the thread still holds a connection to it.
        self._set_inputs_enabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        thread = TickerValidateThread(market, ticker)
        thread.finished.connect(self._on_ticker_validated)
        self._validate_thread = thread
        # The reply is emitted from inside run(): accept() can return exec()
        # and drop this dialog while run() has not returned yet. The owning
        # tab keeps the thread referenced until it has finished and stops it
        # at shutdown (review_agy C2).
        owner = self.parent()
        if owner is not None and hasattr(owner, "_track_thread"):
            owner._track_thread(thread)
        thread.start()

    def _set_inputs_enabled(self, enabled: bool) -> None:
        for w in (self.market_combo, self.ticker_edit, self.buy_date_edit, self.buy_price_edit,
                  self.qty_edit, self.buy_amount_edit, self.save_btn, self.cancel_btn):
            w.setEnabled(enabled)

    def _on_ticker_validated(self, is_valid: bool, company: str, err: str):
        QApplication.restoreOverrideCursor()
        self._set_inputs_enabled(True)
        pending, self._pending = self._pending, None
        if pending is None:
            return

        if not is_valid:
            QMessageBox.warning(
                self, _TITLE,
                tr(f"종목 정보를 찾을 수 없는 종목코드입니다:\n{pending['ticker']}\n\n상세: {err or ''}", f"Ticker is not valid (Could not find stock information):\n{pending['ticker']}\n\nDetails: {err or ''}"),
            )
            return

        self.result_data = {
            **pending,
            "company": company,
            "sell_date": "",
            "sell_price": 0.0,
            "sell_qty": 0.0,
            "sell_amount": 0.0,
        }
        self.accept()

    def _validating(self) -> bool:
        return self._pending is not None or (
            self._validate_thread is not None and self._validate_thread.isRunning())

    def closeEvent(self, event):
        """Block window-close (title-bar X, Alt+F4, ...) while a validation
        thread is still running -- see the comment in on_save()."""
        if self._validating():
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self):
        """Esc goes straight to reject(), not closeEvent(): ignored while the
        ticker check runs too, so the dialog is not dropped under a running
        thread and the wait cursor is always restored (review_agy C2)."""
        if self._validating():
            return
        super().reject()
