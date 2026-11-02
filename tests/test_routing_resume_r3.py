"""Resume routing diagnostics and accurate reporting of tasks that stay put."""

import pytest
from scripted_engine import _reading, repo  # noqa: F401
from test_routing_fallback import run, setup  # noqa: F401

from hermes_control import dispatch, review_pack, routing, runs, watch
from hermes_control.engines.base import ControlTask
from hermes_control.events import RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog


@pytest.mark.parametrize("paused_again", [False, True])
@pytest.mark.parametrize("fresh_process", [False, True])
@pytest.mark.parametrize("original_error", [False, True])
def test_resume_reports_current_routing_error(
        repo, monkeypatch, setup, paused_again, fresh_process, original_error):
    setup({"claude": ["old-unknown"]} if original_error else
          {"claude": ["codex/gpt-6-astra"]}, enabled=False)
    for name in ("claude", "codex"):
        _reading(monkeypatch, name, 95)
    entry = run(repo)
    assert entry["paused"] and entry["engine"] == "claude"
    engines = setup({"claude": ["new-unknown"]}, enabled=False)
    _reading(monkeypatch, "codex", 5)
    if paused_again:
        engines["codex"].outcomes = ["limit"]
    if fresh_process:
        dispatch._FINISHED.clear()
    entry = dispatch.resume_paused(entry["paused"]["id"])
    assert bool(entry["paused"]) == paused_again
    events = RunLog("fallback").read_all()
    resumed = next(e.payload for e in events if e.kind == "resumed")
    summary = [e.payload for e in events if e.kind == "summary"][-1]
    assert "new-unknown" in resumed["routing"]["error"]
    assert entry["routing"] == summary["routing"] == resumed["routing"]
    assert dispatch.control_status("fallback")["routing"] == resumed["routing"]


def test_valid_resume_adds_no_routing_keys(repo, monkeypatch, setup):
    setup({"claude": ["codex/gpt-6-astra"]}, enabled=False)
    for name in ("claude", "codex"):
        _reading(monkeypatch, name, 95)
    entry = run(repo)
    _reading(monkeypatch, "codex", 5)
    entry = dispatch.resume_paused(entry["paused"]["id"])
    assert entry["ok"] and "routing" not in entry
    assert all("routing" not in e.payload for e in RunLog("fallback").read_all()
               if e.kind in ("plan", "resumed", "summary"))


def test_resume_without_error_keeps_plan_diagnostic(repo, monkeypatch, setup):
    setup({"claude": ["unknown"]}, enabled=False)
    for name in ("claude", "codex"):
        _reading(monkeypatch, name, 95)
    entry = run(repo)
    plan = next(e.payload for e in RunLog("fallback").read_all() if e.kind == "plan")
    setup({"claude": ["codex/gpt-6-astra"]}, enabled=False)
    _reading(monkeypatch, "codex", 5)
    dispatch._FINISHED.clear()
    entry = dispatch.resume_paused(entry["paused"]["id"])
    assert entry["ok"] and "routing" not in entry
    events = RunLog("fallback").read_all()
    assert "routing" not in next(e.payload for e in events if e.kind == "resumed")
    assert [e.payload for e in events if e.kind == "summary"][-1]["routing"] == plan["routing"]


def test_stayed_note_is_reported_as_staying(repo, monkeypatch, setup):
    engines = setup({"claude": []})
    monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    entry = run(repo)
    note = entry["rerouted"]
    assert note["stayed"] is True
    assert review_pack.build("fallback")["tasks"][0]["moved"] is False
    task = runs.run_view("fallback")["tasks"][0]
    rendered = "\n".join(str(line) for line in watch._task(task, now=0))
    assert f"stays on claude: {note['reason']}" in rendered
    assert "moved from" not in rendered


def test_stayed_note_does_not_hide_actual_attempt_move(repo, monkeypatch, setup):
    engines = setup({"claude": []})
    monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    run(repo)
    log = RunLog("fallback")
    log.append_next("task", [RawEvent(kind="session", payload={"model": "gpt-6-astra"})],
                    engine="codex", now=1)
    assert review_pack.build("fallback")["tasks"][0]["moved"] is True


def test_later_move_recovers_planned_source_through_stayed_note(setup):
    setup()
    task = ControlTask("t", "review", "claude", ExecPolicy(write=False))
    fallback = {"claude": ["gemini", "codex/gpt-6-astra"], "gemini": ["claude"]}
    _, stayed = routing.route([task], {"claude": "signed out", "gemini": "signed out"},
                              fallback={"claude": []})[0]
    assert stayed["stayed"] is True
    moved = {"from": "claude", "reason": "available now", "earlier": stayed}
    assert routing.fallback_source("gemini", fallback, moved) == (
        "claude", {"claude", "gemini"})
    assert routing.fallback_order("gemini", fallback, earlier=moved) == ["codex"]


@pytest.mark.parametrize("read_first", [False, True])
def test_read_and_validation_warnings_have_separate_process_latches(
        monkeypatch, setup, caplog, read_first):
    setup({"claude": ["unknown"]})
    dispatch._warn_invalid_fallback.cache_clear()
    dispatch._warn_routing_read_failure.cache_clear()

    def unreadable():
        raise OSError("settings unavailable")

    def read_failure():
        with monkeypatch.context() as patch:
            patch.setattr(routing, "read_settings", unreadable)
            for _ in range(3):
                assert dispatch._routing_settings()[1] == "OSError: settings unavailable"

    def invalid_fallback():
        for _ in range(3):
            assert "unknown engine" in dispatch._fallback()[1]

    for action in ((read_failure, invalid_fallback) if read_first else
                   (invalid_fallback, read_failure)):
        action()
    assert sum(r.message == "hermes-control: could not read routing settings; using the defaults"
               for r in caplog.records) == 1
    assert sum(r.message == "hermes-control: fallback settings are invalid; using the defaults"
               for r in caplog.records) == 1
