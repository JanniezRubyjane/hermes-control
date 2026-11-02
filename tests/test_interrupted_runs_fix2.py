"""Round-2 review fixes for A6 part 1: a reconciled run offers no work for resume that a
normal dispatch would not run, and reports no false reason."""

from __future__ import annotations

import subprocess
import time

import pytest

from hermes_control import dispatch, landing, pauses, proof, recover, runs
from hermes_control.runlog import RunLog
from tests.scripted_engine import repo  # noqa: F401
from tests.test_interrupted_runs import (
    UNPROVEN, _hand_run, _past_grace, _start_and_crash, _summary_entry, _task)

pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")


def test_dependent_of_a_failed_task_is_skipped_not_paused(repo, monkeypatch):
    run_id = _hand_run(repo, "fix20001", [_task("a"), _task("b", depends_on=["a"])], [
        ("a", "session", {"session_id": "s"}),
        ("a", "error", {"message": "engine reported failure: boom"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.all_paused() == []
    skipped = "skipped: depends on a, which failed"
    entry = _summary_entry(run_id, "b")
    assert entry["ok"] is False and entry["unproven_reason"] == skipped
    assert not entry["paused_id"] and entry["commit"] == ""
    errors = [e for e in RunLog(run_id).read_all() if e.kind == "error" and e.task_id == "b"]
    assert [e.payload["message"] for e in errors] == [skipped]
    view = runs.run_view(run_id)
    assert view["state"] == "finished"
    assert next(t for t in view["tasks"] if t["id"] == "b")["status"] == "failed"


def test_skip_follows_a_chain_in_dependency_order_even_when_planned_out_of_order(repo, monkeypatch):
    run_id = _hand_run(repo, "fix20002", [
        _task("c", depends_on=["b"]), _task("b", depends_on=["a"]), _task("a")], [
        ("a", "error", {"message": "engine reported failure: boom"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.all_paused() == []
    assert _summary_entry(run_id, "b")["unproven_reason"] == "skipped: depends on a, which failed"
    assert _summary_entry(run_id, "c")["unproven_reason"] == "skipped: depends on b, which failed"
    summary = [e for e in RunLog(run_id).read_all() if e.kind == "summary"][-1]
    assert [e["task_id"] for e in summary.payload["tasks"]] == ["c", "b", "a"]


def test_dependent_of_a_paused_task_stays_paused(repo, monkeypatch):
    run_id = _hand_run(repo, "fix20003", [_task("a"), _task("b", depends_on=["a"])], [
        ("a", "session", {"session_id": "s"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.load(pauses.pause_id(run_id, "a")) is not None
    assert pauses.load(pauses.pause_id(run_id, "b")) is not None


def test_committed_task_without_a_verify_command_is_no_verify_not_a_dead_check(repo, monkeypatch):
    run_id = _hand_run(repo, "fix20004", [_task("a")], [
        ("a", "commit", {"sha": "abc123", "branch": "hermes-subagent/x"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    entry = _summary_entry(run_id, "a")
    assert entry["commit"] == "abc123"
    assert entry["unproven_reason"] == landing.NO_VERIFY_REASON
    assert entry["verdict"] == "no-verify"
    assert pauses.load(pauses.pause_id(run_id, "a")) is None


def test_committed_task_with_a_verify_command_and_no_check_event_is_verify_blocked(repo, monkeypatch):
    # task_verdict with no outcome and a verify command is "verify-blocked".
    assert proof.task_verdict("abc123", None, "true", UNPROVEN) == "verify-blocked"
    run_id = _hand_run(repo, "fix20005", [{**_task("a"), "verify": "true"}], [
        ("a", "commit", {"sha": "abc123", "branch": "hermes-subagent/x"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    entry = _summary_entry(run_id, "a")
    assert entry["unproven_reason"] == UNPROVEN
    assert entry["verdict"] == "verify-blocked"


def test_crash_inside_landing_after_the_commit_is_reported_committed_not_paused(repo, monkeypatch):
    real_land = dispatch.land

    def land_then_die(*args, **kwargs):
        real_land(*args, **kwargs)  # commits and verifies, but its events are never logged
        raise SystemExit("simulated gateway os._exit")

    monkeypatch.setattr(dispatch, "land", land_then_die)
    run_id = _start_and_crash(repo, monkeypatch, [{**_task("a"), "verify": "true"}],
                              crash_tasks=())
    events = RunLog(run_id).read_all()
    assert not [e for e in events if e.kind == "commit"]
    spawned = next(e for e in events if e.kind == "spawned").payload["worktree"]
    head = subprocess.run(["git", "rev-parse", spawned["branch"]], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()
    assert head != spawned["base_commit"]
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.all_paused() == []
    entry = _summary_entry(run_id, "a")
    assert entry["commit"] == head and entry["ok"] is True
    assert entry["branch"] == spawned["branch"]
    assert entry["unproven_reason"] == recover.UNPROVEN_REASON
    assert entry["verdict"] == "verify-blocked"
    assert not [e for e in RunLog(run_id).read_all() if e.kind == "interrupted"]
    assert runs.run_view(run_id)["tasks"][0]["status"] == "unproven"


def test_list_runs_reconciles_a_run_quiet_for_more_than_the_stale_window(repo, monkeypatch):
    run_id = _hand_run(repo, "fix20006", [_task("a")], [
        ("a", "session", {"session_id": "s"}),
        ("a", "result", {"ok": True})])
    _past_grace(monkeypatch, runs.STALE_SECONDS + 600)
    assert runs.run_view(run_id, now=time.time)["state"] == "finished"  # looks settled

    listed = runs.list_runs(now=time.time)

    assert [item["state"] for item in listed if item["run_id"] == run_id] == ["paused"]
    assert pauses.load(pauses.pause_id(run_id, "a")) is not None
