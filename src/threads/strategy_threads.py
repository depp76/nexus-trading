"""threads/strategy_threads.py — QThread workers for the strategy packages:
the Strategy tab's research run, and the current-signal runs of
strategy.signal_api (the Universe "전략 신호" lists and the MA chart's
"전략 점검").

Every network call the Trend Following tab triggers (index/stock histories,
the KOSPI listing, investor flows, the benchmark ETF, the CD91 rate) plus the
CPU-bound matrix run happens here, never in a UI slot (CLAUDE.md threads rule).
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal

from i18n import tr

from strategy import signal_api
from strategy.common.prices import ResearchCancelled
from strategy.trend_following import ResearchRequest, load_dataset, run_research

logger = logging.getLogger(__name__)

# Displayed as-is and compared only against this same constant (the language
# is fixed per process), so translating it is safe.
CANCELLED_MESSAGE = tr("취소됨", "Cancelled")


class TrendFollowingResearchThread(QThread):
    """Loads the dataset for `request` and runs the 6-2 matrix / 6-1 event
    study. ``finished(result | None, error)``; ``progress(text)`` carries short
    status lines for the tab's status label. ``cancel()`` (any thread) makes
    the loader and the matrix runner stop at their next check and finish with
    ``(None, CANCELLED_MESSAGE)``."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(object, str)

    def __init__(self, request: ResearchRequest):
        super().__init__()
        self.request = request
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        req = self.request
        try:
            ds = load_dataset(
                req.start, req.end, universe_size=req.params.universe_size,
                include_flows=req.include_flows, params=req.params, progress=self.progress.emit,
                should_stop=self.is_cancelled, allow_holdout=req.allow_holdout, markets=req.markets,
            )
            result = run_research(ds, req, progress=self.progress.emit, should_stop=self.is_cancelled)
            self.finished.emit(result, "")
        except ResearchCancelled:
            logger.info("Trend Following research run cancelled by the user")
            self.finished.emit(None, CANCELLED_MESSAGE)
        except Exception as e:
            # boundary: the thread must always emit finished; the research run spans data loading and the engine.
            logger.warning("Trend Following research run failed", exc_info=True)
            self.finished.emit(None, str(e))


class SignalPicksThread(QThread):
    """A universe run of one strategy variant (strategy.signal_api.run_picks):
    its Top / Bottom lists over the Universe rows of the variant's markets.
    The Trading Universe "전략 신호" button and the MA chart's "계산" run it
    (named SignalPicksThread until 2026-11-16, when it ran only the 2-5 score).
    ``finished(SignalPicks | None, error)``; ``progress(text)`` as above;
    ``cancel()`` finishes with ``(None, CANCELLED_MESSAGE)``."""
    progress = pyqtSignal(str)
    finished = pyqtSignal(object, str)

    def __init__(self, items: list, variant_id: str, n_top: int = 10):
        super().__init__()
        self.items = list(items)
        self.variant_id = variant_id
        self.n_top = n_top
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            rec = signal_api.run_picks(self.items, self.variant_id, n=self.n_top,
                                       progress=self.progress.emit, should_stop=self.is_cancelled)
            self.finished.emit(rec, "")
        except ResearchCancelled:
            logger.info("Signal run cancelled by the user")
            self.finished.emit(None, CANCELLED_MESSAGE)
        except Exception as e:
            # boundary: the thread must always emit finished; the error text is shown in the status line.
            logger.warning("Signal run %s failed", self.variant_id, exc_info=True)
            self.finished.emit(None, str(e))


class StrategyCheckThread(QThread):
    """The MA chart's "전략 점검" for one stock under one variant
    (strategy.signal_api.run_check): whatever the variant's provider loads for
    one stock (trend following: its daily history and KOSPI, plus investor
    flows for the F rows).
    ``finished(StockCheck | None, error, variant_id)`` -- the variant is echoed
    so the slot can drop a result for a variant the user has moved on from."""
    finished = pyqtSignal(object, str, str)

    def __init__(self, item: dict, variant_id: str, picks=None):
        super().__init__()
        self.item = dict(item)
        self.variant_id = variant_id
        self.picks = picks
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    def run(self):
        try:
            check = signal_api.run_check(self.item, self.variant_id, self.picks, should_stop=self.is_cancelled)
            self.finished.emit(check, "", self.variant_id)
        except ResearchCancelled:
            self.finished.emit(None, CANCELLED_MESSAGE, self.variant_id)
        except Exception as e:
            # boundary: the thread must always emit finished; the error text is shown in the panel.
            logger.warning("Strategy check failed for %s", self.item.get("ticker"), exc_info=True)
            self.finished.emit(None, str(e), self.variant_id)
