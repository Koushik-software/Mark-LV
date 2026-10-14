"""
Drop-in JARVIS plugin template.

Copy this file, rename it (no leading underscore), fill in PLUGIN and run().
No other file needs to change — JARVIS discovers this automatically at startup.
"""

PLUGIN = {
    "name": "my_plugin",                     # snake_case, unique, ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$
    "permission": "external_interaction",    # see note below; optional but strongly recommended
    "description": (
        "One or two sentences Gemini uses to decide when to call this tool. "
        "Be explicit about trigger phrases and, if it could be confused with "
        "another tool, say which tool NOT to use instead (see game_updater's "
        "description in main.py for the pattern)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "example_arg": {"type": "STRING", "description": "What this argument means"},
        },
        "required": [],   # omit or leave empty for a zero-argument tool
    },
}
# permission — one of the four tiers in core/permissions.py:
#   "read_only"              looks and reports; nothing on the machine changes.
#   "reversible"             changes something that can be taken back (push an
#                            undo closure via core.undo.push_undo).
#   "external_interaction"   acts outside this machine — sends a message, fills a
#                            form. Runs, but is announced in the activity log.
#   "privileged_destructive" cannot be undone (shutdown, delete, WiFi off). Held
#                            behind the on-screen CONFIRM gate — a click is the
#                            only way through; the model cannot satisfy it.
# Omit it and the plugin is treated as external_interaction (announced). Do NOT
# claim read_only unless it genuinely only reads.
#
# If your plugin returns text fetched from outside this machine (a web page, a
# file, an API), fence it before returning, so the model reads it as data rather
# than instruction:
#     from core.sanitizer import sanitize
#     return sanitize(raw_text, source="my_plugin: <where it came from>")

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: dict of the args Gemini extracted, matching PLUGIN['parameters'].
    player: the JarvisUI instance — use player.write_log(f"JARVIS: ...") to log,
            same as actions/*.py. May be None.
    session_memory: reserved, usually None today (core tools mostly pass None too).
    Return a short natural-language string — this is spoken back to the user.
    Never raise: catch your own errors and return a spoken error string instead
    (the loader also catches exceptions as a second safety net, but don't rely on it).
    """
    example_arg = parameters.get("example_arg", "")
    try:
        result_text = f"Did the thing with {example_arg}."
    except Exception as e:
        return f"Sir, my_plugin failed: {e}"
    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text
