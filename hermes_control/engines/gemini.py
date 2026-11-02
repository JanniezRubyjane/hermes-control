"""Google Antigravity CLI adapter, built from live probes of agy 1.3.5 and checked on 1.4.12."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Sequence

from ..events import EMPTY_DIFF_REASON, ControlResult, LimitHit, RawEvent
from ..policy import assert_no_forbidden_flags, gemini_permission_files_problem, gemini_settings_problem
from ..proctree import group_popen_kwargs, stop_group
from ..worktree import TaskWorktree, base_settings_paths, changed_gemini_paths
from .base import ControlTask, EngineAdapter, EngineInfo, says_limit


def agy_command(path: str, args: Sequence[str], *, env: dict[str, str],
                timeout: float = 20) -> subprocess.CompletedProcess:
    """Bound account/probe commands, including signed-out OAuth and descendants."""
    process = None
    try:
        process = subprocess.Popen(
            [path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            env=env, **group_popen_kwargs())
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess([path, *args], process.returncode, stdout, stderr)
    finally:
        if process is not None:
            with contextlib.suppress(OSError):
                stop_group(process, grace=0.2)
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                process.wait(timeout=1)
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


_UNIT = r"(days?|d|hours?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-z])"
_RESETS_IN = re.compile(rf"resets\s+in\s+((?:[0-9]+\s*{_UNIT}\s*)+)", re.IGNORECASE)
_DURATION_PART = re.compile(rf"([0-9]+)\s*{_UNIT}", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_MAX_RESET_SECONDS = 30 * 86400


def _resets_at(message: str) -> float | None:
    """Epoch seconds for "Resets in 17h47m31s" in agy's message, None when absent or odd."""
    found = _RESETS_IN.search(message)
    if not found:
        return None
    try:
        seconds = sum(int(n) * _UNIT_SECONDS[u[0].lower()]
                      for n, u in _DURATION_PART.findall(found.group(1)))
        if seconds > _MAX_RESET_SECONDS:
            return None
        return time.time() + seconds
    except (ValueError, OverflowError):
        return None


def model_lines(text: str) -> list[tuple[str, str]]:
    """Ignore the 'Fetching available models...' banner; rows are id<TAB>name."""
    models = []
    for line in text.splitlines():
        model_id, tab, name = line.partition("\t")
        if tab and model_id.strip() and name.strip():
            models.append((model_id.strip(), name.strip()))
    return models


class GeminiAdapter(EngineAdapter):
    name = "gemini"
    planner_note = ("runs agy in its sandbox and writes; usage is report-only (dashboard), never holds work back; goals must stay "
                    "inside the repo and offline; a denied agy action (a shell command when "
                    "agy's toolPermission is not proceed-in-sandbox, a path outside the repo, "
                    "or network) ends the task with failure_mode: guard; change the goal, "
                    "the agy setting or the engine, never retry unchanged")
    sign_in_command = "agy"
    # `agy --help` (agy 1.3.5).
    efforts = ("low", "medium", "high")
    resumes_sessions = True

    def __init__(self) -> None:
        # Registry adapters are shared by concurrent workers; checked bytes belong
        # to the current run's thread, and are consumed by its before-snapshot.
        self._permission_state = threading.local()

    def engine_env(self) -> dict[str, str]:
        env = {**super().engine_env(), "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
               "HERMES_CONTROL_CHECK_COPY": "1"}
        if "PATH" in env:
            roots = [Path(os.path.normpath(Path.home() / ".hermes")).parts]
            if hermes_home := env.get("HERMES_HOME"):
                roots.append(Path(os.path.normpath(hermes_home)).parts)
            kept = []
            for entry in env["PATH"].split(os.pathsep):
                if entry:
                    parts = Path(os.path.normpath(entry)).parts
                    if any(parts[:len(root)] == root for root in roots):
                        continue
                kept.append(entry)
            env["PATH"] = os.pathsep.join(kept)
        return env

    def headless_commands_allowed(self) -> bool:
        """Only invite shell commands when the adapter's validated settings allow them."""
        settings = (Path(self.engine_env().get("HOME", str(Path.home()))) /
                    ".gemini" / "antigravity-cli" / "settings.json")
        try:
            data = json.loads(settings.read_bytes())
            return (gemini_settings_problem(data) is None and
                    data.get("toolPermission") == "proceed-in-sandbox")
        except (OSError, ValueError):
            return False

    def _path(self) -> str | None:
        env = self.engine_env()
        path = shutil.which("agy", path=env.get("PATH", ""))
        if path:
            return path
        # Documented installer location, even when a launcher's PATH omits it.
        home = Path(env.get("HOME", str(Path.home())))
        return shutil.which(str(home / ".local" / "bin" / "agy"))

    def probe(self) -> EngineInfo | None:
        path = self._path()
        if not path:
            return None
        try:
            proc = agy_command(path, ["--version"], env=self.engine_env())
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return EngineInfo(self.name, proc.stdout.strip(), path)

    def auth_problem(self, info: EngineInfo) -> str | None:
        try:
            proc = agy_command(info.path, ["models"], env=self.engine_env())
        except (OSError, subprocess.SubprocessError) as exc:
            return f"agy sign-in check failed ({exc}); run `agy` to sign in with Google"
        if proc.returncode == 0 and model_lines(proc.stdout):
            return None
        return "agy is not signed in or returned no models; run `agy` to sign in with Google"

    def repository_problem(self, worktree: TaskWorktree) -> str | None:
        self._permission_state.checked = None
        # Live probe: .agents/hooks.json executes even in plan mode in an
        # untrusted repository. agy 1.3.5 has no --restricted equivalent.
        paths, failure = base_settings_paths(worktree, self.name)
        if failure:
            return f"refused to run gemini: could not check base .agents/.gemini settings ({failure})"
        if paths:
            return ("refused to run gemini: the repo commits .agents/.gemini project files "
                    f"or settings symlinks ({', '.join(paths)}); agy cannot skip project hooks")
        # Untracked and ignored hooks also execute on the first session.
        paths, failure = changed_gemini_paths(worktree)
        if failure:
            return f"refused to run gemini: could not check .agents/.gemini settings ({failure})"
        if paths:
            return f"refused to run gemini: the worktree changed .agents/.gemini settings ({', '.join(paths)})"
        try:
            links = sorted(entry.name for entry in worktree.path.iterdir()
                           if entry.name.casefold() in {".agents", ".gemini"} and entry.is_symlink())
        except OSError as exc:
            return f"refused to run gemini: could not check worktree settings symlinks ({exc})"
        if links:
            return f"refused to run gemini: agy would load settings symlinks ({', '.join(links)})"
        settings = (Path(self.engine_env().get("HOME", str(Path.home()))) /
                    ".gemini" / "antigravity-cli" / "settings.json")
        try:
            content = settings.read_bytes()
            problem = gemini_settings_problem(json.loads(content))
        except (OSError, ValueError) as exc:
            problem = f"could not read agy settings {settings} ({exc})"
        if problem:
            return f"refused to run gemini: {problem}; review settings before running `agy`"
        checked = {settings: hashlib.sha256(content).hexdigest()}
        problem = gemini_permission_files_problem(settings.parents[1], checked)
        if problem:
            return f"refused to run gemini: {problem}; review settings before running `agy`"
        self._permission_state.checked = checked
        return None

    def permissions_snapshot(self) -> dict[Path, str | None]:
        """Hash external permission state, retaining absent paths in the snapshot."""
        home = Path(self.engine_env().get("HOME", str(Path.home()))) / ".gemini"
        settings = home / "antigravity-cli" / "settings.json"
        projects = home / "config" / "projects"
        checked = getattr(self._permission_state, "checked", None)
        self._permission_state.checked = None
        snapshot = dict(checked) if checked is not None else {}

        def digest(path: Path) -> None:
            try:
                snapshot[path] = hashlib.sha256(path.read_bytes()).hexdigest()
            except FileNotFoundError:
                snapshot[path] = None

        if checked is not None:
            digest(home / "antigravity-cli" / "cache" / "projects.json")
            return snapshot
        for path in (settings, home / "antigravity-cli" / "hooks.json",
                     home / "antigravity-cli" / "cache" / "projects.json"):
            digest(path)
        try:
            entries = list(projects.iterdir())
        except FileNotFoundError:
            snapshot[projects] = None
        else:
            snapshot[projects] = "directory"
            for path in entries:
                if path.name == ".DS_Store" and stat.S_ISREG(path.lstat().st_mode):
                    continue
                digest(path)
        return snapshot

    def model_id(self, task: ControlTask) -> str:
        if not task.model:
            raise ValueError("set plugins.entries.hermes_control.settings.models.gemini.default "
                             "(e.g. gemini-4-argon) before running gemini")
        base, separator, suffix = task.model.rpartition("-")
        has_effort = bool(separator and suffix in self.efforts)
        effort = task.effort or (suffix if has_effort else "medium")
        if effort not in self.efforts:
            raise ValueError(f"gemini effort must be one of {', '.join(self.efforts)}")
        return f"{base if has_effort else task.model}-{effort}"

    def build_argv(self, task: ControlTask, *, cwd: Path) -> list[str]:
        argv = [self._path() or "agy", "-p", task.goal, "--output-format", "stream-json",
                "--sandbox", "--disable-slash-commands", "--mode",
                "accept-edits" if task.policy.write else "plan"]
        model = self.model_id(task)
        argv += ["--model", model]
        self._permission_state.model = model
        if task.resume_session:
            argv += ["--conversation", task.resume_session]
        assert_no_forbidden_flags(argv)
        return argv

    def parse_line(self, line: str) -> list[RawEvent]:
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            obj = None
        if not isinstance(obj, dict):
            return [RawEvent("error", {"message": "unparseable line", "line": line[:2000]})]
        model = getattr(self._permission_state, "model", None)
        metadata = {"model": model} if model is not None else {}
        kind = obj.get("event")
        if kind == "init":
            return [RawEvent("session", {"session_id": obj.get("conversation_id"), **metadata}, "init")]
        if kind == "step_update":
            step = obj.get("step_update") or {}
            step_type, state = step.get("step_type"), step.get("state")
            raw_kind = f"step_update/{step_type}"
            if step_type in ("user_input", "checkpoint"):
                return []
            if step_type == "agent_response":
                text = step.get("text_delta")
                return [RawEvent("text", {"text": text}, raw_kind)] if state == "DONE" and text else []
            if step_type == "tool":
                tool = step.get("tool_info") or {}
                tool_id = f"{step.get('conversation_id')}:{step.get('step_index')}"
                if state == "ACTIVE":
                    return [RawEvent("tool_start", {
                        "tool_id": tool_id, "name": step.get("tool_name"),
                        "input": tool.get("parameters") or {}}, raw_kind)]
                if state == "DONE":
                    error = tool.get("error")
                    message = error.get("message", "") if isinstance(error, dict) else error
                    return [RawEvent("tool_end", {
                        "tool_id": tool_id, "is_error": bool(error), "exit_code": None,
                        "content": tool.get("output") or message or ""}, raw_kind)]
                return [RawEvent("tool_end", {
                    "tool_id": tool_id, "is_error": True, "exit_code": None,
                    "content": f"agy reported tool state {state!r}"}, raw_kind)]
        if kind == "result":
            result = obj.get("result") or {}
            if result.get("status") == "SUCCESS":
                response = result.get("response") or ""
                events = [RawEvent("result", {
                    "ok": True, "usage": result.get("usage") or {}, "response": response,
                    "denied_actions": result.get("denied_actions") or [], **metadata}, "result")]
                if response:
                    events.append(RawEvent("text", {"text": response}, "result"))
                return events
            if result.get("status") == "ERROR":
                return [RawEvent("error", {"message": result.get("error") or
                                          "agy reported ERROR without a message", **metadata}, "result")]
            return [RawEvent("error", {
                "message": f"agy reported unknown result status {result.get('status')!r}"}, "result")]
        return [RawEvent("text", {"text": "", "raw": obj}, str(kind))]

    def extract_result(self, task: ControlTask, raws: Sequence[RawEvent],
                       diff: str) -> ControlResult:
        terminal = next((r for r in reversed(raws) if r.kind in ("result", "error")), None)
        session = next((r.payload.get("session_id") for r in raws if r.kind == "session"), None)
        text = next((r.payload.get("text") for r in reversed(raws)
                     if r.kind == "text" and r.payload.get("text")), "")
        ok = bool(terminal and terminal.kind == "result" and terminal.payload.get("ok"))
        denied = terminal.payload.get("denied_actions") if terminal else None
        if denied:
            actions = [str(action.get("action") or action.get("display_name") or "unknown")
                       if isinstance(action, dict) else str(action) for action in denied]
            causes = []
            commands_allowed = None
            for name in actions:
                action = name.casefold()
                if action in {"command", "run_command"}:
                    if commands_allowed is None:
                        commands_allowed = self.headless_commands_allowed()
                    cause = ("agy denied the command although toolPermission allows shell "
                             "commands; it likely needed a path outside the repo or "
                             "network access; keep the command inside the repo and offline"
                             if commands_allowed else
                             "agy's toolPermission in ~/.gemini/antigravity-cli/settings.json "
                             "does not allow shell commands headless; set it to "
                             "proceed-in-sandbox or keep the task free of commands")
                elif "url" in action or "web" in action:
                    cause = "network access; keep the goal offline"
                elif action in {"read", "write", "file", "read_file", "write_file",
                                "file_read", "file_write", "view_file", "write_to_file",
                                "edit_file", "delete_file", "replace_file_content",
                                "multi_replace_file_content", "list_directory"}:
                    cause = "a path outside the repo; keep the goal inside it"
                else:
                    cause = ("possible causes: agy's toolPermission does not allow shell "
                             "commands headless, a path outside the repo, or network access")
                causes.append(f"{name}: {cause}")
            names = ", ".join(actions)
            ok, reason = False, f"gemini refused {names} in headless mode: {'; '.join(causes)}"
        elif terminal is None:
            reason = "no result event"
        elif not ok:
            reason = "engine reported failure"
        else:
            reason = None if diff else EMPTY_DIFF_REASON
        return ControlResult(task.id, self.name, ok, text, session,
                             usage=(terminal.payload.get("usage") or {}) if terminal else {},
                             diff=diff, unproven_reason=reason,
                             failure_mode="guard" if denied else None)

    def limit_hit(self, raws: Sequence[RawEvent]) -> LimitHit | None:
        if any(r.kind == "result" and r.payload.get("ok") for r in raws):
            return None
        for raw in reversed(raws):
            message = str(raw.payload.get("message") or "")
            if raw.kind == "error" and "invalid model selection" in message.casefold():
                return None
        for raw in reversed(raws):
            message = str(raw.payload.get("message") or "")
            if raw.kind == "error" and (says_limit(message) or any(
                    phrase in message.casefold() for phrase in (
                        "resource_exhausted", "quota exhausted", "quota exceeded",
                        "quota reached", "http 429", "429 too many requests"))):
                return LimitHit(engine=self.name, message=message[:500],
                                resets_at=_resets_at(message))
        return None
