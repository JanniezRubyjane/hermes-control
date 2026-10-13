from pathlib import Path

import pytest
import yaml

from hermes_control.engines.base import ControlTask
from hermes_control.engines.codex import CodexAdapter, read_reasoning_summary, save_reasoning_summary
from hermes_control.policy import ExecPolicy, assert_no_forbidden_flags

FIXTURE = Path(__file__).parent / "fixtures" / "codex_tool_use.jsonl"


def _parse_fixture():
    adapter = CodexAdapter()
    raws = []
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            raws.extend(adapter.parse_line(line))
    return raws


def _task(**kw):
    defaults = {"id": "t2", "goal": "add tests", "engine": "codex",
                "policy": ExecPolicy(write=True)}
    return ControlTask(**{**defaults, **kw})


def test_argv_is_headless_json_and_scoped_to_the_worktree():
    argv = CodexAdapter().build_argv(_task(), cwd=Path("/wt"))
    assert argv[:2] == ["codex", "exec"]
    assert "add tests" in argv
    assert "--json" in argv
    assert argv[argv.index("-C") + 1] == "/wt"


def test_argv_uses_the_sandbox_for_policy_and_no_forbidden_flags():
    write = CodexAdapter().build_argv(_task(), cwd=Path("/wt"))
    assert write[write.index("-s") + 1] == "workspace-write"
    read = CodexAdapter().build_argv(_task(policy=ExecPolicy(write=False)), cwd=Path("/wt"))
    assert read[read.index("-s") + 1] == "read-only"
    assert_no_forbidden_flags(write)
    assert_no_forbidden_flags(read)


def test_argv_keeps_the_git_repo_check_enabled():
    assert "--skip-git-repo-check" not in CodexAdapter().build_argv(_task(), cwd=Path("/wt"))


def test_argv_omits_the_deferred_output_flags():
    argv = CodexAdapter().build_argv(_task(), cwd=Path("/wt"))
    assert "-o" not in argv and "--output-schema" not in argv


def test_fixture_maps_to_the_expected_event_kinds_in_order():
    assert [r.kind for r in _parse_fixture()] == [
        "session", "text", "tool_start", "tool_end", "text", "result"]


def test_session_event_carries_the_thread_id():
    assert _parse_fixture()[0].payload == {"session_id": "THREAD-1"}


def test_tool_start_and_end_correlate_by_item_id():
    raws = _parse_fixture()
    assert raws[2].payload["tool_id"] == "item_1"
    assert raws[2].payload["input"] == {"command": "/bin/zsh -lc 'echo hi'"}
    assert raws[3].payload["tool_id"] == "item_1"
    assert raws[3].payload["is_error"] is False
    assert raws[3].payload["exit_code"] == 0
    assert raws[3].payload["content"] == "hi\n"


def test_nonzero_exit_code_marks_the_tool_end_as_an_error():
    events = CodexAdapter().parse_line(
        '{"type":"item.completed","item":{"id":"i9","type":"command_execution",'
        '"command":"false","aggregated_output":"","exit_code":1,"status":"completed"}}')
    assert events[0].payload["is_error"] is True


def test_result_event_carries_usage():
    assert _parse_fixture()[5].payload["usage"]["output_tokens"] == 133


def test_turn_failed_maps_to_error():
    events = CodexAdapter().parse_line('{"type":"turn.failed","error":{"message":"boom"}}')
    assert [e.kind for e in events] == ["error"]


def test_suppressed_and_unknown_lines():
    adapter = CodexAdapter()
    assert adapter.parse_line('{"type":"turn.started"}') == []
    unknown = adapter.parse_line('{"type":"brand.new"}')
    assert unknown[0].kind == "text" and unknown[0].raw_kind == "brand.new"


def test_unparseable_line_becomes_an_error_event_not_a_crash():
    assert [e.kind for e in CodexAdapter().parse_line("nope")] == ["error"]


@pytest.mark.parametrize("line", ["null", "42", '"a string"', "[1,2,3]"])
def test_valid_json_that_is_not_an_object_becomes_an_error_event(line):
    events = CodexAdapter().parse_line(line)
    assert len(events) == 1
    assert events[0].kind == "error"


def test_extract_result_takes_text_from_the_last_agent_message():
    result = CodexAdapter().extract_result(_task(), _parse_fixture(), diff="D")
    assert result.text == "hi"
    assert result.ok is True
    assert result.session_id == "THREAD-1"


def test_extract_result_leaves_text_empty_when_no_agent_message_arrived():
    adapter = CodexAdapter()
    raws = adapter.parse_line('{"type":"turn.completed","usage":{}}')
    assert adapter.extract_result(_task(), raws, diff="D").text == ""


def test_extract_result_flags_an_empty_diff_as_unproven():
    assert CodexAdapter().extract_result(
        _task(), _parse_fixture(), diff="").unproven_reason == "empty diff"


def test_extract_result_is_not_ok_without_a_turn_completed():
    result = CodexAdapter().extract_result(_task(), [], diff="x")
    assert result.ok is False and result.unproven_reason == "no result event"


def test_argv_passes_effort_as_a_config_override_only_when_set():
    """codex has no effort flag; `-c model_reasoning_effort=...` is its config key,
    TOML-quoted so the value reaches codex as a string."""
    assert 'model_reasoning_effort="low"' not in CodexAdapter().build_argv(
        _task(), cwd=Path("/wt"))
    argv = CodexAdapter().build_argv(_task(effort="low"), cwd=Path("/wt"))
    assert argv[-2:] == ["-c", 'model_reasoning_effort="low"']


def test_argv_limits_context_from_replies_and_tool_output():
    argv = CodexAdapter().build_argv(_task(model="codex-test", effort="low"), cwd=Path("/wt"))
    config = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-c"]
    assert config == [
        'model_verbosity="low"',
        "tool_output_token_limit=8000",
        'model_reasoning_summary="concise"',
        'model_reasoning_effort="low"',
    ]
    assert argv.index('model_reasoning_summary="concise"') < argv.index("-m")
    assert_no_forbidden_flags(argv)


def test_reasoning_summary_defaults_to_concise_when_unset():
    assert read_reasoning_summary() == "concise"
    assert 'model_reasoning_summary="concise"' in CodexAdapter().build_argv(_task(), cwd=Path("/wt"))


def test_argv_reads_the_saved_reasoning_summary_for_each_task(_isolated_hermes_home):
    save_reasoning_summary("none")
    first = CodexAdapter().build_argv(_task(), cwd=Path("/wt"))
    save_reasoning_summary("detailed")
    second = CodexAdapter().build_argv(_task(), cwd=Path("/wt"))

    config = yaml.safe_load((_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["entries"]["hermes_control"]["settings"]["reasoning_summary"] == "detailed"
    assert 'model_reasoning_summary="none"' in first
    assert 'model_reasoning_summary="detailed"' in second


def test_invalid_stored_reasoning_summary_falls_back_to_concise(_isolated_hermes_home):
    (_isolated_hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes_control:\n      settings:\n"
        "        reasoning_summary: loud\n", encoding="utf-8")

    assert read_reasoning_summary() == "concise"
    assert 'model_reasoning_summary="concise"' in CodexAdapter().build_argv(_task(), cwd=Path("/wt"))


def test_invalid_reasoning_summary_is_refused_with_allowed_values():
    with pytest.raises(ValueError, match="none.*concise.*detailed.*auto"):
        save_reasoning_summary("loud")
