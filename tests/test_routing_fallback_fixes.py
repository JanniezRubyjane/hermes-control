"""Regressions for routing list ownership, validation and visible settings errors."""

import pytest
from scripted_engine import _reading, repo  # noqa: F401
from test_routing_fallback import MODELS, run, setup  # noqa: F401

from hermes_control import dispatch, limits, model_policy, pauses, routing
from hermes_control.engines.base import ControlTask
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog


def test_exact_review_trace_cap_then_codex_limit_never_reaches_gemini(repo, monkeypatch, setup):
    engines = setup({"claude": ["codex/gpt-6-astra"]})
    _reading(monkeypatch, "claude", 95)
    engines["codex"].outcomes = ["limit"]
    entry = run(repo)
    assert entry["paused"] and entry["engine"] == "codex"
    assert engines["codex"].tasks[0].model == "gpt-6-astra"
    assert engines["claude"].tasks == engines["gemini"].tasks == []
    record = pauses.load(entry["paused"]["id"])
    _reading(monkeypatch, "codex", blocked=True)
    _reading(monkeypatch, "claude", 5)
    assert dispatch.resume_blocker(record)[1] is None
    with pytest.raises(dispatch.NotReady):
        dispatch.resume_paused(record.id)
    assert engines["gemini"].tasks == []


@pytest.mark.parametrize("path", ["pause", "limit", "resume"])
@pytest.mark.parametrize("middle_list", [None, ["claude", "codex/gpt-6.1-sol"]])
def test_later_moves_use_planned_list_and_entry(repo, monkeypatch, setup, path, middle_list):
    fallback = {"claude": ["gemini", "codex/gpt-6-astra"]}
    if middle_list is not None:
        fallback["gemini"] = middle_list
    engines = setup(fallback)
    if path == "resume":
        engines["claude"].outcomes = ["limit"]
        engines["gemini"].outcomes = ["limit"]
    else:
        monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
        if path == "pause":
            monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
        else:
            engines["gemini"].outcomes = ["limit"]
    entry = run(repo)
    if path == "resume":
        assert entry["paused"]
        monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
        # The planned engine is usable again, but already tried.
        _reading(monkeypatch, "claude", 5)
        record = pauses.load(entry["paused"]["id"])
        assert dispatch.resume_blocker(record)[1] == "codex"
        entry = dispatch.resume_paused(record.id)
    assert entry["engine"] == "codex" and entry["ok"]
    assert engines["codex"].tasks[0].model == "gpt-6-astra"
    assert entry["rerouted"]["model_rule"] == "fallback_entry"


def test_later_move_kept_model_still_precedes_planned_entry(repo, monkeypatch, setup):
    models = {**MODELS, "codex": {"default": "gpt-6.1-sol", "allowed": {
        "gpt-6.1-sol": {}, "gpt-6-astra": {}, "claude-sonnet-5-5": {"effort": "high"}}}}
    engines = setup({"claude": ["gemini", "codex/gpt-6-astra"]}, models=models)
    monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    engines["gemini"].outcomes = ["limit"]
    entry = run(repo)
    assert engines["codex"].tasks[0].model == "claude-sonnet-5-5"
    assert entry["rerouted"]["model_rule"] == "kept"


def test_unconfigured_planned_engine_uses_current_list(repo, monkeypatch, setup):
    engines = setup({"codex": ["gemini/gemini-default"]})
    monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    engines["codex"].outcomes = ["limit"]
    entry = run(repo)
    assert entry["ok"] and entry["engine"] == "gemini"
    assert engines["gemini"].tasks[0].model == "gemini-default"
    assert entry["rerouted"]["model_rule"] == "fallback_entry"


@pytest.mark.parametrize("entries", [["codex", "codex/gpt-6-astra"],
                                    ["codex/gpt-6-astra", "codex"], ["codex", "codex"],
                                    ["codex/gpt-6.1-sol", "codex/gpt-6-astra"]])
def test_validation_errors_when_read(setup, entries):
    setup({"claude": entries})
    with pytest.raises(ValueError, match="duplicate.*codex"):
        routing.read_fallback()


def test_duplicate_disabled_destinations_are_rejected(setup):
    setup({"claude": ["gemini", "gemini/model"]}, enabled=False)
    with pytest.raises(ValueError, match="duplicate.*gemini"):
        routing.read_fallback()


@pytest.mark.parametrize("fallback", [{"claude": []}, {"gemini": ["codex"]}])
def test_no_usable_engine_always_fails_fast(repo, monkeypatch, setup, fallback):
    engines = setup(fallback)
    for engine in engines.values():
        monkeypatch.setattr(engine, "auth_problem", lambda info: "signed out")
    task = ControlTask("t", "g", "claude", ExecPolicy(write=False))
    with pytest.raises(ValueError, match="no engine can take work right now"):
        routing.route([task], {name: "signed out" for name in engines}, fallback=fallback)
    out = dispatch.dispatch([{"id": "t", "goal": "g", "engine": "claude", "why": "review"}],
                            parent_cwd=str(repo), run_id="unusable")
    assert "no engine can take work right now" in out["error"]
    assert pauses.all_paused() == []


@pytest.mark.parametrize("background", [False, True])
def test_invalid_settings_are_visible_in_plan_and_summary(repo, monkeypatch, setup, background):
    setup({"claude": ["unknown"]})
    _reading(monkeypatch, "claude", 95)
    call = dispatch.start_dispatch if background else dispatch.dispatch
    out = call([{"id": "t", "goal": "g", "engine": "claude", "why": "review"}],
               parent_cwd=str(repo),
               **({"wait": 0} if background else {"run_id": "bad-settings"}))
    run_id = out["run_id"]
    if background:
        out = dispatch.control_status(run_id, wait=10)
    events = RunLog(run_id).read_all()
    plan = next(e.payload for e in events if e.kind == "plan")
    summary = next(e.payload for e in events if e.kind == "summary")
    assert "unknown engine" in plan["routing"]["error"]
    assert summary["routing"] == out["routing"] == plan["routing"]
    dispatch._FINISHED.clear()  # A fresh dashboard process must also see the error.
    assert dispatch.control_status(run_id)["routing"] == plan["routing"]


def test_invalid_settings_warning_is_once_per_process_during_polling(repo, monkeypatch, setup, caplog):
    engines = setup({"claude": []})
    _reading(monkeypatch, "claude", 95)
    entry = run(repo)
    record = pauses.load(entry["paused"]["id"])
    setup({"claude": ["unknown"]})
    # Isolate the process-level warning latch from other tests.
    dispatch._warn_invalid_fallback.cache_clear()
    for _ in range(4):
        dispatch.resume_blocker(record)
    assert sum("fallback settings are invalid" in r.message for r in caplog.records) == 1
    assert engines["codex"].tasks == []


@pytest.mark.parametrize("fallback", [{"claude": []}, {"claude": ["codex"]}])
def test_signed_out_task_with_no_destination_has_plan_explanation(repo, monkeypatch, setup, fallback):
    engines = setup(fallback)
    for name in ("claude", "codex"):
        monkeypatch.setattr(engines[name], "auth_problem", lambda info: "signed out")
    entry = run(repo)
    assert entry["paused"] and entry["engine"] == "claude"
    plan = next(e.payload for e in RunLog("fallback").read_all() if e.kind == "plan")
    note = plan["tasks"][0]["rerouted"]
    assert note["from"] == "claude"
    assert "signed out" in note["reason"]
    assert "no usable" in note["reason"] and "fallback" in note["reason"]
    assert engines["gemini"].tasks == []


def test_absent_fallback_preserves_legacy_note_shape(setup):
    setup(None, enabled=False)
    task = ControlTask("t", "g", "claude", ExecPolicy(write=False), model="claude-sonnet-5-5")
    _, note = routing.move(task, "codex", "held", policy=model_policy.load_policy())
    assert note == {"from": "claude", "reason": "held", "dropped_model": task.model}


def test_old_pause_note_without_source_can_use_a_new_fallback(setup):
    setup({"claude": ["codex/gpt-6-astra"]})
    assert routing.fallback_order("claude", routing.read_fallback(),
                                  earlier={"dropped_effort": "high"}) == ["codex"]


def test_resume_does_not_revisit_a_tried_listed_destination(repo, monkeypatch, setup):
    engines = setup({"claude": ["gemini", "codex/gpt-6-astra"]})
    engines["claude"].outcomes = ["limit"]
    engines["gemini"].outcomes = ["limit"]
    engines["codex"].outcomes = ["limit"]
    entry = run(repo)
    assert entry["paused"] and entry["engine"] == "gemini"
    monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
    entry = dispatch.resume_paused(entry["paused"]["id"])
    assert entry["paused"] and entry["engine"] == "codex"
    _reading(monkeypatch, "codex", blocked=True)
    monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": False})
    record = pauses.load(entry["paused"]["id"])
    assert dispatch.resume_blocker(record)[1] is None
    with pytest.raises(dispatch.NotReady):
        dispatch.resume_paused(record.id)
    assert len(engines["claude"].tasks) == len(engines["gemini"].tasks) == 1


def test_settings_read_failure_is_visible_and_keeps_legacy_routing(repo, monkeypatch, setup):
    setup(None, enabled=False)

    def unavailable():
        raise OSError("settings unavailable")

    monkeypatch.setattr(routing, "read_settings", unavailable)
    _reading(monkeypatch, "claude", 95)
    out = dispatch.dispatch([{"id": "t", "goal": "g", "engine": "claude", "why": "review"}],
                            parent_cwd=str(repo), run_id="read-failure")
    assert out["tasks"][0]["engine"] == "codex" and out["tasks"][0]["ok"]
    assert out["routing"] == {"error": "OSError: settings unavailable"}
    events = RunLog("read-failure").read_all()
    assert all(e.payload["routing"] == out["routing"]
               for e in events if e.kind in ("plan", "summary"))


def test_routing_error_survives_resume_in_a_fresh_process(repo, monkeypatch, setup):
    setup({"claude": ["unknown"]})
    for name in ("claude", "codex"):
        _reading(monkeypatch, name, 95)
    monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
    entry = run(repo)
    assert entry["paused"]
    plan = next(e.payload for e in RunLog("fallback").read_all() if e.kind == "plan")
    setup(None)
    _reading(monkeypatch, "claude", 5)
    dispatch._FINISHED.clear()
    assert dispatch.resume_paused(entry["paused"]["id"])["ok"]
    summaries = [e.payload for e in RunLog("fallback").read_all() if e.kind == "summary"]
    assert summaries[-1]["routing"] == plan["routing"]
    assert dispatch.control_status("fallback")["routing"] == plan["routing"]


@pytest.mark.parametrize("path", ["signin", "cap", "limit", "resume"])
def test_gemini_off_without_fallback_keeps_legacy_engine_and_model(repo, monkeypatch, setup, path):
    engines = setup(None, enabled=False)
    if path == "signin":
        monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    elif path == "limit":
        engines["claude"].outcomes = ["limit"]
    else:
        _reading(monkeypatch, "claude", 95)
        if path == "resume":
            _reading(monkeypatch, "codex", 95)
    entry = run(repo)
    if path == "resume":
        assert entry["paused"]
        _reading(monkeypatch, "codex", 5)
        entry = dispatch.resume_paused(entry["paused"]["id"])
    assert entry["ok"] and entry["engine"] == "codex"
    assert (engines["codex"].tasks[0].model, engines["codex"].tasks[0].effort) == (
        "gpt-6.1-sol", "medium")
    assert "model_rule" not in entry["rerouted"]
