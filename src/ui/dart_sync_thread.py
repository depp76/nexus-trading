"""DART collection at app start (docs/dart_db_plan_2026-11-26.md 11; docs/ui.md 8.33).

``DartSyncThread`` runs ``data.dart.sync.run_sync(mode="auto", trigger="app")``
off the UI thread; ``cancel()`` stops it at the next report / page boundary
(everything up to the last finished report is committed, so the next run
resumes there).

``DartAutoSync`` decides when to run and shows the state in one short label
next to the header's refresh status:

- start (11-1): after the window is up, once the Universe's first refresh has
  ended *and* 10 s have passed, so neither the first screen nor the first
  quotes wait for it;
- once a day (11-2): at start unless a run already finished after today's
  19:00 KST; then every 30 minutes, run once more after 19:00 (the decision is
  made inside the thread, which reads dart.db);
- off with ``custom_settings.json`` ``dart_auto_sync: false`` (11-9);
- a run skipped because the CLI / a scheduler held the lock is retried after
  5 minutes; when nothing is due the label shows the newest finished run in
  dart.db, whoever ran it (plan 12), with the runner in the tooltip;
- label (11-8): ``DART 초기 수집 43%`` / ``DART 수집 중 1,234건`` while running,
  ``DART 11-26 19:42`` when done (tooltip: new filings, calls, limit left),
  ``DART 일부 수집`` (WARN) when stopped by the limit or the app closing,
  ``DART 실패`` (DANGER), ``DART 키 없음`` without ``DART_API_KEY``.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import QLabel

from data.dart import sync as dart_sync
from i18n import tr
from ui.colors import DANGER, WARN
from ui.common import FONT_BODY, create_font

logger = logging.getLogger(__name__)

START_DELAY_MS = 10_000          # plan 11-1
CHECK_INTERVAL_MS = 30 * 60_000  # plan 11-2
RETRY_AFTER_SKIP_MS = 5 * 60_000  # another collector held the lock


class DartSyncThread(QThread):
    """One collection run. Signals: ``started_info(dict)`` with the
    collection status read at start, ``progress(done, total, msg)`` from
    run_sync, ``sync_done(dict)`` at the end (``status`` is run_sync's status
    or ``not_due``; ``info`` the status read after the run)."""

    started_info = pyqtSignal(object)
    progress = pyqtSignal(int, int, str)
    sync_done = pyqtSignal(object)

    def __init__(self, at_start: bool = True, *, runner=None, status_reader=None, due=None, parent=None):
        super().__init__(parent)
        self.at_start = at_start
        self._cancel = threading.Event()
        self._runner = runner or dart_sync.run_sync
        self._status = status_reader or dart_sync.collection_status
        self._due = due or dart_sync.is_due

    def cancel(self) -> None:
        self._cancel.set()

    def run(self):
        out = {"status": "failed", "error": None, "result": None, "info": None}
        try:
            info = self._status()
            out["info"] = info
            if not self._due(info.get("last_run"), info.get("last_status"), dart_sync.kst_now(), self.at_start):
                out["status"] = "not_due"
                return
            self.started_info.emit(info)
            res = self._runner("auto", "app", cancel=self._cancel, on_progress=self._on_progress)
            out.update(status=res.status, error=res.error, result=res)
            out["info"] = self._status()
        except Exception as e:
            # boundary: a QThread run() reports any failure through its signal.
            logger.exception("DART auto sync failed")
            out.update(status="failed", error=f"{type(e).__name__}: {e}")
        finally:
            self.sync_done.emit(out)

    def _on_progress(self, done, total, msg):
        self.progress.emit(int(done), int(total), str(msg))


def _short_time(iso: str | None) -> str:
    try:
        t = _dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "-"
    return f"{t:%m-%d %H:%M}"


class DartAutoSync(QObject):
    """Scheduling + the header label for DART auto collection (see module doc)."""

    status_message = pyqtSignal(str)

    def __init__(self, enabled: bool = True, *, thread_factory=DartSyncThread,
                 start_delay_ms: int = START_DELAY_MS, check_interval_ms: int = CHECK_INTERVAL_MS,
                 retry_ms: int = RETRY_AFTER_SKIP_MS, parent=None):
        super().__init__(parent)
        self.enabled = enabled
        self._factory = thread_factory
        self.thread: DartSyncThread | None = None
        self._delay_passed = False
        self._universe_ready = False
        self._started_once = False
        self._stopped = False
        self._initial = False
        self._info: dict = {}
        self.label = QLabel("")
        self.label.setObjectName("faint")
        self.label.setFont(create_font(FONT_BODY, style_name="Semilight"))
        self.label.setVisible(enabled)
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.setInterval(start_delay_ms)
        self._delay_timer.timeout.connect(self._on_delay_passed)
        self._check_timer = QTimer(self)
        self._check_timer.setInterval(check_interval_ms)
        self._check_timer.timeout.connect(self._on_check_timer)
        # After a skip (another collector held the lock): look again soon, so the
        # label shows that run's outcome and the app resumes if it is still due.
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(retry_ms)
        self._retry_timer.timeout.connect(self._on_retry)

    # ---- scheduling -------------------------------------------------------------

    def arm(self, universe_ready: bool = False) -> None:
        """Called once the window is built; `universe_ready` when there is no
        first Universe refresh to wait for (a language switch's new window)."""
        if not self.enabled:
            return
        self._universe_ready = universe_ready
        self._delay_timer.start()
        self._check_timer.start()

    def on_universe_refreshed(self, *_args) -> None:
        """The Universe's first refresh ended (success or failure)."""
        self._universe_ready = True
        self._maybe_start_first()

    def _on_delay_passed(self) -> None:
        self._delay_passed = True
        self._maybe_start_first()

    def _maybe_start_first(self) -> None:
        if self._started_once or not (self._delay_passed and self._universe_ready):
            return
        self._started_once = True
        self._start(at_start=True)

    def _on_check_timer(self) -> None:
        if self._started_once:
            self._start(at_start=False)

    def _on_retry(self) -> None:
        self._start(at_start=True)

    def _start(self, at_start: bool) -> None:
        if self._stopped or not self.enabled or self.is_running():
            return
        t = self._factory(at_start)
        t.started_info.connect(self._on_started)
        t.progress.connect(self._on_progress)
        t.sync_done.connect(self._on_done)
        self.thread = t
        t.start()

    def is_running(self) -> bool:
        try:
            return self.thread is not None and self.thread.isRunning()
        except RuntimeError:
            return False

    def stop(self) -> None:
        """Window closing / replaced: no new runs; the running one is cancelled
        by the window's thread shutdown (cancel_thread)."""
        self._stopped = True
        self._delay_timer.stop()
        self._check_timer.stop()
        self._retry_timer.stop()

    def threads(self) -> list:
        return [self.thread] if self.thread is not None else []

    # ---- label ------------------------------------------------------------------

    def _set(self, text: str, tip: str = "", color: str | None = None) -> None:
        self.label.setText(text)
        self.label.setToolTip(tip)
        self.label.setStyleSheet(f"color: {color};" if color else "")

    def _on_started(self, info: dict) -> None:
        self._info = info or {}
        self._initial = dart_sync.is_initial_load(self._info)
        if self._initial:
            self._set(tr(f"DART 초기 수집 {self._initial_pct(None, None)}%",
                         f"DART initial load {self._initial_pct(None, None)}%"))
        else:
            self._set(tr("DART 수집 중", "DART collecting"))

    def _initial_pct(self, done, total) -> int:
        all_ = self._info.get("periods_total") or 0
        pending = self._info.get("periods_pending") or 0
        if done is not None and total:
            pending = max(0, total - done)
        if all_ <= 0:
            return 0
        return max(0, min(99, int(100 * (all_ - pending) / all_)))

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if not msg.startswith("financials"):
            return
        if self._initial:
            pct = self._initial_pct(done, total)
            self._set(tr(f"DART 초기 수집 {pct}%", f"DART initial load {pct}%"))
        else:
            self._set(tr(f"DART 수집 중 {done:,}건", f"DART collecting {done:,}"))

    def _on_done(self, out: dict) -> None:
        status = out.get("status")
        info = out.get("info") or {}
        res = out.get("result")
        err = out.get("error") or ""
        if status == "not_due":
            # Nothing to do now: show the newest finished run, whoever ran it
            # (an app, the CLI or a scheduler; plan 12).
            self._show_run(info.get("last_run_status") or "", info, None, info.get("last_error") or "", from_db=True)
        elif status == "skipped":
            if not self.label.text():
                self.label.setText(tr("DART 대기", "DART waiting"))
            self.label.setToolTip(tr("다른 수집(CLI·스케줄러)이 진행 중이라 이번 실행은 건너뜀 · 5분 뒤 다시 확인",
                                     "Another collector (CLI / scheduler) is running; skipped, checking again in 5 min"))
            self._retry_timer.start()
        else:
            self._show_run(status, info, res, err)
            if status == "done":
                added = res.filings_added if res is not None else 0
                self.status_message.emit(tr(f"DART 새 공시 {added:,}건 반영", f"DART: {added:,} new filings"))

    _TRIGGER = {"app": ("앱", "app"), "cli": ("CLI", "CLI"), "scheduler": ("스케줄러", "scheduler")}

    def _show_run(self, status: str, info: dict, res, err: str, from_db: bool = False) -> None:
        """The label for a finished run: this app's, or (`from_db`) the newest
        one in dart.db with its runner in the tooltip, numbers from `info`."""
        who = self._TRIGGER.get(info.get("last_trigger") or "", ("", ""))
        by = tr(f" · 실행: {who[0]}", f" · run by {who[1]}") if from_db and who[0] else ""
        if status == "done":
            added = res.filings_added if res is not None else (info.get("last_added") or 0)
            calls = res.calls if res is not None else (info.get("last_calls") or 0)
            tip = tr(f"새 공시 {added:,}건 · 호출 {calls:,}건 · 오늘 남은 한도 {info.get('calls_left', 0):,}건",
                     f"{added:,} new filings · {calls:,} calls · {info.get('calls_left', 0):,} calls left today") + by
            if info.get("periods_pending"):
                tip += tr(f"\n재무 미수집 {info['periods_pending']:,}기간 (다음 실행에서 이어 받음)",
                          f"\n{info['periods_pending']:,} periods still to fetch (resumed next run)")
            self._set(f"DART {_short_time(info.get('last_run'))}", tip)
        elif status == "partial":
            self._set(tr("DART 일부 수집", "DART partial"),
                      tr(f"한도 또는 종료로 멈춤, 다음 실행에서 이어 받음 · {err}",
                         f"Stopped by the limit or shutdown; resumed next run · {err}") + by, WARN)
        elif status == "no_key":
            self._set(tr("DART 키 없음", "DART no key"),
                      tr(".env에 DART_API_KEY를 넣으면 다음 실행부터 수집합니다.",
                         "Add DART_API_KEY to .env to collect from the next run."))
        elif status == "failed":
            self._set(tr("DART 실패", "DART failed"), (err.splitlines()[0] if err else "") + by, DANGER)
