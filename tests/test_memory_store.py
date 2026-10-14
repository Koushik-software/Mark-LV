"""Tests for memory/memory_store.py — the SQLite/FTS5 index.

Each test points DB_PATH at a temp file and clears the module's cached
connection, so tests never touch the developer's real jarvis_memory.db.
"""
from __future__ import annotations

import pytest

from memory import memory_store as ms


@pytest.fixture
def store(tmp_path, monkeypatch):
    ms.reset_for_tests()
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "jarvis_memory.db")
    yield ms
    ms.reset_for_tests()


def _mem():
    return {
        "identity": {"name": {"value": "Ada Lovelace", "updated": "2026-01-02"}},
        "notes": {"book": {"value": "reading Gödel Escher Bach", "updated": "2026-02-01"}},
        "sessions": [{"date": "2026-02-02", "summary": "chat"}],   # list → ignored
    }


# ── import / export ──────────────────────────────────────────────────────────

def test_import_indexes_entries(store):
    n = store.import_memory(_mem())
    assert n == 2                     # sessions (a list) is not indexed
    assert store.count() == 2


def test_import_removes_rows_no_longer_present(store):
    store.import_memory(_mem())
    store.import_memory({"identity": {"name": {"value": "Ada Lovelace"}}})
    assert store.count() == 1         # the note is gone from the JSON → gone here


def test_import_updates_changed_value(store):
    store.import_memory(_mem())
    store.import_memory({"identity": {"name": {"value": "Ada Byron"}}})
    rows = store.search("Byron")
    assert any(r["value"] == "Ada Byron" for r in rows)


def test_export_merges_over_base_and_keeps_unmodelled_keys(store):
    store.import_memory(_mem())
    base = {"sessions": [{"date": "x"}]}
    out = store.export_memory(base)
    assert out["identity"]["name"]["value"] == "Ada Lovelace"
    assert out["sessions"] == [{"date": "x"}]     # untouched


def test_sync_reports_counts(store):
    r = store.sync(_mem())
    assert r["rows"] == 2
    assert r["fts"] is True


# ── search ───────────────────────────────────────────────────────────────────

def test_search_finds_by_value_word(store):
    store.import_memory(_mem())
    rows = store.search("Gödel")
    assert len(rows) == 1
    assert rows[0]["key"] == "book"


def test_search_finds_by_key(store):
    store.import_memory(_mem())
    rows = store.search("name")
    assert any(r["key"] == "name" for r in rows)


def test_search_empty_query_returns_recent(store):
    store.import_memory(_mem())
    rows = store.search("", limit=10)
    assert len(rows) == 2


def test_search_no_match_is_empty(store):
    store.import_memory(_mem())
    assert store.search("zzzznope") == []


def test_search_punctuation_cannot_break_fts(store):
    """FTS5 operators in the query must be neutralised, not passed through."""
    store.import_memory(_mem())
    # A bare NEAR/OR/quote would be FTS syntax; these must simply find nothing.
    for q in ('"', "NEAR", "a OR b", "*", "book AND"):
        store.search(q)               # must not raise


def test_search_respects_limit(store):
    mem = {"notes": {f"k{i}": {"value": f"item number {i}"} for i in range(20)}}
    store.import_memory(mem)
    assert len(store.search("item", limit=5)) == 5


# ── degradation ──────────────────────────────────────────────────────────────

def test_unopenable_db_returns_sentinels(tmp_path, monkeypatch):
    ms.reset_for_tests()
    # A path whose parent is a FILE cannot be created as a directory.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(ms, "DB_PATH", blocker / "sub" / "db.sqlite")
    assert ms.count() == -1
    assert ms.import_memory({"notes": {"a": {"value": "b"}}}) == -1
    assert ms.search("a") == []
    assert ms.export_memory({"keep": 1}) == {"keep": 1}
    ms.reset_for_tests()


def test_entries_of_accepts_bare_strings(store):
    rows = ms._entries_of({"notes": {"k": "plain string"}})
    assert rows == [("notes", "k", "plain string", "")]


def test_entries_of_skips_blank_and_non_dict(store):
    rows = ms._entries_of({"notes": {"a": {"value": "  "}, "b": {"value": "ok"}},
                           "sessions": []})
    assert rows == [("notes", "b", "ok", "")]


def test_fts_query_neutralises_operators(store):
    # Every word is quoted, so FTS operators become literal terms rather than
    # syntax; single-char words are dropped; an all-punctuation query is empty.
    assert ms._fts_query('NEAR "beta" *') == '"NEAR" OR "beta"'
    assert ms._fts_query("!!!") == ""
