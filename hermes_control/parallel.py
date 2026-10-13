"""Opt-in task concurrency, read from Hermes's plugin settings."""
from collections.abc import Mapping
from typing import Any

from .engines.registry import ENGINE_NAMES


def parse_parallel(raw: Any) -> dict[str, Any]:
    settings = {"max_tasks": 1, "per_engine": {engine: 1 for engine in ENGINE_NAMES}}
    if raw is None:
        return settings
    if not isinstance(raw, Mapping):
        raise ValueError("parallel must be a mapping")
    for key in raw:
        if key not in settings:
            raise ValueError(f"parallel: unknown key {key!r}")
    maximum = raw.get("max_tasks", 1)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 8:
        raise ValueError("parallel.max_tasks must be an integer between 1 and 8")
    settings["max_tasks"] = maximum
    settings["per_engine"] = {engine: maximum for engine in ENGINE_NAMES}
    engines = raw.get("per_engine", {})
    if not isinstance(engines, Mapping):
        raise ValueError("parallel.per_engine must be a mapping")
    for engine, count in engines.items():
        if engine not in ENGINE_NAMES:
            raise ValueError(f"parallel.per_engine: unknown key {engine!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError(f"parallel.per_engine.{engine} must be an integer >= 1")
        settings["per_engine"][engine] = count
    return settings


def read_parallel() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
    settings = entry.get("settings")
    return parse_parallel(settings.get("parallel") if isinstance(settings, Mapping) else None)
