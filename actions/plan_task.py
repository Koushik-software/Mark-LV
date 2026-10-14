"""
plan_task — run a request that needs several tools, in order.

This is the only entry point to the planner. Ordinary requests never come here:
the model calls this itself, and only when a request genuinely has more than one
step. Everything the plan runs goes through the same dispatcher and the same
permission tiers as a normal tool call, so a step that needs confirmation stops
the plan and asks — the planner cannot click the button.
"""
from __future__ import annotations

from core import planner


def plan_task(parameters: dict, player=None, speak=None) -> str:
    goal = str((parameters or {}).get("goal", "")).strip()
    if not goal:
        return "I need to know what to plan."

    def progress(msg: str) -> None:
        if player and hasattr(player, "write_log"):
            try:
                player.write_log(f"[Plan] {msg}")
            except Exception:
                pass

    return planner.run_goal(goal, on_progress=progress)


TOOL = {
    "name": "plan_task",
    "permission": "external_interaction",
    "description": (
        "Runs a request that needs SEVERAL tools in order — research then save, "
        "check then report, create then run. Use ONLY when one request genuinely "
        "has multiple dependent steps; for anything a single tool covers, call "
        "that tool directly instead. The plan is proposed once and then executed "
        "step by step, reporting progress out loud. It stops and asks for "
        "confirmation if a step requires it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The full request to break into ordered steps",
            },
        },
        "required": ["goal"],
    },
    "scheduling": "INTERRUPT",
    "behavior": "NON_BLOCKING",
    "handler": plan_task,
}
