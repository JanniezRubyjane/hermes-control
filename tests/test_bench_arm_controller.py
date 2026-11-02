"""Controller dispatch is always fake: no engines, network, or real Hermes home."""

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_controller as arm
from bench.grader import Grade, git
from bench.results import read_results
from bench.spec import load_suite
from hermes_control import dispatch
from hermes_control import verify
from hermes_control import limits


@pytest.fixture(autouse=True)
def readable_quota(monkeypatch, _isolated_hermes_home):
    report = _isolated_hermes_home / "control/usage-claude.json"
    report.parent.mkdir()
    report.write_text(json.dumps({"at": time.time(), "info": {}}))
    monkeypatch.setattr(limits, "usage", lambda engine, **kw: limits.Usage(
        engine, windows=(limits.Window("5-hour", 10, None), limits.Window("weekly", 20, None)),
        account="private-account@example.invalid"))


@pytest.fixture
def suite(tmp_path):
    directory = tmp_path / "suite"
    directory.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "check.py").write_text("assert True\n")
    data = {"id": "pricing", "source": "S", "kind": "bug", "policy": "write",
            "goal": "Fix double discount", "verify": "python -m pytest -q",
            "hidden_dir": str(hidden), "hidden_cmd": "python check.py",
            "protected_paths": ["tests/test_shop.py"], "repo": {"builder": "shop"}}
    (directory / "task.json").write_text(json.dumps(data))
    return directory


LANES = [{"engine": "codex", "model": "test-codex", "effort": "high"},
         {"engine": "claude", "model": "test-claude", "effort": "medium"}]


def test_dry_run_spends_nothing(suite, tmp_path, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not dispatch, build, grade, or read quota")
    monkeypatch.setattr(dispatch, "dispatch", forbidden)
    monkeypatch.setattr(arm, "prepare_repo", forbidden)
    monkeypatch.setattr(arm, "grade", forbidden)
    monkeypatch.setattr(limits, "usage", forbidden)
    out = tmp_path / "results.jsonl"
    assert arm.main(["--suite", str(suite), "--lanes", json.dumps(LANES), "--reps", "2",
                     "--out", str(out), "--dry-run"]) == 0
    plans = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(plans) == 4 and {p["rep"] for p in plans} == {1, 2}
    assert not out.exists()


def fake_dispatch(monkeypatch, tmp_path, calls):
    def fake(tasks, *, parent_cwd, run_id):
        task, = tasks
        repo = Path(parent_cwd)
        assert (repo / "shop/pricing.py").is_file()
        assert "hidden_dir" not in task and "hidden_cmd" not in task
        assert "hidden" not in json.dumps(task)
        assert task["why"] == "hc-bench arm 1 lane"
        assert task["verify"] == "python -m pytest -q" and task["policy"] == "write"
        calls.append((task, repo))
        sha = git(repo, "rev-parse", "HEAD")
        log = tmp_path / f"{run_id}.jsonl"
        events = [{"task_id": task["id"], "kind": "spawned", "ts": 10, "payload": {}},
                  {"task_id": "other", "kind": "result", "ts": 14, "payload": {"usage": {"input_tokens": 999}}},
                  {"task_id": task["id"], "kind": "result", "ts": 15,
                   "payload": {"usage": {"input_tokens": 100, "output_tokens": 20}}}]
        log.write_text("\n".join(json.dumps(e) for e in events) + "\n")
        return {"run_id": run_id, "run_log": str(log), "tasks": [
            {"task_id": task["id"], "commit": sha, "text": "Done",
             "engine": task["engine"], "model": task["model"], "effort": task["effort"],
             "verify": {"exit_code": 0, "passed": True}}]}
    monkeypatch.setattr(dispatch, "dispatch", fake)


def test_runner_records_pins_metrics_grades_and_quota(suite, tmp_path, monkeypatch):
    calls, grades = [], []
    fake_dispatch(monkeypatch, tmp_path, calls)
    def fake_grade(spec, repo, sha, **kwargs):
        assert kwargs["base_commit"] == sha
        assert kwargs["transcript_paths"]
        snapshot = kwargs["hidden_snapshot"]
        assert snapshot.directory != spec.hidden_dir
        assert (snapshot.directory / "check.py").read_text() == "assert True\n"
        assert snapshot.manifest
        grades.append(spec.id)
        return Grade(solved=False, hidden_exit=1)
    monkeypatch.setattr(arm, "grade", fake_grade)
    out = tmp_path / "results.jsonl"
    records = arm.run_suite(suite, LANES, 2, out)
    assert len(calls) == len(grades) == len(records) == 4
    assert all(not repo.exists() for _, repo in calls)
    assert {r["engine"] for r in records} == {"codex", "claude"}
    assert all(r["verify_green"] and not r["solved"] for r in records)
    assert all(r["tokens"] == {"input_tokens": 100, "output_tokens": 20} for r in records)
    assert all(r["worker_s"] == 5 and r["wall_s"] > 0 for r in records)
    assert all(r["quota_before"] == {"5-hour": 10, "weekly": 20} for r in records)
    assert len(read_results(out)) == 4
    assert len({r["run_id"] for r in records}) == 4
    assert all(isinstance(r["bench_dirty"], bool) and r["bench_sha"] for r in records)


def test_quota_false_stops_only_that_lane(suite, tmp_path, monkeypatch):
    calls, guards = [], []
    fake_dispatch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade(solved=True, hidden_exit=0))
    def guard(lane):
        guards.append(lane["engine"])
        return lane["engine"] != "codex"
    records = arm.run_suite(suite, LANES, 3, tmp_path / "out.jsonl", quota_guard=guard)
    assert [c[0]["engine"] for c in calls] == ["claude"] * 3
    assert guards == ["codex", "claude", "claude", "claude"]
    assert len(records) == 3


def test_quota_stop_after_one_attempt(suite, tmp_path, monkeypatch):
    calls = []
    fake_dispatch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade(solved=True, hidden_exit=0))
    records = arm.run_suite(suite, LANES[:1], 4, tmp_path / "out.jsonl",
                            quota_guard=lambda engine: len(calls) == 0)
    assert len(records) == 1


def test_replay_copied_to_fresh_repo(suite, tmp_path):
    spec, = load_suite(suite)
    origin = arm.prepare_repo(spec, tmp_path / "origin")
    (origin / ".gitattributes").write_text("shop/pricing.py filter=evil\n")
    git(origin, "add", ".gitattributes")
    git(origin, "commit", "-m", "plant filter attributes")
    base = git(origin, "rev-parse", "HEAD")
    smudge = tmp_path / "smudge-ran"
    git(origin, "config", "filter.evil.smudge", f"touch {shlex.quote(str(smudge))}; cat")
    git(origin, "config", "filter.evil.required", "true")
    marker = tmp_path / "hook-ran"
    hook = origin / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    hook.chmod(0o755)
    git(origin, "config", "core.fsmonitor", str(hook))
    worktrees = git(origin, "worktree", "list", "--porcelain")
    def metadata():
        return {p.relative_to(origin): p.stat().st_mtime_ns
                for p in (origin / ".git", *(origin / ".git").rglob("*"))}
    before = metadata()
    from dataclasses import replace
    spec = replace(spec, source="R", repo={"origin_repo": str(origin), "base_commit": base})
    replay = arm.prepare_repo(spec, tmp_path / "replay")
    assert (replay / ".git").is_dir()
    assert (replay / "shop/pricing.py").read_text() == (origin / "shop/pricing.py").read_text()
    assert git(replay, "rev-list", "--count", "HEAD") == "1"
    assert worktrees == git(origin, "worktree", "list", "--porcelain")
    assert metadata() == before
    assert not marker.exists()
    assert not smudge.exists()


def test_event_metrics_sum_workers_without_usage_snapshot_double_counting():
    events = [{"kind": "spawned", "ts": 1},
              {"kind": "usage", "ts": 2, "payload": {"input_tokens": 500}},
              {"kind": "result", "ts": 4, "payload": {"usage": {"input_tokens": 3}}},
              {"kind": "spawned", "ts": 6},
              {"kind": "result", "ts": 8, "payload": {"usage": {"input_tokens": 2}}}]
    assert arm.event_metrics(events) == {"worker_s": 5, "tokens": {"input_tokens": 5}}
    assert arm.event_metrics([]) == {"worker_s": None, "tokens": {}}


def test_readonly_without_commit_grades_base_answer(suite, tmp_path, monkeypatch):
    path = suite / "task.json"
    data = json.loads(path.read_text())
    data.update(policy="read-only", kind="readonly", verify="", expected_answer=["shop"])
    path.write_text(json.dumps(data))
    log = tmp_path / "run.jsonl"
    log.write_text('{"task_id": "pricing", "kind": "result"}\n')
    def fake(tasks, **kwargs):
        return {"run_log": str(log),
                "tasks": [{"task_id": "pricing", "commit": "", "text": "SHOP"}]}
    def run_hidden(command, checkout, *, codex_path):
        assert (checkout / "check.py").is_file()
        return verify.VerifyOutcome(command, True, 0, "", None)
    monkeypatch.setattr(dispatch, "dispatch", fake)
    monkeypatch.setattr(verify, "find_sandbox", lambda: "fake")
    monkeypatch.setattr(verify, "run_verify", run_hidden)
    record, = arm.run_suite(suite, LANES[:1], 1, tmp_path / "results.jsonl")
    assert record["answer_match"] and record["solved"]
    assert not record["verify_green"] and record["verify_exit"] is None


def test_readonly_passes_full_worker_text_as_answer(suite, tmp_path, monkeypatch):
    path = suite / "task.json"
    data = json.loads(path.read_text())
    data.update(policy="read-only", kind="readonly", verify="", expected_answer=["shop"])
    path.write_text(json.dumps(data))
    answer = "I inspected all modules.\nANSWER: shop\nThis is the full explanation."
    log = tmp_path / "run.jsonl"
    log.write_text('{"task_id":"pricing","kind":"result"}\n')
    monkeypatch.setattr(dispatch, "dispatch", lambda *a, **kw: {
        "run_log": str(log), "tasks": [{"task_id": "pricing", "text": answer}]})
    def grade(spec, repo, commit, **kwargs):
        assert kwargs["answer"] == answer
        assert commit == kwargs["base_commit"]
        return Grade(solved=True)
    monkeypatch.setattr(arm, "grade", grade)
    row, = arm.run_suite(suite, LANES[:1], 1, tmp_path / "out")
    assert row["solved"]


def test_claude_freshness_cli_flags(suite, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *a, **kw: calls.append(kw))
    assert arm.main(["--suite", str(suite), "--lanes", json.dumps(LANES),
                     "--out", str(tmp_path / "out"), "--claude-max-age-min", "12",
                     "--allow-stale-claude"]) == 0
    guard = calls[0]["quota_guard"]
    assert guard.max_age == 720 and guard.allow_stale_claude


def test_runner_and_real_grader_keep_hidden_red_separate(suite, tmp_path, monkeypatch):
    spec, = load_suite(suite)
    (spec.hidden_dir / "check.py").write_text("raise AssertionError('hidden red')\n")
    calls = []
    fake_dispatch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(verify, "find_sandbox", lambda: "fake")
    def run_hidden(command, checkout, *, codex_path):
        result = subprocess.run([sys.executable, "check.py"], cwd=checkout,
                                capture_output=True, text=True)
        return verify.VerifyOutcome(command, result.returncode == 0, result.returncode,
                                    result.stderr, None)
    monkeypatch.setattr(verify, "run_verify", run_hidden)
    record, = arm.run_suite(suite, LANES[:1], 1, tmp_path / "results.jsonl")
    assert record["verify_green"] and not record["solved"]
    assert record["hidden_exit"] == 1 and record["status"] == "graded"


def test_changed_lane_is_void(suite, tmp_path, monkeypatch):
    calls = []
    fake_dispatch(monkeypatch, tmp_path, calls)
    fake = dispatch.dispatch
    def rerouted(*args, **kwargs):
        result = fake(*args, **kwargs)
        result["tasks"][0]["engine"] = "claude"
        return result
    monkeypatch.setattr(dispatch, "dispatch", rerouted)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade(solved=True, hidden_exit=0))
    record, = arm.run_suite(suite, LANES[:1], 1, tmp_path / "results.jsonl")
    assert not record["solved"] and record["status"] == "void"


def test_live_cli_uses_guard_with_thresholds_or_explicit_override(suite, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *a, **kw: calls.append(kw))
    args = ["--suite", str(suite), "--lanes", json.dumps(LANES),
            "--out", str(tmp_path / "out")]
    assert arm.main(args) == 0
    assert isinstance(calls[0]["quota_guard"], arm.QuotaGuard)
    assert calls[0]["quota_guard"].max_5h == 90
    assert calls[0]["quota_guard"].max_weekly == 80
    assert arm.main([*args, "--max-5h", "75", "--max-weekly", "65"]) == 0
    assert calls[1]["quota_guard"].max_5h == 75
    assert calls[1]["quota_guard"].max_weekly == 65
    assert arm.main([*args, "--no-quota-guard"]) == 0
    assert len(calls) == 3 and calls[2]["dry_run"] is False
    assert calls[2]["quota_guard"](LANES[0]) is True


def test_default_guard_records_only_windows(suite, tmp_path, monkeypatch):
    fake_dispatch(monkeypatch, tmp_path, [])
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade())
    out = tmp_path / "out.jsonl"
    row, = arm.run_suite(suite, LANES[:1], 1, out)
    assert row["quota_before"] == row["quota_after"] == {"5-hour": 10, "weekly": 20}
    assert "private-account" not in out.read_text()


@pytest.mark.parametrize("missing", ["absent", "deleted"])
def test_missing_run_log_records_ungraded_and_continues(suite, tmp_path, monkeypatch, missing):
    calls = []
    fake_dispatch(monkeypatch, tmp_path, calls)
    fake = dispatch.dispatch
    def missing_log(*args, **kwargs):
        result = fake(*args, **kwargs)
        if missing == "deleted":
            Path(result["run_log"]).unlink()
        else:
            result.pop("run_log")
        return result
    monkeypatch.setattr(dispatch, "dispatch", missing_log)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade(solved=True, hidden_exit=0))
    records = arm.run_suite(suite, LANES[:1], 2, tmp_path / "out.jsonl")
    assert len(records) == len(calls) == 2
    assert all(r["status"] == "not-graded" and not r["solved"] for r in records)
    assert all("run log" in r["reason"] for r in records)


def test_hidden_snapshot_precedes_dispatch_and_modification_voids(suite, tmp_path, monkeypatch):
    calls = []
    spec, = load_suite(suite)
    fake_dispatch(monkeypatch, tmp_path, calls)
    fake = dispatch.dispatch
    def tamper(*args, **kwargs):
        (spec.hidden_dir / "check.py").write_text("# worker replaced the hidden tests\n")
        return fake(*args, **kwargs)
    monkeypatch.setattr(dispatch, "dispatch", tamper)
    monkeypatch.setattr(verify, "find_sandbox", lambda: pytest.fail("void must not run"))
    records = arm.run_suite(suite, LANES, 2, tmp_path / "out.jsonl")
    assert len(records) == 4
    assert all(record["status"] == "void" and not record["solved"] for record in records)
    assert all(record["gate_violations"] == ["hidden-modified"] for record in records)
    assert all(record["hidden_checked"] is True for record in records)


@pytest.mark.parametrize("dirty", [False, True])
def test_bench_dirty_recorded_alongside_sha(suite, tmp_path, monkeypatch, dirty):
    calls = []
    fake_dispatch(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(arm, "grade", lambda *a, **kw: Grade(solved=True, hidden_exit=0))
    bench_repo = Path(arm.__file__).resolve().parents[2]
    def bench_git(repo, *args):
        if repo == bench_repo and args == ("status", "--porcelain"):
            return " M scripts/bench/grader.py" if dirty else ""
        return git(repo, *args)
    monkeypatch.setattr(arm, "git", bench_git)
    record, = arm.run_suite(suite, LANES[:1], 1, tmp_path / "out.jsonl")
    assert record["bench_dirty"] is dirty
    assert record["bench_sha"] == git(bench_repo, "rev-parse", "HEAD")
