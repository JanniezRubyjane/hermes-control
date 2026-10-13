from pathlib import Path

from hermes_control.engines.base import ControlTask
from hermes_control.engines import claude
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.events import RawEvent
from hermes_control.policy import ExecPolicy, assert_no_forbidden_flags

FIXTURE = Path(__file__).parent / "fixtures" / "claude_tool_use.jsonl"


def _parse_fixture():
    adapter = ClaudeAdapter()
    raws = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raws.extend(adapter.parse_line(line))
    return raws


def _task(**kw):
    defaults = {"id": "t1", "goal": "add tests", "engine": "claude",
                "policy": ExecPolicy(write=True)}
    return ControlTask(**{**defaults, **kw})


def test_argv_is_headless_streaming_and_scoped_to_the_worktree(monkeypatch):
    monkeypatch.setattr(claude, "_claude_path", lambda: "/fake/claude")
    argv = ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
    assert argv[0] == "/fake/claude"
    assert "-p" in argv and "add tests" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert argv[argv.index("--add-dir") + 1] == "/wt"
    assert "--include-partial-messages" not in argv


def test_argv_carries_the_policy_and_no_forbidden_flags():
    argv = ClaudeAdapter().build_argv(_task(policy=ExecPolicy(write=False)), cwd=Path("/wt"))
    assert argv[argv.index("--permission-mode") + 1] == "plan"
    assert_no_forbidden_flags(argv)


def test_argv_loads_project_settings_and_limits_mcp_and_builtin_tools():
    argv = ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert "--strict-mcp-config" in argv
    tools = argv[argv.index("--tools") + 1].split(",")
    assert "Bash" in tools and "Edit" in tools
    assert argv.index("--append-system-prompt") < argv.index("--setting-sources")
    assert argv.index("--tools") < argv.index("--permission-mode")
    assert_no_forbidden_flags(argv)


def test_worker_contract_explains_refused_bash_commands():
    argv = ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
    contract = argv[argv.index("--append-system-prompt") + 1]
    assert "refused" in contract
    assert "do not retry" in contract
    assert "pre-approved" in contract
    assert "sudo" in contract and "env" in contract and "sh -c" in contract
    assert "syntax checks" in contract.lower() and "test runs" in contract.lower()
    assert "final message" in contract


def test_write_argv_preapproves_exact_check_script_as_one_tool():
    argv = ClaudeAdapter().build_argv(
        _task(policy=ExecPolicy(write=True, allow_tools=("Bash(/x/check.sh)",))),
        cwd=Path("/wt"))
    assert argv[argv.index("--permission-mode"):argv.index("--permission-mode") + 4] == [
        "--permission-mode", "acceptEdits", "--allowedTools", "Bash(/x/check.sh)"]
    assert_no_forbidden_flags(argv)


def test_argv_includes_model_only_when_requested():
    assert "--model" not in ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
    argv = ClaudeAdapter().build_argv(_task(model="claude-opus-5"), cwd=Path("/wt"))
    assert argv[argv.index("--model") + 1] == "claude-opus-5"


def test_fixture_maps_to_the_expected_event_kinds_in_order():
    assert [r.kind for r in _parse_fixture()] == [
        "session", "tool_start", "tool_end", "text", "result"]


def test_session_event_carries_the_session_id():
    assert _parse_fixture()[0].payload == {"session_id": "SESS-1"}


def test_tool_start_carries_id_name_and_input():
    tool_start = _parse_fixture()[1]
    assert tool_start.payload == {
        "tool_id": "toolu_01", "name": "Bash",
        "input": {"command": "echo hi", "description": "Print hi"}}


def test_tool_end_correlates_by_tool_id_and_reports_streams():
    tool_end = _parse_fixture()[2]
    assert tool_end.payload["tool_id"] == "toolu_01"
    assert tool_end.payload["is_error"] is False
    assert tool_end.payload["stdout"] == "hi"


def test_result_event_carries_cost_and_usage():
    result = _parse_fixture()[4]
    assert result.payload["ok"] is True
    assert result.payload["total_cost_usd"] == 0.0139
    assert result.payload["usage"] == {"input_tokens": 4, "output_tokens": 94}


def test_suppressed_lines_produce_no_events():
    adapter = ClaudeAdapter()
    assert adapter.parse_line('{"type": "system", "subtype": "status"}') == []
    assert adapter.parse_line('{"type": "stream_event", "event": {"type": "message_stop"}}') == []


def test_unknown_type_passes_through_with_raw_kind():
    events = ClaudeAdapter().parse_line('{"type": "brand_new_thing", "a": 1}')
    assert len(events) == 1
    assert events[0].kind == "text" and events[0].raw_kind == "brand_new_thing"


def test_unparseable_line_becomes_an_error_event_not_a_crash():
    events = ClaudeAdapter().parse_line("not json at all")
    assert [e.kind for e in events] == ["error"]


def test_valid_json_that_is_not_an_object_becomes_an_error_event():
    for valid_json in ["null", "42", '"x"', "[1,2]"]:
        events = ClaudeAdapter().parse_line(valid_json)
        assert len(events) == 1, f"Expected 1 event for {valid_json}, got {len(events)}"
        assert events[0].kind == "error", f"Expected error event for {valid_json}, got {events[0].kind}"


def test_failed_result_subtype_maps_to_error():
    events = ClaudeAdapter().parse_line(
        '{"type": "result", "subtype": "error_during_execution", "session_id": "S"}')
    assert [e.kind for e in events] == ["error"]


def test_extract_result_uses_last_text_and_session_id():
    raws = _parse_fixture()
    result = ClaudeAdapter().extract_result(_task(), raws, diff="--- a\n+++ b\n")
    assert result.ok is True
    assert result.text == "I ran `echo hi` and it printed `hi`."
    assert result.session_id == "SESS-1"
    assert result.diff == "--- a\n+++ b\n"
    assert result.unproven_reason is None


def test_extract_result_flags_an_empty_diff_as_unproven():
    result = ClaudeAdapter().extract_result(_task(), _parse_fixture(), diff="")
    assert result.unproven_reason == "empty diff"


def test_extract_result_is_not_ok_without_a_result_event():
    result = ClaudeAdapter().extract_result(_task(), [], diff="x")
    assert result.ok is False
    assert result.unproven_reason == "no result event"


def test_unknown_assistant_content_block_passes_through_with_raw_kind():
    """Same contract as the top-level unknown `type`: an unmapped block surfaces
    with `raw_kind` set instead of vanishing (design §4.1, §7). These were
    silently dropped, so a new content-block type would have left no trace at
    all in the run log."""
    events = ClaudeAdapter().parse_line(
        '{"type": "assistant", "message": {"content": ['
        '{"type": "brand_new_block", "value": 7}]}}')
    assert len(events) == 1
    assert events[0].kind == "text"
    assert events[0].raw_kind == "assistant/brand_new_block"
    assert events[0].payload["raw"] == {"type": "brand_new_block", "value": 7}


def test_known_assistant_blocks_are_unaffected_by_the_passthrough():
    events = ClaudeAdapter().parse_line(
        '{"type": "assistant", "message": {"content": ['
        '{"type": "text", "text": "hi"}, {"type": "thinking", "thinking": "hmm"}]}}')
    assert [e.kind for e in events] == ["text", "thinking"]


def test_a_refused_tool_whose_result_is_a_bare_string_becomes_a_tool_end():
    """Captured from the installed claude on 2026-09-23: under --permission-mode
    acceptEdits, `python3 -c ...` is refused and claude reports tool_use_result as
    a bare string instead of the {stdout, stderr} dict other results carry. The
    first real Hermes-Control run crashed on exactly this shape."""
    line = ('{"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", '
            '"content": "This command requires approval", "is_error": true, '
            '"tool_use_id": "toolu_02"}]}, "session_id": "SESS-1", '
            '"tool_use_result": "Error: This command requires approval"}')
    assert ClaudeAdapter().parse_line(line) == [RawEvent(
        kind="tool_end",
        payload={"tool_id": "toolu_02", "is_error": True,
                 "content": "This command requires approval",
                 "stdout": "", "stderr": "Error: This command requires approval"},
        raw_kind="user/tool_result")]


def test_argv_passes_effort_only_when_set():
    assert "--effort" not in ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
    argv = ClaudeAdapter().build_argv(_task(effort="high"), cwd=Path("/wt"))
    assert argv[argv.index("--effort") + 1] == "high"


def test_the_session_event_records_the_model_claude_actually_runs():
    """So the run log shows whether the model policy took effect. The line is the
    shape claude-code 2.1.280 prints for `--model sonnet`."""
    line = ('{"type":"system","subtype":"init","session_id":"S-9","model":"claude-sonnet-5",'
            '"cwd":"/wt","tools":[]}')

    assert ClaudeAdapter().parse_line(line)[0].payload == {"session_id": "S-9",
                                                           "model": "claude-sonnet-5"}
