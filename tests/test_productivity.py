"""Tests for actions/productivity.py — file-backed notes and the stopwatch.

Notes are written to ~/Documents/JARVIS Notes/ in production; each test points
NOTES_DIR at a temp dir so nothing touches the user's Documents folder. The
memory mirror is left pointing at the real manager but never asserted on here —
tests that need it use the isolated_memory fixture.
"""
from __future__ import annotations

import pytest

from actions import productivity as prod


@pytest.fixture
def notes(tmp_path, monkeypatch):
    """Redirect the notes directory to a throwaway path.

    The note actions also mirror into the memory store, so this fixture isolates
    that too — otherwise these tests would write their sample notes into the
    developer's real long_term.json and jarvis_memory.db."""
    from memory import memory_manager as mm
    from memory import memory_store as ms

    ms.reset_for_tests()
    monkeypatch.setattr(mm, "MEMORY_PATH", tmp_path / "long_term.json")
    monkeypatch.setattr(mm, "_trim_notifier", None)
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "jarvis_memory.db")

    d = tmp_path / "JARVIS Notes"
    monkeypatch.setattr(prod, "NOTES_DIR", d)
    yield d
    ms.reset_for_tests()


# ── filename safety ──────────────────────────────────────────────────────────

def test_safe_note_name_strips_traversal():
    # Path separators become '_' and leading dots are stripped, so the result is
    # always a single filename component — never a path that can climb out.
    out = prod.safe_note_name("../../etc/passwd")
    assert "/" not in out
    assert not out.startswith(".")
    assert prod.safe_note_name("") == "note"
    assert prod.safe_note_name("   ") == "note"
    assert prod.safe_note_name("..") == "note"


def test_safe_note_name_keeps_ordinary_names():
    assert prod.safe_note_name("shopping list") == "shopping list"
    assert prod.safe_note_name("recipe-2026") == "recipe-2026"


def test_note_path_stays_inside_dir(notes):
    p = prod._note_path("../../evil")
    assert notes.resolve() in p.parents


# ── add / read / list / append / delete ──────────────────────────────────────

def test_note_add_creates_file(notes):
    out = prod.productivity({"action": "note_add", "key": "shopping",
                             "value": "Buy milk"})
    assert "Noted" in out
    assert (notes / "shopping.txt").read_text(encoding="utf-8") == "Buy milk"


def test_note_add_derives_key_from_text(notes):
    prod.productivity({"action": "note_add", "value": "call the plumber today"})
    assert (notes / "call_the_plumber_today.txt").exists()


def test_note_add_without_text_asks(notes):
    assert "note down" in prod.productivity({"action": "note_add", "value": ""}).lower()


def test_note_add_twice_appends(notes):
    prod.productivity({"action": "note_add", "key": "log", "value": "first"})
    prod.productivity({"action": "note_add", "key": "log", "value": "second"})
    body = (notes / "log.txt").read_text(encoding="utf-8")
    assert body == "first\nsecond"


def test_note_read(notes):
    prod.productivity({"action": "note_add", "key": "recipe", "value": "eggs"})
    out = prod.productivity({"action": "note_read", "key": "recipe"})
    assert "eggs" in out


def test_note_read_missing(notes):
    out = prod.productivity({"action": "note_read", "key": "nope"})
    assert "no note" in out.lower()


def test_note_append_requires_existing(notes):
    out = prod.productivity({"action": "note_append", "key": "ghost", "value": "x"})
    assert "no note" in out.lower()
    prod.productivity({"action": "note_add", "key": "keep", "value": "a"})
    prod.productivity({"action": "note_append", "key": "keep", "value": "b"})
    assert (notes / "keep.txt").read_text(encoding="utf-8") == "a\nb"


def test_note_list(notes):
    prod.productivity({"action": "note_add", "key": "one", "value": "1"})
    prod.productivity({"action": "note_add", "key": "two", "value": "2"})
    out = prod.productivity({"action": "note_list"})
    assert "one" in out and "two" in out


def test_note_list_empty(notes):
    assert "no notes" in prod.productivity({"action": "note_list"}).lower()


def test_note_delete(notes):
    prod.productivity({"action": "note_add", "key": "temp", "value": "x"})
    out = prod.productivity({"action": "note_delete", "key": "temp"})
    assert "Deleted" in out
    assert not (notes / "temp.txt").exists()


def test_note_delete_missing(notes):
    assert "no note" in prod.productivity(
        {"action": "note_delete", "key": "ghost"}).lower()


# ── stopwatch dispatch ───────────────────────────────────────────────────────

def test_stopwatch_lifecycle_via_action(monkeypatch):
    from core import timers
    clock = [100.0]
    c = timers.Stopwatch(clock=lambda: clock[0])
    monkeypatch.setattr(timers, "get_stopwatch", lambda: c)

    assert "started" in prod.productivity({"action": "stopwatch_start"}).lower()
    clock[0] += 42                      # 42s pass
    assert "0:42" in prod.productivity({"action": "stopwatch_read"})
    assert "Lap 1" in prod.productivity({"action": "stopwatch_lap"})
    assert "0:42" in prod.productivity({"action": "stopwatch_stop"})
    assert "reset" in prod.productivity({"action": "stopwatch_reset"}).lower()


def test_stopwatch_lap_while_stopped(monkeypatch):
    from core import timers
    monkeypatch.setattr(timers, "get_stopwatch", lambda: timers.Stopwatch())
    assert "not running" in prod.productivity({"action": "stopwatch_lap"}).lower()
