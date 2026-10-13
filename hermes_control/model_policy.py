"""Which model and effort each task may use, per engine.

Settings live where Hermes keeps every plugin's settings,
``plugins.entries.hermes_control.settings.models`` in config.yaml:

    claude:
      default: sonnet                 # used when a task names no model
      allowed:                        # the only models a task may use
        sonnet: {use_for: routine edits, effort: medium}
        opus: hard bugs across modules   # shorthand: just the use_for text

Saved settings store ``allowed`` as a list of ``{name, use_for, effort}`` and
always name every engine: Hermes writes plugin settings by deep-merging them
into config.yaml, and a merge can replace a list but can never drop a mapping
key, so a model removed from a mapping would silently stay allowed.

No settings (or an engine with an empty list) means no restriction for that
engine: any model the CLI accepts, and the CLI's own default when a task names
none. The
check runs at dispatch time against the live config, so tightening the list
takes effect at once; the list the planner is shown is built when Hermes loads
the plugin, so it refreshes with a new Hermes session.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .engines.base import ControlTask
from .engines.registry import ENGINE_NAMES, get_adapter

PLUGIN_ID = "hermes_control"

# Model names become an argv value after `--model` / `-m`. A leading dash or any
# whitespace would let a planner-chosen "model" pose as a flag; this shape covers
# every real name, aliases like `opus[1m]` included.
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")


@dataclass(frozen=True)
class ModelChoice:
    name: str
    use_for: str = ""
    effort: str | None = None


@dataclass(frozen=True)
class EnginePolicy:
    default: str | None
    allowed: dict[str, ModelChoice] = field(default_factory=dict)  # empty: any model


def check_model_name(name: str) -> str:
    if not isinstance(name, str) or not _MODEL_NAME.fullmatch(name):
        raise ValueError(f"model name {name!r} must start with a letter or digit and use only "
                         "letters, digits and . _ : / [ ] -")
    return name


def _check_effort(engine: str, effort: str, where: str) -> str:
    efforts = get_adapter(engine).efforts
    if effort not in efforts:
        raise ValueError(f"{where}: {engine} effort {effort!r} is not one of {', '.join(efforts)}")
    return effort


def _parse_choice(engine: str, name: str, spec: Any) -> ModelChoice:
    if spec is None or isinstance(spec, str):
        return ModelChoice(name=name, use_for=(spec or "").strip())
    if not isinstance(spec, Mapping):
        raise ValueError(f"{engine} model {name!r}: expected a use_for text or "
                         "{use_for, effort}")
    unknown = set(spec) - {"use_for", "effort"}
    if unknown:
        raise ValueError(f"{engine} model {name!r}: unknown key(s) {', '.join(sorted(unknown))}; "
                         "only use_for and effort are allowed")
    use_for = spec.get("use_for")
    if use_for is not None and not isinstance(use_for, str):
        raise ValueError(f"{engine} model {name!r}: use_for must be text")
    effort = spec.get("effort")
    if effort is not None and not isinstance(effort, str):
        raise ValueError(f"{engine} model {name!r}: effort must be text")
    effort = effort or None
    if effort is not None:
        _check_effort(engine, effort, f"{engine} model {name!r}")
    return ModelChoice(name=name, use_for=(use_for or "").strip(), effort=effort)


def _parse_allowed(engine: str, raw: Any) -> dict[str, ModelChoice]:
    """Either spelling: ``{name: use_for | {use_for, effort}}`` (hand-written) or
    ``[{name, use_for, effort} | name]`` (what save_settings writes)."""
    if isinstance(raw, Mapping):
        items = list(raw.items())
    elif isinstance(raw, list):
        items = []
        for entry in raw:
            if isinstance(entry, str):
                items.append((entry, None))
            elif isinstance(entry, Mapping) and entry.get("name"):
                items.append((entry["name"],
                              {k: v for k, v in entry.items() if k != "name"}))
            else:
                raise ValueError(f"{engine}: each allowed entry needs a name")
    else:
        raise ValueError(f"{engine}: allowed must list the models and their use")
    allowed: dict[str, ModelChoice] = {}
    for name, spec in items:
        if not isinstance(name, str):
            raise ValueError(f"{engine}: allowed model name must be text")
        if name in allowed:
            raise ValueError(f"{engine}: model {name!r} is listed twice")
        allowed[check_model_name(name)] = _parse_choice(engine, name, spec)
    return allowed


def parse_policy(raw: Any) -> dict[str, EnginePolicy]:
    """Validated settings, or ValueError naming the first problem."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("models settings must map each engine to {default, allowed}")
    policy: dict[str, EnginePolicy] = {}
    for engine, block in raw.items():
        if engine not in ENGINE_NAMES:
            raise ValueError(f"unknown engine {engine!r} in models settings; "
                             f"engines are {', '.join(ENGINE_NAMES)}")
        if block is None:
            continue
        if not isinstance(block, Mapping):
            raise ValueError(f"{engine}: expected {{default, allowed}}, got "
                             f"{type(block).__name__}")
        unknown = set(block) - {"default", "allowed"}
        if unknown:
            raise ValueError(f"{engine}: unknown key(s) {', '.join(sorted(unknown))}; "
                             "only default and allowed are allowed")
        raw_allowed = block.get("allowed")
        allowed = _parse_allowed(engine, {} if raw_allowed is None else raw_allowed)
        default = block.get("default")
        if default is not None and not isinstance(default, str):
            raise ValueError(f"{engine}: default must be a model name or null")
        if default is None and not allowed:
            continue  # an engine with nothing set: no restriction
        if default is not None:
            default = check_model_name(default)
            if allowed and default not in allowed:
                raise ValueError(f"{engine}: default {default!r} is not in the allowed models "
                                 f"({', '.join(allowed)})")
        elif allowed:
            # An empty model would otherwise fall through to the CLI's own
            # default, which the allow-list may not include.
            default = next(iter(allowed))
        policy[engine] = EnginePolicy(default=default, allowed=allowed)
    return policy


def apply_policy(task: ControlTask, policy: Mapping[str, EnginePolicy]) -> ControlTask:
    """The task with its model and effort settled, or ValueError the planner can act on."""
    rules = policy.get(task.engine)
    model = task.model
    if rules is not None:
        if model is None:
            model = rules.default
        elif rules.allowed and model not in rules.allowed:
            raise ValueError(
                f"task {task.id!r}: {task.engine} model {model!r} is not allowed; choose one of "
                f"{', '.join(rules.allowed)} (or leave model empty for {rules.default})")
    effort = task.effort
    if effort is not None:
        _check_effort(task.engine, effort, f"task {task.id!r}")
    elif rules is not None and model in rules.allowed:
        effort = rules.allowed[model].effort
    return dataclasses.replace(task, model=model, effort=effort)


def describe(policy: Mapping[str, EnginePolicy]) -> tuple[str, str]:
    """The ``model`` and ``effort`` field descriptions the planner reads."""
    lines = []
    for engine in ENGINE_NAMES:
        rules = policy.get(engine)
        if rules is None or not rules.allowed:
            tail = f" (default {rules.default})" if rules is not None and rules.default else ""
            lines.append(f"{engine}: any model the CLI accepts; empty uses the CLI's own "
                         f"default{tail}.")
            continue
        choices = []
        for choice in rules.allowed.values():
            notes = [n for n in (choice.use_for,
                                 "default" if choice.name == rules.default else "",
                                 f"effort {choice.effort}" if choice.effort else "") if n]
            choices.append(f"{choice.name} ({'; '.join(notes)})" if notes else choice.name)
        lines.append(f"{engine}: only {', '.join(choices)}.")
    model_text = ("The models the user allows on each engine and what each is for. When a "
                  "listed use fits the task, choose that engine and model; otherwise leave this "
                  "empty for the engine's default. " + " ".join(lines))
    effort_text = ("Optional effort: raise it for hard tasks, lower it for routine ones; empty "
                   "uses the model's configured effort, else the CLI default. "
                   + " ".join(f"{engine}: {'|'.join(get_adapter(engine).efforts)}."
                              for engine in ENGINE_NAMES))
    return model_text, effort_text


def to_settings(policy: Mapping[str, EnginePolicy]) -> dict[str, Any]:
    """The canonical config.yaml shape of *policy*: every engine named, allowed as
    a list, so saving it replaces what was there (see the module docstring)."""
    settings: dict[str, Any] = {}
    for engine in ENGINE_NAMES:
        rules = policy.get(engine)
        settings[engine] = {
            "default": rules.default if rules else None,
            "allowed": [{"name": c.name, "use_for": c.use_for, "effort": c.effort}
                        for c in rules.allowed.values()] if rules else [],
        }
    return settings


def read_settings() -> Any:
    """The raw ``models`` settings from the active profile's config.yaml."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID) or {}
    settings = entry.get("settings")
    return settings.get("models") if isinstance(settings, Mapping) else None


def load_policy() -> dict[str, EnginePolicy]:
    return parse_policy(read_settings())


def save_settings(raw: Any) -> dict[str, Any]:
    """Validate, then write through Hermes's single plugin-settings writer (locked,
    refuses managed installs). Nothing is written when validation fails."""
    from hermes_cli.plugins_state import save_plugin_setting

    settings = to_settings(parse_policy(raw))
    save_plugin_setting(PLUGIN_ID, ("models",), settings)
    return settings
