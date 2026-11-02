"""Arm 0 runs only a local fake CLI, then the real commit grader."""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_bare as arm
from bench.grader import Grade
from bench.results import read_results
from hermes_control import limits, verify
from hermes_control.engines.base import ControlTask
from hermes_control.engines.registry import get_adapter
from hermes_control.policy import ExecPolicy

LANES = [{"engine": "codex", "model": "test-codex", "effort": "high"}]


@pytest.fixture
def suite(tmp_path):
    directory = tmp_path / "suite"
    directory.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("from answer import VALUE\nassert VALUE == 42\n")
    data = {"id": "fix", "source": "S", "kind": "bug", "policy": "write",
            "goal": "Create answer.py with VALUE = 42", "verify": "must-not-run",
            "hidden_dir": str(hidden), "hidden_cmd": f"{shlex.quote(sys.executable)} hidden.py",
            "protected_paths": [], "repo": {"builder": "shop"}}
    (directory / "task.json").write_text(json.dumps(data))
    return directory


@pytest.fixture
def fake_worker(tmp_path, monkeypatch):
    script = tmp_path / "engine.sh"
    script.write_text("#!/bin/sh\nprintf 'VALUE = 42\\n' > answer.py\n"
                      "printf '%s\\n' '{\"type\":\"turn.completed\",\"usage\":{\"input_tokens\":12,\"output_tokens\":3}}'\n")
    script.chmod(0o755)
    calls = []
    def argv(task, *, cwd):
        assert task.goal == "Create answer.py with VALUE = 42"
        assert task.verify is None and task.model == "test-codex" and task.effort == "high"
        assert task.policy.write
        calls.append(cwd)
        return [str(script)]
    monkeypatch.setattr(get_adapter("codex"), "build_argv", argv)
    monkeypatch.setattr(get_adapter("codex"), "engine_env", lambda: dict(os.environ))
    monkeypatch.setattr(verify, "find_sandbox", lambda: "fake")
    def hidden(command, checkout, *, codex_path):
        proc = subprocess.run(command, cwd=checkout, shell=True, capture_output=True, text=True)
        return verify.VerifyOutcome(command, proc.returncode == 0, proc.returncode,
                                    proc.stdout + proc.stderr, None)
    monkeypatch.setattr(verify, "run_verify", hidden)
    readings = []
    def usage(engine, *, refresh=False):
        assert refresh is False
        readings.append(engine)
        return limits.Usage(engine, windows=(limits.Window("5-hour", 10 + len(readings), None),),
                            account="private@example.invalid")
    monkeypatch.setattr(limits, "usage", usage)
    return script, calls, readings


def test_fake_cli_committed_and_graded(suite, fake_worker, tmp_path, monkeypatch):
    _, calls, readings = fake_worker
    original_grade = arm.grade
    authors = []
    def grade(spec, repo, commit, **kwargs):
        authors.append(arm.git(repo, "show", "-s", "--format=%an <%ae>|%cn <%ce>", commit))
        assert kwargs["hidden_snapshot"].manifest
        return original_grade(spec, repo, commit, **kwargs)
    monkeypatch.setattr(arm, "grade", grade)
    out = tmp_path / "results.jsonl"
    rows = arm.run_suite(suite, LANES, 2, out)
    assert len(rows) == len(calls) == 2
    assert all(r["arm"] == "0-bare" and r["solved"] and r["hidden_checked"] for r in rows)
    assert all(r["verify_green"] is None and r["verify_exit"] is None for r in rows)
    assert all(r["tokens"] == {"input_tokens": 12, "output_tokens": 3} for r in rows)
    assert rows[0]["quota_before"] == {"5-hour": 11}
    assert rows[0]["quota_after"] == {"5-hour": 12}
    assert readings == ["codex"] * 4
    assert authors == ["hc-bench <bench@localhost>|hc-bench <bench@localhost>"] * 2
    assert all(not p.exists() for p in calls)
    assert all(Path(r["transcript"]).is_file() for r in rows)
    assert "private@example.invalid" not in out.read_text()
    assert read_results(out) == rows


def test_dry_run_spends_and_builds_nothing(suite, tmp_path, monkeypatch, capsys):
    def forbidden(*a, **kw):
        pytest.fail("dry-run performed live work")
    for name in ("prepare_repo", "grade", "get_adapter", "run_cli"):
        monkeypatch.setattr(arm, name, forbidden)
    monkeypatch.setattr(limits, "usage", forbidden)
    out = tmp_path / "out"
    assert arm.main(["--suite", str(suite), "--lanes", json.dumps(LANES), "--reps", "3",
                     "--out", str(out), "--dry-run"]) == 0
    plans = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(plans) == 3 and all(p["arm"] == "0-bare" for p in plans)
    assert not out.exists() and not (out.parent / "evidence").exists()


def test_quota_stop_before_build(suite, tmp_path, monkeypatch):
    monkeypatch.setattr(arm, "prepare_repo", lambda *a: pytest.fail("quota must stop build"))
    out = tmp_path / "out"
    assert arm.run_suite(suite, LANES, 2, out) == []  # conftest supplies unknown usage
    assert not out.exists()


@pytest.mark.parametrize("engine", ["codex", "claude"])
def test_real_adapter_flags_preserved_without_report_guidance(engine, tmp_path):
    adapter = get_adapter(engine)
    task = ControlTask("id", "Only the goal", engine, ExecPolicy(write=True), model="model", effort="high")
    original = adapter.build_argv(task, cwd=tmp_path)
    argv = arm.bare_argv(adapter, task, tmp_path)
    expected = list(original)
    if engine == "claude":
        index = expected.index("--append-system-prompt")
        del expected[index:index + 2]
        assert "--permission-mode" in argv and "acceptEdits" in argv
    else:
        assert "-s" in argv and "workspace-write" in argv
    assert argv == expected and "Only the goal" in argv
    assert not any("REPORT" in item or "verify" in item for item in argv)


def test_cli_timeout_stops_and_grades_changed_files(suite, fake_worker, tmp_path):
    script, _, _ = fake_worker
    script.write_text("#!/bin/sh\nprintf 'VALUE = 42\\n' > answer.py\nsleep 20\n")
    row, = arm.run_suite(suite, LANES, 1, tmp_path / "out", timeout=1)
    assert row["timed_out"] and row["cli_exit"] != 0
    assert row["solved"] and row["tokens"] == {}


def test_hidden_leak_scanned_in_cli_transcript(suite, fake_worker, tmp_path):
    script, _, _ = fake_worker
    hidden = tmp_path / "hidden/hidden.py"
    hidden.write_text(hidden.read_text() + "# hc-bench-canary: UNIQUE-PRIVATE-MARKER\n")
    script.write_text(script.read_text() + "printf '%s\\n' 'UNIQUE-PRIVATE-MARKER'\n")
    row, = arm.run_suite(suite, LANES, 1, tmp_path / "out")
    assert not row["solved"] and row["status"] == "void"
    assert "hidden-leak" in row["gate_violations"]


def test_claude_records_last_rate_limit_report(suite, tmp_path, monkeypatch):
    adapter = get_adapter("claude")
    monkeypatch.setattr(adapter, "build_argv", lambda *a, **kw: ["offline"])
    reports = []
    monkeypatch.setattr(limits, "record_claude_report", lambda info, *, at: reports.append((info, at)))
    first, last = {"utilization": 0.2}, {"utilization": 0.91}
    def run(argv, repo, env, transcript, timeout):
        transcript.write_text("\n".join(json.dumps(event) for event in [
            {"type": "rate_limit_event", "rate_limit_info": first},
            {"type": "rate_limit_event", "rate_limit_info": last},
            {"type": "result", "subtype": "success", "usage": {"input_tokens": 5}}]))
        return 0, False
    monkeypatch.setattr(arm, "run_cli", run)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade())
    lane = {"engine": "claude", "model": "test", "effort": "high"}
    row, = arm.run_suite(suite, [lane], 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert reports and reports[0][0] == last and len(reports) == 1
    assert reports[0][1] > 0 and row["tokens"] == {"input_tokens": 5}


@pytest.mark.parametrize("engine", ["claude", "codex", "gemini"])
def test_readonly_passes_full_last_agent_message(suite, tmp_path, monkeypatch, engine):
    from hermes_control.engines.gemini import GeminiAdapter

    path = suite / "task.json"
    spec = json.loads(path.read_text())
    spec.update(policy="read-only", kind="readonly", verify="", expected_answer=["shop"])
    path.write_text(json.dumps(spec))
    answer = "Explanation in full.\nANSWER: shop\nRemaining explanation."
    adapter = GeminiAdapter() if engine == "gemini" else get_adapter(engine)
    monkeypatch.setattr(arm, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(adapter, "build_argv", lambda *a, **kw: ["offline"])
    if engine == "gemini":
        monkeypatch.setattr(adapter, "permissions_snapshot", lambda: {})
    if engine == "claude":
        messages = [{"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
                    for text in ["Preliminary commentary."]]
        messages.append({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Explanation in full.\n"},
            {"type": "text", "text": "ANSWER: shop\nRemaining explanation."}]}})
        messages.append({"type": "result", "subtype": "success"})
    elif engine == "codex":
        messages = [{"type": "item.completed", "item": {"type": "agent_message", "text": text}}
                    for text in ["Preliminary commentary.", answer]]
        messages.append({"type": "turn.completed"})
    else:
        messages = [{"event": "step_update", "step_update": {
            "step_type": "agent_response", "state": "DONE", "text_delta": "Preliminary commentary."}},
            {"event": "result", "result": {"status": "SUCCESS", "response": answer}}]
    def run(argv, repo, env, transcript, timeout):
        transcript.write_text("\n".join(json.dumps(event) for event in messages))
        return 0, False
    def grade(spec, repo, commit, **kwargs):
        assert kwargs["answer"] == answer
        return Grade(solved=True)
    monkeypatch.setattr(arm, "run_cli", run)
    monkeypatch.setattr(arm, "grade", grade)
    row, = arm.run_suite(suite, [{"engine": engine, "model": "test", "effort": "high"}],
                         1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["solved"]


@pytest.mark.parametrize("readonly,changed_settings", [(False, True), (True, False), (True, True)])
def test_gemini_settings_and_readonly_gates(suite, tmp_path, monkeypatch, readonly, changed_settings):
    from hermes_control.engines.gemini import GeminiAdapter

    if readonly:
        path = suite / "task.json"
        spec = json.loads(path.read_text())
        spec.update(policy="read-only", kind="readonly", verify="", expected_answer=["shop"])
        path.write_text(json.dumps(spec))
    adapter = GeminiAdapter()
    monkeypatch.setattr(arm, "get_adapter", lambda name: adapter)
    monkeypatch.setattr(adapter, "build_argv", lambda *a, **kw: ["offline"])
    states = iter([{Path("settings"): "before"},
                   {Path("settings"): "after" if changed_settings else "before"}])
    monkeypatch.setattr(adapter, "permissions_snapshot", lambda: next(states))
    alias = tmp_path / "linked-temp"
    actual = tmp_path / "actual-temp"
    actual.mkdir()
    alias.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(arm.tempfile, "tempdir", str(alias))
    def run(argv, repo, env, transcript, timeout):
        assert repo == repo.resolve()
        (repo / "answer.py").write_text("VALUE = 42\n")
        transcript.write_text(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "shop"}}))
        return 0, False
    monkeypatch.setattr(arm, "run_cli", run)
    monkeypatch.setattr(verify, "find_sandbox", lambda: "offline")
    monkeypatch.setattr(verify, "run_verify", lambda command, *a, **kw:
                        verify.VerifyOutcome(command, True, 0, "", None))
    row, = arm.run_suite(suite, [{"engine": "gemini", "model": "test", "effort": "high"}],
                         1, tmp_path / "out", quota_guard=lambda lane: True)
    assert not row["solved"] and row["status"] == "graded"
    assert ("engine-settings-changed" in row["gate_violations"]) is changed_settings
    assert ("readonly-wrote" in row["gate_violations"]) is readonly


def test_claude_freshness_cli_flags(suite, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *a, **kw: calls.append(kw))
    assert arm.main(["--suite", str(suite), "--lanes", json.dumps(LANES),
                     "--out", str(tmp_path / "out"), "--claude-max-age-min", "12",
                     "--allow-stale-claude"]) == 0
    guard = calls[0]["quota_guard"]
    assert guard.max_age == 720 and guard.allow_stale_claude


@pytest.mark.parametrize('failure_call', [1, 2])
def test_permissions_snapshot_error_is_attempt_guard(suite, tmp_path, monkeypatch, failure_call):
    from hermes_control.engines.gemini import GeminiAdapter
    adapter = GeminiAdapter()
    monkeypatch.setattr(arm, 'get_adapter', lambda name: adapter)
    monkeypatch.setattr(adapter, 'build_argv', lambda *a, **kw: ['offline'])
    calls, workers = [], []
    def snapshot():
        calls.append(1)
        if len(calls) == failure_call:
            raise PermissionError('unreadable settings')
        return {}
    def run(argv, repo, env, transcript, timeout):
        workers.append(1)
        transcript.write_text('')
        return 0, False
    def grade(spec, repo, commit, **kwargs):
        return Grade(gate_violations=kwargs['run_result']['gate_violations'])
    monkeypatch.setattr(adapter, 'permissions_snapshot', snapshot)
    monkeypatch.setattr(arm, 'run_cli', run)
    monkeypatch.setattr(arm, 'grade', grade)
    rows = arm.run_suite(suite, [{'engine': 'gemini', 'model': 'test', 'effort': 'high'}],
                         2, tmp_path / 'out', quota_guard=lambda lane: True)
    assert len(rows) == 2 and rows[0]['gate_violations'] == ['guard']
    assert len(workers) == (1 if failure_call == 1 else 2)
