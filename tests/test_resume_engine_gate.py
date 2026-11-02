"""Resume gate: a paused task is not resumed while its earlier engine group still runs."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest

from hermes_control import dispatch, pauses, proctree, recover
from hermes_control.events import RawEvent
from hermes_control.runlog import RunLog
from tests.scripted_engine import ScriptedEngine, _engines, repo  # noqa: F401
from tests.test_interrupted_runs import _past_grace, _setup_engines, _start_and_crash

# The simulated os._exit is a SystemExit that ends the run's worker thread on purpose.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")

# Captured at import: tests/conftest.py stubs proctree.group_still_running for every test.
check = proctree.group_still_running


def _fake_ps(stdout: str, returncode: int = 0):
    def _run(argv, **kwargs):
        assert argv[:3] == ["ps", "-o", "lstart="]
        return subprocess.CompletedProcess(argv, returncode, stdout, "")
    return _run


def _paused(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)
    assert recover.reconcile(run_id) is True
    record = pauses.load(pauses.pause_id(run_id, "a"))
    assert record is not None and record.pid is not None
    return run_id, record


def test_group_still_running_cases(monkeypatch):
    if proctree.os.name == "nt":
        pytest.skip("POSIX process groups required")
    started = "Fri Oct 23 22:00:00 2026"

    # Same pid, same start time: the engine leader still runs.
    monkeypatch.setattr(proctree.subprocess, "run", _fake_ps(started + "\n", 0))
    assert check(4242, started) is True

    # Reused pid: another start time is False, and no killpg is made.
    monkeypatch.setattr(proctree.subprocess, "run", _fake_ps("Fri Oct 23 23:00:00 2026\n", 0))
    monkeypatch.setattr(proctree.os, "killpg",
                        lambda pgid, sig: (_ for _ in ()).throw(AssertionError("no killpg")))
    assert check(4242, started) is False

    # Leader gone, group left over: killpg succeeds or is denied. Only signal 0 is sent.
    monkeypatch.setattr(proctree.subprocess, "run", _fake_ps("", 1))
    signals = []
    monkeypatch.setattr(proctree.os, "killpg", lambda pgid, sig: signals.append(sig))
    assert check(4242, started) is True
    assert signals == [0]

    def denied(pgid, sig):
        raise PermissionError("not allowed")

    monkeypatch.setattr(proctree.os, "killpg", denied)
    assert check(4242, started) is True

    # Completely gone.
    def gone(pgid, sig):
        raise ProcessLookupError("no such process group")

    monkeypatch.setattr(proctree.os, "killpg", gone)
    assert check(4242, started) is False

    # Any other killpg error cannot be told.
    def odd(pgid, sig):
        raise OSError("odd")

    monkeypatch.setattr(proctree.os, "killpg", odd)
    assert check(4242, started) is None

    # ps itself cannot run.
    def ps_broken(*args, **kwargs):
        raise OSError("ps broken")

    monkeypatch.setattr(proctree.subprocess, "run", ps_broken)
    assert check(4242, started) is None


def test_group_still_running_without_recorded_start_time(monkeypatch):
    if proctree.os.name == "nt":
        pytest.skip("POSIX process groups required")
    monkeypatch.setattr(proctree.subprocess, "run", _fake_ps("Fri Oct 23 22:00:00 2026\n", 0))
    monkeypatch.setattr(proctree.os, "killpg",
                        lambda pgid, sig: (_ for _ in ()).throw(AssertionError("no killpg")))
    assert check(4242, None) is None


def test_group_still_running_is_unknown_on_windows(monkeypatch):
    monkeypatch.setattr(proctree.os, "name", "nt")
    assert check(4242, "anything") is None


def test_resume_paused_blocks_while_old_engine_group_runs_then_succeeds(repo, monkeypatch):
    run_id, record = _paused(repo, monkeypatch)

    monkeypatch.setattr(proctree, "group_still_running", lambda pid, started: True)
    reason, pick = dispatch.resume_blocker(record)
    assert pick is None
    assert reason == (f"the earlier engine (pid {record.pid}) is still running in its worktree; "
                      "stop it or wait, then resume")
    with pytest.raises(dispatch.NotReady,
                       match=rf"earlier engine \(pid {record.pid}\) is still running"):
        dispatch.resume_paused(record.id)

    monkeypatch.setattr(proctree, "group_still_running", lambda pid, started: None)
    reason, pick = dispatch.resume_blocker(record)
    assert pick is None and "could not check" in reason
    assert f"pid {record.pid}" in reason
    with pytest.raises(dispatch.NotReady, match="could not check"):
        dispatch.resume_paused(record.id)

    monkeypatch.setattr(proctree, "group_still_running", lambda pid, started: False)
    _setup_engines(monkeypatch, crash_tasks=())
    resumed = dispatch.resume_paused(record.id)
    assert resumed["ok"] is True and resumed["commit"]
    assert pauses.load(record.id) is None


def test_gate_uses_the_latest_spawned_event_not_the_pause_record(repo, monkeypatch):
    run_id, record = _paused(repo, monkeypatch)
    newer_pid = record.pid + 1000
    log = RunLog(run_id)
    log.continue_sequences()
    log.append_next("a", [RawEvent(kind="spawned", payload={
        "pid": newer_pid, "started": "Sat Oct 24 01:02:03 2026", "worktree": {}})],
        engine="claude", now=time.time())

    seen = []
    monkeypatch.setattr(proctree, "group_still_running",
                        lambda pid, started: seen.append((pid, started)) or True)
    reason, pick = dispatch.resume_blocker(record)
    assert pick is None and f"pid {newer_pid}" in reason
    assert seen == [(newer_pid, "Sat Oct 24 01:02:03 2026")]
    assert newer_pid != record.pid


def test_no_spawned_event_means_no_gate(repo, monkeypatch):
    run_id, record = _paused(repo, monkeypatch)
    log = RunLog(run_id)
    lines = [line for line in log.path.read_text(encoding="utf-8").splitlines()
             if line.strip() and json.loads(line).get("kind") != "spawned"]
    log.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert not any(e.kind == "spawned" for e in log.read_all())

    def boom(pid, started):
        raise AssertionError("the gate must not run without a spawned event")

    monkeypatch.setattr(proctree, "group_still_running", boom)
    _setup_engines(monkeypatch, crash_tasks=())
    resumed = dispatch.resume_paused(record.id)
    assert resumed["ok"] is True
    assert pauses.load(record.id) is None


def test_unreadable_run_log_blocks_resume(repo, monkeypatch):
    run_id, record = _paused(repo, monkeypatch)

    def broken(self):
        raise OSError("disk gone")

    monkeypatch.setattr(RunLog, "read_all", broken)
    reason, pick = dispatch.resume_blocker(record)
    assert pick is None and reason == "could not read its run log: disk gone"


def test_group_still_running_sees_a_real_process():
    if proctree.os.name == "nt":
        pytest.skip("POSIX process groups required")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        started = proctree.process_start_time(p.pid)
        if started is None:
            pytest.skip("ps cannot read a start time in this environment")
        assert check(p.pid, started) is True
        assert check(p.pid, started + " other") is False
    finally:
        p.kill()
        p.wait()
    assert check(p.pid, started) is False
