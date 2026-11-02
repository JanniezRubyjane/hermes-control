"""Route tasks to usable engines, respecting the user's fallback order."""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Mapping
from typing import Any, Callable, Sequence

from . import model_policy
from .engines.base import ControlTask, EngineAdapter
from .engines.registry import ENGINE_NAMES, engine_problem, get_adapter
from .engines.settings import OPTIONAL_ENGINES

logger = logging.getLogger(__name__)
Fallback = dict[str, list[str]]


def _source_entries(source: str, fallback: Mapping[str, list[str]] | None) -> list[str] | None:
    """*source*'s configured destinations, or None when it has no list.

    A disabled engine keeps its list as an ordinary key (destinations are only
    enabled engines), so tasks planned or paused on it still follow the user's
    order, and a copied or serialized mapping routes the same way."""
    if fallback is not None and source in fallback:
        return fallback[source]
    return None


@functools.cache
def _warn_disabled(engine: str) -> None:
    logger.warning("routing.fallback: engine %r is disabled; it is never used as a destination",
                   engine)


def parse_fallback(raw: Any, policy: Mapping[str, model_policy.EnginePolicy]) -> Fallback:
    """Validate engine/model entries; retain explicit empty lists to forbid moves."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("routing.fallback must be a mapping")
    known = {*ENGINE_NAMES, *OPTIONAL_ENGINES}
    fallback: Fallback = {}
    for source, entries in raw.items():
        if source not in known:
            raise ValueError(f"routing.fallback: unknown engine {source!r}")
        if not isinstance(entries, list):
            raise ValueError(f"routing.fallback.{source} must be a list of strings")
        destinations = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, str):
                raise ValueError(f"routing.fallback.{source}: each entry must be a string")
            engine, slash, model = entry.partition("/")
            if engine not in known:
                raise ValueError(f"routing.fallback.{source}: unknown engine {engine!r}")
            if engine == source:
                raise ValueError(f"routing.fallback.{source}: cannot name its own engine")
            if engine in seen:
                raise ValueError(f"routing.fallback.{source}: duplicate destination engine {engine!r}")
            seen.add(engine)
            if slash:
                model_policy.check_model_name(model)
                rules = policy.get(engine)
                if rules and rules.allowed and model not in rules.allowed:
                    raise ValueError(f"routing.fallback.{source}: {engine} model {model!r} "
                                     "is not in its allowed models")
            if engine not in ENGINE_NAMES:
                _warn_disabled(engine)
                continue
            destinations.append(entry)
        if source not in ENGINE_NAMES:
            _warn_disabled(source)
        fallback[source] = destinations
    return fallback


def read_settings() -> Any:
    """The plugin settings snapshot, also usable by dispatch's stall reader."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
    return entry.get("settings")


def read_fallback(policy: Mapping[str, model_policy.EnginePolicy] | None = None, *,
                  settings: Any = None) -> Fallback:
    if settings is None:
        settings = read_settings()
    block = settings.get("routing") if isinstance(settings, Mapping) else None
    if block is not None and not isinstance(block, Mapping):
        raise ValueError("routing must be a mapping")
    raw = block.get("fallback") if block is not None else None
    if raw is None:
        return {}
    return parse_fallback(raw, model_policy.load_policy() if policy is None else policy)


def fallback_source(engine: str, fallback: Mapping[str, list[str]],
                    earlier: Mapping[str, Any] | None = None) -> tuple[str, set[str]]:
    """Recover the planned source and tried engines from the persisted move history.

    A planned list owns all moves. Without one, the current source keeps the
    historical behavior, including consulting its own list if it has one.
    """
    if not fallback:
        return engine, set()
    planned, tried = engine, {engine}
    while earlier:
        planned = earlier.get("from", planned)  # older pauses may only record dropped values
        tried.add(planned)
        earlier = earlier.get("earlier")
    return (planned, tried) if _source_entries(planned, fallback) is not None else (engine, set())


def fallback_order(engine: str, fallback: Mapping[str, list[str]], *,
                   engines: Sequence[str] | None = None,
                   earlier: Mapping[str, Any] | None = None) -> list[str]:
    """Only configured destinations, or the historical engine order for this source."""
    allowed = set(ENGINE_NAMES if engines is None else engines)
    source, tried = fallback_source(engine, fallback, earlier)
    entries = _source_entries(source, fallback)
    if entries is not None:
        return [name for entry in entries
                if (name := entry.partition("/")[0]) not in tried and name in allowed]
    return [name for name in (ENGINE_NAMES if engines is None else engines) if name != engine]


def engine_status(get_adapter: Callable[[str], EngineAdapter]) -> dict[str, str | None]:
    """Engine name to None (usable) or the reason it cannot take work."""
    return {name: engine_problem(get_adapter(name)) for name in ENGINE_NAMES}


def route(tasks: Sequence[ControlTask], status: dict[str, str | None], *,
          fallback: Mapping[str, list[str]] | None = None,
          policy: Mapping[str, model_policy.EnginePolicy] | None = None,
          ) -> list[tuple[ControlTask, dict[str, Any] | None]]:
    """Route signed-out tasks; leave restricted tasks with no destination to pause."""
    fallback = fallback if fallback is not None else {}
    usable = [name for name in ENGINE_NAMES if status.get(name) is None]
    if not usable:
        reasons = "; ".join(f"{name}: {reason}" for name, reason in status.items())
        raise ValueError(f"no engine can take work right now - {reasons}")
    routed: list[tuple[ControlTask, dict[str, Any] | None]] = []
    for task in tasks:
        problem = (status.get(task.engine) if task.engine in ENGINE_NAMES
                   else (status.get(task.engine) or f"{task.engine} is disabled"))
        destination = next((name for name in fallback_order(task.engine, fallback)
                            if name in usable), None)
        if problem is not None and destination is not None:
            routed.append(move(task, destination, problem, fallback=fallback, policy=policy))
        elif problem is not None:
            routed.append((task, {"from": task.engine, "stayed": True, "reason": (
                f"{problem}; staying on {task.engine}: no usable destination in routing.fallback")
            }))
        else:
            routed.append((task, None))
    return routed


def move(task: ControlTask, engine: str, reason: str, *,
         fallback: Mapping[str, list[str]] | None = None,
         policy: Mapping[str, model_policy.EnginePolicy] | None = None,
         earlier: Mapping[str, Any] | None = None,
         ) -> tuple[ControlTask, dict[str, Any]]:
    """Resolve the moved model and effort, and record only values actually lost."""
    policy = policy or {}
    rules = policy.get(engine)
    model, effort, model_rule = None, None, "default"
    source, _ = fallback_source(task.engine, fallback if fallback is not None else {}, earlier)
    entries = _source_entries(source, fallback)
    if entries is not None:
        if task.model and rules and task.model in rules.allowed:
            model, model_rule = task.model, "kept"
            effort = task.effort if task.effort in get_adapter(engine).efforts else None
        else:
            entry = next((entry for entry in entries
                          if entry.partition("/")[0] == engine), "")
            _, slash, entry_model = entry.partition("/")
            if slash:
                model, model_rule = entry_model, "fallback_entry"
    moved = model_policy.apply_policy(
        dataclasses.replace(task, engine=engine, model=model, effort=effort), policy)
    note: dict[str, Any] = {"from": task.engine, "reason": reason}
    if fallback:
        note["model_rule"] = model_rule
    if task.model and task.model != moved.model:
        note["dropped_model"] = task.model
    if task.effort and task.effort != moved.effort:
        note["dropped_effort"] = task.effort
    return moved, note
