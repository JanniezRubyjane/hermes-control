"""Adapter for the ``claude`` CLI (Claude Code) in headless stream-json mode.

Invocation shape verified with ``claude --help`` on 2026-09-24:
``claude -p <goal> --output-format stream-json --verbose --add-dir <wt>``
``--append-system-prompt <contract> --setting-sources project``
``--strict-mcp-config --tools Read,Edit,Write,Grep,Glob,Bash <policy flags> ...``
``--verbose`` is required for stream-json under ``-p``.
Measured pre-goal context fell from about 39k to about 18k tokens per turn.

Claude Code is *prompt-gated*: containment comes from the permission mode and
the tool allow/deny lists, not from an OS sandbox. See ``policy.py``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..events import EMPTY_DIFF_REASON, ControlResult, LimitHit, RawEvent
from ..policy import assert_no_forbidden_flags, claude_policy_flags
from ..worker_report import REPORT_REQUEST
from ..worktree import TaskWorktree, base_settings_paths
from .base import (
    ControlTask,
    EngineAdapter,
    EngineInfo,
    engine_env,
    reset_in_text,
    says_limit,
    sign_in_problem,
)


def _claude_path() -> str | None:
    """Resolve in the worker environment. Probe and exec this same path: resolving
    symlinks breaks launchers that dispatch on their own name (e.g. volta shims)."""
    return shutil.which("claude", path=engine_env().get("PATH", os.defpath))


_WORKER_CONTRACT = (
    "You are a Hermes-Control worker. Complete exactly the task given, inside "
    "this working directory only. Do not commit, push, or switch branches. "
    "State plainly what you changed and whether you verified it.\n\n"
    "Under this permission mode every Bash command is refused except the one "
    "pre-approved check script named in the goal, when there is one. If a command "
    "is refused, do not retry it in another spelling, with sudo, through env, sh -c, "
    "a pipe, or by writing a scratch file to run. Syntax checks, test runs, and "
    "version probes must go through that script or be skipped. In your final message, "
    "say which checks you could not run and why."
)


_RESTRICTED_SUPPORT: dict[str, bool] = {}


def _supports_restricted(path: str) -> bool:
    """Probe each installed binary once per process (keyed by its real path, so two
    links to one binary share the answer); failed help is no capability."""
    key = os.path.realpath(path)
    if key not in _RESTRICTED_SUPPORT:
        _RESTRICTED_SUPPORT[key] = _probe_restricted(path)
    return _RESTRICTED_SUPPORT[key]


_supports_restricted.cache_clear = _RESTRICTED_SUPPORT.clear  # type: ignore[attr-defined]


def _probe_restricted(path: str) -> bool:
    try:
        proc = subprocess.run([path, "--help"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=20,
                              stdin=subprocess.DEVNULL, env=engine_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and re.search(
        r"^\s*--restricted\b(?![-\w])", proc.stdout, re.M) is not None


def _auth_status_summary(output: str) -> str:
    """The two fields of `claude auth status` that say why; the rest is paths."""
    try:
        status = json.loads(output)
    except json.JSONDecodeError:
        return " ".join(output.split())[:200]
    if not isinstance(status, dict):
        return " ".join(output.split())[:200]
    return ", ".join(
        f"{key}: {value if isinstance(value, str) else json.dumps(value)}"
        for key, value in ((k, status[k]) for k in ("loggedIn", "authMethod") if k in status))


class ClaudeAdapter(EngineAdapter):
    name = "claude"
    sign_in_command = "claude auth login"
    resumes_sessions = True
    # `claude --help`: --effort <level> (low, medium, high, xhigh, max).
    efforts = ("low", "medium", "high", "xhigh", "max")

    def probe(self) -> EngineInfo | None:
        path = _claude_path()
        if not path:
            return None
        try:
            proc = subprocess.run([path, "--version"], capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=20, stdin=subprocess.DEVNULL, env=engine_env())
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        _supports_restricted(path)
        return EngineInfo(name=self.name, version=proc.stdout.strip(), path=path)

    def auth_problem(self, info: EngineInfo) -> str | None:
        # `claude auth status` prints {"loggedIn": false, ...} and exits 1 when
        # signed out (claude-code 2.1.280).
        return sign_in_problem(info, ["auth", "status"], sign_in_command=self.sign_in_command,
                               summarize=_auth_status_summary)

    def repository_problem(self, worktree: TaskWorktree) -> str | None:
        path = _claude_path()
        restricted = bool(path and _supports_restricted(path))
        paths, failure = base_settings_paths(worktree, "claude", restricted=restricted)
        if failure:
            return f"refused to run claude: could not check base .claude settings ({failure})"
        if paths:
            if restricted:
                return ("refused to run claude: the repo commits engine settings symlinks "
                        f"({', '.join(paths)}), which could load project settings")
            info = self.probe()
            version = info.version if info else "unknown version"
            return (f"claude {version} lacks --restricted and the repo commits .claude settings "
                    "(hooks would run unsandboxed); update claude or use codex")
        return None

    def build_argv(self, task: ControlTask, *, cwd: Path) -> list[str]:
        path = _claude_path()
        argv = [
            path or "claude", "-p", task.goal,
            "--output-format", "stream-json",
            # stream-json under -p requires --verbose.
            "--verbose",
            "--add-dir", str(cwd),
            "--append-system-prompt", (_WORKER_CONTRACT + "\n\n" + REPORT_REQUEST
                                       if task.policy.write else _WORKER_CONTRACT),
            # Restricted mode ignores repo settings/hooks while loading CLAUDE.md.
            # Keep project scoping for old CLIs; the base-settings guard gates those.
            # Strict MCP and --tools exclude operator extensions.
            # --tools limits built-ins; policy --allowedTools Bash(<script>) still works.
            # --safe-mode still loaded CLAUDE.md in measurement, and
            # --exclude-dynamic-system-prompt-sections defeated prompt caching.
            "--setting-sources", "project",
            "--strict-mcp-config",
            "--tools", "Read,Edit,Write,Grep,Glob,Bash",
            *claude_policy_flags(task.policy),
        ]
        if path and _supports_restricted(path):
            argv += ["--restricted"]
        if task.model:
            argv += ["--model", task.model]
        if task.effort:
            argv += ["--effort", task.effort]
        if task.resume_session:
            # Same flags, same worktree: only the conversation carries over.
            argv += ["--resume", task.resume_session]
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
        if kind == "system":
            if obj.get("subtype") == "init":
                payload = {"session_id": obj.get("session_id")}
                if obj.get("model"):
                    # The model claude resolved `--model` to: the run log's proof of
                    # which model the policy actually put on the task.
                    payload["model"] = obj["model"]
                return [RawEvent(kind="session", payload=payload, raw_kind="system/init")]
            return []
        if kind == "stream_event":
            return []
        if kind == "assistant":
            return self._from_assistant(obj)
        if kind == "user":
            return self._from_user(obj)
        if kind == "rate_limit_event":
            return [RawEvent(kind="usage",
                             payload={"rate_limit_info": obj.get("rate_limit_info") or {}},
                             raw_kind="rate_limit_event")]
        if kind == "result":
            return self._from_result(obj)
        return [RawEvent(kind="text", payload={"text": "", "raw": obj}, raw_kind=str(kind))]

    @staticmethod
    def _from_assistant(obj: dict[str, Any]) -> list[RawEvent]:
        events: list[RawEvent] = []
        for block in (obj.get("message") or {}).get("content") or []:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                events.append(RawEvent(
                    kind="tool_start",
                    payload={"tool_id": block.get("id"), "name": block.get("name"),
                             "input": block.get("input") or {}},
                    raw_kind="assistant/tool_use"))
            elif block_type == "text":
                events.append(RawEvent(kind="text", payload={"text": block.get("text") or ""},
                                       raw_kind="assistant/text"))
            elif block_type == "thinking":
                events.append(RawEvent(kind="thinking",
                                       payload={"text": block.get("thinking") or ""},
                                       raw_kind="assistant/thinking"))
            else:
                # Same contract as the top-level unknown `type` fallback: an event
                # kind this adapter does not map surfaces as a passthrough with
                # `raw_kind` set rather than vanishing (design §4.1, §7).
                events.append(RawEvent(kind="text", payload={"text": "", "raw": block},
                                       raw_kind=f"assistant/{block_type}"))
        return events

    @staticmethod
    def _from_user(obj: dict[str, Any]) -> list[RawEvent]:
        result = obj.get("tool_use_result")
        if isinstance(result, str):
            # A refused or failed tool arrives as a bare error string, not the
            # {stdout, stderr} dict other results carry - e.g. a Bash call that
            # --permission-mode acceptEdits does not approve under -p.
            streams: dict[str, Any] = {"stderr": result}
        else:
            streams = result if isinstance(result, dict) else {}
        events: list[RawEvent] = []
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(RawEvent(
                    kind="tool_end",
                    payload={"tool_id": block.get("tool_use_id"),
                             "is_error": bool(block.get("is_error")),
                             "content": block.get("content"),
                             "stdout": streams.get("stdout", ""),
                             "stderr": streams.get("stderr", "")},
                    raw_kind="user/tool_result"))
        return events

    @staticmethod
    def _from_result(obj: dict[str, Any]) -> list[RawEvent]:
        # claude -p reports some failures (a usage limit among them) as subtype
        # "success" with is_error set, the message in `result`.
        ok = obj.get("subtype") == "success" and not obj.get("is_error")
        payload = {
            "ok": ok,
            "is_error": bool(obj.get("is_error")),
            "message": str(obj.get("result") or "")[:500] if not ok else "",
            "stop_reason": obj.get("stop_reason"),
            "session_id": obj.get("session_id"),
            "total_cost_usd": obj.get("total_cost_usd"),
            "duration_api_ms": obj.get("duration_api_ms"),
            "usage": obj.get("usage") or {},
            "subtype": obj.get("subtype"),
        }
        return [RawEvent(kind="result" if ok else "error", payload=payload,
                         raw_kind=f"result/{obj.get('subtype')}")]

    def extract_result(self, task: ControlTask, raws: Sequence[RawEvent],
                       diff: str) -> ControlResult:
        terminal = next((r for r in reversed(raws) if r.kind in ("result", "error")), None)
        texts = [r.payload.get("text", "") for r in raws if r.kind == "text"]
        session = next((r.payload.get("session_id") for r in raws if r.kind == "session"), None)
        ok = bool(terminal and terminal.kind == "result" and terminal.payload.get("ok"))
        if terminal is None:
            unproven: str | None = "no result event"
        elif not ok:
            unproven = "engine reported failure"
        elif not diff:
            unproven = EMPTY_DIFF_REASON
        else:
            unproven = None
        return ControlResult(
            task_id=task.id, engine=self.name, ok=ok,
            text=next((t for t in reversed(texts) if t), ""),
            session_id=session or (terminal.payload.get("session_id") if terminal else None),
            usage=(terminal.payload.get("usage") or {}) if terminal else {},
            diff=diff, unproven_reason=unproven)

    def limit_hit(self, raws: Sequence[RawEvent]) -> LimitHit | None:
        terminal = next((r for r in reversed(raws) if r.kind in ("result", "error")), None)
        if terminal is not None and terminal.kind == "result":
            return None  # finished: a rejected report along the way did not stop it
        rejected = next((r.payload.get("rate_limit_info") for r in reversed(raws)
                         if r.kind == "usage"
                         and (r.payload.get("rate_limit_info") or {}).get("status") == "rejected"),
                        None)
        message = str((terminal.payload.get("message") if terminal else "") or "")
        if rejected is None and not says_limit(message):
            return None
        resets_at = (rejected or {}).get("resetsAt") or reset_in_text(message)
        kind = (rejected or {}).get("rateLimitType")
        return LimitHit(engine=self.name,
                        message=message or f"claude's {kind or 'usage'} limit was reached",
                        resets_at=float(resets_at) if resets_at else None)
