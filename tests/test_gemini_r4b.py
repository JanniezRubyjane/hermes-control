"""Permission-aware Gemini prompts and denial causes, without a live agy."""

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_control import dispatch
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import RawEvent
from hermes_control.worker_report import REPORT_REQUEST
from test_gemini_r3 import task


@pytest.mark.parametrize("permission", [True, False, "missing", "raises"])
@pytest.mark.parametrize("entry", ["direct", "fallback", "run_task"])
def test_guidance_uses_resolved_adapter_without_reading_settings(monkeypatch, permission, entry):
    def no_settings(*args):
        pytest.fail("guidance read settings instead of using the resolved adapter")

    monkeypatch.setattr(GeminiAdapter, "headless_commands_allowed", no_settings)
    adapter = SimpleNamespace(name="gemini", model_id=lambda task: task.model)
    if permission != "missing":
        def allowed():
            if permission == "raises":
                raise OSError("permission unavailable")
            return permission
        adapter.headless_commands_allowed = allowed

    def check_goal(prepared):
        assert ("you may run the check command" in prepared.goal) is (permission is True)
        assert ("do not run any shell command" in prepared.goal) is (permission is not True)

    if entry == "direct":
        monkeypatch.setattr(dispatch, "get_adapter", no_settings)
        check_goal(dispatch.with_check_guidance(task(verify="check"), adapter=adapter))
    elif entry == "fallback":
        def resolve(name):
            assert name == "gemini"
            return adapter
        monkeypatch.setattr(dispatch, "get_adapter", resolve)
        check_goal(dispatch.with_check_guidance(task(verify="check")))
    else:
        monkeypatch.setattr(dispatch, "get_adapter", no_settings)
        result = object()

        def worker(prepared, **kwargs):
            assert kwargs["engine"] is adapter
            check_goal(prepared)
            return result

        monkeypatch.setattr(dispatch, "_run_task_engine", worker)
        assert dispatch.run_task(task(verify="check"), run_id="guidance", log=None,
                                 worktree=None, adapter=adapter) is result


@pytest.fixture(autouse=True)
def settings(tmp_path, monkeypatch):
    # engine_env HOME deliberately differs from process HOME.
    monkeypatch.setenv("HOME", str(tmp_path / "process-home"))
    monkeypatch.setattr(GeminiAdapter, "engine_env", lambda _: {"HOME": str(tmp_path)})
    monkeypatch.setattr(GeminiAdapter, "_path", lambda _: pytest.fail("agy probe"))
    path = tmp_path / ".gemini/antigravity-cli/settings.json"
    path.parent.mkdir(parents=True)
    return path


@pytest.mark.parametrize("permission,allowed", [
    ("proceed-in-sandbox", True), ("request-review", False), ("strict", False),
    (None, False),
])
def test_guidance_respects_headless_tool_permission(settings, permission, allowed):
    data = {"enableTerminalSandbox": True}
    if permission is not None:
        data["toolPermission"] = permission
    settings.write_text(json.dumps(data))
    assert GeminiAdapter().headless_commands_allowed() is allowed
    goal = dispatch.with_report_request(dispatch.with_check_guidance(
        task(verify="check"), adapter=GeminiAdapter())).goal
    assert REPORT_REQUEST in goal
    assert "Never skip, weaken or delete a test" in goal
    if allowed:
        assert "you may run the check command" in goal
        assert "tests that cover the files you change" in goal
        assert "run the full check command once" in goal
        assert "You must not run git: Hermes-Control commits and diffs for you." in goal
        assert "git does not work" not in goal
        assert "Read files in the line ranges you need" in goal
    else:
        assert "do not run any shell command, including tests and git" in goal
        assert "agy's settings deny commands in headless mode" in goal
        assert "a denied action ends the task" in goal
        assert "Hermes-Control runs the check after you finish" in goal
        assert "you may run" not in goal


@pytest.mark.parametrize("content", [
    None, "not json", "[]", "null", '"settings"',
    '{"enableTerminalSandbox": false, "toolPermission": "proceed-in-sandbox"}',
    '{"enableTerminalSandbox": true, "toolPermission": "unknown"}',
    '{"enableTerminalSandbox": true, "toolPermission": "proceed-in-sandbox", "hooks": {}}',
    '{"enableTerminalSandbox": true, "toolPermission": "proceed-in-sandbox", '
    '"permissions": {"allow": ["command"]}}',
])
def test_bad_or_missing_settings_never_invite_commands(settings, content):
    if content is not None:
        settings.write_text(content)
    assert GeminiAdapter().headless_commands_allowed() is False
    goal = dispatch.with_check_guidance(task(verify="check"), adapter=GeminiAdapter()).goal
    assert "do not run any shell command, including tests and git" in goal
    assert "you may run" not in goal


def test_unreadable_settings_never_invite_commands(settings, monkeypatch):
    settings.write_text('{"enableTerminalSandbox": true, "toolPermission": "proceed-in-sandbox"}')
    original = Path.read_bytes

    def unreadable(path):
        if path == settings:
            raise PermissionError("unreadable settings")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert GeminiAdapter().headless_commands_allowed() is False
    assert "do not run any shell command" in dispatch.with_check_guidance(
        task(verify="check"), adapter=GeminiAdapter()).goal


def test_codex_guidance_is_unchanged():
    chosen = dataclasses.replace(task(verify="check"), engine="codex")
    assert dispatch.with_check_guidance(chosen).goal == (
        "fix it\n\nHow to work efficiently here: you may run the check command above "
        "yourself inside your sandbox at any time. While you work, run only the tests "
        "that cover the files you change (for example the matching test file), and run "
        "the full check command once when you think you are done; if it fails, fix and "
        "run it again. Read files in the line ranges you need rather than printing "
        "whole files, and read a whole file when you need its full context. Never skip, "
        "weaken or delete a test to make the check pass.")


def test_claude_gets_no_check_guidance():
    chosen = dataclasses.replace(task(verify="check"), engine="claude")
    assert dispatch.with_check_guidance(chosen) == chosen


def test_gemini_continuation_reads_files_without_git():
    continued = dispatch.with_continuation(task(resume_session="old"), previous="codex")
    assert continued.resume_session is None
    assert "git" not in continued.goal
    assert "unfinished edits are already in the files" in continued.goal
    assert "read the files you need, keep what is right, and finish" in continued.goal


def test_codex_continuation_is_unchanged():
    chosen = dataclasses.replace(task(resume_session="old"), engine="codex")
    continued = dispatch.with_continuation(chosen, previous="gemini")
    assert continued.resume_session is None
    assert continued.goal == (
        "fix it\n\nAn earlier worker (gemini) started this task and stopped before "
        "finishing, because it hit a usage limit. Its unfinished edits are already in this "
        "directory: check `git status` and `git diff` first, keep what is right, and finish "
        "the task.")


CAUSES = {
    "command": "toolPermission in ~/.gemini/antigravity-cli/settings.json does not allow shell "
               "commands headless; set it to proceed-in-sandbox or keep the task free of commands",
    "write_file": "a path outside the repo; keep the goal inside it",
    "read_file": "a path outside the repo; keep the goal inside it",
    "read_url": "network access; keep the goal offline",
    "mystery": "possible causes",
    "profile": "possible causes",
}


@pytest.mark.parametrize("actions", [
    ["command"], ["write_file"], ["read_file"], ["read_url"], ["mystery"], ["profile"],
    ["command", "write_file", "read_url", "mystery"],
])
def test_denial_names_each_real_cause(actions):
    result = GeminiAdapter().extract_result(task(), [RawEvent("result", {
        "ok": True, "denied_actions": [{"action": action} for action in actions],
    })], "diff")
    assert result.ok is False
    assert result.failure_mode == "guard"
    reason = result.unproven_reason
    assert reason.startswith(f"gemini refused {', '.join(actions)} in headless mode:")
    for action in actions:
        assert f"{action}:" in reason
        assert CAUSES[action] in reason
    if actions == ["command"]:
        assert "outside the repo" not in reason and "network" not in reason
    if actions == ["write_file"]:
        assert "toolPermission" not in reason and "network" not in reason
    if actions == ["read_url"]:
        assert "toolPermission" not in reason and "outside the repo" not in reason
    if any(CAUSES[action] == "possible causes" for action in actions):
        for cause in ("toolPermission", "outside the repo", "network access"):
            assert cause in reason


def test_command_denial_under_allowed_commands_blames_the_sandbox_not_the_setting(settings):
    settings.write_text(json.dumps({"enableTerminalSandbox": True,
                                    "toolPermission": "proceed-in-sandbox"}))
    result = GeminiAdapter().extract_result(task(), [RawEvent("result", {
        "ok": True, "denied_actions": [{"action": "command"}],
    })], "diff")
    reason = result.unproven_reason
    assert result.failure_mode == "guard"
    assert reason.startswith("gemini refused command in headless mode:")
    assert "set it to proceed-in-sandbox" not in reason
    assert "outside the repo" in reason and "network" in reason


def test_planner_note_explains_all_denial_classes():
    note = GeminiAdapter.planner_note
    for text in ("toolPermission", "proceed-in-sandbox", "path outside the repo", "network",
                 "ends the task", "failure_mode: guard", "change the goal, the agy setting or "
                 "the engine, never retry unchanged"):
        assert text in note
