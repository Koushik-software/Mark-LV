"""Tests for core/permissions.py and its wiring into the loaders.

The permission tier is a security control, so the tests that matter are the
negative ones: an unknown tier must not be accepted, an undeclared tool must not
be assumed safe, and a CONFIRM-tier tool must never run its callable without a
click.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import permissions as perms
from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins


@pytest.fixture(autouse=True)
def _clean_registry():
    perms.reset()
    yield
    perms.reset()


def _silent(_msg):
    pass


# ── registry basics ──────────────────────────────────────────────────────────

def test_unknown_tier_is_rejected():
    assert perms.register("x", "not_a_tier") is False
    assert perms.classify("x") == perms.DEFAULT_TIER


def test_undeclared_tool_defaults_to_external():
    assert perms.classify("never_heard_of_it") == perms.EXTERNAL_INTERACTION
    assert perms.policy_for("never_heard_of_it") == perms.ANNOUNCE


@pytest.mark.parametrize("raw,expected", [
    ("READ_ONLY", perms.READ_ONLY),
    ("read-only", perms.READ_ONLY),
    ("  Privileged Destructive ", perms.PRIVILEGED_DESTRUCTIVE),
    ("external_interaction", perms.EXTERNAL_INTERACTION),
])
def test_normalize_accepts_common_spellings(raw, expected):
    assert perms.normalize(raw) == expected


def test_core_tools_are_classified():
    assert perms.classify("recall_memory") == perms.READ_ONLY
    assert perms.classify("undo") == perms.REVERSIBLE


# ── policy enforcement ───────────────────────────────────────────────────────

def test_run_tier_executes_immediately():
    perms.register("reader", perms.READ_ONLY)
    calls = []
    out = perms.enforce("reader", lambda: calls.append(1) or "done")
    assert out == "done" and calls == [1]


def test_announce_tier_runs_and_announces():
    perms.register("sender", perms.EXTERNAL_INTERACTION)
    logged = []
    out = perms.enforce("sender", lambda: "sent", announce=logged.append)
    assert out == "sent"
    assert any("sender" in line for line in logged)


def test_announce_without_sink_still_runs():
    perms.register("sender", perms.EXTERNAL_INTERACTION)
    assert perms.enforce("sender", lambda: "sent") == "sent"


def test_confirm_tier_does_not_run_without_a_click(monkeypatch):
    perms.register("danger", perms.PRIVILEGED_DESTRUCTIVE)
    ran = []
    parked = {}

    def fake_request(key, title, detail, run):
        parked["run"] = run                      # simulate the UI holding it
        return "Awaiting confirmation."

    monkeypatch.setattr("core.confirm.request", fake_request)
    out = perms.enforce("danger", lambda: ran.append(1) or "boom")
    assert "confirmation" in out.lower()
    assert ran == []                             # never executed
    assert callable(parked["run"])               # but handed to the UI


def test_confirm_tier_runs_when_ui_resolves(monkeypatch):
    perms.register("danger", perms.PRIVILEGED_DESTRUCTIVE)
    ran = []

    def fake_request(key, title, detail, run):
        run()                                    # the "click"
        return "done"

    monkeypatch.setattr("core.confirm.request", fake_request)
    perms.enforce("danger", lambda: ran.append(1) or "boom")
    assert ran == [1]


# ── loader wiring ────────────────────────────────────────────────────────────

ACTION_WITH_PERM = '''
def h(parameters): return "x"
TOOL = {"name": "deleter", "description": "d",
        "parameters": {"type": "OBJECT", "properties": {}}, "handler": h,
        "permission": "privileged_destructive"}
'''

PLUGIN_WITH_PERM = '''
PLUGIN = {"name": "msg", "description": "d",
          "parameters": {"type": "OBJECT", "properties": {}},
          "permission": "external_interaction"}
def run(parameters, player=None, session_memory=None): return "ok"
'''

PLUGIN_BAD_PERM = '''
PLUGIN = {"name": "typo", "description": "d",
          "parameters": {"type": "OBJECT", "properties": {}},
          "permission": "super_safe_promise"}
def run(parameters, player=None, session_memory=None): return "ok"
'''


def test_action_permission_is_read_from_tool_dict(tmp_path):
    (tmp_path / "deleter.py").write_text(ACTION_WITH_PERM, encoding="utf-8")
    reg = discover_actions(tmp_path, logger=_silent)
    rec = reg._all_records[0]
    assert rec.permission == perms.PRIVILEGED_DESTRUCTIVE


def test_load_from_registries_reads_declarations(tmp_path):
    (tmp_path / "deleter.py").write_text(ACTION_WITH_PERM, encoding="utf-8")
    actions = discover_actions(tmp_path, logger=_silent)
    n = perms.load_from_registries(action_registry=actions)
    assert n == 1
    assert perms.policy_for("deleter") == perms.CONFIRM


def test_plugin_permission_is_read(tmp_path):
    (tmp_path / "msg.py").write_text(PLUGIN_WITH_PERM, encoding="utf-8")
    reg = discover_plugins(tmp_path, core_tool_names=set(), logger=_silent)
    perms.load_from_registries(plugin_registry=reg)
    assert perms.classify("msg") == perms.EXTERNAL_INTERACTION


def test_misspelled_permission_falls_back_to_default(tmp_path):
    (tmp_path / "typo.py").write_text(PLUGIN_BAD_PERM, encoding="utf-8")
    reg = discover_plugins(tmp_path, core_tool_names=set(), logger=_silent)
    perms.load_from_registries(plugin_registry=reg)
    assert perms.classify("typo") == perms.DEFAULT_TIER   # not silently permissive


def test_loader_permission_list_matches_core():
    """The loader's local allow-list must equal core.permissions.TIERS."""
    from core import action_loader, plugin_loader
    assert set(action_loader._opt_permission(v) for v in perms.TIERS) == set(perms.TIERS)
    assert set(plugin_loader._opt_permission(v) for v in perms.TIERS) == set(perms.TIERS)


# ── every shipped action declares a valid tier ───────────────────────────────
# A bundled action that declares nothing would fall to the external default and
# start being announced as "external action", which is noise. This test fails
# if a new action forgets its tier. Actions whose third-party deps are absent in
# a bare environment (playwright, numpy, psutil) cannot be imported here and are
# checked by reading their source for the declaration instead.

def _declared_permission_text(path: Path) -> str | None:
    import re
    m = re.search(r'"permission"\s*:\s*"([^"]+)"', path.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def test_all_shipped_actions_declare_a_valid_tier(repo_root):
    missing = []
    for path in sorted((repo_root / "actions").glob("*.py")):
        if path.name.startswith("_"):
            continue
        if '"TOOL' not in path.read_text(encoding="utf-8"):
            continue                                  # helper / capture-only module
        declared = _declared_permission_text(path)
        if perms.normalize(declared) is None:
            missing.append((path.name, declared))
    assert not missing, f"actions missing a valid permission tier: {missing}"

