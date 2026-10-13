"""Hermes-Control: delegate coding work to external agent CLIs.

Hermes plans and reviews; ``claude`` and ``codex`` do the editing, each in a
throwaway git worktree. Registers two tools in the ``control`` toolset:
``control_dispatch`` starts the work, and ``control_status`` checks on a run
``control_dispatch`` reported as still running past its wait budget.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Mapping

from . import model_policy
from .engines.registry import ENGINE_NAMES, available_engines

logger = logging.getLogger(__name__)

# Kept under Hermes's own `timeouts.tools.sequential_call` deadline (420s on a
# default install), so a run that finishes on its own almost always beats it.
DEFAULT_DISPATCH_WAIT_SECONDS = 300

CONTROL_TOOL_DESCRIPTION = (
    "Delegate coding tasks to external agent CLIs. Break the work into tasks that can "
    "each be completed independently, choose an engine per task, and state why. Each "
    "task runs in its own git worktree; a successful edit is committed to the task's "
    "branch, and nothing reaches the working branch until a human merges it. Give every "
    "write task a 'verify' command: a task is only reported proven (unproven_reason "
    "null) when that command passes. The model field lists the models the user allows and what "
    "each is for: when a listed use fits a task, choose that engine and model, unless the user "
    "pinned an engine (@claude, @codex), which decides the engine. Otherwise, engines "
    "differ: 'codex' runs inside an OS-level sandbox and writes by default; "
    "'claude' is prompt-gated and reviews codex's diffs read-only (see the model "
    "field for which model). Use policy 'read-only' for anything "
    "that only needs to inspect the repo. An engine over the user's usage cap, or out of "
    "usage, hands its tasks to the other engine; when neither can take a task it comes back "
    "'paused' and the user resumes it from the dashboard - report it, never dispatch it again. "
    "A run that is still going after the 'wait' budget comes back with state 'running' and a "
    "run_id instead of the result: call control_status with that run_id to check on it, and "
    "again later if it is still running - never dispatch the same tasks again. Workers "
    "answer in English; report their results in the language the user writes in."
)

def build_schema(policy: Mapping[str, Any]) -> dict[str, Any]:
    """The tool schema, with the allowed models and efforts from *policy* written into
    the ``model`` and ``effort`` descriptions the planner reads."""
    model_text, effort_text = model_policy.describe(policy)
    return {
        "name": "control_dispatch",
        "description": CONTROL_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Tasks to run, in any order; dependencies are declared per task.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string",
                                    "description": "Short stable id matching "
                                                   "[A-Za-z0-9][A-Za-z0-9._-]{0,63}, "
                                                   "e.g. 'add-oidc-tests'."},
                            "goal": {"type": "string",
                                     "description": "What the worker must accomplish, self-contained."},
                            "engine": {"type": "string", "enum": list(ENGINE_NAMES),
                                       "description": "Which CLI runs this task."},
                            "policy": {"type": "string", "enum": ["read-only", "write"],
                                       "description": "Default 'read-only'. Use 'write' to let the worker edit files."},
                            "files_hint": {"type": "array", "items": {"type": "string"},
                                           "description": "Optional paths to point the worker at; "
                                                          "they are appended to the goal it receives."},
                            "depends_on": {"type": "array", "items": {"type": "string"},
                                           "description": "Task ids that must finish first. Ordering "
                                                          "ONLY: every task branches a fresh worktree "
                                                          "from HEAD, so a task never sees another "
                                                          "task's changes or result."},
                            "why": {"type": "string",
                                    "description": "Why this engine for this task. Recorded in the run log."},
                            "model": {"type": "string", "description": model_text},
                            "effort": {"type": "string", "description": effort_text},
                            "verify": {"type": "string",
                                       "description": "Shell command that proves the task "
                                                      "worked, e.g. '/abs/path/.venv/bin/python "
                                                      "-m pytest -q tests/test_x.py'. Runs with "
                                                      "/bin/sh in a detached clean checkout of the task's commit, "
                                                      "in an OS sandbox: no network, writes only inside the "
                                                      "checkout and temp. It has only committed files: no "
                                                      "ignored or untracked worker files, node_modules, build "
                                                      "output, or .pyc. The check must pass from the commit "
                                                      "alone and regenerate needed files offline, or the "
                                                      "worker must commit them. Name interpreters by absolute "
                                                      "path (no checkout virtualenv). A claude worker can run "
                                                      "its pre-approved check in the task worktree; a pass "
                                                      "there is not proof. The controller's run proves it."},
                            "red": {"type": "string",
                                    "description": "Optional, opt-in red-green proof for a write task "
                                                   "with verify. Shell command running only new or changed "
                                                   "tests. After verify passes, runs in an OS sandbox with "
                                                   "/bin/sh on base code with the committed test files "
                                                   "overlaid, then in a clean checkout of the task commit. "
                                                   "An expected failure on base and exit 0 on the commit "
                                                   "prove red-green. Exit 2 outside red_exit_codes also "
                                                   "counts when output contains 'ImportError while importing "
                                                   "test module', 'cannot import name', 'ModuleNotFoundError: "
                                                   "No module named', or 'has no attribute', and the same "
                                                   "command exits 0 on the commit. A base pass is advisory only. "
                                                   "Task verdicts: red-green-proven, tests-passed, red-passed, "
                                                   "red-inconclusive, red-skipped, no-verify, verify-failed, "
                                                   "verify-blocked, unproven, not-committed. A passing check "
                                                   "with a final unproven_reason yields unproven. "
                                                   "Must not start with '-'."},
                        },
                        "required": ["id", "goal", "engine", "why"],
                    },
                },
                "wait": {
                    "type": "integer",
                    "description": (
                        "Seconds to wait for the tasks to finish before this call returns. "
                        "After this many seconds, a run still going comes back with state "
                        "'running' and a run_id instead of the result - call control_status "
                        "with that run_id to check on it, and again later if it is still "
                        "running; never dispatch these tasks again. "
                        f"Default {DEFAULT_DISPATCH_WAIT_SECONDS}, kept under Hermes's own "
                        "tool-call deadline."
                    ),
                },
            },
            "required": ["tasks"],
        },
    }


# The schema with no model settings; register_tools builds the live one.
CONTROL_TOOL_SCHEMA: dict[str, Any] = build_schema({})

CONTROL_STATUS_TOOL_DESCRIPTION = (
    "Current state and per-task summary of a Hermes-Control run started by "
    "control_dispatch. Use it when control_dispatch returned state 'running'; "
    "pass wait to block until it finishes or the wait runs out (then it is still 'running') "
    "instead of sleeping between calls."
)

CONTROL_STATUS_TOOL_SCHEMA: dict[str, Any] = {
    "name": "control_status",
    "description": CONTROL_STATUS_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "run_id": {"type": "string",
                       "description": "The run_id a control_dispatch call returned."},
            "wait": {"type": "integer",
                     "description": "Seconds to block until the run finishes (at most 300); if the wait runs out first, the run comes back still 'running'. Use it instead of sleeping between calls: one call with wait replaces a sleep-and-check loop. 0 or omitted returns at once."},
        },
        "required": ["run_id"],
    },
}


def control_available(cfg: dict[str, Any] | None = None) -> bool:
    """True when at least one engine CLI is installed and usable. Without this the
    tool would be offered to the model and then fail on every call."""
    try:
        return bool(available_engines())
    except Exception:  # noqa: BLE001 - availability must never raise into the registry
        logger.debug("hermes-control: availability probe failed", exc_info=True)
        return False


def _dispatch_wait_seconds() -> float:
    """How long ``control_dispatch`` blocks before handing back a still-running
    summary: ``plugins.entries.hermes_control.settings.dispatch_wait_seconds``
    when set, else ``DEFAULT_DISPATCH_WAIT_SECONDS``. Read live, like every other
    Hermes-Control setting (limits.read_caps), so a change takes effect at once."""
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry

        entry = _plugin_settings_entry(load_config_readonly() or {}, model_policy.PLUGIN_ID) or {}
        settings = entry.get("settings")
        value = settings.get("dispatch_wait_seconds") if isinstance(settings, Mapping) else None
        return float(value) if value is not None else float(DEFAULT_DISPATCH_WAIT_SECONDS)
    except Exception:  # noqa: BLE001 - a bad setting must never break dispatch
        logger.warning("hermes-control: dispatch_wait_seconds setting is invalid; using the default",
                       exc_info=True)
        return float(DEFAULT_DISPATCH_WAIT_SECONDS)


def control_dispatch_handler(args: dict[str, Any], **_kw: Any) -> str:
    """``control_dispatch`` entry point.

    The tool registry calls every handler as ``handler(args, **context)`` and
    rejects any result that is not a string, so the summary goes back as JSON.
    """
    # Imported here, not at module level, to match dispatch.dispatch()'s own
    # import: a module-level `from .dispatch import dispatch` would rebind this
    # package's attribute `dispatch` from the submodule to that function,
    # breaking monkeypatch in test_dispatch_run.py.
    from .dispatch import start_dispatch
    wait = args.get("wait")
    wait = float(wait) if wait is not None else _dispatch_wait_seconds()
    # session_id is the Hermes session calling the tool: the Runs view offers it, since
    # that conversation holds the planner's reasoning.
    summary = start_dispatch(args.get("tasks") or [], parent_cwd=os.getcwd(),
                             timeout=args.get("timeout"), planner_session=_kw.get("session_id"),
                             wait=wait)
    return json.dumps(summary, ensure_ascii=False)


def control_status_handler(args: dict[str, Any], **_kw: Any) -> str:
    """``control_status`` entry point: same handler contract as control_dispatch_handler."""
    from .dispatch import control_status
    try:
        wait = float(args.get("wait") or 0)
    except (TypeError, ValueError):
        wait = 0.0
    if not wait >= 0:
        wait = 0.0
    result = control_status(str(args.get("run_id") or ""), wait=wait)
    return json.dumps(result, ensure_ascii=False)


def register(ctx) -> None:
    """The name ``hermes_cli.plugins_loader`` looks for.

    The loader does ``getattr(module, "register", None)`` and skips the plugin
    outright when it is absent, so this - not ``register_tools`` - is the entry
    point that decides whether Hermes-Control exists at runtime. ``register_tools``
    stays as the implementation (and as the name the design doc's §3.4 reused from
    ``plugins/platforms/a2a``, where it is a plugin-internal helper, not the hook).
    """
    register_tools(ctx)
    if hasattr(ctx, "register_cli_command"):  # `hermes control watch`
        from .watch import run_cli, setup_cli

        ctx.register_cli_command(
            name="control", help="Hermes-Control: watch delegated runs live",
            setup_fn=setup_cli, handler_fn=run_cli,
            description="Commands of the Hermes-Control plugin (claude and codex workers).")


def _policy_at_load() -> dict[str, Any]:
    """Model settings as of plugin load. The schema must stay byte-stable for a whole
    session (prompt cache), so it is built once, here; the live check happens at
    dispatch. Invalid settings still load the plugin - dispatch refuses them with the
    reason, which is where the planner can act on it."""
    try:
        return model_policy.load_policy()
    except ValueError:
        logger.warning("hermes-control: model settings are invalid; offering no model list",
                       exc_info=True)
        return {}


def register_tools(ctx) -> None:
    """Register ``control_dispatch`` and ``control_status`` in the ``control`` toolset."""
    ctx.register_tool(
        name="control_dispatch",
        toolset="control",
        schema=build_schema(_policy_at_load()),
        handler=control_dispatch_handler,
        check_fn=control_available,
        description=CONTROL_TOOL_DESCRIPTION,
        emoji="\U0001f39b",  # control knobs
    )
    ctx.register_tool(
        name="control_status",
        toolset="control",
        schema=CONTROL_STATUS_TOOL_SCHEMA,
        handler=control_status_handler,
        check_fn=control_available,
        description=CONTROL_STATUS_TOOL_DESCRIPTION,
        emoji="\U0001f39b",  # control knobs
    )
