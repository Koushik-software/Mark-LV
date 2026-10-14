"""
productivity — quick notes and countdown timers.

Notes go through the existing memory store (category "notes"), so they are the
same notes the assistant already remembers and can recall — not a second, hidden
store. Timers use core/timers.py and announce through the running app.

WHY NOT reminder.py
    reminder.py schedules a wall-clock reminder with the OS scheduler and
    survives quitting the app. That is right for "remind me Tuesday at 9". This
    is for "set a timer for ten minutes" — relative, short, and gone when you
    quit. Two different jobs; this one is deliberately the simpler.
"""
from __future__ import annotations

import re

from core import timers


def _parse_duration(text: str) -> int | None:
    """Turn '10 minutes', '90 seconds', '1h30m', '2 hours' into seconds.

    Returns None when nothing parseable is present, so the caller can ask for a
    clearer duration rather than silently defaulting."""
    t = (text or "").strip().lower()
    if not t:
        return None

    # Compact form: 1h30m, 90s, 5m
    compact = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?\s*", t)
    if compact and any(compact.groups()):
        h, m, s = (int(g) if g else 0 for g in compact.groups())
        total = h * 3600 + m * 60 + s
        return total or None

    # Verbose form: "10 minutes", "2 hours", "45 secs". Longer unit names come
    # first so the alternation does not stop at a shorter prefix, and the plural
    # `s?` sits before the word boundary — without it "minutes" matches neither
    # "minute" (no boundary before the s) nor "min" (same), and the parse fails.
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*"
        r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", t)
    if m:
        n = float(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            return int(n * 3600)
        if unit.startswith("m"):
            return int(n * 60)
        return int(n)

    # Bare number → treat as minutes, the common case for "set a timer for 5"
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*", t)
    if m:
        return int(float(m.group(1)) * 60)
    return None


def _fmt(seconds: int) -> str:
    if seconds >= 3600:
        h, rem = divmod(seconds, 3600)
        m = rem // 60
        return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")
    if seconds >= 60:
        m, s = divmod(seconds, 60)
        return f"{m} minute{'s' if m != 1 else ''}" + (f" {s} seconds" if s else "")
    return f"{seconds} seconds"


def _note(action: str, params: dict, player) -> str:
    from memory import memory_manager as mm

    key = (params.get("key") or "").strip()
    value = (params.get("value") or params.get("text") or "").strip()

    if action in ("note_add", "note", "remember_note", "save_note"):
        if not value:
            return "What should I note down?"
        # Derive a key from the text when none was given, so the note is
        # recallable by its own words.
        if not key:
            words = re.findall(r"[A-Za-z0-9]+", value)
            key = "_".join(words[:4]).lower() or "note"
        mm.remember(key, value, category="notes")
        if player and hasattr(player, "write_log"):
            player.write_log(f"[Note] {key} — {value[:40]}")
        return f"Noted: {value}"

    if action in ("note_list", "list_notes", "notes"):
        rows = [r for r in mm.all_entries_for_ui() if r["category"] == "notes"]
        if not rows:
            return "You have no notes saved."
        lines = [f"{r['key']}: {r['value']}" for r in rows[:12]]
        return "Your notes:\n" + "\n".join(lines)

    if action in ("note_search", "search_notes", "find_note"):
        if not value and not key:
            return "What should I search your notes for?"
        return mm.search_memory(value or key, limit=8)

    if action in ("note_delete", "note_forget", "delete_note"):
        if not key:
            return "Which note should I delete? Give me its key."
        return mm.forget(key, category="notes")

    return f"Unknown note action: {action}"


def _timer(action: str, params: dict, player) -> str:
    mgr = timers.get_manager()

    if action in ("timer_set", "timer", "set_timer", "start_timer"):
        raw = (params.get("duration") or params.get("value")
               or params.get("text") or "").strip()
        secs = _parse_duration(raw)
        if secs is None:
            return "How long should the timer run? Try '10 minutes'."
        label = (params.get("label") or params.get("name") or "timer").strip()
        mgr.start(secs, label)
        return f"Timer set for {_fmt(secs)}."

    if action in ("timer_list", "list_timers", "timers"):
        active = mgr.list_active()
        if not active:
            return "No timers running."
        return "Running timers:\n" + "\n".join(
            f"{t['label']}: {_fmt(t['remaining'])} left" for t in active)

    if action in ("timer_cancel", "cancel_timer", "timer_stop"):
        tid = (params.get("id") or params.get("key") or "").strip()
        if tid and mgr.cancel(tid):
            return f"Cancelled timer {tid}."
        if not tid and mgr.cancel_all():
            return "Cancelled all timers."
        return "I found no such timer."

    return f"Unknown timer action: {action}"


def productivity(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()

    if not action:
        return "What would you like — a note or a timer?"

    if action.startswith("note"):
        return _note(action, params, player)
    if action.startswith("timer"):
        return _timer(action, params, player)
    return f"Unknown action: {action}"


TOOL = {
    "name": "productivity",
    "permission": "reversible",
    "description": (
        "Quick notes and countdown timers. Notes are saved to memory under "
        "'notes' and can be searched later. Timers are short and relative "
        "('set a timer for 10 minutes') and announce out loud when they finish. "
        "For a wall-clock reminder on a specific date use the reminder tool "
        "instead. Actions: note_add | note_list | note_search | note_delete | "
        "timer_set | timer_list | timer_cancel."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "note_add | note_list | note_search | note_delete | "
                    "timer_set | timer_list | timer_cancel"
                ),
            },
            "value": {
                "type": "STRING",
                "description": "Note text, search term, or duration for a timer",
            },
            "key": {
                "type": "STRING",
                "description": "Note key (for delete/search); optional for add",
            },
            "duration": {
                "type": "STRING",
                "description": "Timer length, e.g. '10 minutes' or '90 seconds'",
            },
            "label": {
                "type": "STRING",
                "description": "What the timer is for, spoken when it fires",
            },
            "id": {
                "type": "STRING",
                "description": "Timer id, for timer_cancel",
            },
        },
        "required": ["action"],
    },
    "handler": productivity,
}
