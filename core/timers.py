"""
core/timers.py — short countdown timers that speak when they finish.

WHY THIS IS SEPARATE FROM reminder.py
    reminder.py schedules a *wall-clock* reminder (a specific date and time) by
    registering it with the OS scheduler — Task Scheduler, launchd, systemd — so
    it survives the app being closed. That is the right tool for "remind me on
    Tuesday at 9".

    "Set a timer for ten minutes" is a different thing: it is relative, short,
    and only meaningful while the assistant is running. Registering it with the
    OS would be overkill and would fire even after you quit. So timers live in
    memory, in this module, for the life of the process.

TESTABILITY
    The clock and the notifier are both injectable, and firing is done by an
    explicit `tick()`. Tests advance a fake clock and call tick() — no real
    sleeping, no threads, no flakiness. The background thread is started only by
    `start_thread()`, which the app calls and tests do not.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass
class Timer:
    id: str
    label: str
    seconds: float
    due_at: float          # clock() value at which this fires
    fired: bool = False


class TimerManager:
    """A small set of in-process countdown timers.

    `notifier` is called with a human sentence when a timer fires. It runs on
    whatever thread calls tick(), which for the app is the poll thread — so it
    must itself be thread-safe (main.py's speak() already is)."""

    def __init__(self, notifier: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._timers: dict[str, Timer] = {}
        self._lock = threading.Lock()
        self._notifier = notifier
        self._clock = clock
        self._seq = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # ── registration ─────────────────────────────────────────────────────────

    def start(self, seconds: float, label: str = "timer") -> str:
        """Register a timer. Returns its id. Seconds are clamped to a sane range."""
        seconds = max(1.0, min(24 * 3600.0, float(seconds)))
        with self._lock:
            self._seq += 1
            tid = f"t{self._seq}"
            self._timers[tid] = Timer(
                id=tid, label=(label or "timer").strip()[:80],
                seconds=seconds, due_at=self._clock() + seconds,
            )
        return tid

    def cancel(self, tid: str) -> bool:
        with self._lock:
            return self._timers.pop(str(tid), None) is not None

    def cancel_all(self) -> int:
        with self._lock:
            n = len(self._timers)
            self._timers.clear()
        return n

    def list_active(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            return [
                {"id": t.id, "label": t.label,
                 "seconds": t.seconds,
                 "remaining": max(0, round(t.due_at - now))}
                for t in self._timers.values() if not t.fired
            ]

    # ── firing ───────────────────────────────────────────────────────────────

    def tick(self) -> list[str]:
        """Fire every timer whose due time has passed. Returns fired labels.

        Called by the poll thread, or directly by tests with a fake clock."""
        now = self._clock()
        fired: list[Timer] = []
        with self._lock:
            for t in list(self._timers.values()):
                if not t.fired and now >= t.due_at:
                    t.fired = True
                    fired.append(t)
                    self._timers.pop(t.id, None)
        for t in fired:
            if self._notifier:
                try:
                    self._notifier(self._message(t))
                except Exception:
                    pass
        return [t.label for t in fired]

    @staticmethod
    def _message(t: Timer) -> str:
        return f"Sir, your {t.label} timer is done."

    # ── background poll thread (app only) ────────────────────────────────────

    def start_thread(self, interval: float = 1.0) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_loop, daemon=True,
                                        name="jarvis-timers")
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()
        self._thread = None


# ── One manager for the process ──────────────────────────────────────────────
_manager: Optional[TimerManager] = None


def get_manager() -> TimerManager:
    global _manager
    if _manager is None:
        _manager = TimerManager()
    return _manager


def bind_notifier(fn: Callable[[str], None]) -> None:
    """Register how a fired timer is announced. Called once from main.py."""
    get_manager()._notifier = fn
