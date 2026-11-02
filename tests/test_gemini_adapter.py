import dataclasses
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_control import agy_quota, dispatch, limits, model_catalog
from hermes_control.engines import base, gemini, registry
from hermes_control.engines.base import ControlTask, EngineInfo
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import ControlResult, LimitHit, RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worktree import TaskWorktree


# Fixture paths, conversation IDs, timings and token counts are synthetic.
FIXTURES = Path(__file__).parent / "fixtures"
EDIT = "00000000-0000-4000-8000-000000000003"
DENIED = "00000000-0000-4000-8000-000000000002"
ANSWER = "00000000-0000-4000-8000-000000000001"
EDIT_USAGE = {"input_tokens": 60, "output_tokens": 12, "thinking_tokens": 6,
              "cache_read_tokens": 0, "total_tokens": 72}
DENIED_USAGE = {"input_tokens": 10, "output_tokens": 2, "thinking_tokens": 1,
                "cache_read_tokens": 0, "total_tokens": 12}
ANSWER_USAGE = {"input_tokens": 10, "output_tokens": 1, "thinking_tokens": 0,
                "cache_read_tokens": 0, "total_tokens": 11}
COMMAND = {"CommandLine": "python3 -m unittest -q test_calc.py"}
FAIL_OUTPUT = (
    "======================================================================\r\n"
    "FAIL: test_add (test_calc.T)\r\n"
    "----------------------------------------------------------------------\r\n"
    "Traceback (most recent call last):\r\n"
    '  File "/path/to/repo/test_calc.py", line 6, in test_add\r\n'
    "    self.assertEqual(add(2, 3), 5)\r\n"
    "AssertionError: -1 != 5\r\n\r\n"
    "----------------------------------------------------------------------\r\n"
    "Ran 1 test in 0.000s\r\n\r\nFAILED (failures=1)\r\n")
PASS_OUTPUT = "----------------------------------------------------------------------\r\nRan 1 test in 0.000s\r\n\r\nOK\r\n"
EDIT_RESPONSE = (
    "I have fixed the bug in [`add`](file:///path/to/repo/calc.py#L1-L2) in "
    "[calc.py](file:///path/to/repo/calc.py) so it returns `a + b` instead of `a - b`.\n\n"
    "### Test Execution Output\n\nRunning `python3 -m unittest -q test_calc.py`:\n\n"
    "```\n----------------------------------------------------------------------\n"
    "Ran 1 test in 0.000s\n\nOK\n```\n")
CONFLICT = ('invalid model selection (--model "gemini-3.8-flash-medium" --effort "low"): '
            '--model gemini-3.8-flash-medium conflicts with --effort=low')


def start(session, index, name, parameters):
    return [RawEvent("tool_start", {"tool_id": f"{session}:{index}", "name": name,
                                    "input": parameters}, "step_update/tool")]


def end(session, index, content):
    return [RawEvent("tool_end", {"tool_id": f"{session}:{index}", "is_error": False,
                                  "exit_code": None, "content": content}, "step_update/tool")]


GOLDENS = {
    "edit": [
        [RawEvent("session", {"session_id": EDIT}, "init")], [], [],
        start(EDIT, 2, "run_command", COMMAND), end(EDIT, 2, FAIL_OUTPUT), [],
        start(EDIT, 4, "view_file", {"AbsolutePath": "/path/to/repo/calc.py"}),
        end(EDIT, 4, "3 lines, 32 bytes"), [],
        start(EDIT, 6, "view_file", {"AbsolutePath": "/path/to/repo/test_calc.py"}),
        end(EDIT, 6, "10 lines, 177 bytes"), [],
        start(EDIT, 8, "replace_file_content", {"TargetFile": "/path/to/repo/calc.py"}),
        end(EDIT, 8, ""), [], start(EDIT, 10, "run_command", COMMAND),
        end(EDIT, 10, PASS_OUTPUT),
        [RawEvent("text", {"text": "\n"}, "step_update/agent_response")],
        [RawEvent("result", {"ok": True, "usage": EDIT_USAGE, "response": EDIT_RESPONSE,
                             "denied_actions": []}, "result"),
         RawEvent("text", {"text": EDIT_RESPONSE}, "result")]],
    "denied": [
        [RawEvent("session", {"session_id": DENIED}, "init")], [], [],
        start(DENIED, 2, "run_command", COMMAND), end(DENIED, 2, ""),
        [RawEvent("result", {"ok": True, "usage": DENIED_USAGE, "response": "",
                             "denied_actions": [{"action": "command", "display_name": "RunCommand"}]},
                  "result")]],
    "conflict": [[RawEvent("error", {"message": CONFLICT}, "result")]],
    "answer": [
        [RawEvent("session", {"session_id": ANSWER}, "init")], [],
        [RawEvent("text", {"text": "\n"}, "step_update/agent_response")],
        [RawEvent("result", {"ok": True, "usage": ANSWER_USAGE, "response": "ok\n",
                             "denied_actions": []}, "result"),
         RawEvent("text", {"text": "ok\n"}, "result")]],
}


@pytest.fixture(autouse=True)
def no_real_agy(monkeypatch):
    # Each test must explicitly replace a probe/command; never execute host agy.
    monkeypatch.setattr(GeminiAdapter, "_path", lambda _: "/stub/agy")
    monkeypatch.setattr(gemini, "agy_command", lambda *a, **k: pytest.fail("real agy command"))


def task(**kwargs):
    kwargs.setdefault("model", "gemini-4-argon")
    return ControlTask("t", "fix it", "gemini", ExecPolicy(write=True), **kwargs)


def parse_fixture(name):
    return [GeminiAdapter().parse_line(line) for line in
            (FIXTURES / f"gemini_stream_{name}.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("name", GOLDENS)
def test_captured_agy_streams_equal_exact_events(name):
    assert parse_fixture(name) == GOLDENS[name]


@pytest.mark.parametrize("name,ok,text,session,usage,reason", [
    ("edit", True, EDIT_RESPONSE, EDIT, EDIT_USAGE, None),
    ("denied", False, "", DENIED, DENIED_USAGE,
     "gemini refused command in headless mode: command: agy's toolPermission in "
     "~/.gemini/antigravity-cli/settings.json does not allow shell commands headless; "
     "set it to proceed-in-sandbox or keep the task free of commands"),
    ("answer", True, "ok\n", ANSWER, ANSWER_USAGE, None),
    ("conflict", False, "", None, {}, "engine reported failure"),
])
def test_extract_captured_results(name, ok, text, session, usage, reason, monkeypatch):
    # The captured denial ran under request-review; never read the machine's real agy settings.
    monkeypatch.setattr(GeminiAdapter, "headless_commands_allowed", lambda _: False)
    raws = [raw for batch in parse_fixture(name) for raw in batch]
    assert GeminiAdapter().extract_result(task(), raws, "diff") == ControlResult(
        "t", "gemini", ok, text, session, usage=usage, diff="diff", unproven_reason=reason,
        failure_mode="guard" if name == "denied" else None)


def test_incomplete_and_empty_diff_are_unproven():
    adapter = GeminiAdapter()
    assert adapter.extract_result(task(), [], "").unproven_reason == "no result event"
    raws = [raw for batch in parse_fixture("answer") for raw in batch]
    assert adapter.extract_result(task(), raws, "").unproven_reason == "empty diff"


@pytest.mark.parametrize("line", ["not json", "[]", "null", "1"])
def test_unparseable_lines(line):
    assert GeminiAdapter().parse_line(line) == [
        RawEvent("error", {"message": "unparseable line", "line": line})]


def test_unknown_events_and_tool_errors():
    adapter = GeminiAdapter()
    assert adapter.parse_line('{"event":"future","x":1}') == [RawEvent(
        "text", {"text": "", "raw": {"event": "future", "x": 1}}, "future")]
    for error in ("denied", {"message": "denied"}):
        assert adapter.parse_line(json.dumps({"event": "step_update", "step_update": {
            "conversation_id": "abc", "step_index": 2, "state": "DONE", "step_type": "tool",
            "tool_info": {"error": error}}})) == [RawEvent("tool_end", {
                "tool_id": "abc:2", "is_error": True, "exit_code": None, "content": "denied"},
                "step_update/tool")]


@pytest.mark.parametrize("kind,state,text", [
    ("agent_response", "ACTIVE", "partial"), ("agent_response", "DONE", ""),
    ("user_input", "DONE", "user text"), ("checkpoint", "DONE", "checkpoint"),
])
def test_nonfinal_fragments_are_suppressed(kind, state, text):
    assert GeminiAdapter().parse_line(json.dumps({"event": "step_update", "step_update": {
        "step_type": kind, "state": state, "text_delta": text}})) == []


def test_limit_detection_only_on_unsuccessful_stream():
    adapter = GeminiAdapter()
    error = RawEvent("error", {"message": "Quota exceeded; try later"}, "result")
    assert adapter.limit_hit([error]) == LimitHit("gemini", "Quota exceeded; try later")
    assert adapter.limit_hit([RawEvent("result", {"ok": True}), error]) is None
    assert adapter.limit_hit([RawEvent("error", {"message": "bad model"})]) is None


@pytest.mark.parametrize("write", [True, False])
@pytest.mark.parametrize("model,effort", [("gemini-4-argon", None), ("gemini-4-argon", "high"),
                                          ("gemini-3.8-flash-medium", "low")])
@pytest.mark.parametrize("resume", [None, "conversation-id"])
def test_argv_exactly_matches_probed_contract(write, model, effort, resume):
    chosen = dataclasses.replace(task(model=model, effort=effort, resume_session=resume),
                                 policy=ExecPolicy(write=write))
    expected = ["/stub/agy", "-p", "fix it", "--output-format", "stream-json", "--sandbox",
                "--disable-slash-commands", "--mode", "accept-edits" if write else "plan"]
    expected += ["--model", "gemini-3.8-flash-low" if model == "gemini-3.8-flash-medium"
                 else f"gemini-4-argon-{effort or 'medium'}"]
    if resume:
        expected += ["--conversation", resume]
    assert GeminiAdapter().build_argv(chosen, cwd=Path("/wt")) == expected
    assert GeminiAdapter.efforts == ("low", "medium", "high")
    assert GeminiAdapter.sign_in_command == "agy"
    assert GeminiAdapter.resumes_sessions is True


@pytest.mark.parametrize("installed,version,status", [(True, "1.3.5\n", 0),
                                                     (False, "", 0), (True, "", 1)])
def test_probe(installed, version, status, monkeypatch):
    monkeypatch.setattr(GeminiAdapter, "_path", lambda _: "/stub/agy" if installed else None)
    command = Mock(return_value=subprocess.CompletedProcess([], status, version, ""))
    monkeypatch.setattr(gemini, "agy_command", command)
    assert GeminiAdapter().probe() == (EngineInfo("gemini", "1.3.5", "/stub/agy")
                                       if installed and status == 0 else None)
    if installed:
        assert command.call_args.args == ("/stub/agy", ["--version"])
        assert command.call_args.kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"


@pytest.mark.parametrize("stdout,status,expected", [
    ("Fetching available models...\ngemini-3.8-flash-medium\tGemini Flash\n", 0, None),
    ("Fetching available models...\n", 0, "agy"),
    ("OAuth URL\nWaiting for authentication (timeout 60s)", 0, "agy"),
    ("model\tModel", 1, "agy"),
])
def test_auth_requires_success_and_a_model_line(stdout, status, expected, monkeypatch):
    command = Mock(return_value=subprocess.CompletedProcess([], status, stdout, ""))
    monkeypatch.setattr(gemini, "agy_command", command)
    problem = GeminiAdapter().auth_problem(EngineInfo("gemini", "1.3.5", "/stub/agy"))
    assert problem is None if expected is None else "run `agy`" in problem
    assert command.call_args.args == ("/stub/agy", ["models"])


@pytest.mark.parametrize("failure", [OSError("cannot start"), subprocess.TimeoutExpired("agy", 20)])
def test_auth_and_probe_failures_are_bounded_and_actionable(monkeypatch, failure):
    monkeypatch.setattr(gemini, "agy_command", Mock(side_effect=failure))
    assert GeminiAdapter().probe() is None
    assert "run `agy`" in GeminiAdapter().auth_problem(EngineInfo("gemini", "1", "/stub/agy"))


# Preserve functions replaced by the safety fixture for tests of their internals.
AGY_COMMAND = gemini.agy_command
AGY_PATH = GeminiAdapter._path


@pytest.mark.parametrize("on_path", [True, False])
def test_path_uses_engine_path_then_documented_install(monkeypatch, tmp_path, on_path):
    monkeypatch.setattr(base, "engine_env", lambda: {"HOME": str(tmp_path), "PATH": "/engine/bin"})
    which = Mock(side_effect=["/engine/bin/agy"] if on_path else [None, str(tmp_path / ".local/bin/agy")])
    monkeypatch.setattr(gemini.shutil, "which", which)
    assert AGY_PATH(GeminiAdapter()) == ("/engine/bin/agy" if on_path else str(tmp_path / ".local/bin/agy"))
    assert which.call_args_list[0].args == ("agy",)
    assert which.call_args_list[0].kwargs == {"path": "/engine/bin"}
    if not on_path:
        assert which.call_args_list[1].args == (str(tmp_path / ".local/bin/agy"),)


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("cleanup_error", [False, True])
def test_agy_commands_own_and_stop_their_process_group(monkeypatch, timeout, cleanup_error):
    process = SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO(), returncode=0,
                              wait=Mock(), communicate=Mock(return_value=("model\tModel\n", "")))
    if timeout:
        process.communicate.side_effect = subprocess.TimeoutExpired("agy", 20)
    popen = Mock(return_value=process)
    stopped = Mock(side_effect=OSError("gone") if cleanup_error else None)
    monkeypatch.setattr(gemini.subprocess, "Popen", popen)
    monkeypatch.setattr(gemini, "group_popen_kwargs", lambda: {"start_new_session": True})
    monkeypatch.setattr(gemini, "stop_group", stopped)
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            AGY_COMMAND("/stub/agy", ["models"], env={"AGY_CLI_DISABLE_AUTO_UPDATE": "1"})
    else:
        result = AGY_COMMAND("/stub/agy", ["models"], env={"AGY_CLI_DISABLE_AUTO_UPDATE": "1"})
        assert (result.returncode, result.stdout) == (0, "model\tModel\n")
    assert popen.call_args.args == (["/stub/agy", "models"],)
    assert popen.call_args.kwargs == {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace",
        "env": {"AGY_CLI_DISABLE_AUTO_UPDATE": "1"}, "start_new_session": True}
    process.communicate.assert_called_once_with(timeout=20)
    stopped.assert_called_once_with(process, grace=0.2)
    assert process.stdout.closed and process.stderr.closed


def test_dispatch_uses_adapter_environment(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "engine_env", lambda: {"PATH": "/safe/bin", "KEPT": "yes"})
    captured = {}

    def cannot_start(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("test start failure")

    monkeypatch.setattr(dispatch.subprocess, "Popen", cannot_start)
    result = dispatch._run_task_engine(task(), run_id="env", log=RunLog("env"),
                                      worktree=TaskWorktree(tmp_path, "b", tmp_path, "base"),
                                      engine=GeminiAdapter(), timeout=1)
    assert result.failure_mode == "start-failure"
    assert captured["env"] == {"PATH": "/safe/bin", "KEPT": "yes", "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
                               "HERMES_CONTROL_CHECK_COPY": "1"}
    assert base.EngineAdapter.engine_env(GeminiAdapter()) == {"PATH": "/safe/bin", "KEPT": "yes"}


def test_unknown_usage_never_holds_work(monkeypatch):
    stubbed = limits.Usage(
        "gemini", report_only=True, error="stubbed: tests never read the real Antigravity app")
    monkeypatch.setattr(agy_quota, "fetch_quota", lambda: stubbed)
    monkeypatch.setitem(limits._FETCHERS, "gemini", limits._gemini)
    assert limits.usage("gemini") == stubbed
    assert limits.engine_hold("gemini", caps={"gemini": 1}) is None
    assert "gemini" not in limits.parse_caps(None)


def test_catalog_parses_only_model_rows_and_uses_safe_command(monkeypatch):
    monkeypatch.setattr(registry, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    stdout = "Fetching available models...\ngemini-3.8-flash-medium\tGemini 3.8 Flash\nopus\tClaude Opus\n"
    expected = [model_catalog.CatalogModel("gemini-3.8-flash-medium", "Gemini 3.8 Flash"),
                model_catalog.CatalogModel("opus", "Claude Opus")]
    assert model_catalog.parse_gemini_catalog(stdout) == expected
    command = Mock(return_value=subprocess.CompletedProcess([], 0, stdout, ""))
    monkeypatch.setattr(gemini, "agy_command", command)
    monkeypatch.setattr(GeminiAdapter, "probe", lambda _: EngineInfo("gemini", "1", "/stub/agy"))
    assert model_catalog._gemini() == {
        "source": "agy models (the catalog agy uses for your account)", "models": expected}
    assert command.call_args.args == ("/stub/agy", ["models"])
    assert command.call_args.kwargs["env"]["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"


@pytest.mark.parametrize("stdout,status", [("Fetching available models...", 0), ("x\tX", 1)])
def test_catalog_failures_ask_for_agy_sign_in(monkeypatch, stdout, status):
    monkeypatch.setattr(registry, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    monkeypatch.setattr(GeminiAdapter, "probe", lambda _: EngineInfo("gemini", "1", "/stub/agy"))
    monkeypatch.setattr(gemini, "agy_command", Mock(return_value=subprocess.CompletedProcess([], status, stdout, "")))
    with pytest.raises(RuntimeError, match="run `agy`"):
        model_catalog._gemini()


@pytest.mark.parametrize("error", [None, ""])
def test_error_result_without_message_is_actionable(error):
    result = {"status": "ERROR"}
    if error is not None:
        result["error"] = error
    assert GeminiAdapter().parse_line(json.dumps({"event": "result", "result": result})) == [
        RawEvent("error", {"message": "agy reported ERROR without a message"}, "result")]


@pytest.mark.parametrize("status", ["CANCELLED", "PARTIAL", "future", None])
def test_unknown_result_status_is_an_error(status):
    events = GeminiAdapter().parse_line(json.dumps({"event": "result", "result": {"status": status}}))
    assert len(events) == 1 and events[0].kind == "error"
    assert str(status) in events[0].payload["message"]


@pytest.mark.parametrize("state", ["ERROR", "CANCELLED", "future", None])
def test_unknown_tool_state_closes_active_tool_as_error(state):
    step = {"step_type": "tool", "conversation_id": "c", "step_index": 2, "state": "ACTIVE"}
    adapter = GeminiAdapter()
    active = adapter.parse_line(json.dumps({"event": "step_update", "step_update": step}))[0]
    step["state"] = state
    ended = adapter.parse_line(json.dumps({"event": "step_update", "step_update": step}))[0]
    assert active.kind == "tool_start" and ended.kind == "tool_end"
    assert ended.payload["tool_id"] == active.payload["tool_id"]
    assert ended.payload["is_error"] is True


@pytest.mark.parametrize("message", [
    "RESOURCE_EXHAUSTED", "resource_exhausted: exhausted", "QUOTA exceeded",
    "HTTP 429", "429 Too Many Requests", "http 429: retry later",
])
def test_google_limit_wording_is_case_insensitive(message):
    adapter = GeminiAdapter()
    errors = [RawEvent("error", {"message": message})]
    assert adapter.limit_hit(errors) == LimitHit("gemini", message)
    assert adapter.limit_hit([*errors, RawEvent("result", {"ok": True})]) is None
    for wording in ("RATE LIMIT", "Usage Limit"):
        assert adapter.limit_hit([RawEvent("error", {"message": wording})]) is not None


def test_gemini_process_cwd_resolves_symlinks(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    captured = {}

    def cannot_start(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("test start failure")

    monkeypatch.setattr(GeminiAdapter, "engine_env", lambda _: {"HOME": str(tmp_path)})
    monkeypatch.setattr(dispatch.subprocess, "Popen", cannot_start)
    dispatch._run_task_engine(task(), run_id="cwd", log=RunLog("cwd"),
                              worktree=TaskWorktree(link, "b", real, "base"),
                              engine=GeminiAdapter(), timeout=1)
    assert captured["cwd"] == str(real.resolve())


def test_registry_callers_with_gemini_disabled_and_enabled(tmp_path):
    import os
    import sys

    script = '''
import subprocess
from hermes_control import limits, model_catalog
from hermes_control.engines import gemini, registry
from hermes_control.engines.base import EngineInfo
from hermes_control.dashboard import plugin_api
from hermes_control import agy_quota

agy_quota.fetch_quota = lambda: limits.Usage(
    "gemini", report_only=True, error="stubbed: tests never read the real Antigravity app")

enabled = "gemini" in registry.ENGINE_NAMES
if enabled:
    assert isinstance(registry.get_adapter("gemini"), gemini.GeminiAdapter)
else:
    try:
        registry.get_adapter("gemini")
    except KeyError as exc:
        assert exc.args[0] == "unknown engine 'gemini'; known engines: claude, codex"
    else:
        raise AssertionError("disabled gemini adapter was returned")
for adapter in registry._ADAPTERS.values():
    adapter.probe = lambda: None
registry._ADAPTERS["gemini"].probe = lambda: EngineInfo("gemini", "1.3.5", "/stub/agy")
registry._ADAPTERS["gemini"].auth_problem = lambda info: None
gemini.agy_command = lambda *a, **k: subprocess.CompletedProcess([], 0, "model\\tModel\\n", "")
for name in ("claude", "codex"):
    model_catalog._FETCHERS[name] = lambda: {"source": "fake", "models": []}
assert set(model_catalog.available(refresh=True)) == set(registry.ENGINE_NAMES)
if enabled:
    assert model_catalog.available()["gemini"]["error"] is None
plugin_api.find_sandbox = lambda: None
assert [row["name"] for row in plugin_api.engines()["engines"]] == list(registry.ENGINE_NAMES)
assert set(limits.parse_caps(None)) == set(registry.ENGINE_NAMES)
for name in ("claude", "codex"):
    limits._FETCHERS[name] = lambda refresh=False: limits.Usage("fake", error="unknown")
assert set(plugin_api.get_limits()["engines"]) == set(registry.ENGINE_NAMES)
if enabled:
    assert limits.usage("gemini").error
print("registry callers passed")
'''
    for enabled in (False, True):
        home = tmp_path / str(enabled)
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps({"plugins": {"entries": {
            "hermes_control": {"settings": {"engines": {"gemini": {"enabled": enabled}}}}
        }}}))
        proc = subprocess.run([sys.executable, "-c", script], text=True, capture_output=True,
                              env={**os.environ, "HOME": str(home), "HERMES_HOME": str(home)},
                              timeout=20)
        assert proc.returncode == 0, proc.stderr
