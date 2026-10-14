"""
memory/memory_store.py — a SQLite + FTS5 index of long-term memory.

WHY A SECOND STORE
    long_term.json is the memory: human-readable, diffable, and trivially
    correct. What it is not is *searchable*. The recall tool currently scores
    entries with a hand-rolled lexical function over every entry on every query
    — fine at fifty entries, linear and increasingly wrong at five thousand.

    So this is not a replacement. It is an index: the JSON stays the source of
    truth, and this mirrors it into SQLite with an FTS5 full-text table so recall
    becomes a real ranked search.

TWO-WAY SYNC
    * import_memory(memory)  — JSON → DB (upsert every entry, drop DB rows no
      longer in the JSON, so the JSON decides membership).
    * export_memory(base)    — DB → JSON (merge DB rows over a base dict,
      leaving keys the DB does not model — 'sessions', 'monitors' — untouched).
    * sync(memory)           — the common case: import, and report counts.

DEGRADATION
    Every function is best-effort. If FTS5 is missing (some Python builds ship
    SQLite without it) the table falls back to LIKE, and if the DB cannot be
    opened at all the callers fall back to the JSON-only path. A broken index
    must never cost you your memory.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from threading import Lock

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = _base_dir()
DB_PATH  = BASE_DIR / "memory" / "jarvis_memory.db"

_lock = Lock()
_conn: sqlite3.Connection | None = None
_fts5: bool | None = None       # None = untested


def _connect() -> sqlite3.Connection | None:
    """Open (once) and prepare the database. None when it cannot be opened.

    Guarded by `_lock`: the index is touched from the memory thread and the
    poll thread, and without this two callers racing on first use would each
    open their own connection."""
    global _conn
    with _lock:
        if _conn is not None:
            return _conn
        try:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            _init_schema(conn)
            _conn = conn
            return conn
        except Exception as e:
            print(f"[MemoryStore] unavailable: {e}")
            return None


def _init_schema(conn: sqlite3.Connection) -> None:
    global _fts5
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entries ("
        "  category TEXT NOT NULL,"
        "  key      TEXT NOT NULL,"
        "  value    TEXT NOT NULL,"
        "  updated  TEXT NOT NULL DEFAULT '',"
        "  PRIMARY KEY (category, key))"
    )
    if _fts5 is None:
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts USING fts5("
                "  category, key, value,"
                "  content='entries', content_rowid='rowid')"
            )
            _fts5 = True
        except sqlite3.OperationalError:
            _fts5 = False
    if _fts5:
        # Keep the FTS index in step with the table. `IF NOT EXISTS` is not
        # available for triggers, so they are dropped and recreated — cheap, and
        # it means a schema change cannot leave a stale trigger behind.
        conn.execute("DROP TRIGGER IF EXISTS entries_ai")
        conn.execute("DROP TRIGGER IF EXISTS entries_ad")
        conn.execute("DROP TRIGGER IF EXISTS entries_au")
        conn.execute(
            "CREATE TRIGGER entries_ai AFTER INSERT ON entries BEGIN "
            "  INSERT INTO entries_fts(rowid, category, key, value) "
            "  VALUES (new.rowid, new.category, new.key, new.value); END")
        conn.execute(
            "CREATE TRIGGER entries_ad AFTER DELETE ON entries BEGIN "
            "  INSERT INTO entries_fts(entries_fts, rowid, category, key, value) "
            "  VALUES ('delete', old.rowid, old.category, old.key, old.value); END")
        conn.execute(
            "CREATE TRIGGER entries_au AFTER UPDATE ON entries BEGIN "
            "  INSERT INTO entries_fts(entries_fts, rowid, category, key, value) "
            "  VALUES ('delete', old.rowid, old.category, old.key, old.value); "
            "  INSERT INTO entries_fts(rowid, category, key, value) "
            "  VALUES (new.rowid, new.category, new.key, new.value); END")
    conn.commit()


def _entries_of(memory: dict) -> list[tuple[str, str, str, str]]:
    """Flatten the nested memory dict into rows. Mirrors memory_manager's shape:
    {category: {key: {'value':…, 'updated':…}}} (a bare string is also accepted)."""
    rows: list[tuple[str, str, str, str]] = []
    for cat, items in (memory or {}).items():
        if not isinstance(items, dict):
            continue                    # 'sessions' is a list; skip it
        for key, entry in items.items():
            if isinstance(entry, dict):
                val = str(entry.get("value", "") or "").strip()
                upd = str(entry.get("updated", "") or "")
            else:
                val, upd = str(entry or "").strip(), ""
            if val:
                rows.append((str(cat), str(key), val, upd))
    return rows


# ── sync ─────────────────────────────────────────────────────────────────────

def import_memory(memory: dict) -> int:
    """Upsert every entry from the JSON, and drop DB rows the JSON no longer has.

    Returns the number of rows now in the index, or -1 when unavailable."""
    conn = _connect()
    if conn is None:
        return -1
    rows = _entries_of(memory)
    with _lock:
        try:
            keys = {(c, k) for c, k, _, _ in rows}
            existing = conn.execute("SELECT category, key FROM entries").fetchall()
            for cat, key in existing:
                if (cat, key) not in keys:
                    conn.execute(
                        "DELETE FROM entries WHERE category=? AND key=?",
                        (cat, key))
            conn.executemany(
                "INSERT INTO entries(category, key, value, updated) "
                "VALUES(?,?,?,?) "
                "ON CONFLICT(category, key) DO UPDATE SET "
                "  value=excluded.value, updated=excluded.updated",
                rows)
            conn.commit()
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        except Exception as e:
            print(f"[MemoryStore] import failed: {e}")
            return -1


def export_memory(base: dict | None = None) -> dict:
    """DB → JSON. Merge the indexed rows over `base` (which is preserved, so
    'sessions' and 'monitors' survive). Returns `base` unchanged if unavailable."""
    conn = _connect()
    out = dict(base or {})
    if conn is None:
        return out
    with _lock:
        try:
            rows = conn.execute(
                "SELECT category, key, value, updated FROM entries").fetchall()
        except Exception:
            return out
    for cat, key, value, updated in rows:
        out.setdefault(cat, {})
        if not isinstance(out[cat], dict):
            continue
        out[cat][key] = {"value": value, "updated": updated}
    return out


def sync(memory: dict) -> dict:
    """Reconcile the index with the JSON memory. Returns {'rows': n, 'fts': bool}."""
    rows = import_memory(memory)
    return {"rows": rows, "fts": bool(_fts5)}


# ── search ───────────────────────────────────────────────────────────────────

_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: each word quoted and
    OR-ed, so punctuation in the query can never be read as FTS syntax."""
    words = [w for w in _WORD_RE.split(query or "") if len(w) > 1]
    return " OR ".join(f'"{w}"' for w in words)


def search(query: str, limit: int = 8) -> list[dict]:
    """Ranked full-text search. Returns [] when unavailable or nothing matches."""
    conn = _connect()
    if conn is None:
        return []
    limit = max(1, int(limit))
    with _lock:
        try:
            if _fts5 and (query or "").strip():
                expr = _fts_query(query)
                if not expr:
                    return []
                rows = conn.execute(
                    "SELECT e.category, e.key, e.value, e.updated "
                    "FROM entries_fts f JOIN entries e ON e.rowid = f.rowid "
                    "WHERE entries_fts MATCH ? "
                    "ORDER BY rank LIMIT ?", (expr, limit)).fetchall()
                if rows:
                    return [{"category": c, "key": k, "value": v, "updated": u}
                            for c, k, v, u in rows]
                return []
            if (query or "").strip():
                like = f"%{query.strip()}%"
                rows = conn.execute(
                    "SELECT category, key, value, updated FROM entries "
                    "WHERE value LIKE ? OR key LIKE ? LIMIT ?",
                    (like, like, limit)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT category, key, value, updated FROM entries "
                    "ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
            return [{"category": c, "key": k, "value": v, "updated": u}
                    for c, k, v, u in rows]
        except Exception as e:
            print(f"[MemoryStore] search failed: {e}")
            return []


def count() -> int:
    conn = _connect()
    if conn is None:
        return -1
    with _lock:
        try:
            return conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        except Exception:
            return -1


def reset_for_tests() -> None:
    """Close and forget the cached connection. Tests point DB_PATH elsewhere."""
    global _conn, _fts5
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None
        _fts5 = None
