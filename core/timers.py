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


class Stopwatch:
    """A single stopwatch: start, stop, lap, elapsed.

    The plan groups stopwatch with timers, but the two are different animals: a
    timer fires and disappears, a stopwatch *accumulates* and can be paused and
    resumed. It shares the injectable clock so tests stay deterministic, and
    holds no thread of its own — elapsed time is computed on read, so a paused
    stopwatch costs nothing while the app idles."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._accumulated = 0.0     # seconds banked across previous runs
        self._started_at: Optional[float] = None
        self._laps: list[float] = []

    @property
    def running(self) -> bool:
        return self._started_at is not None

    def start(self) -> bool:
        """Start or resume. False if it was already running."""
        if self.running:
            return False
        self._started_at = self._clock()
        return True

    def stop(self) -> float:
        """Pause and return the total elapsed seconds. 0 if it was not running."""
        if not self.running:
            return self.elapsed()
        self._accumulated += self._clock() - self._started_at
        self._started_at = None
        return self._accumulated

    def reset(self) -> None:
        self._accumulated = 0.0
        self._started_at = None
        self._laps.clear()

    def elapsed(self) -> float:
        """Total elapsed seconds, counting the current run if it is going."""
        total = self._accumulated
        if self.running:
            total += self._clock() - self._started_at
        return total

    def lap(self) -> float:
        """Record and return the current elapsed time as a lap."""
        t = self.elapsed()
        self._laps.append(t)
        return t

    def laps(self) -> list[float]:
        return list(self._laps)


def format_duration(seconds: float) -> str:
    """Human-readable H:MM:SS / M:SS, for timers, stopwatches and the HUD. Pure."""
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


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


# ── One stopwatch for the process ────────────────────────────────────────────
_stopwatch: Optional[Stopwatch] = None


def get_stopwatch() -> Stopwatch:
    global _stopwatch
    if _stopwatch is None:
        _stopwatch = Stopwatch()
    return _stopwatch
