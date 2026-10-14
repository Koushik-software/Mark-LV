"""Baseline tests for core/undo.py and the pure helpers in core/gemini.py.

undo.py is the recovery path every reversible action relies on, and gemini.py's
error classifiers decide how the model ladder cools a failing rung. Both are
pure logic with no network or Qt dependency, so they are pinned here.
"""
from __future__ import annotations

import pytest

from core import gemini
from core import undo


@pytest.fixture(autouse=True)
def _clean_undo_stack():
    undo.clear()
    yield
    undo.clear()


@pytest.fixture(autouse=True)
def _clean_cooldowns(monkeypatch):
    monkeypatch.setattr(gemini, "_cooldown", {})


# ── undo ─────────────────────────────────────────────────────────────────────

def test_push_and_undo_reverses():
    state = {"v": 1}
    undo.push_undo("set v=2", lambda: state.update(v=1) or "restored")
    assert undo.can_undo()
    out = undo.undo_last()
    assert state["v"] == 1
    assert "Undone" in out and "restored" in out


def test_undo_empty_stack_is_graceful():
    assert "nothing to undo" in undo.undo_last().lower()


def test_peek_and_history_order():
    undo.push_undo("first", lambda: "")
    undo.push_undo("second", lambda: "")
    assert undo.peek() == "second"
    assert undo.history() == ["second", "first"]


def test_stack_is_capped_at_max_depth():
    for i in range(undo.MAX_DEPTH + 5):
        undo.push_undo(f"op{i}", lambda: "")
    assert len(undo.history()) == undo.MAX_DEPTH
    assert "op0" not in undo.history()          # oldest evicted


def test_non_callable_undo_is_ignored():
    undo.push_undo("bad", None)                  # type: ignore[arg-type]
    assert undo.can_undo() is False


def test_failing_undo_is_contained_and_popped():
    def boom():
        raise RuntimeError("nope")
    undo.push_undo("boom", boom)
    out = undo.undo_last()
    assert "Could not undo" in out
    assert undo.can_undo() is False              # popped, not retried forever


# ── gemini error classifiers ─────────────────────────────────────────────────

def test_is_quota_error():
    assert gemini.is_quota_error("429 RESOURCE_EXHAUSTED")
    assert not gemini.is_quota_error("500 internal")


def test_is_unavailable_error():
    assert gemini.is_unavailable_error("503 Service Unavailable")
    assert gemini.is_unavailable_error("504 DEADLINE_EXCEEDED")
    assert gemini.is_unavailable_error("model is unavailable")


def test_is_gone_error():
    assert gemini.is_gone_error("404 model not found")
    assert gemini.is_gone_error("403 permission denied")
    assert not gemini.is_gone_error("429")


def test_cooldown_sets_and_expires(monkeypatch):
    gemini._cool("m1", 300)
    assert gemini._cooling("m1") is True
    real = gemini.time.monotonic
    monkeypatch.setattr(gemini.time, "monotonic", lambda: real() + 10_000)
    assert gemini._cooling("m1") is False


def test_note_live_failure_only_steps_on_quota_or_gone():
    assert gemini.note_live_failure("models/x", "429 quota") is True
    assert gemini.note_live_failure("models/y", "404 not found") is True
    assert gemini.note_live_failure("models/z", "connection reset") is False


def test_live_model_skips_resting_rung():
    first = gemini.LIVE_MODELS[0]
    gemini._cool(first, 300)
    assert gemini.live_model() != first


def test_to_live_parts_accepts_strings_and_blobs():
    class Blob:
        class inline_data:  # noqa: N801 - mirror SDK attribute shape
            data = b"abc"
            mime_type = "image/png"

    parts = gemini._to_live_parts(["hello", Blob()])
    assert parts[0] == {"text": "hello"}
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["data"] == "YWJj"     # base64 of b"abc"


def test_as_json_strips_markdown_fences(monkeypatch):
    monkeypatch.setattr(gemini, "text", lambda *a, **k: '```json\n{"a": 1}\n```')
    assert gemini.as_json("q") == {"a": 1}


def test_as_json_returns_default_on_garbage(monkeypatch):
    monkeypatch.setattr(gemini, "text", lambda *a, **k: "not json at all")
    assert gemini.as_json("q", default="fallback") == "fallback"
