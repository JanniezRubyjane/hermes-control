"""agy's "Individual quota reached" error is a usage limit: the task moves on."""

import time

from scripted_engine import (  # noqa: F401 - repo is a fixture
    ScriptedEngine, _git, repo)

from hermes_control import dispatch as dispatch_module, routing
from hermes_control.dispatch import dispatch
from hermes_control.engines import registry
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import RawEvent
from hermes_control.runlog import RunLog

QUOTA = ("Individual quota reached. Please upgrade your subscription to increase "
         "your limits. Resets in 17h47m31s.")
NOW = 1_800_000_000.0


def _error(message):
    return RawEvent(kind="error", payload={"message": message, "model": "gemini-4-argon-high"})


def test_quota_reached_is_a_limit_with_the_reset_time(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)

    hit = GeminiAdapter().limit_hit([_error(QUOTA)])

    assert hit is not None and hit.engine == "gemini"
    assert abs(hit.resets_at - (NOW + 64051)) < 5


def test_quota_reached_without_a_reset_text_has_no_reset_time():
    hit = GeminiAdapter().limit_hit([_error("Individual quota reached.")])

    assert hit is not None and hit.resets_at is None


def test_unparseable_reset_text_leaves_the_reset_time_empty():
    hit = GeminiAdapter().limit_hit([_error("Individual quota reached. Resets in soon.")])

    assert hit is not None and hit.resets_at is None


def test_other_reset_durations_parse(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    adapter = GeminiAdapter()
    for text, seconds in (("47m31s", 2851), ("31s", 31), ("2h", 7200), ("1d3h", 97200),
                          ("1D 3H", 97200)):
        hit = adapter.limit_hit([_error(f"Quota reached. Resets in {text}.")])
        assert hit.resets_at == NOW + seconds, text


def test_huge_reset_numbers_never_raise():
    for digits in (400, 5000):
        hit = GeminiAdapter().limit_hit([_error(f"Quota reached. Resets in {'9' * digits}s.")])
        assert hit is not None and hit.resets_at is None


def test_reset_over_30_days_is_dropped(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    adapter = GeminiAdapter()
    assert adapter.limit_hit([_error("Quota reached. Resets in 31d.")]).resets_at is None
    assert adapter.limit_hit([_error("Quota reached. Resets in 30d.")]).resets_at == NOW + 30 * 86400


def test_worded_reset_units(monkeypatch):
    monkeypatch.setattr(time, "time", lambda: NOW)
    adapter = GeminiAdapter()
    for text, seconds in (("2 hours 30 minutes", 9000), ("1 day", 86400), ("5 mins", 300),
                          ("1 hour", 3600), ("10 secs", 10), ("1 second", 1)):
        hit = adapter.limit_hit([_error(f"Quota reached. Resets in {text}.")])
        assert hit.resets_at == NOW + seconds, text


def test_unknown_or_missing_units_give_no_reset_time():
    adapter = GeminiAdapter()
    for text in ("1 month", "3 ms", "30", "2 weeks"):
        hit = adapter.limit_hit([_error(f"Quota reached. Resets in {text}.")])
        assert hit is not None and hit.resets_at is None, text


def test_model_text_saying_invalid_model_selection_does_not_hide_a_quota_error():
    raws = [RawEvent(kind="text", payload={"message": "invalid model selection"}), _error(QUOTA)]

    assert GeminiAdapter().limit_hit(raws) is not None


def test_quota_text_outside_an_error_event_is_not_a_limit():
    message = "Individual quota reached. Resets in 1h."
    for kind in ("text", "tool_end"):
        assert GeminiAdapter().limit_hit([RawEvent(kind=kind, payload={"message": message})]) is None


def test_a_successful_result_wins_over_quota_text():
    raws = [_error(QUOTA), RawEvent(kind="result", payload={"ok": True, "text": QUOTA})]

    assert GeminiAdapter().limit_hit(raws) is None


class QuotaGemini(ScriptedEngine):
    """Emits agy's quota error; the limit is judged by the real gemini adapter."""

    def model_id(self, task):
        return task.model

    def parse_line(self, line):
        return [_error(QUOTA)]

    def limit_hit(self, raws):
        return GeminiAdapter().limit_hit(raws)


def test_a_quota_error_reroutes_the_task_instead_of_ending_as_engine_exit(repo, monkeypatch):
    gemini, claude = QuotaGemini("gemini", "limit"), ScriptedEngine("claude", "ok")
    engines = {"gemini": gemini, "claude": claude, "codex": ScriptedEngine("codex")}
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", engines.__getitem__)
    monkeypatch.setattr("hermes_control.dispatch.find_sandbox", lambda: None)
    names = ("claude", "codex", "gemini")
    for module in (dispatch_module, routing, registry):
        monkeypatch.setattr(module, "ENGINE_NAMES", names)
    monkeypatch.setattr(dispatch_module, "engine_status", lambda _: {
        "claude": None, "codex": "signed out", "gemini": None})
    monkeypatch.setattr(dispatch_module, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {name: 1 for name in names}}, None))

    result = dispatch([{"id": "a", "goal": "add a line", "engine": "gemini", "why": "w",
                        "policy": "write", "model": "gemini-4-argon"}],
                      parent_cwd=str(repo), run_id="r1")
    [entry] = result["tasks"]

    assert entry["engine"] == "claude" and entry["rerouted"]["continued"] is True
    assert entry.get("failure_mode") != "engine-exit"
    assert "limit" in [e.kind for e in RunLog("r1").read_all()]
