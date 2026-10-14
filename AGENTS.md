# AGENTS.md — working notes for Mark-LV / JARVIS ULTRA

Persistent memory for future agent sessions. Read this before changing code.

## What this is

**Mark LV (55)** by FatihMakes — a cross-platform (Windows/macOS/Linux) real-time
voice assistant ("JARVIS") built on the Gemini Live API. ~26k lines of Python.

- Entry points: `main.py` (core Live loop + tool dispatch), `ui.py` (PyQt6 HUD, ~5.8k lines)
- `core/` — Gemini model ladder, avatar/viseme rendering, echo guard, undo, confirm, loaders
- `actions/` — bundled skills, each self-describing via a module-level `TOOL` dict
- `memory/` — `long_term.json` fact store + `api_keys.json` config
- `dashboard/` — FastAPI + TLS remote-control server
- `plugins/` — drop-in skills, each self-describing via a `PLUGIN` dict + `run()`

## Current work: upgrade to "JARVIS ULTRA"

Branch: `upgrade/jarvis-ultra`. Backup snapshot of the baseline lives in
`/workspace/project/_backup_jarvis_ultra/` (outside the repo).

Planned stages (see the user's audit plan):
- **A2 (done)** — test harness + baseline tests
- **C (done)** — `core/permissions.py`, 4 privilege tiers, wired into dispatch
- **B (done)** — `core/sanitizer.py` + `core/planner.py` + `plan_task` action
- **D** — macOS Big Sur / Intel telemetry, brightness, window management
- **E** — `actions/productivity.py` + `actions/developer.py`
- **F** — Stargate Atlantis HUD theme in `ui.py`
- **G** — SQLite + FTS5 memory store

## Permission system (Stage C)

`core/permissions.py` classifies every tool into one of four tiers and decides
what happens before it runs:

| Tier | Policy | Meaning |
|---|---|---|
| `read_only` | RUN | looks and reports |
| `reversible` | RUN | changes something, undoable |
| `external_interaction` | ANNOUNCE | acts outside the machine — runs, but logged |
| `privileged_destructive` | CONFIRM | irreversible — parked behind the HUD gate |

- **Where a tier is declared:** actions put `"permission": "<tier>"` in their
  `TOOL` dict; plugins put it in `PLUGIN`. Core inline tools are classified in
  `core/permissions.py`'s `_CORE_TIERS`. The key is **not** sent to the model
  (`get_tool_declarations` never emits it), so it cannot be seen or forged.
- **Undeclared tools** default to `external_interaction` (announced), never to
  read-only. A misspelled tier is ignored and falls back to that default.
- **Enforcement** happens in `main.py`'s `_execute_tool`, via
  `perms.enforce(name, run, announce=...)`, for both the action and plugin
  branches. The core inline if/elif chain is *not* wrapped — those tools are
  non-destructive by construction.
- **Destructive sub-actions** (system shutdown/restart, WiFi off) live inside
  `actions/computer_settings.py` and keep their own internal `confirm.request`
  gate — a tool-level tier cannot see arguments, so `computer_settings` stays
  `reversible` at the top level.

When adding a new action or plugin, always declare a tier; `tests/test_permissions.py`
fails if a shipped action forgets one.

## Prompt-injection defence (Stage B)

`core/sanitizer.py` fences text the assistant did not write itself — web results,
page text, file contents. It **never strips content** (a filter that removes
injection phrases also removes the legitimate text they resemble); it wraps the
text in an unmistakable boundary with a header saying it is data, not
instruction, and escapes the fence tokens so the payload cannot close the fence
early.

- `sanitize(text, source=...)` / `wrap(...)` — fence untrusted text.
- `unwrap(text)` — recover the payload for display (the HUD content panel uses
  this so the user sees the page, not the fence).
- `looks_suspicious(text)` — a **flag**, never a removal.
- Applied in `actions/web_search.py` (all modes) and `actions/file_processor.py`
  (the unknown-type branch that feeds file bytes to the model).
- New tools that return external text must fence it too.

## Planner (Stage B)

`core/planner.py` decomposes a multi-step request into ordered tool calls and
runs them one at a time, reporting progress.

- **Opt-in.** Ordinary requests never go through it — the model calls the
  `plan_task` action itself, only when a request genuinely has several dependent
  steps. This keeps normal conversation latency untouched.
- `TaskPlanner.make_plan(goal)` asks the model (via `core/gemini`) for JSON and
  **drops any step naming a tool that does not exist** — a hallucinated tool must
  not reach the dispatcher.
- Execution goes through `main._plan_and_run` → `_plan_step_dispatch` →
  `_execute_tool`, so every step passes the same permission tiers and confirm
  gate as a normal call. A step that returns `[CONFIRMATION_PENDING]` **stops the
  plan** — the planner cannot press the HUD button.
- `plan_task` declares `behavior: NON_BLOCKING` + `scheduling: INTERRUPT`: the
  tool returns immediately, the plan runs on the event loop, and its result
  re-enters the conversation when ready.
- The model call is injected (`ask=`), so `tests/test_planner.py` runs it with a
  stub and no network.

## Line endings

Most files in this repository are **CRLF** (it was authored on Windows). When
editing an existing file, preserve its endings � a tool that rewrites the whole
file with LF turns a one-line change into a whole-file diff and makes the branch
unreviewable. New files use LF. Check with `git diff --stat`; if a file you
barely touched shows hundreds of changed lines, it is line endings, not content.
`git diff --ignore-cr-at-eol` shows the true change.

## Testing

No CI and no test suite existed before Stage A2. Tests live in `tests/`, run with:

```bash
python -m pytest          # from the repo root; pytest.ini sets testpaths=tests
```

Only **dependency-light** modules are covered (memory, config, loaders, undo,
gemini helpers). Modules importing PyQt6 / `google.genai` / `sounddevice` are
**not** imported by tests — keep new tests on that side of the line so the suite
runs on a bare interpreter (verified on Linux, Python 3.13).

## Environment constraints (important)

- The dev/CI container is **Linux**; the target machine is an **Intel MacBook Air,
  macOS Big Sur 11.7.x**. AppleScript / `sysctl` / `pmset` / brightness / window
  automation **cannot be executed here** — they must be verified by the user on
  the Mac. Write those changes defensively and flag them for manual testing.
- The app is a GUI + audio + hardware app and does **not** run end-to-end headless.

## Conventions

- Match the author's style: module docstrings explain *why* (often at length),
  minimal inline comments, `from __future__ import annotations`, `| None` types.
- Actions/plugins must **never raise** out of a handler — catch and return a
  spoken error string.
- Never commit `config/api_keys.json`, `config/certs/`, `memory/long_term.json`,
  or any token/credential file (all git-ignored — keep it that way).

## Known gotchas

- `memory_manager.update_memory()` **re-stamps** `updated` with today's date, so
  distinct dates can only be set by writing `long_term.json` directly.
- `system_monitor.py`'s header claims "Zero subprocess calls on all platforms";
  Stage D relaxes this deliberately for macOS `sysctl`/`pmset`.
- Window snapping lives in `actions/computer_settings.py` (~lines 282–300), **not**
  `actions/desktop.py` — the plan had this wrong.
