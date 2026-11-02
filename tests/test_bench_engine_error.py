"""Harness failure classification with adapter events and isolated planner homes."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_bare, arm_full, report
from bench.grader import Grade
from bench.results import short_error
from hermes_control.engines.registry import get_adapter
from hermes_control.events import LimitHit


MESSAGE = "exceeded retry limit, last status: 429 Too Many Requests"
FAILED = [{"type": "error", "message": MESSAGE},
          {"type": "turn.failed", "error": {"message": MESSAGE}}]
LANE = {"engine": "codex", "model": "test", "effort": "high"}


@pytest.fixture
def suite(tmp_path):
    directory = tmp_path / "suite"
    directory.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "check.py").write_text("assert False\n")
    (directory / "task.json").write_text(json.dumps({
        "id": "fix", "source": "S", "kind": "bug", "policy": "write",
        "goal": "Fix the fixture", "verify": "true", "hidden_dir": str(hidden),
        "hidden_cmd": "python check.py", "protected_paths": [],
        "repo": {"builder": "shop"}}))
    return directory


@pytest.mark.parametrize("events,solved,status,timed_out,expected", [
    (FAILED, False, "graded", False, "engine-error"),
    ([{"type": "item.completed", "item": {"type": "agent_message", "text": MESSAGE}},
      {"type": "turn.completed"}], False, "graded", False, "graded"),
    (FAILED, True, "graded", False, "graded"),
    (FAILED, False, "void", False, "void"),
    (FAILED, False, "not-graded", False, "not-graded"),
    (FAILED, False, "graded", True, "graded"),
    (FAILED + [{"type": "turn.completed"}], False, "graded", False, "graded"),
])
def test_bare_event_classification(suite, tmp_path, monkeypatch, events, solved,
                                   status, timed_out, expected):
    def run(argv, repo, env, transcript, timeout):
        transcript.write_text("\n".join(map(json.dumps, events)))
        return 1, timed_out
    monkeypatch.setattr(arm_bare, "run_cli", run)
    monkeypatch.setattr(arm_bare, "grade", lambda *a, **kw: Grade(solved=solved, status=status))
    row, = arm_bare.run_suite(suite, [LANE], 1, tmp_path / "out",
                             quota_guard=lambda lane: True)
    assert row["status"] == expected and row["solved"] == solved
    if expected == "engine-error":
        assert row["reason"].startswith("engine error: exceeded retry limit")
        assert MESSAGE in Path(row["transcript"]).read_text()
    else:
        assert row["reason"] is None


def test_adapter_limit_message_takes_priority(tmp_path, monkeypatch):
    transcript = tmp_path / "cli.jsonl"
    transcript.write_text(json.dumps(FAILED[-1]))
    adapter = get_adapter("codex")
    def limit(raws):
        assert raws[-1].kind == "error"
        return LimitHit(engine="codex", message="adapter limit")
    monkeypatch.setattr(adapter, "limit_hit", limit)
    assert arm_bare.engine_error(adapter, transcript) == "adapter limit"


def test_short_error():
    assert short_error("  first\n\tsecond   third  ") == "first second third"
    assert short_error("abcde", 5) == "abcde"
    assert short_error("abcdef", 5) == "ab..."
    assert short_error("x" * 500) == "x" * 297 + "..."
    assert short_error("abcd", 2) == ".."


def test_report_engine_errors_outside_denominator(tmp_path, capsys):
    rows = [{"arm": "0-bare", "engine": "codex", "status": status,
             "solved": status == "graded"} for status in
            ("graded", "engine-error", "void", "not-graded")]
    summary, = report.summarize(rows)
    assert summary["n"] == summary["solved"] == summary["engine_error"] == 1
    assert summary["solved_rate"] == 1
    path = tmp_path / "results.jsonl"
    path.write_text("\n".join(map(json.dumps, rows)))
    assert report.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "void not-graded engine-error" in output
    assert "| 0 1 1 1" in output


@pytest.mark.parametrize("body", ["content", "text", "message"])
@pytest.mark.parametrize("logs,footer", [(False, True), (False, False), (True, True)])
def test_full_refused_dispatch(suite, tmp_path, monkeypatch, body, logs, footer):
    error = "refused\n   " + "too many tasks " * 80
    home = tmp_path / "eval-home"
    home.mkdir()
    monkeypatch.setattr(arm_full, "create_eval_home", lambda *a, **kw: home)
    def run(command, repo, home, timeout):
        with sqlite3.connect(home / "state.db") as db:
            db.execute("CREATE TABLE sessions (id TEXT)")
            db.execute("INSERT INTO sessions VALUES ('planner')")
            db.execute(f"CREATE TABLE messages (session_id TEXT, role TEXT, tool_name TEXT, {body} TEXT)")
            db.execute("INSERT INTO messages VALUES (?, 'tool', 'control_dispatch', ?)",
                       ("planner", json.dumps({"run_id": "refused", "run_log": "unused",
                                               "error": error, "tasks": []})))
            db.execute("INSERT INTO messages VALUES ('other', 'tool', 'control_dispatch', ?)",
                       (json.dumps({"error": "unrelated" if footer else error}),))
        if logs:
            (home / "control").mkdir()
            (home / "control/run-refused.jsonl").write_text(json.dumps({
                "kind": "plan", "run_id": "refused", "payload": {"tasks": [],
                "planner": {"session_id": "planner"}}}) + "\n")
        return 0, "Session: planner\n" if footer else "", "", False
    monkeypatch.setattr(arm_full, "run_hermes", run)
    monkeypatch.setattr(arm_full, "grade", lambda *a, **kw: Grade())
    row, = arm_full.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["status"] == "not-graded" and not row["solved"]
    assert row["reason"] == short_error(f"dispatch error: {error}")
    assert len(row["reason"]) == 300 and row["reason"].endswith("...")
    evidence = Path(row["transcript"]).parent
    assert json.loads((evidence / "dispatch-error.json").read_text())["error"] == error
    assert not home.exists()


def test_controller_dispatch_reason_and_print_are_short(suite, tmp_path, monkeypatch, capsys):
    from bench import arm_controller
    from hermes_control import dispatch

    error = "refused\n" + "details " * 100
    monkeypatch.setattr(dispatch, "dispatch", lambda *a, **kw: {"error": error, "tasks": []})
    monkeypatch.setattr(arm_controller, "grade", lambda *a, **kw: Grade())
    row, = arm_controller.run_suite(suite, [LANE], 1, tmp_path / "out",
                                   quota_guard=lambda lane: True)
    expected = short_error(f"dispatch error: {error}")
    assert row["status"] == "not-graded" and row["reason"] == expected
    assert capsys.readouterr().out.strip() == expected


def test_full_merge_reason_is_short_and_evidence_complete(suite, tmp_path, monkeypatch):
    import subprocess

    home = tmp_path / "eval-home"
    home.mkdir()
    error = "fatal:\n" + "merge failed " * 100
    monkeypatch.setattr(arm_full, "create_eval_home", lambda *a, **kw: home)
    monkeypatch.setattr(arm_full, "run_hermes", lambda *a: (0, "", "", False))
    monkeypatch.setattr(arm_full, "collect_runs", lambda home: (
        [{"id": "write", "policy": "write", "run_id": "run"}], ["run"], [], []))
    def fail(*a):
        raise subprocess.CalledProcessError(128, "merge-tree", stderr=error)
    monkeypatch.setattr(arm_full, "grading_commit", fail)
    monkeypatch.setattr(arm_full, "grade", lambda *a, **kw: Grade())
    row, = arm_full.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["status"] == "not-graded"
    assert row["reason"] == short_error(f"planner tasks merge failed: {error}")
    assert (Path(row["transcript"]).parent / "merge-error.txt").read_text() == error
