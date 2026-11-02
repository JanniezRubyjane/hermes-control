"""Statistics, append-only storage, and report CLI checks without engines."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import report
from bench.grader import Grade
from bench.results import append_result, read_results


def row(task="a", solved=True, **overrides):
    return {"bench_sha": "abc", "suite": "suite", "task_id": task,
            "arm": "1-controller", "engine": "codex", "model": "test", "effort": "high",
            "rep": 1, "started": "2026-10-27T00:00:00Z", "ended": "2026-10-27T00:00:10Z",
            "wall_s": 10, "worker_s": 8, "verify_exit": 0, "verify_green": True,
            "tokens": {"input_tokens": 10, "output_tokens": 5}, "run_id": "test",
            **vars(Grade(solved=solved, hidden_exit=0 if solved else 1)), **overrides}


def test_wilson_known_values():
    lo, hi = report.wilson_ci(8, 10)
    assert lo == pytest.approx(.490162, abs=1e-6)
    assert hi == pytest.approx(.943318, abs=1e-6)
    assert report.wilson_ci(0, 0) == (0, 1)
    assert report.wilson_ci(0, 10)[0] == pytest.approx(0)
    assert report.wilson_ci(10, 10)[1] == pytest.approx(1)


def test_exact_mcnemar():
    assert report.mcnemar_exact(0, 6) == .03125
    assert report.mcnemar_exact(6, 0) == .03125
    assert report.mcnemar_exact(0, 0) == 1
    assert report.mcnemar_exact(3, 3) == 1


def test_false_green_counts_latency_tokens_and_voids():
    records = [row("a", wall_s=1, worker_s=1), row("b", False, wall_s=4, worker_s=2),
               row("c", False, wall_s=9, worker_s=3, verify_green=False,
                   gate_violations=["network", "secret-committed"], status="void")]
    group, = report.summarize(records)
    assert group["n"] == 2 and group["solved"] == 1
    assert group["false_green"] == 1 and group["false_green_rate"] == .5
    assert group["median_wall_s"] == 4 and group["p90_wall_s"] == 9
    assert group["median_worker_s"] == 2 and group["median_tokens"] == 15
    assert group["gate_violations"] == 2 and group["void"] == 1
    assert len(report.summarize(records + [row(engine="claude")])) == 2


def test_paired_majority_and_unmatched_tasks():
    baseline = [row(str(i)) for i in range(6)]
    current = [row(str(i), False) for i in range(6)]
    baseline += [row("unmatched")]
    current += [row("new")]
    comparison = report.compare(baseline, current)
    assert comparison["paired"] == 6 and comparison["lost"] == 6
    assert comparison["p_value"] == .03125 and comparison["regression"]
    assert not report.compare(current, baseline)["regression"]
    # Repetitions are votes for a task, not independent statistical pairs.
    comparison = report.compare([row("a")] * 5,
                                [row("a", False), row("a"), row("a")])
    assert comparison["paired"] == 1 and comparison["lost"] == 0
    assert not comparison["regression"]


def test_gate_and_latency_regression_thresholds():
    baseline = [row()]
    assert report.compare(baseline, [row(gate_violations=["network"])])["regression"]
    assert not report.compare(baseline, [row(wall_s=12.5)])["regression"]
    assert report.compare(baseline, [row(wall_s=12.51)])["regression"]
    assert not report.compare([], [row()])["regression"]
    assert report.compare(baseline, [row("unmatched", False)])["paired"] == 0


def test_results_append_malformed_count_and_report_cli(tmp_path, capsys):
    path = tmp_path / "nested/results.jsonl"
    append_result(path, row())
    append_result(path, row("b", False, quota_before={"codex": {"used": .2}},
                             quota_after={"codex": {"used": .3}}))
    with path.open("a") as stream:
        stream.write('{broken\n[]\n{"partial":\n')
    results = read_results(path)
    assert len(results) == 2 and results.malformed_lines == results.malformed_count == 3
    assert results[1]["quota_after"] == {"codex": {"used": .3}}
    assert report.main([str(path), "--baseline", str(path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["malformed_lines"] == 3
    assert not output["comparison"]["regression"]
    assert report.main([str(path)]) == 0
    assert "false-green" in capsys.readouterr().out


def test_append_accepts_grade_dataclass_and_validates_required_fields(tmp_path):
    record = row()
    for name in vars(Grade()):
        record.pop(name)
    record["grade"] = Grade(solved=True)
    path = tmp_path / "attempts.jsonl"
    append_result(path, record)
    assert read_results(path)[0]["solved"] is True
    with pytest.raises(ValueError, match="arm"):
        append_result(path, row(arm="wrong"))
    with pytest.raises(ValueError, match="tokens"):
        append_result(path, row(tokens=[]))


def test_only_graded_attempts_enter_rate_denominators():
    records = [row("solved"), row("red", False), row("void", status="void"),
               row("missing", status="not-graded"), row("unknown", status="other"),
               row("no-status", status=None)]
    group, = report.summarize(records)
    assert group["n"] == 2 and group["solved"] == 1
    assert group["solved_rate"] == group["false_green_rate"] == .5
    assert group["false_green"] == group["void"] == group["not_graded"] == 1
    assert group["wilson_ci"] == report.wilson_ci(1, 2)
    assert report.compare([row()], [row(status="void")])["paired"] == 0


def test_no_graded_attempts_have_unknown_rates_and_cli_columns(tmp_path, capsys):
    records = [row(status="void"), row(status="not-graded")]
    group, = report.summarize(records)
    assert group["n"] == group["solved"] == group["false_green"] == 0
    assert group["solved_rate"] is group["solved_pct"] is group["false_green_rate"] is None
    assert group["wilson_ci"] == (0, 1)
    path = tmp_path / "results.jsonl"
    for record in records:
        append_result(path, record)
    assert report.main([str(path)]) == 0
    output = capsys.readouterr().out
    assert "gates void not-graded" in output and "?%" in output


def test_all_not_graded_lane_flags_regression_among_graded_lanes():
    baseline = [row(), row(engine="claude")]
    current = [row(status="not-graded"), row(status="not-graded", rep=2), row(engine="claude")]
    comparison = report.compare(baseline, current)
    groups = {g["engine"]: g for g in comparison["groups"]}
    assert groups["codex"]["ungraded"] and groups["codex"]["regression"]
    assert groups["codex"]["paired"] == 0
    assert not groups["claude"]["ungraded"] and not groups["claude"]["regression"]
    assert comparison["regression"] and not comparison["ungraded"]
    assert report.compare(baseline, current[:2])["ungraded"]
    assert not report.compare(baseline, [row(), row(status="not-graded")])["ungraded"]
    assert not report.compare(baseline, [])["ungraded"]
