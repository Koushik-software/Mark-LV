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
- **D (done)** — macOS Big Sur / Intel telemetry (battery, thermal, CPU brand,
  load), brightness, volume, window management (incl. named layouts)
- **E (done)** — `actions/productivity.py` (file-backed notes, timers, stopwatch)
  + `actions/developer.py`
- **F (skipped)** — Stargate Atlantis HUD theme in `ui.py` — *user instructed to skip*
- **G (done)** — SQLite + FTS5 memory store (incl. duplicate detection)
- **H (done)** — third selectable HUD: `core/hitech_hud.py` (native QPainter
  instrument cluster) + a 3-way selector in the ⚙ controls drawer

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

## macOS integration (Stage D)

`core/macos.py` holds every macOS-specific call, and **nothing in it raises**: a
missing tool, denied permission or absent key returns `None`/a sentinel, so a
Mac that cannot answer still gets a working assistant.

- `parse_*` functions are **pure** (they take command output as text) and are the
  only part testable on the Linux dev box. Keep new parsing logic on that side.
- Brightness has no built-in macOS read/set. `core/macos.py` uses the `brightness`
  CLI (brew) when present, else relative F2/F1 key codes (144/145). Without the
  CLI `brightness_get()` returns `None`, so the undo path correctly declines.
- Telemetry also carries `cpu_brand` (`sysctl -n machdep.cpu.brand_string`) and
  1/5/15-minute `load` (`sysctl -n vm.loadavg`), plus `cpu_core_count()`.
- Volume lives in `core/macos.py` too: `volume_get()` / `volume_set()` / `mute()`.
  `set volume` **unmutes**, so the setter reads the mute flag inside the same
  AppleScript and restores it — a muted Mac must never become audible. This
  deliberately relaxes the "route macOS calls only through macos.py" rule for
  `computer_settings.py`, which now delegates its Darwin volume branches here.
- Named window layouts live in `WINDOW_LAYOUTS` (left/right/top/bottom, four
  corners, full, center) with `resolve_layout()` handling spoken aliases
  ("maximize" → full, "left half" → left). Geometry is computed **inside** the
  AppleScript from the desktop bounds, and `layout_geometry()` is a pure mirror
  of that math for tests. `actions/mac_control.py` exposes it as `window_layout`.
- Window ops (System Events) need **Accessibility** permission; without it
  osascript returns -1743 and the functions report unavailable.
- **macOS exposes no CPU temperature.** Do not fake it. `system_monitor` and
  `mac_control` report the thermal *throttle* level (`pmset -g therm`
  `CPU_Speed_Limit`) and battery instead.
- `actions/mac_control.py` is the voice-facing wrapper; it returns a plain
  "macOS-only" sentence on Windows/Linux.

## Productivity and developer actions (Stage E)

- `core/timers.py` — in-process countdown timers, distinct from `reminder.py`
  (which uses the OS scheduler and survives quitting). Clock and notifier are
  **injectable** and firing is an explicit `tick()`, so tests are deterministic.
  `main.py` binds the notifier to `speak` and starts the poll thread.
- `core/timers.py` also holds `Stopwatch` (start/stop/resume/lap/reset, same
  injectable clock) and `format_duration()` (H:MM:SS). `get_stopwatch()` is the
  process-wide instance.
- `actions/productivity.py` — notes are **plain text files** in
  `~/Documents/JARVIS Notes/` (override `NOTES_DIR` for tests). Filenames go
  through `safe_note_name()`, which strips separators and traversal, and
  `_note_path()` re-checks containment. Each note is *also* mirrored into the
  memory store (category `notes`) so voice recall still works — the file is the
  record, memory is the index. Timers and the stopwatch use `core/timers.py`.
- `actions/developer.py` — the one tool that runs commands. Allowlist by
  **executable basename** (`git`, `python`, `pytest`, `pip`, `node`, `npm`, `ls`,
  `cat`), git subcommand denylist (`push`/`reset`/`clean`/`remote`/…), cwd
  confined to the project root, bounded timeouts, and **never a shell** (argv
  lists only). Declared `privileged_destructive`, so the confirm gate applies.
  The confirm gate is the *second* line of defence, not the first — keep the
  allowlist tight.

## Memory index (Stage G)

`memory/memory_store.py` is a **SQLite + FTS5 index of `long_term.json`** — an
index, not a replacement. The JSON stays the source of truth.

- `save_memory()` calls `sync_index()`; `main.py` rebuilds the index once at
  startup so a pre-existing memory is searchable immediately.
- `search_memory()` uses the index when available and falls back to the old
  lexical scorer otherwise. `export_memory()` merges over a base dict so keys the
  DB does not model (`sessions`, `monitors`) survive.
- `find_duplicates()` reports entries sharing a `normalize_value()` (case/space/
  punctuation-folded) value **within a category** — it reports, it never deletes,
  because the JSON is the source of truth. `category_counts()` is the per-category
  tally for diagnostics.
- Degrades safely: no FTS5 → LIKE; no DB → JSON-only. A broken index must never
  cost the user their memory.
- The DB is `memory/jarvis_memory.db` (git-ignored). Tests redirect `DB_PATH` via
  the `isolated_memory` fixture in `tests/conftest.py` — **keep that fixture in
  step** or tests will write the developer's real DB. `tests/test_productivity.py`
  isolates both the notes dir **and** the memory mirror for the same reason.

## Line endings

Most files in this repository are **CRLF** (it was authored on Windows). When
editing an existing file, preserve its endings — a tool that rewrites the whole
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
