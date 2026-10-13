"""Engine name to adapter. One instance per engine; adapters are stateless."""

from __future__ import annotations

from .base import EngineAdapter, EngineInfo
from .claude import ClaudeAdapter
from .codex import CodexAdapter

_ADAPTERS: dict[str, EngineAdapter] = {
    "claude": ClaudeAdapter(),
    "codex": CodexAdapter(),
}

ENGINE_NAMES: tuple[str, ...] = tuple(_ADAPTERS)


def get_adapter(name: str) -> EngineAdapter:
    """Adapter for *name*; KeyError names the bad engine so the planner sees it."""
    try:
        return _ADAPTERS[name]
    except KeyError:
        raise KeyError(
            f"unknown engine {name!r}; known engines: {', '.join(ENGINE_NAMES)}") from None


NOT_INSTALLED = "not installed, or `--version` failed"


def engine_state(adapter: EngineAdapter) -> tuple[EngineInfo | None, str | None]:
    """``(info, problem)``: info is None when the CLI is missing; problem is None
    exactly when the CLI can take work right now."""
    info = adapter.probe()
    if info is None:
        return None, NOT_INSTALLED
    return info, adapter.auth_problem(info)


def engine_problem(adapter: EngineAdapter) -> str | None:
    """None when *adapter*'s CLI can take work right now, otherwise why not."""
    return engine_state(adapter)[1]


def available_engines() -> dict[str, EngineInfo]:
    """Engines usable right now: installed AND signed in. This is what keeps the
    planner from scheduling work for a CLI that is missing or signed out."""
    found: dict[str, EngineInfo] = {}
    for name, adapter in _ADAPTERS.items():
        info = adapter.probe()
        if info is not None and adapter.auth_problem(info) is None:
            found[name] = info
    return found
