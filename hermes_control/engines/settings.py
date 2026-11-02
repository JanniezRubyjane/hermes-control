"""Opt-in engines resolved at registry load, and live dashboard pauses."""

from collections.abc import Mapping
from typing import Any

OPTIONAL_ENGINES = ("codex", "gemini")
_DEFAULTS = {"codex": True, "gemini": False}


def parse_engines(raw: Any) -> tuple[str, ...]:
    """Return enabled optional engines; pauses are validated by the live reader."""
    if raw is None:
        return ("codex",)
    if not isinstance(raw, Mapping):
        raise ValueError("engines must be a mapping")
    flags = dict(_DEFAULTS)
    for name, spec in raw.items():
        if name not in OPTIONAL_ENGINES:
            raise ValueError(f"engines: unknown engine {name!r}")
        if not isinstance(spec, Mapping) or set(spec) - {"enabled", "paused"}:
            raise ValueError(f"engines.{name} must be a mapping with only enabled and paused")
        value = spec.get("enabled", _DEFAULTS[name])
        if type(value) is not bool:
            raise ValueError(f"engines.{name}.enabled must be a bool")
        flags[name] = value
    return tuple(name for name in OPTIONAL_ENGINES if flags[name])


def read_engines() -> tuple[str, ...]:
    return parse_engines(_read_settings())


def _read_settings() -> Any:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
    settings = entry.get("settings")
    return settings.get("engines") if isinstance(settings, Mapping) else None


def read_paused() -> dict[str, bool | None]:
    """Read live optional-engine pauses: True, False, or None for an invalid spec.

    Validate each engine independently of enabled flags and other engine specs.
    Config read errors still propagate, as does an invalid engines mapping.
    """
    raw = _read_settings()
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("engines must be a mapping")
    states: dict[str, bool | None] = {}
    for name in OPTIONAL_ENGINES:
        spec = raw.get(name, {})
        value = spec.get("paused", False) if isinstance(spec, Mapping) else None
        states[name] = value if type(value) is bool else None
    return states


def save_paused(engine: str, paused: bool) -> None:
    """Save just one pause flag, retaining enabled and every other setting."""
    from hermes_cli.plugins_state import save_plugin_setting

    if engine not in OPTIONAL_ENGINES:
        raise ValueError(f"engines: cannot pause engine {engine!r}")
    if type(paused) is not bool:
        raise ValueError(f"engines.{engine}.paused must be a bool")
    save_plugin_setting("hermes_control", ("engines", engine, "paused"), paused)
