"""A dispatch that outlives Hermes's tool-call deadline must not lose the run: the
planner gets back an early "still running" summary while the work keeps going on a
daemon thread, and control_status() lets it check back later. See the docstring on
start_dispatch() for the failure this fixes - the tool call timing out on the
planner's side while the workers ran on and finished fine.
"""

import json
import time

from hermes_control import CONTROL_STATUS_TOOL_SCHEMA, control_status_handler
from hermes_control.dispatch import _FINISHED, _THREADS, control_status, dispatch, start_dispatch
from hermes_control.runlog import RunLog
from hermes_control import runs
from hermes_control.events import RawEvent, stamp

from test_dispatch_run import EditingAdapter, FakeAdapter, repo  # noqa: F401 - fixture


class SlowAdapter(FakeAdapter):
    """Writes a file, then sleeps before reporting - long enough to still be
    running when start_dispatch()'s wait budget elapses."""

    def build_argv(self, task, *, cwd):
        script = (
            "import time;"
            "open('new.txt', 'w', encoding='utf-8').write('made by the worker\\n');"
            "time.sleep(2);"
            "print('{\"k\": \"result\"}')"
        )
        return ["python3", "-c", script]


def _task(task_id="a"):
    return {"id": task_id, "goal": "1", "engine": "claude", "why": "x", "policy": "write"}


def _planner_phases(run_id):
    return [event.payload["phase"] for event in RunLog(run_id).read_all()
            if event.kind == "planner"]


def test_control_status_reads_paused_run_as_finished_with_entries(monkeypatch, tmp_path):
    monkeypatch.setattr("hermes_control.runlog.run_log_dir", lambda: tmp_path)
    run_id = "paused-status"
    log = RunLog(run_id)
    for task_id, kind, payload in (
        ("", "plan", {"tasks": [{"id": "a", "engine": "claude", "policy": "write"}]}),
        ("a", "paused", {"id": "paused-status-a", "reason": "limit"}),
        ("", "summary", {"tasks": [{"task_id": "a", "engine": "claude", "ok": False,
                                    "paused_id": "paused-status-a", "commit": ""}]}),
    ):
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id=run_id,
                         task_id=task_id, engine="claude", start_seq=log.next_seq(task_id),
                         now=time.time()))
    assert runs.run_view(run_id)["state"] == "paused"
    assert run_id not in _THREADS and run_id not in _FINISHED
    result = control_status(run_id)
    assert result["state"] == "finished"
    assert result["tasks"][0]["paused"] is not None


def test_a_slow_run_comes_back_running_within_its_wait_budget(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: SlowAdapter())

    out = start_dispatch([_task()], parent_cwd=str(repo), wait=0.2)

    assert out["state"] == "running"
    assert out["run_id"]
    assert "control_status" in out["note"]
    assert "do not dispatch" in out["note"]
    assert out["tasks"] and out["tasks"][0]["id"] == "a"
    assert _planner_phases(out["run_id"]) == ["waiting", "returned_running"]


def test_control_status_reports_running_then_finished(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: SlowAdapter())

    out = start_dispatch([_task()], parent_cwd=str(repo), wait=0.2)
    run_id = out["run_id"]
    thread = _THREADS.get(run_id)
    assert thread is not None, "the background thread must still be tracked while it runs"

    status = control_status(run_id)
    assert status["state"] == "running"
    assert status["tasks"][0]["id"] == "a"
    assert _planner_phases(run_id) == ["waiting", "returned_running", "status_checked"]
    checked = [event.payload for event in RunLog(run_id).read_all()
               if event.kind == "planner" and event.payload["phase"] == "status_checked"]
    assert checked[-1]["waited_seconds"] == 0.0

    thread.join(timeout=20)
    assert not thread.is_alive(), "the background dispatch never finished"

    status = control_status(run_id)
    assert status["state"] == "finished"
    entry = status["tasks"][0]
    assert entry["task_id"] == "a"
    assert entry["ok"] is True
    # Every planner event continues the run-level sequence: a status check logged
    # through a fresh RunLog must not restart at seq 0 (the trace keys entries by seq).
    seqs = [event.seq for event in RunLog(run_id).read_all() if event.kind == "planner"]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), seqs
    assert entry["commit"]
    assert entry["branch"] == f"hermes-subagent/subagent-control-{run_id}-a"
    assert run_id not in _THREADS
    assert run_id in _FINISHED


def test_background_adapter_exception_finishes_and_reports_next_task(repo, monkeypatch):
    class RaisingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            if task.id == "a":
                raise RuntimeError("cannot build argv")
            return super().build_argv(task, cwd=cwd)

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: RaisingAdapter())
    out = start_dispatch([_task("a"), _task("b")], parent_cwd=str(repo), wait=0.01)
    status = control_status(out["run_id"], wait=10)

    assert status["state"] == "finished"
    assert [entry["ok"] for entry in status["tasks"]] == [False, True]
    assert "RuntimeError: cannot build argv" in status["tasks"][0]["unproven_reason"]
    assert out["run_id"] in _FINISHED


def test_background_loop_exception_still_settles_run(repo, monkeypatch):
    def fail_loop(ctx, routed):
        raise RuntimeError("loop exploded")

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr("hermes_control.dispatch._run_routed", fail_loop)
    out = start_dispatch([_task()], parent_cwd=str(repo), wait=0.01)
    status = control_status(out["run_id"], wait=10)

    assert status["state"] == "finished"
    assert "RuntimeError: loop exploded" in status["note"]
    assert out["run_id"] in _FINISHED
    assert any(event.kind == "summary" for event in RunLog(out["run_id"]).read_all())


def test_summary_log_failure_keeps_completed_background_results(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: EditingAdapter())
    monkeypatch.setattr("hermes_control.dispatch._log_summary",
                        lambda *args: (_ for _ in ()).throw(OSError("log full")))

    out = start_dispatch([_task()], parent_cwd=str(repo), wait=10)
    status = control_status(out["run_id"], wait=10)

    assert status["tasks"][0]["ok"] is True
    assert status["tasks"][0]["commit"]
    assert "note" not in status


def test_summary_log_failure_keeps_completed_sync_results(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: EditingAdapter())
    monkeypatch.setattr("hermes_control.dispatch._log_summary",
                        lambda *args: (_ for _ in ()).throw(OSError("log full")))

    out = dispatch([_task()], parent_cwd=str(repo), run_id="sync-summary-failure")

    assert out["tasks"][0]["ok"] is True
    assert out["tasks"][0]["commit"]


def test_control_status_waits_until_a_slow_run_finishes(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: SlowAdapter())
    run_id = start_dispatch([_task()], parent_cwd=str(repo), wait=0.2)["run_id"]

    started = time.monotonic()
    status = control_status(run_id, wait=10)
    elapsed = time.monotonic() - started

    assert status["state"] == "finished"
    assert status["tasks"][0]["task_id"] == "a"
    assert status["tasks"][0]["ok"] is True
    assert elapsed < 10
    checked = [event.payload for event in RunLog(run_id).read_all()
               if event.kind == "planner" and event.payload["phase"] == "status_checked"]
    assert checked[-1]["state"] == "finished"
    assert 0 < checked[-1]["waited_seconds"] <= elapsed


def test_control_status_wait_timeout_keeps_run_running(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: SlowAdapter())
    run_id = start_dispatch([_task()], parent_cwd=str(repo), wait=0.2)["run_id"]

    started = time.monotonic()
    status = control_status(run_id, wait=0.05)
    elapsed = time.monotonic() - started

    # "running" already bounds the call from above (the worker sleeps 2 s); no
    # upper time bound here, which only flakes on a loaded machine.
    assert status["state"] == "running"
    assert elapsed >= 0.04
    checked = [event.payload for event in RunLog(run_id).read_all()
               if event.kind == "planner" and event.payload["phase"] == "status_checked"]
    assert checked[-1]["state"] == "running"
    assert 0.04 <= checked[-1]["waited_seconds"] <= elapsed
    _THREADS[run_id].join(timeout=20)


def test_control_status_clamps_wait(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: SlowAdapter())
    monkeypatch.setattr("hermes_control.dispatch.MAX_STATUS_WAIT_SECONDS", 0.05)
    run_id = start_dispatch([_task()], parent_cwd=str(repo), wait=0.2)["run_id"]

    started = time.monotonic()
    status = control_status(run_id, wait=301)
    elapsed = time.monotonic() - started

    # Still "running" means the clamp, not the 301 s, ended the wait.
    assert status["state"] == "running"
    assert elapsed >= 0.04
    _THREADS[run_id].join(timeout=20)


def test_control_status_says_a_wait_can_run_out():
    """Review: 'block until it finishes' overstated it - a run that outlasts the
    wait still comes back 'running'."""
    from hermes_control import CONTROL_STATUS_TOOL_DESCRIPTION

    for text in (CONTROL_STATUS_TOOL_DESCRIPTION,
                 CONTROL_STATUS_TOOL_SCHEMA["parameters"]["properties"]["wait"]["description"]):
        assert "runs out" in text and "running" in text


def test_control_status_schema_and_invalid_handler_wait():
    parameters = CONTROL_STATUS_TOOL_SCHEMA["parameters"]
    assert parameters["required"] == ["run_id"]
    assert parameters["properties"]["wait"]["type"] == "integer"
    assert json.loads(control_status_handler({"run_id": "no-such-run", "wait": "abc"})) == {
        "error": "no run no-such-run"}


def test_a_fast_run_finishes_within_its_wait_budget(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FakeAdapter())

    out = start_dispatch([_task()], parent_cwd=str(repo), wait=30)

    assert out["state"] == "finished"
    assert "run_log" in out
    assert out["tasks"][0]["task_id"] == "a"
    assert _planner_phases(out["run_id"]) == ["waiting", "settled", "returned"]


def test_a_validation_error_returns_immediately_and_starts_no_thread(repo):
    out = start_dispatch([{"id": "a", "goal": "1", "engine": "gemini"}],
                         parent_cwd=str(repo), wait=5)

    assert out["error"]
    assert out["tasks"] == []
    assert out["run_id"] not in _THREADS


def test_control_status_reports_no_run_for_an_unknown_id():
    started = time.monotonic()
    assert control_status("no-such-run", wait=10) == {"error": "no run no-such-run"}
    assert time.monotonic() - started < 5, "an unknown run must not wait out its 10 s"


def test_dispatch_still_reports_state_finished(repo, monkeypatch):
    """dispatch() itself (not start_dispatch()) must also carry "state": "finished",
    so the planner can treat both entry points the same way."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FakeAdapter())

    out = dispatch([_task()], parent_cwd=str(repo), run_id="reg1")

    assert out["state"] == "finished"


def test_dispatch_after_a_background_run_still_works(repo, monkeypatch):
    """A regression guard for the refactor: dispatch() must keep behaving exactly
    as before once its task loop is shared with start_dispatch()."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FakeAdapter())

    out = dispatch([_task()], parent_cwd=str(repo), run_id="reg2")

    assert out["tasks"][0]["task_id"] == "a"
    assert out["tasks"][0]["ok"] is True
