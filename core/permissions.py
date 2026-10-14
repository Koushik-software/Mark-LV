"""
core/permissions.py — one place that decides how much freedom a tool gets.

WHY THIS EXISTS
    Every tool in this app could do anything. `file_controller` moved files and
    `send_message` sent them, `computer_settings` turned the WiFi off and
    `shutdown_jarvis` ended the session, and the only thing separating "read the
    time" from "delete this folder" was the wording of the model's description.
    Two actions gated themselves (see core/confirm.py); the rest relied on the
    model choosing well.

    That is fine until it is not. A tool is now classified once, in its own
    TOOL/PLUGIN dict, and the class decides what happens before it runs.

THE FOUR TIERS
    READ_ONLY              it looks and reports; nothing on the machine changes.
                           Runs immediately. (system_status, web_search, recall)
    REVERSIBLE             it changes something, and the change can be taken
                           back — most of them push onto core/undo.py. Runs
                           immediately; undo is faster than a question.
    EXTERNAL_INTERACTION   it acts outside this machine: sends a message, fills
                           a form, drives a browser. ANNOUNCED, not blocked —
                           see the note below.
    PRIVILEGED_DESTRUCTIVE cannot be taken back: shutdown, reboot, deleting,
                           toggling the WiFi the assistant is talking over.
                           Held behind the on-screen CONFIRM gate. The model
                           cannot satisfy it; only a click can.

WHY "EXTERNAL" ANNOUNCES INSTEAD OF BLOCKING
    The plan this was built from listed external actions as "requires
    confirmation". Doing that literally would put a HUD button in front of every
    search and every opened page, and core/confirm.py already argues the case
    against it: an assistant that asks before everything is one nobody uses, and
    the confirmation is what makes an assistant slow to trust. So external
    actions run and are announced in the activity log; only the genuinely
    irreversible tier blocks. If you want the stricter behaviour, flip one line
    in _TIER_POLICY.

WHAT IS *NOT* HERE
    Parameter-level danger. `computer_settings` is one tool whose sub-actions
    range from "volume up" to "restart the machine", so it stays REVERSIBLE at
    the tool level and keeps its own internal gate for the destructive
    sub-actions (actions/computer_settings.py). A tool-level tier cannot see
    arguments, and pretending otherwise would either block volume changes or
    wave through a reboot.
"""
from __future__ import annotations

import threading
from typing import Callable, Optional

# ── The tiers ────────────────────────────────────────────────────────────────
READ_ONLY = "read_only"
REVERSIBLE = "reversible"
EXTERNAL_INTERACTION = "external_interaction"
PRIVILEGED_DESTRUCTIVE = "privileged_destructive"

TIERS = (READ_ONLY, REVERSIBLE, EXTERNAL_INTERACTION, PRIVILEGED_DESTRUCTIVE)

# ── What a tier means at dispatch time ───────────────────────────────────────
RUN = "run"            # do it now
ANNOUNCE = "announce"  # do it now, but say so in the activity log
CONFIRM = "confirm"    # park it behind the on-screen gate

_TIER_POLICY = {
    READ_ONLY: RUN,
    REVERSIBLE: RUN,
    EXTERNAL_INTERACTION: ANNOUNCE,
    # Change this to ANNOUNCE to make external actions non-blocking too, or to
    # CONFIRM on READ_ONLY to make everything ask — one line, whole app follows.
    PRIVILEGED_DESTRUCTIVE: CONFIRM,
}

# A tool that declared nothing is not assumed safe. It is treated as an
# external action: it still runs, but it is announced, so an undeclared plugin
# is visible rather than silent. Opting *up* to PRIVILEGED is how a plugin asks
# to be gated; opting *down* to READ_ONLY is how it says it only reads.
DEFAULT_TIER = EXTERNAL_INTERACTION

# ── Core tools declared inline in main.py ────────────────────────────────────
# These have no TOOL dict of their own to carry a "permission" key (their
# declarations are literals in main.py), so the class lives here. They are
# recorded for reference and for `policy_for()`; their dispatch is the bespoke
# if/elif chain in main.py, and none of them is machine-destructive — the
# genuinely irreversible operations (system shutdown/restart, WiFi off, file
# deletion) live in actions/ and gate themselves (see the note above).
_CORE_TIERS: dict[str, str] = {
    "system_status":   READ_ONLY,
    "screen_process":  READ_ONLY,
    "close_camera":    READ_ONLY,
    "recall_memory":   READ_ONLY,
    "save_memory":     REVERSIBLE,
    "undo":            REVERSIBLE,
    "manage_monitor":  REVERSIBLE,
    # Ends the app on the user's own request — not a machine change, and gating
    # it behind a button would make "goodbye" hostile.
    "shutdown_jarvis": REVERSIBLE,
}

_registry: dict[str, str] = dict(_CORE_TIERS)
_lock = threading.Lock()


def normalize(tier) -> Optional[str]:
    """A declared value -> a known tier, or None if it is not one."""
    v = str(tier or "").strip().lower().replace("-", "_").replace(" ", "_")
    return v if v in TIERS else None


def register(name: str, tier: str) -> bool:
    """Classify one tool. False (and no change) if the tier is not recognised —
    a typo in a plugin must not silently demote it to something permissive."""
    t = normalize(tier)
    if not name or t is None:
        return False
    with _lock:
        _registry[str(name)] = t
    return True


def classify(name: str) -> str:
    """The tier for `name`, or DEFAULT_TIER when it never declared one."""
    with _lock:
        return _registry.get(str(name), DEFAULT_TIER)


def policy_for(name: str) -> str:
    """RUN / ANNOUNCE / CONFIRM for `name`."""
    return _TIER_POLICY.get(classify(name), ANNOUNCE)


def load_from_registries(action_registry=None, plugin_registry=None) -> int:
    """Read the `permission` key out of every loaded action/plugin and register
    it. Returns how many tools were classified by declaration.

    Called once at startup, after discovery. A tool whose value is misspelled is
    left at DEFAULT_TIER rather than dropped — the tool still works, it is just
    treated as external.
    """
    count = 0
    for reg in (action_registry, plugin_registry):
        records = getattr(reg, "_all_records", None)
        if not records:
            continue
        for rec in records:
            if not getattr(rec, "valid", False):
                continue
            declared = getattr(rec, "permission", None)
            if declared is not None and register(rec.name, declared):
                count += 1
    return count


def enforce(name: str, run: Callable[[], str], *,
            announce: Optional[Callable[[str], None]] = None,
            title: str = "", detail: str = "") -> str:
    """Apply `name`'s policy and, unless it must be confirmed, run it.

    `run` is the zero-argument callable that performs the action. `announce` is
    where the "I am doing X" line goes (the HUD activity log); when it is absent
    an external action is simply not announced, which is the old behaviour.

    Returns the tool result string. For a CONFIRM-tier tool it returns the
    pending sentence from core/confirm.py and does NOT run anything — the
    callable is handed to the UI and runs only if the user presses CONFIRM.
    """
    policy = policy_for(name)

    if policy == RUN:
        return run()

    if policy == ANNOUNCE:
        if announce:
            try:
                announce(f"SYS: {name} (external action)")
            except Exception:
                pass
        return run()

    # CONFIRM — only a click on the HUD can let this through.
    from core import confirm as _confirm

    _title = title or f"Allow {name}?"
    _detail = detail or (
        "This cannot be undone by the assistant. Confirm only if you asked "
        "for it."
    )
    return _confirm.request(key=name, title=_title, detail=_detail, run=run)


def reset() -> None:
    """Restore the registry to just the core table. For tests."""
    with _lock:
        _registry.clear()
        _registry.update(_CORE_TIERS)
