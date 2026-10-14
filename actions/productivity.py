"""
productivity — quick notes and countdown timers / stopwatch.

Notes are plain text files under ~/Documents/JARVIS Notes/, so they are yours in
a form you can open, sync and grep without the assistant. Each note is *also*
mirrored into the memory store under "notes", so "what did I note about the
boiler" still works by voice — the file is the record, memory is the index.

WHY NOT reminder.py
    reminder.py schedules a wall-clock reminder with the OS scheduler and
    survives quitting the app. That is right for "remind me Tuesday at 9". This
    is for "set a timer for ten minutes" — relative, short, and gone when you
    quit. Two different jobs; this one is deliberately the simpler.
"""
from __future__ import annotations

import re
from pathlib import Path

from core import timers

# Overridable so tests write to a temp dir instead of the user's Documents.
NOTES_DIR = Path.home() / "Documents" / "JARVIS Notes"

# Characters allowed in a note filename; everything else collapses to '_'.
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._-]")


def notes_dir() -> Path:
    """The notes directory, created on first use."""
    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    return NOTES_DIR


def safe_note_name(name: str) -> str:
    """Turn an arbitrary string into a filename confined to notes_dir(). Pure.

    Strips path separators and traversal so a model-supplied name can never
    escape the notes directory or overwrite something outside it."""
    cleaned = _UNSAFE.sub("_", (name or "").strip()).strip(" .")
    cleaned = cleaned[:80]
    return cleaned or "note"


def _note_path(name: str) -> Path:
    """Absolute path of a note, guaranteed to sit inside notes_dir()."""
    p = (notes_dir() / f"{safe_note_name(name)}.txt").resolve()
    root = notes_dir().resolve()
    if root not in p.parents:
        raise ValueError("note path escaped the notes directory")
    return p


def _mirror_to_memory(key: str, value: str) -> None:
    """Best-effort: index the note in memory so it is voice-recallable."""
    try:
        from memory import memory_manager as mm
        mm.remember(key, value, category="notes")
    except Exception:
        pass


def _note(action: str, params: dict, player) -> str:
    key = (params.get("key") or "").strip()
    value = (params.get("value") or params.get("text") or "").strip()

    if action in ("note_add", "note", "remember_note", "save_note"):
        if not value:
            return "What should I note down?"
        if not key:
            words = re.findall(r"[A-Za-z0-9]+", value)
            key = "_".join(words[:4]).lower() or "note"
        path = _note_path(key)
        existed = path.exists()
        try:
            with path.open("a", encoding="utf-8") as f:
                if existed and f.tell() > 0:
                    f.write("\n")
                f.write(value)
        except OSError as e:
            return f"I could not write that note: {e}"
        _mirror_to_memory(safe_note_name(key), value)
        if player and hasattr(player, "write_log"):
            player.write_log(f"[Note] {path.name} — {value[:40]}")
        verb = "Appended to" if existed else "Noted in"
        return f"{verb} {path.name}: {value}"

    if action in ("note_append", "append_note", "add_to_note"):
        if not key or not value:
            return "Which note should I add to, and what should I add?"
        path = _note_path(key)
        if not path.exists():
            return f"I have no note called '{safe_note_name(key)}' to append to."
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + value)
        except OSError as e:
            return f"I could not append to that note: {e}"
        _mirror_to_memory(safe_note_name(key), value)
        return f"Added to {path.name}."

    if action in ("note_read", "read_note", "open_note", "get_note"):
        if not key:
            return "Which note should I read?"
        path = _note_path(key)
        if not path.exists():
            return f"I have no note called '{safe_note_name(key)}'."
        try:
            body = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            return f"I could not read that note: {e}"
        return f"{path.name}:\n{body}" if body else f"{path.name} is empty."

    if action in ("note_list", "list_notes", "notes"):
        try:
            files = sorted(notes_dir().glob("*.txt"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        if not files:
            return "You have no notes saved."
        return "Your notes:\n" + "\n".join(p.stem for p in files[:20])

    if action in ("note_search", "search_notes", "find_note"):
        if not value and not key:
            return "What should I search your notes for?"
        from memory import memory_manager as mm
        return mm.search_memory(value or key, limit=8)

    if action in ("note_delete", "note_forget", "delete_note"):
        if not key:
            return "Which note should I delete? Give me its name."
        path = _note_path(key)
        if not path.exists():
            return f"I have no note called '{safe_note_name(key)}'."
        try:
            path.unlink()
        except OSError as e:
            return f"I could not delete that note: {e}"
        try:
            from memory import memory_manager as mm
            mm.forget(safe_note_name(key), category="notes")
        except Exception:
            pass
        return f"Deleted {path.name}."

    return f"Unknown note action: {action}"


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


def _stopwatch(action: str, params: dict, player) -> str:
    sw = timers.get_stopwatch()

    if action in ("stopwatch_start", "start_stopwatch"):
        if not sw.start():
            return f"The stopwatch is already running, at {timers.format_duration(sw.elapsed())}."
        return "Stopwatch started."

    if action in ("stopwatch_stop", "stop_stopwatch", "stopwatch_pause"):
        if not sw.running:
            return f"The stopwatch is not running. It is at {timers.format_duration(sw.elapsed())}."
        total = sw.stop()
        return f"Stopwatch stopped at {timers.format_duration(total)}."

    if action in ("stopwatch_reset", "reset_stopwatch"):
        sw.reset()
        return "Stopwatch reset."

    if action in ("stopwatch_lap", "lap", "stopwatch_split"):
        if not sw.running:
            return "The stopwatch is not running, so there is nothing to lap."
        lap = sw.lap()
        return f"Lap {len(sw.laps())} at {timers.format_duration(lap)}."

    if action in ("stopwatch_read", "stopwatch_status", "read_stopwatch",
                  "stopwatch_elapsed"):
        state = "running" if sw.running else "stopped"
        out = f"Stopwatch is {state} at {timers.format_duration(sw.elapsed())}."
        if sw.laps():
            out += " Laps: " + ", ".join(
                timers.format_duration(x) for x in sw.laps())
        return out

    return f"Unknown stopwatch action: {action}"


def productivity(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()

    if not action:
        return "What would you like — a note, a timer or the stopwatch?"

    if action.startswith("note"):
        return _note(action, params, player)
    if action.startswith("timer"):
        return _timer(action, params, player)
    if action.startswith("stopwatch") or action == "lap":
        return _stopwatch(action, params, player)
    return f"Unknown action: {action}"


TOOL = {
    "name": "productivity",
    "permission": "reversible",
    "description": (
        "Quick notes, countdown timers and a stopwatch. Notes are saved as text "
        "files in the user's Documents/JARVIS Notes folder and are also "
        "searchable by voice. Timers are short and relative ('set a timer for 10 "
        "minutes') and announce out loud when they finish. The stopwatch counts "
        "up and supports laps. For a wall-clock reminder on a specific date use "
        "the reminder tool instead. Actions: note_add | note_append | note_read "
        "| note_list | note_search | note_delete | timer_set | timer_list | "
        "timer_cancel | stopwatch_start | stopwatch_stop | stopwatch_lap | "
        "stopwatch_read | stopwatch_reset."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "note_add | note_append | note_read | note_list | "
                    "note_search | note_delete | timer_set | timer_list | "
                    "timer_cancel | stopwatch_start | stopwatch_stop | "
                    "stopwatch_lap | stopwatch_read | stopwatch_reset"
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
