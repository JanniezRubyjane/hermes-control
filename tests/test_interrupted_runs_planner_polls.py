"""A17: planner status polls must not keep an interrupted run running forever."""

import time

import pytest

from hermes_control import dispatch, recover, runs
from hermes_control.runlog import RunLog
from tests.scripted_engine import repo  # noqa: F401
from tests.test_interrupted_runs import _hand_run, _past_grace, _task


@pytest.mark.parametrize("planner_polls", [0, 3], ids=["without-polls", "recent-polls"])
def test_list_runs_reconciles_interrupted_run_despite_recent_planner_polls(
        repo, monkeypatch, planner_polls):
    run_id = _hand_run(repo, "a1700001", [_task("a")], [
        ("a", "session", {"session_id": "s"}),
        ("a", "result", {"ok": True}),
    ])
    # Without polling this run already looks finished; polling makes it look running.
    # Either way the missing summary must cause the list to reconcile it.
    _past_grace(monkeypatch, runs.STALE_SECONDS + recover.GRACE_SECONDS)
    for _ in range(planner_polls):
        _past_grace(monkeypatch, 1.0)
        dispatch._log_status_checked(run_id, "running", 0.0)

    events = RunLog(run_id).read_all()
    last_engine_event = next(e for e in reversed(events) if e.kind != "planner")
    assert time.time() - last_engine_event.ts > recover.GRACE_SECONDS
    polls = [e for e in events if e.kind == "planner"]
    assert len(polls) == planner_polls
    assert all(0 <= time.time() - e.ts < recover.GRACE_SECONDS for e in polls)
    assert all(e.payload == {"phase": "status_checked", "state": "running",
                             "waited_seconds": 0.0} for e in polls)
    assert dispatch._run_is_live(run_id) is False
    assert not any(e.kind == "summary" for e in events)
    view = runs.run_view(run_id, now=time.time)
    assert view["state"] == ("running" if planner_polls else "finished")
    assert view["updated_at"] == events[-1].ts

    listed = runs.list_runs(now=time.time)

    entry, = [item for item in listed if item["run_id"] == run_id]
    assert entry["state"] == "paused"
    assert entry["tasks"][0]["status"] == "paused"
    settled_events = RunLog(run_id).read_all()
    assert settled_events[:len(events)] == events
    assert len([e for e in settled_events if e.kind == "summary"]) == 1
    assert settled_events[-1].kind == "summary"
