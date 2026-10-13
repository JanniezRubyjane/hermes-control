"""Adapter for the ``codex`` CLI in headless ``exec --json`` mode.

Invocation shape verified on 2026-09-23:
``codex exec <goal> --json -C <wt> -s <sandbox>``

Codex is *sandbox-contained*: ``-s`` selects an OS-level sandbox policy, which
is a real boundary rather than a prompt heuristic. It has no per-tool
allow/deny flags, so ``ExecPolicy`` tool lists are dropped for this engine.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..events import EMPTY_DIFF_REASON, ControlResult, LimitHit, RawEvent
from ..policy import assert_no_forbidden_flags, codex_policy_flags
from ..worktree import TaskWorktree, base_settings_paths
from .base import (
    ControlTask,
    EngineAdapter,
    EngineInfo,
    says_limit,
    sign_in_problem,
)

_TOOL_ITEM_TYPES = frozenset({"command_execution", "patch_apply", "file_change", "mcp_tool_call"})
REASONING_SUMMARY_CHOICES = ("none", "concise", "detailed", "auto")


def read_reasoning_summary() -> str:
    """Read the saved codex summary mode, using concise for missing or invalid values."""
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry
    from ..model_policy import PLUGIN_ID

    entry = _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID) or {}
    settings = entry.get("settings")
    value = settings.get("reasoning_summary") if isinstance(settings, Mapping) else None
    return value if isinstance(value, str) and value in REASONING_SUMMARY_CHOICES else "concise"


def save_reasoning_summary(value: str) -> str:
    """Validate and save the mode through Hermes's plugin-settings writer."""
    from hermes_cli.plugins_state import save_plugin_setting
    from ..model_policy import PLUGIN_ID

    if not isinstance(value, str) or value not in REASONING_SUMMARY_CHOICES:
        raise ValueError("reasoning_summary must be one of: "
                         + ", ".join(REASONING_SUMMARY_CHOICES))
    save_plugin_setting(PLUGIN_ID, ("reasoning_summary",), value)
    return value


class CodexAdapter(EngineAdapter):
    name = "codex"
    sign_in_command = "codex login"
    # Codex config reference, `model_reasoning_effort`: "such as low, medium, high,
    # xhigh, max, or ultra. Available levels depend on the model." codex itself does
    # not reject an unknown value, so this list is the only check there is.
    efforts = ("low", "medium", "high", "xhigh", "max", "ultra")

    def probe(self) -> EngineInfo | None:
        path = shutil.which("codex")
        if not path:
            return None
        try:
            proc = subprocess.run([path, "--version"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=20, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return EngineInfo(name=self.name, version=proc.stdout.strip(), path=path)

    def auth_problem(self, info: EngineInfo) -> str | None:
        # `codex login status` prints "Not logged in" and exits 1 when signed out
        # (codex-cli 0.155.1). `codex sandbox` needs no login, so this gates
        # engine work only, never the verify step.
        return sign_in_problem(info, ["login", "status"], sign_in_command=self.sign_in_command)

    def repository_problem(self, worktree: TaskWorktree) -> str | None:
        # codex-cli 0.161.0 `exec --help` offers only --ignore-user-config:
        # "Do not load `$CODEX_HOME/config.toml`; auth still uses `CODEX_HOME`".
        # It cannot skip project .codex/ configuration, including rules that
        # can auto-approve execution outside the sandbox.
        paths, failure = base_settings_paths(worktree, "codex")
        if failure:
            return f"refused to run codex: could not check base .codex settings ({failure})"
        if paths:
            return ("refused to run codex: the repo commits .codex project files or "
                    "engine settings symlinks "
                    f"({', '.join(paths)}); codex cannot skip project-scoped config, "
                    "which could run code outside the sandbox")
        return None

    def build_argv(self, task: ControlTask, *, cwd: Path) -> list[str]:
        argv = ["codex", "exec", task.goal, "--json", "-C", str(cwd),
                *codex_policy_flags(task.policy)]
        # `codex exec --help` on 2026-09-24 (codex-cli 0.155.1): these config
        # overrides trim tool output and replies kept in context, not reasoning
        # tokens; model_reasoning_effort controls those separately.
        argv += ["-c", 'model_verbosity="low"',
                 "-c", "tool_output_token_limit=8000",
                 "-c", f'model_reasoning_summary="{read_reasoning_summary()}"']
        if task.model:
            argv += ["-m", task.model]
        if task.effort:
            # No effort flag exists; TOML-quoted so codex reads the value as a string.
            argv += ["-c", f'model_reasoning_effort="{task.effort}"']
        assert_no_forbidden_flags(argv)
        return argv

    def parse_line(self, line: str) -> list[RawEvent]:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return [RawEvent(kind="error",
                             payload={"message": "unparseable line", "line": line[:2000]})]
        if not isinstance(obj, dict):
            return [RawEvent(kind="error",
                             payload={"message": "unparseable line", "line": line[:2000]})]
        kind = obj.get("type")
        if kind == "thread.started":
            return [RawEvent(kind="session", payload={"session_id": obj.get("thread_id")},
                             raw_kind="thread.started")]
        if kind == "turn.started":
            return []
        if kind == "turn.completed":
            return [RawEvent(kind="result", payload={"ok": True, "usage": obj.get("usage") or {}},
                             raw_kind="turn.completed")]
        if kind == "turn.failed":
            return [RawEvent(kind="error", payload={"ok": False, "error": obj.get("error") or {}},
                             raw_kind="turn.failed")]
        if kind == "error":
            # e.g. "You've hit your usage limit. ... try again at <time>."
            return [RawEvent(kind="error", payload={"message": str(obj.get("message") or "")},
                             raw_kind="error")]
        if kind in ("item.started", "item.completed", "item.updated"):
            return self._from_item(kind, obj)
        return [RawEvent(kind="text", payload={"text": "", "raw": obj}, raw_kind=str(kind))]

    @staticmethod
    def _from_item(envelope: str, obj: dict[str, Any]) -> list[RawEvent]:
        item = obj.get("item") or {}
        item_type = item.get("type")
        raw_kind = f"{envelope}/{item_type}"
        if item_type == "agent_message":
            if envelope != "item.completed":
                return []
            return [RawEvent(kind="text", payload={"text": item.get("text") or ""},
                             raw_kind=raw_kind)]
        if item_type == "reasoning":
            if envelope != "item.completed":
                return []
            return [RawEvent(kind="thinking", payload={"text": item.get("text") or ""},
                             raw_kind=raw_kind)]
        if item_type in _TOOL_ITEM_TYPES:
            if envelope == "item.started":
                return [RawEvent(
                    kind="tool_start",
                    payload={"tool_id": item.get("id"), "name": item_type,
                             "input": {"command": item.get("command")}
                                      if item.get("command") is not None else {}},
                    raw_kind=raw_kind)]
            if envelope == "item.completed":
                exit_code = item.get("exit_code")
                return [RawEvent(
                    kind="tool_end",
                    payload={"tool_id": item.get("id"),
                             "is_error": exit_code is not None and exit_code != 0,
                             "exit_code": exit_code,
                             "content": item.get("aggregated_output") or "",
                             "status": item.get("status")},
                    raw_kind=raw_kind)]
            return []
        return [RawEvent(kind="text", payload={"text": "", "raw": obj}, raw_kind=raw_kind)]

    def extract_result(self, task: ControlTask, raws: Sequence[RawEvent],
                       diff: str) -> ControlResult:
        terminal = next((r for r in reversed(raws) if r.kind in ("result", "error")), None)
        session = next((r.payload.get("session_id") for r in raws if r.kind == "session"), None)
        ok = bool(terminal and terminal.kind == "result")
        texts = [r.payload.get("text", "") for r in raws if r.kind == "text"]
        text = next((t for t in reversed(texts) if t), "")
        if terminal is None:
            unproven: str | None = "no result event"
        elif not ok:
            unproven = "engine reported failure"
        elif not diff:
            unproven = EMPTY_DIFF_REASON
        else:
            unproven = None
        return ControlResult(
            task_id=task.id, engine=self.name, ok=ok, text=text, session_id=session,
            usage=(terminal.payload.get("usage") or {}) if terminal else {},
            diff=diff, unproven_reason=unproven)

    def limit_hit(self, raws: Sequence[RawEvent]) -> LimitHit | None:
        if any(r.kind == "result" for r in raws):
            return None  # the turn completed
        for raw in reversed(raws):
            if raw.kind != "error":
                continue
            message = str(raw.payload.get("message")
                          or (raw.payload.get("error") or {}).get("message") or "")
            if says_limit(message):
                # codex words its reset time for people ("Oct 24th, 2026 1:51 AM");
                # the exact time comes from its app-server when a pause needs it.
                return LimitHit(engine=self.name, message=message[:500])
        return None
