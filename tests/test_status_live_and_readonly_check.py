"""Cross-process status and write-only Claude check-script regressions."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import hermes_control.dispatch as dispatch
from hermes_control import runs
from hermes_control.engines.base import ControlTask
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.verify import check_script_path
from hermes_control.worktree import create_task_worktree
from tests.test_dispatch_background import FakeAdapter, repo  # noqa: F401
from tests.test_resume_live_lock import append


@pytest.mark.parametrize("live,expected", [(True, "running"), (False, "stopped"),
                                          (None, "stopped")])
def test_log_only_running_status_uses_live_lock(monkeypatch, live, expected):
    run_id = "other-process"
    log = RunLog(run_id)
    append(log, "plan", {"tasks": [{"id": "a", "engine": "claude", "policy": "write"}]})
    routing = {"reason": "original routing"}
    append(log, "summary", {"tasks": [], "routing": routing})
    assert runs.run_view(run_id)["state"] == "running"
    assert run_id not in dispatch._THREADS and run_id not in dispatch._FINISHED
    probe = Mock(return_value=live)
    monkeypatch.setattr(dispatch, "_run_is_live", probe)

    status = dispatch.control_status(run_id)

    probe.assert_called_once_with(run_id)
    assert status == {"run_id": run_id, "state": expected,
                      "tasks": [{"id": "a", "engine": "claude", "status": "waiting"}],
                      "routing": routing}


@pytest.fixture
def status_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    monkeypatch.setattr(dispatch.time, "monotonic", lambda: clock.now)
    monkeypatch.setattr(dispatch.time, "sleep", sleep)
    return clock


def test_status_waits_for_external_run_to_finish(monkeypatch, status_clock):
    run_id = "external-finishes"
    log = RunLog(run_id)
    append(log, "plan", {"tasks": [{"id": "a", "engine": "claude"}]})
    probes = []

    def is_live(probed_run_id):
        assert probed_run_id == run_id
        probes.append(status_clock.now)
        if len(probes) <= 2:
            return True
        append(log, "summary", {"tasks": [{"task_id": "a", "engine": "claude",
                                           "ok": False,
                                           "unproven_reason": "finished externally"}]})
        return False

    monkeypatch.setattr(dispatch, "_run_is_live", is_live)

    status = dispatch.control_status(run_id, wait=30)

    assert status["state"] == runs.run_view(run_id)["state"] == "finished"
    assert status["tasks"][0]["unproven_reason"] == "finished externally"
    assert len(probes) == 3
    assert status_clock.sleeps == [5.0, 5.0]
    checked = [event.payload for event in log.read_all()
               if event.kind == "planner" and event.payload.get("phase") == "status_checked"]
    assert checked[-1]["waited_seconds"] == 10.0
    assert checked[-1]["state"] == "finished"


@pytest.mark.parametrize("wait", [12, dispatch.MAX_STATUS_WAIT_SECONDS + 30])
def test_status_external_wait_is_bounded(monkeypatch, status_clock, wait):
    run_id = "external-still-live"
    log = RunLog(run_id)
    append(log, "plan", {"tasks": [{"id": "a", "engine": "claude"}]})
    monkeypatch.setattr(dispatch, "_run_is_live", Mock(return_value=True))

    status = dispatch.control_status(run_id, wait=wait)

    elapsed = min(wait, dispatch.MAX_STATUS_WAIT_SECONDS)
    assert status["state"] == "running"
    assert status_clock.now == 100.0 + elapsed
    assert sum(status_clock.sleeps) == elapsed
    assert all(0 < seconds <= 5 for seconds in status_clock.sleeps)
    if wait == 12:
        assert status_clock.sleeps == [5.0, 5.0, 2.0]
    checked = [event.payload for event in log.read_all()
               if event.kind == "planner" and event.payload.get("phase") == "status_checked"]
    assert checked[-1]["waited_seconds"] == elapsed


@pytest.mark.parametrize("live,wait,expected", [(True, 0, "running"),
                                              (False, 30, "stopped"),
                                              (None, 30, "stopped")])
def test_status_does_not_wait_without_live_run_or_positive_wait(
        monkeypatch, status_clock, live, wait, expected):
    run_id = "external-no-wait"
    log = RunLog(run_id)
    append(log, "plan", {"tasks": [{"id": "a", "engine": "claude"}]})
    monkeypatch.setattr(dispatch, "_run_is_live", Mock(return_value=live))

    assert dispatch.control_status(run_id, wait=wait)["state"] == expected
    assert status_clock.sleeps == []
    assert status_clock.now == 100.0


def test_status_observes_real_live_lock_in_another_thread():
    run_id = "external-real-lock"
    log = RunLog(run_id)
    append(log, "plan", {"tasks": [{"id": "a", "engine": "claude"}]})
    assert runs.run_view(run_id)["state"] == "running"
    locked, release = threading.Event(), threading.Event()

    def hold_lock():
        with dispatch._live_run_lock(run_id):
            locked.set()
            release.wait(10)

    thread = threading.Thread(target=hold_lock, daemon=True)
    thread.start()
    try:
        assert locked.wait(5), "helper never acquired the live lock"
        assert dispatch.control_status(run_id)["state"] == "running"
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert dispatch.control_status(run_id)["state"] == "stopped"


@pytest.mark.parametrize("write", [False, True], ids=["read-only", "write"])
def test_claude_check_script_requires_write_policy(repo, monkeypatch, write):
    run_id = "check-policy"
    script = check_script_path(run_id, "a")
    worktree = create_task_worktree(str(repo), "a", run_id=run_id)
    writer = Mock(wraps=dispatch.write_check_script)
    monkeypatch.setattr(dispatch, "write_check_script", writer)

    class RecordingClaude(FakeAdapter):
        name = "claude"

        def build_argv(self, task, *, cwd):
            self.received = task
            assert script.is_file() is write
            return super().build_argv(task, cwd=cwd)

    adapter = RecordingClaude()
    task = ControlTask(id="a", goal="Review the code", engine="claude",
                       policy=ExecPolicy(write=write), verify="exit 0")
    result = dispatch.run_task(task, run_id=run_id, log=RunLog(run_id),
                               worktree=worktree, adapter=adapter,
                               sandbox=lambda: "/fake/codex")

    assert result.ok is True
    assert ("pre-approved" in adapter.received.goal) is write
    assert (str(script) in adapter.received.goal) is write
    assert (f"Bash({script})" in adapter.received.policy.allow_tools) is write
    if write:
        writer.assert_called_once_with("/fake/codex", worktree.path, "exit 0", path=script)
    else:
        writer.assert_not_called()
    assert not script.exists()
