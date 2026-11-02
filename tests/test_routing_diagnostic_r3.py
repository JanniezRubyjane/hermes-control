"""Run summaries retain the latest routing error across successive resumes."""

import pytest
from scripted_engine import _reading, repo  # noqa: F401
from test_routing_fallback import setup  # noqa: F401

from hermes_control import dispatch
from hermes_control.runlog import RunLog


@pytest.mark.parametrize("plan_error,resume_a_error,resume_b_error", [
    (None, "x", None),
    (None, "x", "y"),
    (None, None, None),
    ("plan-unknown", None, None),
    ("plan-unknown", "x", None),
])
def test_latest_routing_error_survives_resumes(
        repo, monkeypatch, setup, plan_error, resume_a_error, resume_b_error):
    monkeypatch.setattr(dispatch, "_FINISHED", {})

    def configure(error, *, pause=False):
        engines = setup({"claude": [error or "codex/gpt-6-astra"]}, enabled=False)
        if pause:
            engines["codex"].outcomes = ["limit"]
        return engines

    configure(plan_error)
    for name in ("claude", "codex"):
        _reading(monkeypatch, name, 95)
    result = dispatch.start_dispatch([
        {"id": "task", "goal": "review the diff", "why": "reviewer",
         "engine": "claude", "model": "claude-sonnet-5-5", "effort": "high",
         "policy": "write"}], parent_cwd=str(repo), wait=10)
    run_id = result["run_id"]
    entry = result["tasks"][0]
    assert entry["paused"]

    def payloads(kind):
        return [event.payload for event in RunLog(run_id).read_all() if event.kind == kind]

    expected = payloads("plan")[0].get("routing")
    for error, pause in ((resume_a_error, True), (resume_b_error, False)):
        configure(error, pause=pause)
        _reading(monkeypatch, "codex", 5)
        entry = dispatch.resume_paused(entry["paused"]["id"])
        assert bool(entry["paused"]) == pause
        resumed = payloads("resumed")[-1]
        if error:
            assert error in resumed["routing"]["error"]
            expected = resumed["routing"]
        else:
            assert "routing" not in resumed
        summary = payloads("summary")[-1]
        cached = dispatch._FINISHED[run_id]
        status = dispatch.control_status(run_id)
        for view in (summary, cached, status):
            if expected is None:
                assert "routing" not in view
            else:
                assert view["routing"] == expected

    dispatch._FINISHED.clear()
    fresh_status = dispatch.control_status(run_id)
    if expected is None:
        assert "routing" not in fresh_status
        assert all("routing" not in summary for summary in payloads("summary"))
    else:
        assert fresh_status["routing"] == expected
