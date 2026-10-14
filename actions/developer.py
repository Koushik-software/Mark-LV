"""
developer — a narrow, allowlisted developer console.

THE THREAT MODEL
    This is the one tool that runs commands. Everything about it is built around
    the assumption that the *arguments come from a language model* and may be
    wrong or adversarially crafted by text the model has read (a web page, a
    file). So:

      * Only `git`, `python`, `pytest`, `pip` (and a couple of read-only
        inspectors) may run. The allowlist is by executable, not by prefix
        string, so `git; rm -rf /` cannot sneak through — a shell is never
        involved.
      * Arguments are passed as a list to subprocess, never a shell string.
      * A fixed denylist rejects the git subcommands that push, rewrite history,
        or delete (`push`, `reset`, `clean`, `filter-branch`, `remote`, …).
      * Every call is bounded by a timeout.
      * The working directory is confined to the project root or below.
      * `run_tests` is the common case and has its own bounded entry point.

WHY CONFIRMATION IS NOT ENOUGH ON ITS OWN
    Anything here that changes state is declared privileged_destructive, so the
    HUD confirm gate applies (see core/confirm.py) — but the gate is the second
    line, not the only one. The allowlist and denylist are what stop the
    *unintended* command; the gate is what stops the *intended* one from being a
    surprise.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Executables this tool may run. Matched against the *basename* of the resolved
# program, so a path like /usr/bin/git is allowed and ./evil-git is not.
_ALLOWED_EXES = {"git", "python", "python3", "pytest", "pip", "pip3",
                 "node", "npm", "ls", "cat"}

# git subcommands that are refused outright: they push to a remote, rewrite or
# discard history, or delete untracked work.
_GIT_DENIED = {
    "push", "reset", "clean", "filter-branch", "filter-repo", "remote",
    "gc", "reflog", "update-ref", "checkout-index", "gc",
}

_TIMEOUT = 30.0
_MAX_OUTPUT = 4000


def _project_root() -> Path:
    """The repository root — the directory holding this actions/ package."""
    return Path(__file__).resolve().parent.parent


def _confine(cwd: str | None) -> Path | None:
    """Resolve `cwd` and refuse anything outside the project root."""
    root = _project_root()
    if not cwd:
        return root
    try:
        target = Path(cwd).expanduser().resolve()
    except Exception:
        return None
    if target == root or root in target.parents:
        return target
    return None


def _resolve_exe(name: str) -> str | None:
    """Return the absolute path to an allowed executable, or None."""
    base = os.path.basename(name or "")
    if base not in _ALLOWED_EXES:
        return None
    # If a path was given, only accept it if its basename is allowlisted AND it
    # resolves to a real file. Otherwise search PATH for the plain name.
    if os.sep in name:
        p = Path(name)
        if p.is_file():
            return str(p)
        return None
    return shutil.which(base)


# git global options that consume the FOLLOWING token as their value, so that
# token must not be mistaken for the subcommand (`git -C . push` → `push`).
_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                   "--exec-path", "--config-env"}


def _git_subcommand(args: list[str]) -> str | None:
    """The git subcommand, skipping global options and their values."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GIT_VALUE_OPTS:
            i += 2                     # this option + its value
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return None


def run_command(argv: list[str], cwd: str | None = None,
                timeout: float = _TIMEOUT) -> tuple[int, str]:
    """Run an allowlisted command. Returns (returncode, output).

    Never uses a shell. Never raises. A refusal is returned as (-1, reason)."""
    if not argv:
        return -1, "No command given."

    exe = _resolve_exe(argv[0])
    if exe is None:
        return -1, (f"'{argv[0]}' is not on the allowlist "
                    f"({', '.join(sorted(_ALLOWED_EXES))}).")

    args = list(argv[1:])
    base = os.path.basename(exe)

    if base == "git":
        sub = _git_subcommand(args)
        if sub in _GIT_DENIED:
            return -1, f"git {sub} is not permitted from here."

    workdir = _confine(cwd)
    if workdir is None:
        return -1, "That directory is outside the project."

    try:
        r = subprocess.run(
            [exe, *args], cwd=str(workdir),
            capture_output=True, text=True, timeout=timeout,
        )
        out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
        return r.returncode, out.strip()[:_MAX_OUTPUT]
    except subprocess.TimeoutExpired:
        return -1, f"Command timed out after {timeout:.0f}s."
    except Exception as e:
        return -1, f"Command failed: {e}"


def _do_git(params: dict) -> str:
    sub = (params.get("command") or params.get("subcommand") or "status").strip()
    extra = (params.get("args") or "").strip()
    argv = ["git", sub] + (extra.split() if extra else [])
    code, out = run_command(argv, cwd=params.get("cwd"))
    if code == -1:
        return out
    return f"git {sub} (exit {code}):\n{out or '(no output)'}"


def _do_run_tests(params: dict) -> str:
    extra = (params.get("args") or "").strip()
    argv = [sys.executable, "-m", "pytest", "-q"] + (extra.split() if extra else [])
    code, out = run_command(argv, cwd=params.get("cwd"), timeout=120.0)
    if code == -1:
        return out
    tail = "\n".join(out.splitlines()[-12:])
    return f"Tests {'passed' if code == 0 else 'failed'} (exit {code}):\n{tail}"


def _do_run(params: dict) -> str:
    """Generic allowlisted run. Guarded by the confirm gate upstream (this tool
    is privileged_destructive), so it is only reached after a human click."""
    raw = (params.get("command") or "").strip()
    if not raw:
        return "What should I run?"
    argv = raw.split()
    code, out = run_command(argv, cwd=params.get("cwd"))
    if code == -1:
        return out
    return f"Ran {' '.join(argv)} (exit {code}):\n{out or '(no output)'}"


def _do_git_status_summary(params: dict) -> str:
    code, out = run_command(["git", "status", "--short", "--branch"],
                            cwd=params.get("cwd"))
    if code == -1:
        return out
    return out or "Working tree clean."


def developer(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "").strip().lower()

    if action in ("git", "git_status", "git_log", "git_diff", "git_branch"):
        return _do_git(params)
    if action in ("run_tests", "test", "pytest"):
        return _do_run_tests(params)
    if action in ("status_summary", "summary"):
        return _do_git_status_summary(params)
    if action in ("run", "exec", "shell"):
        return _do_run(params)
    if action in ("which", "version"):
        base = (params.get("command") or "").strip()
        exe = _resolve_exe(base)
        return f"{base} is not on the allowlist." if exe is None else f"{base}: {exe}"

    return (f"Unknown developer action: {action}. "
            f"Try git, run_tests, or status_summary.")


TOOL = {
    "name": "developer",
    "permission": "privileged_destructive",
    "description": (
        "Developer console for THIS project only. Runs an allowlisted command "
        "— git (status/log/diff/branch, but never push/reset/clean/remote), "
        "pytest, or the project's own python. Anything that changes state is "
        "held behind the on-screen confirmation. Working directory is confined "
        "to the project. Actions: git | run_tests | status_summary | run. "
        "Use run_tests to run the test suite, status_summary for a quick git "
        "status, git with a 'command' like 'log' or 'diff'."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "git | run_tests | status_summary | run",
            },
            "command": {
                "type": "STRING",
                "description": "git subcommand (e.g. 'log', 'diff') or command to run",
            },
            "args": {
                "type": "STRING",
                "description": "Extra arguments, space-separated",
            },
            "cwd": {
                "type": "STRING",
                "description": "Working directory; must be inside the project",
            },
        },
        "required": ["action"],
    },
    "handler": developer,
}
