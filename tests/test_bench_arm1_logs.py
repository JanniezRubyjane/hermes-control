"""Arm 1 keeps synthetic controller logs out of the isolated live home."""

import json
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_controller as arm
from bench.grader import Grade, git
from bench.results import read_results
from hermes_control import dispatch


LANES = [{"engine": "codex", "model": "test-codex", "effort": "high"}]


@pytest.fixture
def attempt(tmp_path, monkeypatch, _isolated_hermes_home):
    suite = tmp_path / "suite"
    suite.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "check.py").write_text("assert True\n")
    (suite / "task.json").write_text(json.dumps({
        "id": "pricing", "source": "S", "kind": "bug", "policy": "write",
        "goal": "Fix double discount", "verify": "python -m pytest -q",
        "hidden_dir": str(hidden), "hidden_cmd": "python check.py",
        "protected_paths": ["tests/test_shop.py"], "repo": {"builder": "shop"}}))
    control = _isolated_hermes_home / "control"
    control.mkdir()
    out = tmp_path / "results" / "attempts.jsonl"
    logs = []

    def fake(tasks, *, parent_cwd, run_id):
        task, = tasks
        log = control / f"run-{run_id}.jsonl"
        log.write_text(json.dumps({"task_id": task["id"], "kind": "result"}) + "\n")
        for suffix in ("live.lock", "summary.lock"):
            log.with_suffix("." + suffix).touch()
        logs.append(log)
        return {"run_log": str(log), "tasks": [{"task_id": task["id"],
                "commit": git(Path(parent_cwd), "rev-parse", "HEAD")} ]}

    def grade(*args, **kwargs):
        # Grading must scan evidence before its original path disappears.
        assert kwargs["transcript_paths"] == [str(logs[-1])]
        assert Path(kwargs["transcript_paths"][0]).read_text()
        return Grade(solved=True, hidden_exit=0)

    monkeypatch.setattr(dispatch, "dispatch", fake)
    monkeypatch.setattr(arm, "grade", grade)

    def run(**kwargs):
        records = arm.run_suite(suite, LANES, 1, out, quota_guard=lambda lane: True, **kwargs)
        assert read_results(out) == records
        return records[0]

    return run, logs, control, out


@pytest.mark.parametrize("locks_present", [True, False])
def test_moves_log_after_grading_and_only_removes_own_locks(attempt, monkeypatch, locks_present):
    run, logs, control, out = attempt
    unrelated = [control / name for name in (
        "run-other.jsonl", "run-other.live.lock", "run-other.summary.lock", "codex-slot.lock")]
    for path in unrelated:
        path.write_text("untouched")
    fake = dispatch.dispatch

    def dispatch_without_locks(*args, **kwargs):
        result = fake(*args, **kwargs)
        if not locks_present:
            for suffix in ("live.lock", "summary.lock"):
                logs[-1].with_suffix("." + suffix).unlink()
        return result

    monkeypatch.setattr(dispatch, "dispatch", dispatch_without_locks)
    row = run()
    destination = out.parent / "evidence" / f"{row['run_id']}.jsonl"
    assert row["run_log"] == str(destination)
    assert destination.read_text() == '{"task_id": "pricing", "kind": "result"}\n'
    assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700
    assert not logs[0].exists()
    assert all(not logs[0].with_suffix("." + suffix).exists()
               for suffix in ("live.lock", "summary.lock"))
    assert all(path.read_text() == "untouched" for path in unrelated)
    assert "run_log_move_error" not in row


def test_existing_evidence_keeps_original_and_records_error(attempt, monkeypatch):
    run, logs, _, out = attempt
    fake = dispatch.dispatch
    destinations = []

    def collision(*args, **kwargs):
        result = fake(*args, **kwargs)
        destination = out.parent / "evidence" / f"{kwargs['run_id']}.jsonl"
        destination.parent.mkdir(parents=True)
        destination.write_text("existing evidence")
        destinations.append(destination)
        return result

    monkeypatch.setattr(dispatch, "dispatch", collision)
    row = run()
    assert row["solved"] and row["run_log_move_error"]
    assert row["run_log"] == str(logs[0]) and logs[0].is_file()
    assert destinations[0].read_text() == "existing evidence"
    assert all(logs[0].with_suffix("." + suffix).is_file()
               for suffix in ("live.lock", "summary.lock"))


def test_copy_failure_keeps_source_and_removes_partial_evidence(attempt, monkeypatch):
    run, logs, _, out = attempt

    def broken_copy(reader, writer):
        writer.write(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(arm.shutil, "copyfileobj", broken_copy)
    row = run()
    assert row["run_log_move_error"] == "copy failed" and row["solved"]
    assert row["run_log"] == str(logs[0]) and logs[0].is_file()
    assert not (out.parent / "evidence" / f"{row['run_id']}.jsonl").exists()


def test_keep_run_logs_leaves_log_and_locks(attempt):
    run, logs, _, out = attempt
    row = run(keep_run_logs=True)
    assert row["run_log"] == str(logs[0]) and logs[0].is_file()
    assert all(logs[0].with_suffix("." + suffix).is_file()
               for suffix in ("live.lock", "summary.lock"))
    assert not (out.parent / "evidence").exists()
    assert "run_log_move_error" not in row


@pytest.mark.parametrize("failure_point", ["grade", "read_run_events", "after"])
def test_attempt_error_relocates_log_and_propagates(attempt, monkeypatch, failure_point):
    run, logs, control, out = attempt
    error = RuntimeError("attempt failed")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(arm, failure_point, fail)
    with pytest.raises(RuntimeError) as caught:
        run()
    assert caught.value is error
    destination = out.parent / "evidence" / logs[0].name.removeprefix("run-")
    assert destination.read_text() == '{"task_id": "pricing", "kind": "result"}\n'
    assert not list(control.glob("run-bench-*"))
    assert not out.exists()


@pytest.mark.parametrize("suffix", ["live.lock", "summary.lock"])
def test_lock_cleanup_error_keeps_moved_log_path(attempt, monkeypatch, suffix):
    run, logs, _, out = attempt
    unlink = Path.unlink

    def fail_lock(path, *args, **kwargs):
        if logs and path == logs[0].with_suffix("." + suffix):
            raise OSError("lock cleanup failed")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_lock)
    row = run()
    destination = out.parent / "evidence" / f"{row['run_id']}.jsonl"
    assert row["run_log"] == str(destination)
    assert destination.is_file() and not logs[0].exists()
    assert row["run_lock_cleanup_error"] == "lock cleanup failed"
    assert "run_log_move_error" not in row
    other = "summary.lock" if suffix == "live.lock" else "live.lock"
    assert not logs[0].with_suffix("." + other).exists()


def test_keep_run_logs_cli_passes_option(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *args, **kwargs: calls.append(kwargs))
    args = ["--suite", str(tmp_path), "--out", str(tmp_path / "out"),
            "--lanes", json.dumps(LANES)]
    assert arm.main(args) == 0
    assert calls[-1]["keep_run_logs"] is False
    assert arm.main([*args, "--keep-run-logs"]) == 0
    assert calls[-1]["keep_run_logs"] is True
