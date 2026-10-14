"""
core/planner.py — turn one high-level request into an ordered list of tool calls.

WHY THIS EXISTS
    The assistant is reactive: one utterance, one tool. "Research the new Dune
    film, save the key facts to a note, then read it back" is three tools and a
    decision about their order, and the model had to hold all of that in a live
    voice turn — where it is also listening, speaking and being interrupted. It
    does the first step and drifts.

    A planner is not a new capability. It is the same tools, chosen up front,
    written down, and then run one at a time with a running commentary, so the
    order survives contact with a real conversation.

WHAT THIS IS NOT
    It is not always-on. Ordinary requests do not go through here — the model
    calls `plan_task` itself, only when a request genuinely has several steps.
    Wrapping every turn in a planner would put a second model call in front of
    "what time is it", and this app is judged on how fast it starts talking.

    It is also not autonomous. The plan is a proposal made once; each step is
    executed through the same dispatcher and the same permission tiers as a
    normal tool call. A step the permission system says must be confirmed
    returns its pending sentence and the plan STOPS there — the planner cannot
    click a button, which is the point.

DESIGN
    The model call is injected (`ask`), so the decomposition can be tested
    without a network. `make_plan` asks for JSON and validates it against the
    tools that actually exist; `run_plan` executes and reports progress through
    a callback. Both are pure with respect to the outside world except for the
    two callables handed in.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# A plan longer than this is a sign the model is padding, and every extra step
# is another chance to go wrong out loud. Eight covers the multi-step requests
# this exists for; anything longer is better done as a conversation.
MAX_STEPS = 8

# One sentence per step, so the user hears what is happening without a running
# monologue. The results are spoken once at the end.
_PLAN_SYSTEM = (
    "You decompose a request into an ordered list of tool calls. Reply with "
    "ONLY JSON, no prose. Schema: "
    '{"steps": [{"tool": "<exact tool name>", "args": {...}, "why": "<short>"}]}. '
    "Use only the tools listed. Use the fewest steps that complete the request. "
    "If the request needs no tool, return {\"steps\": []}. "
    "Each step's args must match that tool's parameters. Do not invent tools."
)


@dataclass
class Step:
    tool: str
    args: dict = field(default_factory=dict)
    why: str = ""
    status: str = "pending"     # pending | done | failed | blocked
    result: str = ""

    @property
    def label(self) -> str:
        return self.why or self.tool


@dataclass
class Plan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])

    def is_empty(self) -> bool:
        return not self.steps


class TaskPlanner:
    """Builds and runs multi-step plans.

    `ask(prompt, system) -> str` is the only model dependency; the default uses
    the app's own one-shot ladder (core.gemini), but a test passes a stub.
    """

    def __init__(self, tools: dict[str, str] | None = None,
                 ask: Optional[Callable[[str, str], str]] = None):
        self._tools: dict[str, str] = dict(tools or {})
        self._ask = ask
        self._lock = threading.Lock()

    # -- tools the planner is allowed to name --
    def set_tools(self, tools: dict[str, str]) -> None:
        with self._lock:
            self._tools = dict(tools or {})

    def available(self) -> dict[str, str]:
        with self._lock:
            return dict(self._tools)

    # -- decomposition --
    def _ask_model(self, prompt: str, system: str) -> str:
        if self._ask is not None:
            return self._ask(prompt, system)
        from core import gemini
        return gemini.text(prompt, tier=gemini.SMART, config={"system_instruction": system},
                           timeout_ms=30_000)

    def make_plan(self, goal: str) -> Plan:
        """Ask the model to decompose `goal`. Never raises; a failure yields an
        empty plan, which the caller reports as 'cannot break this down'."""
        goal = (goal or "").strip()
        if not goal:
            return Plan(goal="")

        tools = self.available()
        if not tools:
            return Plan(goal=goal)

        listing = "\n".join(f"- {name}: {desc}" for name, desc in sorted(tools.items()))
        prompt = (f"Request: {goal}\n\nAvailable tools:\n{listing}\n\n"
                  f"Return the JSON plan now.")

        try:
            raw = self._ask_model(prompt, _PLAN_SYSTEM) or ""
        except Exception as e:
            print(f"[Planner] planning call failed: {e}")
            return Plan(goal=goal)

        return self._parse(goal, raw)

    def _parse(self, goal: str, raw: str) -> Plan:
        """Turn the model's reply into a validated Plan. Unknown tools are
        dropped rather than trusted — a hallucinated tool must not reach the
        dispatcher."""
        text = raw.strip()
        if "{" in text and "}" in text:
            text = text[text.find("{"): text.rfind("}") + 1]
        try:
            data = json.loads(text)
        except Exception:
            print("[Planner] model reply was not JSON; no plan.")
            return Plan(goal=goal)

        known = self.available()
        steps: list[Step] = []
        for item in (data.get("steps") or [])[:MAX_STEPS]:
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool", "")).strip()
            if tool not in known:
                print(f"[Planner] dropping step with unknown tool: {tool!r}")
                continue
            args = item.get("args")
            if not isinstance(args, dict):
                args = {}
            steps.append(Step(tool=tool, args=args, why=str(item.get("why", "")).strip()))
        return Plan(goal=goal, steps=steps)

    # -- execution --
    # Two executors, one for each calling shape: the app dispatches through an
    # async method (main._execute_tool), tests dispatch through a plain
    # function. Both share _stop_reason so their behaviour cannot drift.

    @staticmethod
    def _stop_reason(step: Step, result: str) -> Optional[str]:
        """Why this step ends the plan, or None to continue.

        A blocked step is a success for the permission system and a stop for
        the plan: the tool returned the pending sentence because a human must
        press CONFIRM, and a plan cannot press a button.
        """
        if "[CONFIRMATION_PENDING]" in result:
            step.status = "blocked"
            return ("needs your confirmation on the HUD before I continue")
        return None

    def run_plan(self, plan: Plan,
                 dispatch: Callable[[str, dict], str],
                 on_progress: Optional[Callable[[str], None]] = None) -> str:
        """Execute `plan` step by step with a synchronous dispatcher.

        `dispatch(tool, args) -> str` performs one call. `on_progress(text)` is
        where each line goes — the HUD activity log and the spoken channel.

        Stops at the first failure or blocked step rather than ploughing on: a
        plan whose second step failed should not silently run steps three to
        eight.
        """
        if plan.is_empty():
            return f"I could not break '{plan.goal}' into steps I can actually run."

        def say(msg: str) -> None:
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass

        done: list[str] = []
        total = len(plan.steps)
        say(f"Starting a {total}-step plan for: {plan.goal}")

        for i, step in enumerate(plan.steps, 1):
            say(f"Step {i}/{total}: {step.label}")
            try:
                result = dispatch(step.tool, step.args) or ""
            except Exception as e:
                step.status = "failed"
                step.result = f"{type(e).__name__}: {e}"
                say(f"Step {i} failed: {e}")
                return self._summary(plan, i, f"({step.tool} failed: {e})", done)

            step.result = str(result)
            reason = self._stop_reason(step, step.result)
            if reason:
                say(f"Step {i} {reason}.")
                return self._summary(plan, i, f"({step.tool} {reason})", done)

            step.status = "done"
            done.append(f"{step.tool}: {step.result[:120]}")
            say(f"Step {i} done.")

        say("Plan complete.")
        return self._final(plan, done)

    async def run_plan_async(self, plan: Plan, dispatch,
                             on_progress: Optional[Callable[[str], None]] = None) -> str:
        """`run_plan` for an async dispatcher — `await dispatch(tool, args)`."""
        if plan.is_empty():
            return f"I could not break '{plan.goal}' into steps I can actually run."

        def say(msg: str) -> None:
            if on_progress:
                try:
                    on_progress(msg)
                except Exception:
                    pass

        done: list[str] = []
        total = len(plan.steps)
        say(f"Starting a {total}-step plan for: {plan.goal}")

        for i, step in enumerate(plan.steps, 1):
            say(f"Step {i}/{total}: {step.label}")
            try:
                result = await dispatch(step.tool, step.args) or ""
            except Exception as e:
                step.status = "failed"
                step.result = f"{type(e).__name__}: {e}"
                say(f"Step {i} failed: {e}")
                return self._summary(plan, i, f"({step.tool} failed: {e})", done)

            step.result = str(result)
            reason = self._stop_reason(step, step.result)
            if reason:
                say(f"Step {i} {reason}.")
                return self._summary(plan, i, f"({step.tool} {reason})", done)

            step.status = "done"
            done.append(f"{step.tool}: {step.result[:120]}")
            say(f"Step {i} done.")

        say("Plan complete.")
        return self._final(plan, done)

    @staticmethod
    def _summary(plan: Plan, stopped_at: int, why: str, done: list[str]) -> str:
        total = len(plan.steps)
        completed = "; ".join(done) if done else "nothing"
        return (f"'{plan.goal}' stopped at step {stopped_at} of {total} {why}. "
                f"Completed so far: {completed}.")

    @staticmethod
    def _final(plan: Plan, done: list[str]) -> str:
        total = len(plan.steps)
        summary = "; ".join(done) if done else "nothing to report"
        return f"'{plan.goal}' complete ({total} steps). {summary}"

    # -- reporting --
    def describe(self, plan: Plan) -> str:
        """The plan as a sentence, for the assistant to say before starting."""
        if plan.is_empty():
            return ""
        lines = [f"{i}. {s.label} ({s.tool})" for i, s in enumerate(plan.steps, 1)]
        return f"Plan for '{plan.goal}':\n" + "\n".join(lines)


# ── One planner for the process ──────────────────────────────────────────────
# main.py owns it and keeps its tool list current; a plugin can read it without
# constructing its own.
_default: Optional[TaskPlanner] = None

# How a plan is actually run is a property of the app, not of this module: it
# needs the event loop, the permission-enforcing dispatcher and the HUD. main.py
# binds that once; `run_goal` is what the plan_task action calls.
_runner: Optional[Callable[[str, Optional[Callable[[str], None]]], str]] = None


def get_planner() -> TaskPlanner:
    global _default
    if _default is None:
        _default = TaskPlanner()
    return _default


def bind_runner(fn: Optional[Callable[[str, Optional[Callable[[str], None]]], str]]) -> None:
    """Register the app's plan executor. Called once from main.py at startup."""
    global _runner
    _runner = fn


def run_goal(goal: str, on_progress: Optional[Callable[[str], None]] = None) -> str:
    """Plan and run `goal` through the app's executor. Safe to call from a
    worker thread — the bound runner marshals onto the event loop itself."""
    if _runner is None:
        return "I cannot run a multi-step plan right now."
    return _runner(goal, on_progress) or "The plan produced no result."
