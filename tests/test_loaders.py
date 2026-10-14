"""Baseline tests for core/action_loader.py and core/plugin_loader.py.

These pin the discovery contract Stage C will extend: only files exposing a
well-formed TOOL/PLUGIN dict become tools, malformed files are skipped without
raising, name collisions are rejected, and handlers receive only the context
kwargs they actually declare.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.action_loader import discover_actions
from core.plugin_loader import discover_plugins

GOOD_ACTION = '''
def my_action(parameters, player=None):
    return "ran:" + str(parameters.get("x"))
TOOL = {
    "name": "my_action",
    "description": "Does a thing.",
    "parameters": {"type": "OBJECT", "properties": {"x": {"type": "STRING"}}},
    "handler": my_action,
}
'''

BROKEN_ACTION = "this is not valid python !!!"

NO_TOOL_FILE = "VALUE = 1\n"          # helper module, must be ignored

BAD_NAME_ACTION = '''
def h(parameters): return "x"
TOOL = {"name": "123bad", "description": "d",
        "parameters": {"type": "OBJECT", "properties": {}}, "handler": h}
'''

MISSING_DESC_ACTION = '''
def h(parameters): return "x"
TOOL = {"name": "nodesc", "description": "  ",
        "parameters": {"type": "OBJECT", "properties": {}}, "handler": h}
'''


def _write(d: Path, name: str, body: str) -> None:
    (d / name).write_text(body, encoding="utf-8")


def _silent(_msg):  # logger sink
    pass


def test_discovers_valid_action(tmp_path):
    _write(tmp_path, "my_action.py", GOOD_ACTION)
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.has("my_action")
    assert reg.run("my_action", {"x": "hi"}) == "ran:hi"


def test_helper_file_without_tool_is_ignored(tmp_path):
    _write(tmp_path, "helper.py", NO_TOOL_FILE)
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.names() == set()


def test_underscore_files_are_skipped(tmp_path):
    _write(tmp_path, "_private.py", GOOD_ACTION)
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.names() == set()


def test_broken_file_does_not_raise(tmp_path):
    _write(tmp_path, "broken.py", BROKEN_ACTION)
    _write(tmp_path, "my_action.py", GOOD_ACTION)      # still discovered
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.has("my_action")


@pytest.mark.parametrize("body", [BAD_NAME_ACTION, MISSING_DESC_ACTION])
def test_invalid_metadata_rejected(tmp_path, body):
    _write(tmp_path, "bad.py", body)
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.names() == set()


def test_reserved_name_collision_rejected(tmp_path):
    _write(tmp_path, "my_action.py", GOOD_ACTION)
    reg = discover_actions(tmp_path, reserved_names={"my_action"}, logger=_silent)
    assert reg.names() == set()


def test_duplicate_name_rejected(tmp_path):
    _write(tmp_path, "a.py", GOOD_ACTION)
    dup = GOOD_ACTION.replace("my_action", "my_action")   # same name, other file
    _write(tmp_path, "b.py", dup)
    reg = discover_actions(tmp_path, logger=_silent)
    assert reg.names() == {"my_action"}                   # exactly one survives


def test_unknown_action_returns_message(tmp_path):
    reg = discover_actions(tmp_path, logger=_silent)
    assert "not available" in reg.run("nope", {})


def test_handler_receives_only_declared_context(tmp_path):
    body = '''
def h(parameters, player=None):
    return "player=" + str(player)
TOOL = {"name": "ctx", "description": "d",
        "parameters": {"type": "OBJECT", "properties": {}}, "handler": h}
'''
    _write(tmp_path, "ctx.py", body)
    reg = discover_actions(tmp_path, logger=_silent)
    # `speak` is a known context key but h() does not declare it -> must not be passed
    assert reg.run("ctx", {}, {"player": "UI", "speak": object()}) == "player=UI"


def test_crashing_handler_is_contained(tmp_path):
    body = '''
def h(parameters):
    raise RuntimeError("boom")
TOOL = {"name": "boom", "description": "d",
        "parameters": {"type": "OBJECT", "properties": {}}, "handler": h}
'''
    _write(tmp_path, "boom.py", body)
    reg = discover_actions(tmp_path, logger=_silent)
    out = reg.run("boom", {})
    assert "failed" in out.lower() or "boom" in out


# ── plugins ──────────────────────────────────────────────────────────────────

GOOD_PLUGIN = '''
PLUGIN = {"name": "my_plugin", "description": "d",
          "parameters": {"type": "OBJECT", "properties": {}}}
def run(parameters, player=None, session_memory=None):
    return "plugin-ran"
'''


def test_discovers_valid_plugin(tmp_path):
    _write(tmp_path, "my_plugin.py", GOOD_PLUGIN)
    reg = discover_plugins(tmp_path, core_tool_names=set(), logger=_silent)
    assert reg.has("my_plugin")
    assert reg.run("my_plugin", {}) == "plugin-ran"


def test_plugin_without_plugin_dict_rejected(tmp_path):
    _write(tmp_path, "nope.py", "X = 1\n")
    reg = discover_plugins(tmp_path, core_tool_names=set(), logger=_silent)
    assert not reg.has("nope")


def test_plugin_colliding_with_core_tool_rejected(tmp_path):
    _write(tmp_path, "my_plugin.py", GOOD_PLUGIN)
    reg = discover_plugins(tmp_path, core_tool_names={"my_plugin"}, logger=_silent)
    assert not reg.has("my_plugin")


def test_plugin_template_is_not_discovered(tmp_path):
    # _template.py starts with '_' and must never load as a live tool.
    repo_root = Path(__file__).resolve().parent.parent
    reg = discover_plugins(repo_root / "plugins", core_tool_names=set(), logger=_silent)
    assert not reg.has("my_plugin")
