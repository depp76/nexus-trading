"""Inter-process lock for DART collection (docs/dart_db_plan_2026-11-26.md 6, 11-5).

The lock is a file (``cache/dart/sync.lock``) created with O_EXCL and holding
the owner's pid and start time. A second collector (app, CLI or scheduler) that
finds it skips its run. A lock whose process is gone, or that is older than
``STALE_AFTER`` seconds, is taken over (plan 8: stale-lock recovery).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time

logger = logging.getLogger(__name__)

STALE_AFTER = 2 * 60 * 60   # seconds


def pid_alive(pid: int) -> bool:
    """Whether process `pid` is running. On Windows os.kill would terminate the
    process, so OpenProcess / GetExitCodeProcess is asked instead."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class SyncLock:
    """``acquire()`` -> True when this process now holds the lock; ``release()``
    removes the file only if it is still ours. Usable as a context manager
    (``__enter__`` returns whether the lock was taken)."""

    def __init__(self, path: str, stale_after: float = STALE_AFTER, clock=time.time):
        self.path = path
        self.stale_after = stale_after
        self._clock = clock
        self._token = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        for _ in range(2):
            token = {"pid": os.getpid(), "started": self._clock(), "nonce": os.urandom(4).hex()}
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._reclaim_if_stale():
                    return False
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(token, f)
            self._token = token
            return True
        return False

    def release(self) -> None:
        if self._token is None:
            return
        try:
            if self._read() == self._token:
                os.remove(self.path)
        except OSError:
            logger.warning("Could not remove DART sync lock %s", self.path, exc_info=True)
        self._token = None

    @property
    def held(self) -> bool:
        return self._token is not None

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _reclaim_if_stale(self) -> bool:
        """Remove the existing lock when its owner is gone or it is too old."""
        info = self._read()
        if isinstance(info, dict):
            age = self._clock() - float(info.get("started", 0))
            alive = pid_alive(int(info.get("pid", -1)))
            if alive and age < self.stale_after:
                return False
            logger.warning("Reclaiming stale DART sync lock (pid %s, alive=%s, age %.0f s)",
                           info.get("pid"), alive, age)
        else:
            # An unreadable lock: a writer may be between O_EXCL and the dump; only
            # take it over when the file itself is old.
            try:
                if self._clock() - os.path.getmtime(self.path) < 60:
                    return False
            except OSError:
                return True
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not remove stale DART sync lock %s", self.path, exc_info=True)
            return False
        return True

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
