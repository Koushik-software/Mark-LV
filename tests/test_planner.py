"""Tests for core/planner.py.

The planner is tested with a stub model call, so nothing here touches the
network. The tests that matter are the safety ones: an unknown tool must be
dropped, a failed step must stop the plan, and a confirmation-blocked step must
never be treated as done.
"""
from __future__ import annotations

import json

from core import planner


TOOLS = {
    "web_search": "search the web",
    "save_memory": "save a fact",
    "send_message": "send a message",
}


def _planner(reply: str) -> planner.TaskPlanner:
    return planner.TaskPlanner(tools=TOOLS, ask=lambda prompt, system: reply)


def _steps(*specs) -> str:
    return json.dumps({"steps": [
        {"tool": t, "args": a, "why": w} for t, a, w in specs]})


# ── decomposition ────────────────────────────────────────────────────────────

def test_make_plan_parses_steps():
    p = _planner(_steps(("web_search", {"query": "dune"}, "look it up"),
                        ("save_memory", {"key": "dune"}, "save it")))
    plan = p.make_plan("research dune and save it")
    assert [s.tool for s in plan.steps] == ["web_search", "save_memory"]
    assert plan.steps[0].args == {"query": "dune"}


def test_make_plan_strips_markdown_fences():
    raw = "```json\n" + _steps(("web_search", {}, "x")) + "\n```"
    plan = _planner(raw).make_plan("g")
    assert len(plan.steps) == 1


def test_unknown_tool_is_dropped():
    raw = _steps(("web_search", {}, "ok"), ("rm_rf_everything", {}, "bad"))
    plan = _planner(raw).make_plan("g")
    assert [s.tool for s in plan.steps] == ["web_search"]


def test_garbage_reply_yields_empty_plan():
    plan = _planner("I'm sorry, I can't do that.").make_plan("g")
    assert plan.is_empty()


def test_model_exception_yields_empty_plan():
    def boom(prompt, system):
        raise RuntimeError("network down")
    p = planner.TaskPlanner(tools=TOOLS, ask=boom)
    assert p.make_plan("g").is_empty()


def test_empty_goal_yields_empty_plan():
    assert _planner(_steps(("web_search", {}, "x"))).make_plan("  ").is_empty()


def test_no_tools_yields_empty_plan():
    p = planner.TaskPlanner(tools={}, ask=lambda a, b: _steps(("x", {}, "y")))
    assert p.make_plan("g").is_empty()


def test_step_count_is_capped():
    specs = [("web_search", {"i": i}, "s") for i in range(planner.MAX_STEPS + 5)]
    plan = _planner(_steps(*specs)).make_plan("g")
    assert len(plan.steps) == planner.MAX_STEPS


def test_non_dict_args_become_empty():
    raw = json.dumps({"steps": [{"tool": "web_search", "args": "nope", "why": "x"}]})
    plan = _planner(raw).make_plan("g")
    assert plan.steps[0].args == {}


# ── execution ────────────────────────────────────────────────────────────────

def test_run_plan_executes_in_order():
    p = _planner(_steps(("web_search", {}, "a"), ("save_memory", {}, "b")))
    plan = p.make_plan("g")
    calls = []

    def dispatch(tool, args):
        calls.append(tool)
        return f"{tool} ok"

    out = p.run_plan(plan, dispatch)
    assert calls == ["web_search", "save_memory"]
    assert "complete" in out
    assert all(s.status == "done" for s in plan.steps)


def test_run_plan_stops_at_first_failure():
    p = _planner(_steps(("web_search", {}, "a"), ("save_memory", {}, "b"),
                        ("send_message", {}, "c")))
    plan = p.make_plan("g")
    calls = []

    def dispatch(tool, args):
        calls.append(tool)
        if tool == "save_memory":
            raise RuntimeError("disk full")
        return "ok"

    out = p.run_plan(plan, dispatch)
    assert calls == ["web_search", "save_memory"]        # third never ran
    assert "stopped at step 2" in out
    assert plan.steps[2].status == "pending"


def test_run_plan_stops_at_a_confirmation_block():
    p = _planner(_steps(("send_message", {}, "a"), ("save_memory", {}, "b")))
    plan = p.make_plan("g")
    calls = []

    def dispatch(tool, args):
        calls.append(tool)
        return "[CONFIRMATION_PENDING] waiting on the HUD"

    out = p.run_plan(plan, dispatch)
    assert calls == ["send_message"]                     # second never ran
    assert "confirmation" in out.lower()
    assert plan.steps[0].status == "blocked"
    assert plan.steps[1].status == "pending"


def test_run_plan_reports_progress():
    p = _planner(_steps(("web_search", {}, "look")))
    plan = p.make_plan("g")
    lines = []
    p.run_plan(plan, lambda t, a: "ok", on_progress=lines.append)
    assert any("Starting a 1-step plan" in ln for ln in lines)
    assert any("Step 1/1" in ln for ln in lines)
    assert any("Plan complete" in ln for ln in lines)


def test_run_plan_progress_callback_errors_are_contained():
    p = _planner(_steps(("web_search", {}, "look")))
    plan = p.make_plan("g")

    def bad(_msg):
        raise RuntimeError("log exploded")

    assert "complete" in p.run_plan(plan, lambda t, a: "ok", on_progress=bad)


def test_empty_plan_reports_inability():
    out = _planner("nope").make_plan("g")
    assert "could not break" in planner.TaskPlanner(tools=TOOLS).run_plan(
        out, lambda t, a: "x").lower()


# ── async execution ──────────────────────────────────────────────────────────
# asyncio.run() rather than pytest-asyncio, so the suite needs no plugin.

def test_run_plan_async_matches_sync_behaviour():
    import asyncio

    p = _planner(_steps(("web_search", {}, "a"), ("save_memory", {}, "b")))
    plan = p.make_plan("g")
    calls = []

    async def dispatch(tool, args):
        calls.append(tool)
        return "ok"

    out = asyncio.run(p.run_plan_async(plan, dispatch))
    assert calls == ["web_search", "save_memory"]
    assert "complete" in out


def test_run_plan_async_stops_at_block():
    import asyncio

    p = _planner(_steps(("send_message", {}, "a"), ("save_memory", {}, "b")))
    plan = p.make_plan("g")
    calls = []

    async def dispatch(tool, args):
        calls.append(tool)
        return "[CONFIRMATION_PENDING] parked"

    out = asyncio.run(p.run_plan_async(plan, dispatch))
    assert calls == ["send_message"]
    assert "confirmation" in out.lower()


# ── runner binding ───────────────────────────────────────────────────────────

def test_run_goal_without_runner_is_graceful():
    planner.bind_runner(None)
    try:
        assert "cannot run" in planner.run_goal("anything").lower()
    finally:
        planner.bind_runner(None)


def test_run_goal_delegates_to_bound_runner():
    seen = {}

    def runner(goal, on_progress=None):
        seen["goal"] = goal
        return "done"

    planner.bind_runner(runner)
    try:
        assert planner.run_goal("do the thing") == "done"
        assert seen["goal"] == "do the thing"
    finally:
        planner.bind_runner(None)
