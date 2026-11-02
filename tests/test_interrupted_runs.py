"""Interrupted runs recover on read into paused tasks (resume gate: a later task)."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import importlib.util
import json
from pathlib import Path
import subprocess
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from hermes_control import dispatch, pauses, proctree, recover, runs
from hermes_control.events import ControlResult, RawEvent
from hermes_control.runlog import RunLog, run_log_dir
from hermes_control.verify import VerifyOutcome
from tests.scripted_engine import ScriptedEngine, _engines, repo  # noqa: F401

# The simulated os._exit is a SystemExit that ends the run's worker thread on purpose.
pytestmark = pytest.mark.filterwarnings(
    "ignore::pytest.PytestUnhandledThreadExceptionWarning")

DASHBOARD = Path(__file__).resolve().parents[1] / "hermes_control" / "dashboard"
REASON = "the Hermes process running this run ended before the task finished"


class InterruptingEngine(ScriptedEngine):
    """Commits tasks scripted as 'ok' and simulates process exit on 'crash'."""

    def __init__(self, name: str, *outcomes: str, crash_tasks: set[str] | None = None):
        super().__init__(name, *outcomes)
        self.crash_tasks = set(crash_tasks or ())

    def parse_line(self, line: str):
        return [RawEvent(kind="session", payload={"session_id": f"{self.name}-sess"}),
                *super().parse_line(line)]

    def extract_result(self, task, raws, diff):
        if task.id in self.crash_tasks:
            raise SystemExit("simulated gateway os._exit")
        return ControlResult(task_id=task.id, engine=self.name, ok=True, text="done",
                             session_id=f"{self.name}-sess", diff=diff, unproven_reason=None)


def _setup_engines(monkeypatch, *, crash_tasks=("a",)):
    claude = InterruptingEngine("claude", *(["ok"] * 8), crash_tasks=set(crash_tasks))
    codex = InterruptingEngine("codex", *(["ok"] * 8), crash_tasks=set())
    engines = _engines(monkeypatch, claude, codex)

    def verify(command, cwd, *, codex_path):
        res = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True, text=True)
        return VerifyOutcome(command, res.returncode == 0, res.returncode,
                             res.stdout + res.stderr, None)

    monkeypatch.setattr("hermes_control.landing.run_verify", verify)
    return engines


def _task(task_id="a", *, engine="claude", depends_on=()):
    return {"id": task_id, "goal": f"finish {task_id}", "engine": engine,
            "why": f"reason for {task_id}", "policy": "write",
            "depends_on": list(depends_on)}


def _start_and_crash(repo, monkeypatch, tasks=None, *, crash_tasks=("a",)):
    _setup_engines(monkeypatch, crash_tasks=crash_tasks)
    out = dispatch.start_dispatch(tasks or [_task("a")], parent_cwd=str(repo), wait=0.05)
    run_id = out["run_id"]
    # The worker removes itself from _THREADS before it releases the live lock, so
    # wait for the lock itself (real clock: tests patch time.time later, not here).
    deadline = time.monotonic() + 10
    while dispatch._run_is_live(run_id) is not False and time.monotonic() < deadline:
        time.sleep(0.02)
    dispatch._THREADS.pop(run_id, None)
    dispatch._FINISHED.pop(run_id, None)
    assert dispatch._run_is_live(run_id) is False
    assert not any(e.kind == "summary" for e in RunLog(run_id).read_all())
    return run_id


def _past_grace(monkeypatch, seconds=120.0):
    now = time.time() + seconds
    monkeypatch.setattr(time, "time", lambda: now)


def test_reconcile_turns_interrupted_task_into_paused_record_and_event(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    record = pauses.load(pauses.pause_id(run_id, "a"))
    assert record is not None
    assert record.reason == REASON
    assert record.engine == "claude"
    assert record.session_id == "claude-sess"
    assert record.task["goal"] == "finish a"
    assert record.worktree is not None and Path(record.worktree["path"]).is_dir()
    assert isinstance(record.pid, int) and record.pid > 0

    events = RunLog(run_id).read_all()
    spawned = [e for e in events if e.kind == "spawned" and e.task_id == "a"]
    interrupted = [e for e in events if e.kind == "interrupted" and e.task_id == "a"]
    paused = [e for e in events if e.kind == "paused" and e.task_id == "a"]
    assert len(spawned) == 1 and spawned[0].payload["pid"] == record.pid
    assert set(spawned[0].payload) == {"pid", "started", "worktree"}
    assert spawned[0].payload["worktree"] == record.worktree
    assert record.started == spawned[0].payload["started"]
    assert len(interrupted) == 1
    assert interrupted[0].payload["reason"] == REASON
    assert interrupted[0].payload.get("id") == record.id or interrupted[0].payload.get("pause_id") == record.id
    assert len(paused) == 1 and paused[0].payload["id"] == record.id

    view = runs.run_view(run_id)
    assert view["state"] == "paused"
    assert view["tasks"][0]["status"] == "paused"
    assert view["tasks"][0]["status_reason"] == REASON
    trace = runs.task_trace(run_id, "a")
    assert trace is not None


def test_reconcile_leaves_run_with_held_live_lock_untouched(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)
    lock_path = run_log_dir() / f"run-{run_id}.live.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert recover.reconcile(run_id) is False
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    assert pauses.all_paused() == []


def test_reconcile_leaves_run_with_held_resume_lock_untouched(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)
    resume_lock = run_log_dir() / f"run-{run_id}.resume-{run_id}-a.lock"
    with resume_lock.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert recover.reconcile(run_id) is False
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    assert pauses.all_paused() == []


def test_reconcile_leaves_run_untouched_when_liveness_is_unknown(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)
    monkeypatch.setattr(dispatch, "_run_is_live", lambda _rid: None)

    assert recover.reconcile(run_id) is False
    assert pauses.all_paused() == []


def test_reconcile_leaves_recent_events_within_grace_untouched(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)

    assert recover.reconcile(run_id) is False
    assert pauses.all_paused() == []


def test_reconcile_twice_creates_only_one_record(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch, 120.0)

    assert recover.reconcile(run_id) is True
    _past_grace(monkeypatch, 300.0)
    assert recover.reconcile(run_id) is False

    records = pauses.all_paused()
    assert len(records) == 1
    interrupted = [e for e in RunLog(run_id).read_all() if e.kind == "interrupted"]
    assert len(interrupted) == 1


def test_run_that_died_long_ago_is_still_reconciled(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch, 3 * 3600.0)  # far beyond runs.STALE_SECONDS (35 minutes)

    assert recover.reconcile(run_id) is True

    assert pauses.load(pauses.pause_id(run_id, "a")) is not None
    assert runs.run_view(run_id)["state"] == "paused"


def test_log_whose_plan_has_no_goals_is_left_alone():
    RunLog("old1").append_next("", [RawEvent(kind="plan", payload={
        "tasks": [{"id": "a", "engine": "claude", "why": "x"}], "repo": None})],
        engine="", now=1.0)

    assert recover.reconcile("old1") is False
    assert pauses.all_paused() == []
    assert not any(e.kind == "summary" for e in RunLog("old1").read_all())


def test_committed_task_is_not_paused_and_unstarted_dependent_has_no_worktree(repo, monkeypatch):
    tasks = [
        {**_task("done_task"), "verify": "true"},
        _task("crashed_task", depends_on=["done_task"]),
        _task("waiting_dep", depends_on=["crashed_task"]),
    ]
    run_id = _start_and_crash(repo, monkeypatch, tasks, crash_tasks=("crashed_task",))
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.load(pauses.pause_id(run_id, "done_task")) is None
    crashed = pauses.load(pauses.pause_id(run_id, "crashed_task"))
    waiting = pauses.load(pauses.pause_id(run_id, "waiting_dep"))
    assert crashed is not None and crashed.worktree is not None
    assert waiting is not None and waiting.worktree is None
    assert waiting.pid is None and waiting.started is None

    view = runs.run_view(run_id)
    assert view["state"] == "paused"
    done_view = next(t for t in view["tasks"] if t["id"] == "done_task")
    assert done_view["commit"] and done_view["commit"]["sha"]
    assert done_view["status"] == "done"
    summary = [e for e in RunLog(run_id).read_all() if e.kind == "summary"][-1]
    done_entry = next(e for e in summary.payload["tasks"] if e["task_id"] == "done_task")
    assert done_entry["commit"] == done_view["commit"]["sha"]
    # Its check passed before the crash: the log says so, so the summary must too.
    assert done_entry["ok"] is True
    assert done_entry["unproven_reason"] is None
    assert done_entry["verify"]["passed"] is True

    reason, pick = dispatch.resume_blocker(waiting)
    assert pick is None
    assert "depends on crashed_task" in reason and "resume crashed_task first" in reason


def test_task_with_existing_pause_record_keeps_it_unchanged(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch, [_task("a"), _task("b")])
    earlier = pauses.Pause(
        id=pauses.pause_id(run_id, "b"), run_id=run_id,
        task={"id": "b", "goal": "finish b", "engine": "claude", "policy": "write"},
        repo=str(repo), worktree=None, engine="claude", session_id=None,
        reason="claude hit its usage limit", resets_at=None, paused_at=1.0)
    pauses.save(earlier)
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.load(earlier.id) == earlier
    assert pauses.load(pauses.pause_id(run_id, "a")) is not None
    events = RunLog(run_id).read_all()
    assert not [e for e in events if e.kind == "interrupted" and e.task_id == "b"]
    summary = [e for e in events if e.kind == "summary"][-1]
    entry_b = next(e for e in summary.payload["tasks"] if e["task_id"] == "b")
    assert entry_b["paused_id"] == earlier.id


def test_busy_resume_lock_of_one_record_skips_only_that_task(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch, [_task("a"), _task("b")])
    _past_grace(monkeypatch)
    real_lock = dispatch._resume_record_lock

    @contextmanager
    def lock(lock_run_id, record_id):
        if record_id.endswith("-b"):
            raise dispatch.NotReady("this paused task is already resuming")
        with real_lock(lock_run_id, record_id):
            yield

    monkeypatch.setattr(dispatch, "_resume_record_lock", lock)

    assert recover.reconcile(run_id) is True
    assert pauses.load(pauses.pause_id(run_id, "a")) is not None
    assert pauses.load(pauses.pause_id(run_id, "b")) is None
    # The skipped task is not settled, so no summary hides it from the next pass.
    assert not any(e.kind == "summary" for e in RunLog(run_id).read_all())

    monkeypatch.setattr(dispatch, "_resume_record_lock", real_lock)
    assert recover.reconcile(run_id) is False  # our own paused event is still recent
    _past_grace(monkeypatch)
    assert recover.reconcile(run_id) is True
    assert pauses.load(pauses.pause_id(run_id, "b")) is not None
    events = RunLog(run_id).read_all()
    assert len([e for e in events if e.kind == "interrupted" and e.task_id == "a"]) == 1
    assert len([e for e in events if e.kind == "interrupted" and e.task_id == "b"]) == 1
    assert runs.run_view(run_id)["state"] == "paused"


def test_two_threads_reconciling_write_one_interrupted_event_per_task(repo, monkeypatch):
    run_id = _start_and_crash(repo, monkeypatch, [_task("a"), _task("b")])
    _past_grace(monkeypatch)
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def work():
        barrier.wait()
        results.append(recover.reconcile(run_id))

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(results) == [False, True]
    events = RunLog(run_id).read_all()
    for task_id in ("a", "b"):
        assert len([e for e in events if e.kind == "interrupted" and e.task_id == task_id]) == 1
    assert len([e for e in events if e.kind == "summary"]) == 1


def test_old_pause_record_without_pid_and_started_still_loads(repo):
    old = {"id": "r1-a", "run_id": "r1",
           "task": {"id": "a", "goal": "g", "engine": "claude", "policy": "write"},
           "repo": None, "worktree": None, "engine": "claude", "session_id": None,
           "reason": "limit", "resets_at": None, "paused_at": 1.0, "rerouted": None}
    (pauses._directory() / "r1-a.json").write_text(json.dumps(old), encoding="utf-8")

    record = pauses.load("r1-a")

    assert record is not None and record.pid is None and record.started is None


def test_process_start_time_pins_locale_and_zone_and_strips(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"] = argv, kwargs.get("env")
        return subprocess.CompletedProcess(argv, 0, "Fri Oct 23 22:00:00 2026\n", "")

    monkeypatch.setattr(proctree.subprocess, "run", fake_run)

    assert proctree.process_start_time(4242) == "Fri Oct 23 22:00:00 2026"
    assert seen["argv"][:3] == ["ps", "-o", "lstart="]
    assert seen["env"]["LC_ALL"] == "C" and seen["env"]["TZ"] == "UTC"


def test_process_start_time_is_none_when_ps_fails_or_prints_nothing(monkeypatch):
    def respond(stdout, returncode):
        return lambda argv, **kwargs: subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(proctree.subprocess, "run", respond("", 1))
    assert proctree.process_start_time(4242) is None
    monkeypatch.setattr(proctree.subprocess, "run", respond("Fri Oct 23 22:00:00 2026\n", 1))
    assert proctree.process_start_time(4242) is None
    monkeypatch.setattr(proctree.subprocess, "run", respond("  \n", 0))
    assert proctree.process_start_time(4242) is None

    def broken(argv, **kwargs):
        raise OSError("ps missing")

    monkeypatch.setattr(proctree.subprocess, "run", broken)
    assert proctree.process_start_time(4242) is None


def test_control_status_and_dashboard_routes_trigger_reconcile(repo, monkeypatch):
    run1 = _start_and_crash(repo, monkeypatch)
    run2 = _start_and_crash(repo, monkeypatch)
    _past_grace(monkeypatch)

    status = dispatch.control_status(run1)
    assert status["state"] == "finished"
    assert status["tasks"][0]["paused"]["id"] == pauses.pause_id(run1, "a")
    assert status["tasks"][0]["paused"]["reason"] == REASON

    spec = importlib.util.spec_from_file_location(
        "hc_plugin_api_interrupted", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        listed = client.get("/runs").json()["runs"]
        by_id = {item["run_id"]: item for item in listed}
        assert by_id[run2]["state"] == "paused"
        assert by_id[run2]["tasks"][0]["status"] == "paused"

        run3 = _start_and_crash(repo, monkeypatch)
        _past_grace(monkeypatch, 240.0)
        detail = client.get(f"/runs/{run3}").json()
        assert detail["state"] == "paused"
        assert detail["tasks"][0]["status"] == "paused"
        assert detail["tasks"][0]["status_reason"] == REASON


# --- review findings on part 1 ---------------------------------------------------------

UNPROVEN = "the Hermes process running this run ended before the check finished"


def _hand_run(repo, run_id, tasks, events):
    """A run whose process died, written by hand: its plan, then (task, kind, payload)."""
    log = RunLog(run_id)
    log.append_next("", [RawEvent(kind="plan", payload={"tasks": tasks, "repo": str(repo)})],
                    engine="", now=time.time())
    for task_id, kind, payload in events:
        log.append_next(task_id, [RawEvent(kind=kind, payload=payload)], engine="claude",
                        now=time.time())
    return run_id


def _summary_entry(run_id, task_id):
    summary = [e for e in RunLog(run_id).read_all() if e.kind == "summary"][-1]
    return next(e for e in summary.payload["tasks"] if e["task_id"] == task_id)


def test_committed_task_whose_check_never_logged_stays_unproven(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0001", [{**_task("a"), "verify": "true"}], [
        ("a", "commit", {"sha": "abc123", "branch": "hermes-subagent/x"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    entry = _summary_entry(run_id, "a")
    assert entry["commit"] == "abc123"
    assert entry["unproven_reason"] == UNPROVEN
    assert pauses.load(pauses.pause_id(run_id, "a")) is None
    assert runs.run_view(run_id)["tasks"][0]["status"] == "unproven"


def test_committed_task_whose_check_failed_keeps_the_failed_reason(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0002", [{**_task("a"), "verify": "false"}], [
        ("a", "commit", {"sha": "abc123", "branch": "hermes-subagent/x"}),
        ("a", "verify", {"command": "false", "passed": False, "exit_code": 1,
                         "output_tail": "", "unproven_reason": "verification failed (exit 1)"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    entry = _summary_entry(run_id, "a")
    assert entry["unproven_reason"] == "verification failed (exit 1)"
    assert entry["verify"]["passed"] is False
    assert entry["verdict"] == "verify-failed"
    view = runs.run_view(run_id)["tasks"][0]
    assert view["status"] == "unproven" and view["status_reason"] == "verification failed (exit 1)"


def test_read_only_task_with_a_final_result_is_settled_not_paused(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0003", [{**_task("a"), "policy": "read-only"}], [
        ("a", "session", {"session_id": "s"}),
        ("a", "text", {"text": "the answer is 42"}),
        ("a", "result", {"ok": True})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.all_paused() == []
    entry = _summary_entry(run_id, "a")
    assert entry["ok"] is True and entry["text"] == "the answer is 42"
    assert entry["unproven_reason"] is None and not entry["paused_id"]
    view = runs.run_view(run_id)
    assert view["state"] == "finished" and view["tasks"][0]["status"] == "done"
    assert not [e for e in RunLog(run_id).read_all() if e.kind == "interrupted"]


def test_write_task_whose_engine_failed_is_settled_as_failed(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0004", [_task("a")], [
        ("a", "session", {"session_id": "s"}),
        ("a", "error", {"message": "engine reported failure: boom"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.all_paused() == []
    entry = _summary_entry(run_id, "a")
    assert entry["ok"] is False and entry["commit"] == ""
    assert entry["unproven_reason"] == "engine reported failure: boom"
    view = runs.run_view(run_id)["tasks"][0]
    assert view["status"] == "failed" and view["status_reason"] == "engine reported failure: boom"


def test_write_task_with_an_ok_result_but_no_commit_is_paused(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0005", [_task("a")], [
        ("a", "session", {"session_id": "s"}),
        ("a", "result", {"ok": True})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    record = pauses.load(pauses.pause_id(run_id, "a"))
    assert record is not None and record.reason == REASON


def test_result_from_before_a_limit_is_not_final(repo, monkeypatch):
    run_id = _hand_run(repo, "hand0006", [{**_task("a"), "policy": "read-only"}], [
        ("a", "error", {"message": "You've hit your usage limit."}),
        ("a", "limit", {"message": "limit", "resets_at": None, "next": "gemini"})])
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    assert pauses.load(pauses.pause_id(run_id, "a")) is not None


def test_crash_during_the_engine_slot_wait_still_records_the_worktree(repo, monkeypatch):
    @contextmanager
    def dying_slot(*args, **kwargs):
        raise SystemExit("simulated gateway os._exit")
        yield  # pragma: no cover

    monkeypatch.setattr("hermes_control.slots.engine_slot", dying_slot)
    run_id = _start_and_crash(repo, monkeypatch)
    assert not [e for e in RunLog(run_id).read_all() if e.kind == "spawned"]
    _past_grace(monkeypatch)

    assert recover.reconcile(run_id) is True

    record = pauses.load(pauses.pause_id(run_id, "a"))
    assert record is not None and record.worktree is not None
    assert record.worktree["branch"] == f"hermes-subagent/subagent-control-{run_id}-a"
    assert Path(record.worktree["path"]).is_dir()
    assert Path(record.worktree["repo_root"]).resolve() == repo.resolve()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=record.worktree["path"], check=True,
                          capture_output=True, text=True).stdout.strip()
    assert record.worktree["base_commit"] == head
    assert record.pid is None and record.started is None


def test_list_runs_reconciles_only_runs_that_are_not_settled(repo, monkeypatch):
    done = _hand_run(repo, "hand0007", [_task("a")], [])
    log = RunLog(done)
    log.append_next("", [RawEvent(kind="summary", payload={"tasks": [
        {"task_id": "a", "engine": "claude", "ok": True, "unproven_reason": None,
         "commit": "abc", "paused_id": None}]})], engine="", now=time.time())
    dead = _hand_run(repo, "hand0008", [_task("a")], [("a", "session", {"session_id": "s"})])
    seen = []
    monkeypatch.setattr(recover, "reconcile", lambda run_id: seen.append(run_id) or False)

    listed = runs.list_runs()

    assert {item["run_id"] for item in listed} == {done, dead}
    assert seen == [dead]


def test_list_runs_survives_recover_failing_to_import(repo, monkeypatch, caplog):
    import hermes_control

    dead = _hand_run(repo, "hand0009", [_task("a")], [("a", "session", {"session_id": "s"})])
    monkeypatch.delattr(hermes_control, "recover")
    monkeypatch.setitem(__import__("sys").modules, "hermes_control.recover", None)

    with caplog.at_level("WARNING"):
        listed = runs.list_runs()

    assert [item["run_id"] for item in listed] == [dead]
    assert any("reconcile" in record.getMessage() for record in caplog.records)


def test_spawned_is_logged_after_the_drain_threads_start(repo, monkeypatch):
    _setup_engines(monkeypatch, crash_tasks=())
    seen = []

    def start_time(pid):
        names = [thread.name for thread in threading.enumerate()]
        seen.append(any("_drain_stderr" in n for n in names)
                    and any("_read_stdout" in n for n in names))
        return "Fri Oct 23 22:00:00 2026"

    monkeypatch.setattr(dispatch, "process_start_time", start_time)

    out = dispatch.start_dispatch([_task("a")], parent_cwd=str(repo), wait=30)

    assert out["state"] == "finished"
    assert seen == [True]
