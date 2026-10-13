"""Send every task to an engine that can take work right now.

The planner chooses from a fixed engine list, but either CLI may be missing or
signed out when the run starts. Failing those tasks one by one wastes the run;
instead they move to an engine that can run them, and the move is recorded in
the plan event and the summary so nobody mistakes it for the planner's choice.
If no engine can run, nothing starts and the call says why for each engine.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Sequence

from .engines.base import ControlTask, EngineAdapter
from .engines.registry import ENGINE_NAMES, engine_problem


def engine_status(get_adapter: Callable[[str], EngineAdapter]) -> dict[str, str | None]:
    """Engine name to None (usable) or the reason it cannot take work."""
    return {name: engine_problem(get_adapter(name)) for name in ENGINE_NAMES}


def route(tasks: Sequence[ControlTask], status: dict[str, str | None],
          ) -> list[tuple[ControlTask, dict[str, Any] | None]]:
    """Each task paired with its reroute note, None when it runs where it was sent.

    ``model`` and ``effort`` are dropped on a move: both belong to one CLI
    (``claude --model gpt-5.5`` would fail the task it was meant to rescue, and
    codex's ``ultra`` effort does not exist in claude); the model policy then
    gives the task the new engine's default.
    """
    usable = [name for name in ENGINE_NAMES if status.get(name) is None]
    if not usable:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in status.items())
        raise ValueError(f"no engine can take work right now - {reasons}")
    routed: list[tuple[ControlTask, dict[str, Any] | None]] = []
    for task in tasks:
        problem = status.get(task.engine)
        routed.append((task, None) if problem is None else move(task, usable[0], problem))
    return routed


def move(task: ControlTask, engine: str, reason: str) -> tuple[ControlTask, dict[str, Any]]:
    """*task* sent to *engine* instead, and the note that says why. ``model`` and
    ``effort`` are dropped: both belong to the engine it was planned for."""
    note: dict[str, Any] = {"from": task.engine, "reason": reason}
    if task.model:
        note["dropped_model"] = task.model
    if task.effort:
        note["dropped_effort"] = task.effort
    return dataclasses.replace(task, engine=engine, model=None, effort=None), note
