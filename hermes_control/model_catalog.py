"""The models each engine's account can use, for the dashboard's model picker.

codex lists them itself: ``codex debug models`` prints the catalog codex fetched
for this account, with each model's supported effort levels. The claude CLI has
no such command, so its list combines CLI aliases with Hermes's built-in
Anthropic model list. No account is contacted for the claude catalog.

The lists guide the picker and nothing is refused for being absent from them: a
catalog can lag a new model, and claude also takes names like ``opus[1m]``.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable

from .engines.base import engine_env
from .engines.registry import get_adapter

CACHE_SECONDS = 600
_TIMEOUT_SECONDS = 30
# `claude --help`: --model takes "an alias for the latest model (e.g. 'fable', 'opus',
# or 'sonnet') or a model's full name".
CLAUDE_ALIASES = ("fable", "opus", "sonnet")


@dataclass(frozen=True)
class CatalogModel:
    name: str
    label: str = ""
    description: str = ""
    efforts: tuple[str, ...] | None = None  # None: the engine's full list applies
    default_effort: str | None = None


def parse_codex_catalog(text: str) -> list[CatalogModel]:
    """The models codex shows its user (``visibility: list``), in its own order."""
    data = json.loads(text)
    entries = data.get("models") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        raise ValueError("codex printed no model list")
    models = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility") != "list" or not entry.get("slug"):
            continue
        levels = tuple(level["effort"] for level in entry.get("supported_reasoning_levels") or []
                       if isinstance(level, dict) and level.get("effort"))
        models.append(CatalogModel(
            name=str(entry["slug"]), label=str(entry.get("display_name") or ""),
            description=str(entry.get("description") or ""), efforts=levels or None,
            default_effort=entry.get("default_reasoning_level")))
    return models


def _codex() -> dict[str, Any]:
    info = get_adapter("codex").probe()
    if info is None:
        raise RuntimeError("codex is not installed")
    proc = subprocess.run(
        [info.path, "debug", "models"], capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, env=engine_env())
    if proc.returncode != 0:
        said = " ".join((proc.stderr or proc.stdout).split())[:200]
        raise RuntimeError(f"codex debug models exited {proc.returncode}: {said}")
    return {"source": "codex debug models (the catalog codex uses for your account)",
            "models": parse_codex_catalog(proc.stdout)}


def _claude() -> dict[str, Any]:
    # provider_model_ids("anthropic") may fetch live models and stored credentials.
    # Read Hermes's curated list directly so this catalog stays local.
    names = None
    try:
        from hermes_cli import models as hermes_models

        built_in = getattr(hermes_models, "_PROVIDER_MODELS", None)
        if isinstance(built_in, dict):
            value = built_in.get("anthropic")
            if isinstance(value, (list, tuple)) and all(isinstance(n, str) for n in value):
                names = value
    except Exception:  # noqa: BLE001 - private data differs across Hermes versions
        pass
    source = ("Hermes's built-in model list (no account is contacted)" if names is not None
              else "Claude CLI aliases (Hermes's built-in list was unavailable; no account is contacted)")
    models = [CatalogModel(name=alias, label=f"{alias} (latest)") for alias in CLAUDE_ALIASES]
    # Show the newest model ids first after the CLI aliases.
    models += [CatalogModel(name=name) for name in sorted(names or (), reverse=True)
               if name not in CLAUDE_ALIASES]
    return {"source": source, "models": models}


_FETCHERS: dict[str, Callable[[], dict[str, Any]]] = {"claude": _claude, "codex": _codex}
# engine -> (monotonic time, entry). Successes only: a transient failure should not
# stick for the whole cache period.
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def available(*, refresh: bool = False, now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Per engine: ``{source, models, error, fetched_at}``; one engine failing never
    hides the other's list."""
    result: dict[str, Any] = {}
    for engine, fetch in _FETCHERS.items():
        with _lock:
            cached = _cache.get(engine)
        if cached and not refresh and now() - cached[0] < CACHE_SECONDS:
            result[engine] = cached[1]
            continue
        try:
            fetched = fetch()
        except Exception as exc:  # noqa: BLE001 - a subprocess, a network call, a parse
            result[engine] = {"source": "", "models": [], "fetched_at": time.time(),
                              "error": f"{type(exc).__name__}: {exc}"[:300]}
            continue
        entry = {"source": fetched["source"], "models": [asdict(m) for m in fetched["models"]],
                 "error": None, "fetched_at": time.time()}
        with _lock:
            _cache[engine] = (now(), entry)
        result[engine] = entry
    return result
