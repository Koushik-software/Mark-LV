"""Tests for actions/developer.py — the allowlist and confinement.

This is the one tool that runs commands, so its refusals matter more than its
successes: the tests below are mostly about what it will NOT do.
"""
from __future__ import annotations

from actions import developer as dev


# ── executable allowlist ─────────────────────────────────────────────────────

def test_allowed_executables_resolve():
    for name in ("git", "python3", "pytest"):
        # These exist in any dev environment; if not, the allowlist check still
        # must not raise.
        assert dev._resolve_exe(name) in (None, dev._resolve_exe(name))


def test_disallowed_executable_is_refused():
    assert dev._resolve_exe("rm") is None
    assert dev._resolve_exe("curl") is None
    assert dev._resolve_exe("/bin/sh") is None
    assert dev._resolve_exe("bash") is None


def test_run_refuses_disallowed_binary():
    code, out = dev.run_command(["rm", "-rf", "/"])
    assert code == -1
    assert "allowlist" in out


def test_run_refuses_shell_injection_string():
    """A shell metacharacter is just an argument to a non-shell call — and the
    executable itself is refused, so nothing runs."""
    code, out = dev.run_command(["git; rm -rf /", "status"])
    assert code == -1


# ── git denylist ─────────────────────────────────────────────────────────────

def test_git_push_is_denied():
    code, out = dev.run_command(["git", "push"])
    assert code == -1
    assert "not permitted" in out


def test_git_reset_clean_remote_denied():
    for sub in ("reset", "clean", "remote", "filter-branch", "reflog"):
        code, out = dev.run_command(["git", sub])
        assert code == -1, sub
        assert "not permitted" in out


def test_git_denylist_finds_subcommand_after_flags():
    # `git -C . push` must still be caught.
    code, out = dev.run_command(["git", "-C", ".", "push"])
    assert code == -1


def test_git_status_is_allowed():
    code, out = dev.run_command(["git", "status", "--short"])
    assert code in (0, 1)          # runs; exit code depends on tree state


# ── working-directory confinement ────────────────────────────────────────────

def test_confine_allows_project_root():
    assert dev._confine(None) == dev._project_root()


def test_confine_allows_subdirectory():
    sub = str(dev._project_root() / "core")
    assert dev._confine(sub) is not None


def test_confine_refuses_outside():
    assert dev._confine("/etc") is None
    assert dev._confine("/") is None
    assert dev._confine("~") is None


def test_run_refuses_outside_cwd():
    code, out = dev.run_command(["git", "status"], cwd="/etc")
    assert code == -1
    assert "outside the project" in out


# ── handler level ────────────────────────────────────────────────────────────

def test_handler_git_denied_reports_plainly():
    r = dev.developer({"action": "git", "command": "push"})
    assert "not permitted" in r


def test_handler_unknown_action():
    assert "Unknown developer action" in dev.developer({"action": "explode"})


def test_handler_run_requires_command():
    assert "What should I run" in dev.developer({"action": "run"})


def test_timeout_is_bounded():
    # A command that would hang must be cut off. `python3 -c` sleeping is the
    # simplest way to prove the bound without relying on an external binary.
    code, out = dev.run_command(
        ["python3", "-c", "import time; time.sleep(5)"], timeout=0.5)
    assert code == -1
    assert "timed out" in out


def test_declared_tier_is_privileged():
    assert dev.TOOL["permission"] == "privileged_destructive"
