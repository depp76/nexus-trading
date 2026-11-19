"""ui/dialogs — Dialog windows, one module per dialog group (split from the former
single ui/dialogs.py on 2026-09-17). Import from this package, e.g.
`from ui.dialogs import StockMaDialog`."""
from ui.dialogs.index_ma import IndexMaDialog
from ui.dialogs.stock_ma import StockMaDialog
from ui.dialogs.trade_edit import BuyEditDialog, SellEditDialog, TradeEntryDialog
from ui.dialogs.trade_history import StockTradeHistoryDialog
from ui.dialogs.assets_graph import TotalAssetsGraphDialog
from ui.dialogs.assets_report import AssetsReportDialog
from ui.dialogs.variant_picker import VariantPickerDialog
from ui.dialogs.trend_conditions import TrendConditions, TrendConditionsDialog
from ui.dialogs.strategy_trades import StrategyTradesDialog
from ui.dialogs.add_ticker import AddTickerDialog
from ui.dialogs.add_record import AddRecordDialog
from ui.dialogs.kis_sync import KisSyncDialog
from ui.dialogs.signal_conditions import SignalConditionsDialog

__all__ = [
    "IndexMaDialog", "StockMaDialog",
    "BuyEditDialog", "SellEditDialog", "TradeEntryDialog",
    "StockTradeHistoryDialog", "TotalAssetsGraphDialog", "AssetsReportDialog",
    "VariantPickerDialog", "TrendConditions", "TrendConditionsDialog", "StrategyTradesDialog",
    "AddTickerDialog",
    "AddRecordDialog", "KisSyncDialog", "SignalConditionsDialog",
]
