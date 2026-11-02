"""Configured engine fallback order and model choices across every move path."""

import pytest
from scripted_engine import ScriptedEngine, _reading, repo  # noqa: F401

from hermes_control import dispatch, limits, model_policy, parallel, routing
from hermes_control.engines import registry
from hermes_control.engines.base import ControlTask
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog


FALLBACK = {
    "codex": ["gemini", "claude"],
    "claude": ["gemini", "codex/gpt-6-astra"],
    "gemini": ["codex", "claude"],
}
MODELS = {
    "claude": {"default": "claude-sonnet-5-5", "allowed": {
        "claude-sonnet-5-5": {"effort": "high"}}},
    "codex": {"default": "gpt-6.1-sol", "allowed": {
        "gpt-6.1-sol": {"effort": "medium"}, "gpt-6-astra": {"effort": "xhigh"}}},
    "gemini": {"default": "gemini-default", "allowed": {
        "gemini-default": {"effort": "low"}, "claude-sonnet-5-5": {"effort": "medium"}}},
}


class ScriptedGeminiEngine(ScriptedEngine):
    """Keep Gemini's model guard while scripting worker outcomes."""

    efforts = GeminiAdapter.efforts
    model_id = GeminiAdapter.model_id


@pytest.fixture
def setup(monkeypatch):
    def configure(fallback=FALLBACK, *, enabled=True, models=MODELS):
        names = ("claude", "codex", "gemini") if enabled else ("claude", "codex")
        for module in (registry, routing, dispatch, model_policy, parallel):
            monkeypatch.setattr(module, "ENGINE_NAMES", names)
        settings = {"models": models}
        if fallback is not None:
            settings["routing"] = {"fallback": fallback}
        monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
            "plugins": {"entries": {"hermes_control": {"settings": settings}}}})
        engines = {name: (ScriptedGeminiEngine if name == "gemini" else ScriptedEngine)(
            name, "ok") for name in names}
        monkeypatch.setattr(dispatch, "get_adapter", engines.__getitem__)
        monkeypatch.setattr(dispatch, "find_sandbox", lambda: None)
        return engines
    return configure


def run(repo, *, engine="claude", model="claude-sonnet-5-5", effort="high"):
    out = dispatch.dispatch([{"id": "task", "goal": "review the diff", "why": "reviewer",
                              "engine": engine, "model": model, "effort": effort,
                              "policy": "write"}], parent_cwd=str(repo), run_id="fallback")
    assert "error" not in out, out
    return out["tasks"][0]


def test_codex_cap_tries_gemini_before_claude(repo, monkeypatch, setup):
    engines = setup()
    _reading(monkeypatch, "codex", 95)
    entry = run(repo, engine="codex", model="gpt-6.1-sol")
    assert entry["engine"] == "gemini" and entry["ok"]
    assert engines["claude"].tasks == []
    assert entry["rerouted"]["model_rule"] == "default"
    assert (engines["gemini"].tasks[0].model, engines["gemini"].tasks[0].effort) == (
        "gemini-default", "low")


def test_explicit_gemini_fallback_model_without_default(repo, monkeypatch, setup):
    engines = setup({"codex": ["gemini/explicit"]}, models={**MODELS, "gemini": {}})
    _reading(monkeypatch, "codex", 95)
    entry = run(repo, engine="codex", model="gpt-6.1-sol")
    assert entry["engine"] == "gemini" and entry["ok"]
    assert engines["gemini"].tasks[0].model == "explicit"
    assert entry["rerouted"]["model_rule"] == "fallback_entry"


def test_gemini_fallback_without_model_refuses_worker(repo, monkeypatch, setup):
    engines = setup(models={**MODELS, "gemini": {}})
    _reading(monkeypatch, "codex", 95)
    entry = run(repo, engine="codex", model="gpt-6.1-sol")
    assert entry["engine"] == "gemini" and not entry["ok"]
    assert "settings.models.gemini.default" in entry["unproven_reason"]
    assert engines["gemini"].tasks == []


@pytest.mark.parametrize("path", ["signin", "cap", "limit", "resume"])
@pytest.mark.parametrize("enabled", [True, False])
def test_review_keeps_model_or_uses_astra_across_move_paths(
        repo, monkeypatch, setup, path, enabled):
    engines = setup(enabled=enabled)
    if path == "signin":
        monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    elif path == "cap":
        _reading(monkeypatch, "claude", 95)
    elif path == "limit":
        engines["claude"].outcomes = ["limit"]
    else:
        for name in engines:
            _reading(monkeypatch, name, 95)
        # Gemini's quota is report-only; a dashboard pause holds it for resume.
        monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
    entry = run(repo)
    if path == "resume":
        assert entry["paused"]
        monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": False})
        for name in engines:
            if name != "claude":
                _reading(monkeypatch, name, 5)
        entry = dispatch.resume_paused(entry["paused"]["id"])
    destination = "gemini" if enabled else "codex"
    expected = ("claude-sonnet-5-5", "high") if enabled else ("gpt-6-astra", "xhigh")
    assert entry["engine"] == destination and entry["ok"]
    assert (engines[destination].tasks[-1].model, engines[destination].tasks[-1].effort) == expected
    note = entry["rerouted"]
    assert note["from"] == "claude"
    assert note["model_rule"] == ("kept" if enabled else "fallback_entry")
    if enabled:
        assert "dropped_model" not in note and "dropped_effort" not in note
    else:
        assert note["dropped_model"] == "claude-sonnet-5-5"
        assert note["dropped_effort"] == "high"
    if path == "signin":
        plan = next(e for e in RunLog("fallback").read_all() if e.kind == "plan")
        assert plan.payload["tasks"][0]["rerouted"]["model_rule"] == note["model_rule"]


@pytest.mark.parametrize("path", ["signin", "cap", "limit", "resume"])
def test_empty_list_pauses_instead_of_using_unlisted_engines(repo, monkeypatch, setup, path):
    engines = setup({"claude": []})
    if path == "signin":
        monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    elif path == "limit":
        engines["claude"].outcomes = ["limit"]
    else:
        _reading(monkeypatch, "claude", 95)
    entry = run(repo)
    assert entry["paused"] and not entry["ok"]
    assert engines["gemini"].tasks == engines["codex"].tasks == []
    if path == "resume":
        with pytest.raises(dispatch.NotReady):
            dispatch.resume_paused(entry["paused"]["id"])
        _reading(monkeypatch, "claude", 5)
        entry = dispatch.resume_paused(entry["paused"]["id"])
        assert entry["engine"] == "claude" and entry["ok"]


@pytest.mark.parametrize("fallback", [None, {}, {"gemini": ["codex"]}])
def test_unconfigured_source_keeps_legacy_order_and_default(repo, monkeypatch, setup, fallback):
    # Even a shared allowed name must not change the absent-setting behavior.
    models = {**MODELS, "codex": {"default": "gpt-6.1-sol", "allowed": {
        "gpt-6.1-sol": {"effort": "medium"}, "claude-sonnet-5-5": {"effort": "low"}}}}
    engines = setup(fallback, models=models)
    _reading(monkeypatch, "claude", 95)
    entry = run(repo)
    assert entry["engine"] == "codex" and entry["ok"]
    assert (engines["codex"].tasks[-1].model, engines["codex"].tasks[-1].effort) == (
        "gpt-6.1-sol", "medium")
    if fallback:
        assert entry["rerouted"]["model_rule"] == "default"
    else:
        assert "model_rule" not in entry["rerouted"]


def test_unlisted_available_engine_is_never_tried(repo, monkeypatch, setup):
    engines = setup({"claude": ["gemini"]})
    _reading(monkeypatch, "claude", 95)
    monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": True})
    entry = run(repo)
    assert entry["paused"] and engines["codex"].tasks == []


@pytest.mark.parametrize("effort,expected,dropped", [("high", "high", False),
                                                     ("ultra", "medium", True)])
def test_kept_model_effort_is_checked_against_destination(setup, effort, expected, dropped):
    setup()
    policy = model_policy.load_policy()
    fallback = routing.read_fallback(policy)
    task = ControlTask("t", "g", "codex", ExecPolicy(write=False),
                       model="claude-sonnet-5-5", effort=effort)
    moved, note = routing.move(task, "gemini", "held", policy=policy, fallback=fallback)
    assert (moved.model, moved.effort) == ("claude-sonnet-5-5", expected)
    assert note["model_rule"] == "kept" and "dropped_model" not in note
    assert ("dropped_effort" in note) is dropped


def test_kept_model_precedes_explicit_fallback_model(setup):
    setup({"claude": ["gemini/gemini-default"]})
    task = ControlTask("t", "g", "claude", ExecPolicy(write=False), model="claude-sonnet-5-5")
    moved, note = routing.move(task, "gemini", "held", policy=model_policy.load_policy(),
                               fallback=routing.read_fallback())
    assert (moved.model, moved.effort, note["model_rule"]) == (
        "claude-sonnet-5-5", "medium", "kept")


def test_unrestricted_destination_does_not_imply_shared_model(setup):
    setup({"claude": ["gemini/explicit"]}, models={"gemini": {"default": "default"}})
    task = ControlTask("t", "g", "claude", ExecPolicy(write=False), model="claude-sonnet-5-5")
    moved, note = routing.move(task, "gemini", "held", policy=model_policy.load_policy(),
                               fallback=routing.read_fallback())
    assert moved.model == "explicit" and note["model_rule"] == "fallback_entry"


@pytest.mark.parametrize("raw,message", [
    ([], "mapping"), ({"other": []}, "unknown engine.*other"),
    ({"codex": ["other"]}, "unknown engine.*other"),
    ({"codex": ["other/model"]}, "unknown engine.*other"),
    ({"codex": ["codex"]}, "own"), ({"codex": ["codex/gpt-6-astra"]}, "own"),
    ({"codex": "claude"}, "list"), ({"codex": None}, "list"),
    ({"codex": [1]}, "string"), ({"codex": [""]}, "engine"),
    ({"claude": ["codex/"]}, "model"),
    ({"claude": ["codex/not-allowed"]}, "not.*allowed"),
])
def test_validation_errors_when_read(setup, raw, message):
    setup(raw)
    with pytest.raises(ValueError, match=message):
        routing.read_fallback()


def test_disabled_engine_warns_once_and_skips_entries_but_keeps_its_list(setup, caplog, monkeypatch):
    # A disabled engine is never a destination. Its own list stays as plain data
    # so its planned or paused tasks still follow the user's order (codex can be
    # turned off); it reaches no planner text (see test_codex_optional).
    setup(enabled=False)
    monkeypatch.setattr(routing, "_warn_disabled", routing._warn_disabled.__wrapped__)
    # Give this test its own once-per-process cache.
    import functools
    monkeypatch.setattr(routing, "_warn_disabled", functools.cache(routing._warn_disabled))
    for _ in range(3):
        settings = routing.read_fallback()
        assert settings == {"claude": ["codex/gpt-6-astra"], "codex": ["claude"],
                            "gemini": ["codex", "claude"]}
    assert sum(r.name == routing.__name__ and "disabled" in r.message and "gemini" in r.message
               for r in caplog.records) == 1


@pytest.mark.parametrize("raw", [{"claude": ["unknown"]}, {"claude": ["codex/nope"]}])
def test_invalid_fallback_logs_warning_and_dispatch_uses_legacy(repo, monkeypatch, setup, caplog, raw):
    dispatch._warn_invalid_fallback.cache_clear()  # isolate the once-per-process warning
    engines = setup(raw)
    _reading(monkeypatch, "claude", 95)
    entry = run(repo)
    assert entry["engine"] == "codex" and entry["ok"]
    assert engines["codex"].tasks[-1].model == "gpt-6.1-sol"
    assert "fallback settings are invalid" in caplog.text


def test_usage_handover_after_signin_move_cannot_leave_planned_list(repo, monkeypatch, setup):
    engines = setup({"claude": ["gemini"], "gemini": ["codex/gpt-6-astra"]})
    monkeypatch.setattr(engines["claude"], "auth_problem", lambda info: "signed out")
    engines["gemini"].outcomes = ["limit"]
    entry = run(repo)
    assert entry["engine"] == "gemini" and entry["paused"]
    assert engines["codex"].tasks == []
    assert entry["rerouted"]["from"] == "claude"


def test_only_disabled_destination_still_forbids_other_engines(repo, monkeypatch, setup):
    engines = setup({"claude": ["gemini"]}, enabled=False)
    _reading(monkeypatch, "claude", 95)
    entry = run(repo)
    assert entry["paused"] and engines["codex"].tasks == []


def test_default_rule_does_not_report_unchanged_model_or_effort(setup):
    setup(None)
    task = ControlTask("t", "g", "gemini", ExecPolicy(write=False),
                       model="claude-sonnet-5-5", effort="high")
    moved, note = routing.move(task, "claude", "held", policy=model_policy.load_policy())
    assert (moved.model, moved.effort) == (task.model, task.effort)
    assert note == {"from": "gemini", "reason": "held"}


def test_resumed_limit_handover_does_not_stack_continuation_instructions(repo, monkeypatch, setup):
    engines = setup({"claude": ["gemini", "codex/gpt-6-astra"],
                     "gemini": ["codex/gpt-6-astra"]})
    engines["claude"].outcomes = ["limit"]
    engines["gemini"].outcomes = ["limit", "limit"]
    entry = run(repo)
    assert entry["paused"]
    _reading(monkeypatch, "claude", blocked=True)
    entry = dispatch.resume_paused(entry["paused"]["id"])
    assert entry["engine"] == "codex" and entry["ok"]
    goal = engines["codex"].tasks[0].goal
    assert goal.count("An earlier worker") == 1
    assert goal.count(dispatch.WORK_STYLE) == 1
    assert goal.startswith(dispatch.WORK_STYLE + "\n\nTask: review the diff\n\n")


def test_legacy_free_engine_uses_dispatch_availability_order(monkeypatch):
    monkeypatch.setattr(dispatch, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    monkeypatch.setattr(dispatch.limits, "engine_hold", lambda engine, caps: None)
    ctx = dispatch._Context("available", RunLog("available"), None, ["gemini"], {}, {}, None)
    assert dispatch._free_engine(ctx, besides="codex") == "gemini"
