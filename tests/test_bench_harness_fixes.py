"""Regressions from the 2026-10-28 benchmark runs."""

import pytest

from test_bench_replay import history
from test_bench_replay_goals import add_merge
from test_bench_arm_controller import suite
from bench import arm_controller, replay
from bench.grader import Grade
from bench.spec import load_spec
from hermes_control import dispatch


PREAMBLE = "Implement every part below in this one change; none of them is in the repository yet."


@pytest.mark.parametrize("parts", [1, 2])
def test_replay_goal_preamble_only_for_multiple_parts(history, tmp_path, parts):
    repo, _, _ = history
    messages = ["Fix recovery", "Add a gate"][:parts]
    task_id = add_merge(repo, messages)
    out = tmp_path / "extracted"
    replay.extract(repo, out)
    spec = load_spec(out / task_id / "spec.json")
    if parts == 1:
        assert spec.goal == "Fix recovery\n"
        assert PREAMBLE not in spec.goal
    else:
        assert spec.goal == (PREAMBLE + "\n\nPart 1 of 2:\nFix recovery\n\n\n"
                             "Part 2 of 2:\nAdd a gate\n")
    assert spec.kind == "bug"


@pytest.mark.parametrize("log", [None, "nonexistent.jsonl"])
def test_controller_dispatch_error_is_reason_and_printed_once_per_attempt(
        suite, tmp_path, monkeypatch, capsys, log):
    error = "task 'p': codex model 'gpt-6-luna' is not allowed; choose one of test-codex"
    calls = []

    def reject(*args, **kwargs):
        calls.append(kwargs["run_id"])
        result = {"error": error, "tasks": []}
        if log:
            result["run_log"] = str(tmp_path / log)
        return result

    monkeypatch.setattr(dispatch, "dispatch", reject)
    monkeypatch.setattr(arm_controller, "grade", lambda *a, **kw: Grade(solved=True))
    lanes = [{"engine": "codex", "model": "gpt-6-luna", "effort": "high"}]
    rows = arm_controller.run_suite(suite, lanes, 2, tmp_path / "rows.jsonl",
                                    quota_guard=lambda lane: True)
    assert len(rows) == len(calls) == 2
    for row in rows:
        assert row["status"] == "not-graded" and not row["solved"]
        assert row["reason"] == f"dispatch error: {error}"
    assert capsys.readouterr().out.splitlines().count(f"dispatch error: {error}") == 2
