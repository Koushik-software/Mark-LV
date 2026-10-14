"""Tests for core/timers.py.

The clock is injected, so firing is deterministic — no sleeping, no threads.
"""
from __future__ import annotations

from core.timers import TimerManager


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


def _mgr():
    fired = []
    clock = FakeClock()
    m = TimerManager(notifier=fired.append, clock=clock)
    return m, clock, fired


def test_timer_fires_after_its_duration():
    m, clock, fired = _mgr()
    m.start(60, "tea")
    assert m.tick() == []
    clock.advance(59)
    assert m.tick() == []
    clock.advance(2)
    assert m.tick() == ["tea"]
    assert len(fired) == 1
    assert "tea" in fired[0]


def test_timer_fires_only_once():
    m, clock, fired = _mgr()
    m.start(10, "x")
    clock.advance(11)
    m.tick()
    clock.advance(100)
    assert m.tick() == []
    assert len(fired) == 1


def test_duration_is_clamped():
    m, _, _ = _mgr()
    m.start(0, "tiny")            # below 1s → clamped up
    m.start(10 ** 9, "huge")      # above 24h → clamped down
    active = m.list_active()
    assert active[0]["seconds"] == 1.0
    assert active[1]["seconds"] == 24 * 3600.0


def test_cancel_removes_timer():
    m, clock, fired = _mgr()
    tid = m.start(30, "x")
    assert m.cancel(tid) is True
    clock.advance(60)
    assert m.tick() == []
    assert fired == []


def test_cancel_unknown_is_false():
    m, _, _ = _mgr()
    assert m.cancel("nope") is False


def test_cancel_all():
    m, _, _ = _mgr()
    m.start(10, "a"); m.start(20, "b")
    assert m.cancel_all() == 2
    assert m.list_active() == []


def test_remaining_counts_down():
    m, clock, _ = _mgr()
    m.start(100, "x")
    clock.advance(30)
    assert m.list_active()[0]["remaining"] == 70


def test_notifier_error_does_not_propagate():
    def bad(_msg):
        raise RuntimeError("speaker exploded")
    m = TimerManager(notifier=bad, clock=FakeClock())
    m.start(1, "x")
    m._clock.advance(2)
    assert m.tick() == ["x"]      # label still returned, no raise


def test_no_notifier_is_fine():
    m = TimerManager(clock=FakeClock())
    m.start(1, "x")
    m._clock.advance(2)
    assert m.tick() == ["x"]


# ── duration parsing (actions/productivity.py) ───────────────────────────────
# A "10 minutes" request once silently failed to parse, so these pin the units.

def test_parse_duration_plural_units():
    from actions.productivity import _parse_duration
    assert _parse_duration("10 minutes") == 600
    assert _parse_duration("1 minute") == 60
    assert _parse_duration("2 hours") == 7200
    assert _parse_duration("45 secs") == 45
    assert _parse_duration("30 seconds") == 30


def test_parse_duration_abbreviations_and_compounds():
    from actions.productivity import _parse_duration
    assert _parse_duration("30s") == 30
    assert _parse_duration("5 min") == 300
    assert _parse_duration("10m") == 600
    assert _parse_duration("1h") == 3600
    assert _parse_duration("1h30m") == 5400
    assert _parse_duration("1.5 hours") == 5400


def test_parse_duration_bare_number_is_minutes():
    from actions.productivity import _parse_duration
    assert _parse_duration("5") == 300


def test_parse_duration_rejects_nonsense():
    from actions.productivity import _parse_duration
    assert _parse_duration("soon") is None
    assert _parse_duration("10 minuets") is None
    assert _parse_duration("") is None


# ── stopwatch (core/timers.py) ───────────────────────────────────────────────

class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def test_stopwatch_counts_up():
    from core.timers import Stopwatch
    c = FakeClock()
    sw = Stopwatch(clock=c)
    assert sw.start() is True
    c.advance(5)
    assert sw.elapsed() == 5
    c.advance(3)
    assert sw.elapsed() == 8


def test_stopwatch_start_twice_is_false():
    from core.timers import Stopwatch
    sw = Stopwatch(clock=FakeClock())
    assert sw.start() is True
    assert sw.start() is False


def test_stopwatch_pause_resume_accumulates():
    from core.timers import Stopwatch
    c = FakeClock()
    sw = Stopwatch(clock=c)
    sw.start(); c.advance(10)
    assert sw.stop() == 10
    assert sw.running is False
    c.advance(100)               # paused time must not count
    assert sw.elapsed() == 10
    sw.start(); c.advance(5)
    assert sw.elapsed() == 15


def test_stopwatch_laps():
    from core.timers import Stopwatch
    c = FakeClock()
    sw = Stopwatch(clock=c)
    sw.start()
    c.advance(4); assert sw.lap() == 4
    c.advance(6); assert sw.lap() == 10
    assert sw.laps() == [4, 10]


def test_stopwatch_reset():
    from core.timers import Stopwatch
    c = FakeClock()
    sw = Stopwatch(clock=c)
    sw.start(); c.advance(9); sw.lap()
    sw.reset()
    assert sw.elapsed() == 0
    assert sw.laps() == []
    assert sw.running is False


def test_format_duration():
    from core.timers import format_duration
    assert format_duration(0) == "0:00"
    assert format_duration(45) == "0:45"
    assert format_duration(90) == "1:30"
    assert format_duration(3600) == "1:00:00"
    assert format_duration(3725) == "1:02:05"
