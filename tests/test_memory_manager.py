"""Baseline tests for memory/memory_manager.py — the persistent fact store.

These pin the behaviour a Stage B/G refactor must not regress: entry shapes,
the prompt-budget split, lexical recall, and session summaries.
"""
from __future__ import annotations

import json

from memory import memory_manager as mm


def test_empty_memory_has_all_categories():
    m = mm._empty_memory()
    assert set(m) == {"identity", "preferences", "projects",
                      "relationships", "wishes", "notes"}
    assert all(v == {} for v in m.values())


def test_load_memory_missing_file_returns_empty(isolated_memory):
    assert mm.load_memory() == mm._empty_memory()


def test_load_memory_backfills_missing_categories(isolated_memory):
    isolated_memory.write_text(json.dumps({"identity": {"name": "Ada"}}), encoding="utf-8")
    m = mm.load_memory()
    assert m["identity"]["name"] == "Ada"
    assert m["notes"] == {}          # category added on load


def test_load_memory_corrupt_file_falls_back(isolated_memory, capsys):
    isolated_memory.write_text("{not json", encoding="utf-8")
    assert mm.load_memory() == mm._empty_memory()


def test_update_memory_persists_and_stamps_date(isolated_memory):
    mm.update_memory({"identity": {"name": "Ada"}})
    m = mm.load_memory()
    assert m["identity"]["name"]["value"] == "Ada"
    assert m["identity"]["name"]["updated"]      # a YYYY-MM-DD stamp exists


def test_update_memory_skips_blank_and_none(isolated_memory):
    mm.update_memory({"notes": {"empty": "   ", "none": None, "ok": "kept"}})
    m = mm.load_memory()
    assert "empty" not in m["notes"]
    assert "none" not in m["notes"]
    assert m["notes"]["ok"]["value"] == "kept"


def test_update_memory_ignores_non_dict(isolated_memory):
    assert mm.update_memory("nonsense") == mm._empty_memory()


def test_long_value_is_truncated(isolated_memory):
    mm.update_memory({"notes": {"big": "x" * 500}})
    val = mm.load_memory()["notes"]["big"]["value"]
    assert len(val) <= mm.MAX_VALUE_LENGTH + 1     # +1 for the ellipsis
    assert val.endswith("…")


def test_remember_and_forget_round_trip(isolated_memory):
    mm.remember("favorite_food", "pizza", "preferences")
    assert "pizza" in mm.load_memory()["preferences"]["favorite_food"]["value"]
    mm.forget("favorite_food", "preferences")
    assert "favorite_food" not in mm.load_memory()["preferences"]


def test_remember_rejects_unknown_category(isolated_memory):
    mm.remember("k", "v", "not_a_category")
    assert "k" in mm.load_memory()["notes"]       # collapsed to notes


def test_entry_value_accepts_bare_string_and_dict():
    assert mm._entry_value("hello") == "hello"
    assert mm._entry_value({"value": "hi"}) == "hi"
    assert mm._entry_value(None) == ""


def test_search_memory_scores_key_exact_match_highest(isolated_memory):
    mm.update_memory({"identity": {"name": "Ada"},
                      "notes": {"name_day": "Tuesday"}})
    out = mm.search_memory("name")
    # exact key match outranks substring, so both appear but name is listed first
    assert "identity/name" in out
    assert out.index("identity/name") < out.index("notes/name day")


def test_search_memory_empty_query_lists_everything(isolated_memory):
    mm.update_memory({"identity": {"name": "Ada"}, "notes": {"hobby": "chess"}})
    out = mm.search_memory("")
    assert "Ada" in out and "chess" in out


def test_search_memory_no_match(isolated_memory):
    mm.update_memory({"identity": {"name": "Ada"}})
    assert "Nothing stored" in mm.search_memory("zzzznomatch")


def test_all_entries_for_ui_sorted_newest_first(isolated_memory):
    # Written directly: update_memory re-stamps `updated` with today's date, so
    # distinct dates can only be set by hand.
    store = mm._empty_memory()
    store["notes"]["old"] = {"value": "a", "updated": "2020-01-01"}
    store["notes"]["new"] = {"value": "b", "updated": "2030-01-01"}
    isolated_memory.write_text(json.dumps(store), encoding="utf-8")
    rows = mm.all_entries_for_ui()
    assert rows[0]["key"] == "new"


def test_session_summary_is_consumed_once(isolated_memory):
    mm.save_session_summary("Talked about rockets.")
    first = mm.pop_last_session()
    assert first["summary"] == "Talked about rockets."
    assert mm.pop_last_session() is None          # consumed


def test_session_summaries_are_capped(isolated_memory):
    for i in range(6):
        mm.save_session_summary(f"session {i}")
    assert len(mm.load_memory()["sessions"]) <= mm._SESSION_MAX


def test_trim_to_limit_drops_oldest(isolated_memory, monkeypatch, capsys):
    monkeypatch.setattr(mm, "MEMORY_MAX_CHARS", 200)
    memory = mm._empty_memory()
    for i in range(20):
        memory["notes"][f"k{i}"] = {"value": "y" * 30, "updated": f"2020-01-{i+1:02d}"}
    trimmed = mm._trim_to_limit(memory)
    assert len(json.dumps(trimmed, ensure_ascii=False)) <= 200
    assert "k0" not in trimmed["notes"]            # oldest went first


# ── Stage G: SQLite/FTS5 index integration ───────────────────────────────────
# These prove the JSON stays the source of truth while the index mirrors it, and
# that search falls back cleanly when the index is unavailable.

def test_save_memory_populates_the_index(isolated_memory):
    from memory import memory_store as ms
    mm.remember("sister", "Ayse", category="relationships")
    assert ms.count() >= 1
    rows = ms.search("Ayse")
    assert any(r["value"] == "Ayse" for r in rows)


def test_search_memory_uses_index(isolated_memory):
    mm.remember("hobby", "fencing on Tuesdays", category="preferences")
    out = mm.search_memory("fencing")
    assert "fencing on Tuesdays" in out


def test_forget_removes_from_index(isolated_memory):
    from memory import memory_store as ms
    mm.remember("temp", "ephemeral fact", category="notes")
    assert ms.search("ephemeral")            # present
    mm.forget("temp", category="notes")
    assert not ms.search("ephemeral")        # gone from the index too


def test_search_falls_back_when_index_unavailable(isolated_memory, monkeypatch):
    from memory import memory_store as ms
    mm.remember("pet", "a cat named Turing", category="notes")
    monkeypatch.setattr(ms, "count", lambda: -1)      # pretend the DB is broken
    out = mm.search_memory("Turing")
    assert "Turing" in out                            # lexical fallback still works
