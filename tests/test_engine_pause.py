"""Live dashboard engine pauses use the existing usage hold and fallback paths."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hermes_cli.plugins_state import save_plugin_setting
from scripted_engine import ScriptedEngine, _reading, repo  # noqa: F401 - shared fixture

from hermes_control import dispatch, limits, model_policy, parallel, pauses, routing
from hermes_control.engines import registry, settings
from hermes_control.engines.gemini import GeminiAdapter


def _save(key, value):
    save_plugin_setting("hermes_control", (key,), value)


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
@pytest.mark.parametrize("paused", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_parse_pause_does_not_change_enabled_engines(engine, paused, enabled):
    raw = {engine: {"enabled": enabled, "paused": paused}}
    assert settings.parse_engines(raw) == settings.parse_engines({engine: {"enabled": enabled}})


BAD_BOOLS = [0, 1, "", "false", [], {}, None, 0.0]


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
@pytest.mark.parametrize("value", BAD_BOOLS)
def test_bad_pause_types_leave_enabled_engines_unchanged_and_fail_closed(engine, value):
    raw = {engine: {"enabled": True, "paused": value}}
    assert settings.parse_engines(raw) == settings.parse_engines({engine: {"enabled": True}})
    _save("engines", raw)
    assert settings.read_paused()[engine] is None
    other = next(name for name in settings.OPTIONAL_ENGINES if name != engine)
    assert settings.read_paused()[other] is False
    with pytest.raises(ValueError, match="paused must be a bool"):
        settings.save_paused(engine, value)


@pytest.mark.parametrize("engine", ["claude", "unknown"])
def test_only_optional_engines_can_have_pause_settings(engine):
    with pytest.raises(ValueError, match="unknown engine"):
        settings.parse_engines({engine: {"paused": True}})
    with pytest.raises(ValueError, match="cannot pause"):
        settings.save_paused(engine, True)


def test_pause_defaults_and_writer_preserves_every_other_setting(_isolated_hermes_home):
    assert settings.read_paused() == {"codex": False, "gemini": False}
    engines = {"codex": {"enabled": True}, "gemini": {"enabled": True, "paused": True}}
    _save("engines", engines)
    _save("routing", {"fallback": {"codex": ["gemini"]}})
    _save("limits", {"codex": {"max_percent": 61}})
    path = _isolated_hermes_home / "config.yaml"
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings.save_paused("codex", True)
    assert settings.read_paused() == {"codex": True, "gemini": True}
    before["plugins"]["entries"]["hermes_control"]["settings"]["engines"]["codex"]["paused"] = True
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == before
    settings.save_paused("codex", False)
    assert settings.read_paused() == {"codex": False, "gemini": True}


@pytest.mark.parametrize("paused", [False, True])
def test_registry_and_dispatch_schema_keep_paused_engines_in_fresh_session(
        _isolated_hermes_home, paused):
    _save("engines", {name: {"enabled": True, "paused": paused}
                      for name in settings.OPTIONAL_ENGINES})
    script = """
from hermes_control import build_schema
from hermes_control.engines.registry import ENGINE_NAMES
assert ENGINE_NAMES == ('claude', 'codex', 'gemini')
assert build_schema({})['parameters']['properties']['tasks']['items']['properties']['engine']['enum'] == list(ENGINE_NAMES)
print('engine names and schema preserved')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "HERMES_HOME": str(_isolated_hermes_home)}, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("codex, expected", [
    (None, ("claude", "codex", "gemini")),
    ({"enabled": False, "paused": 1}, ("claude", "gemini")),
])
def test_invalid_pauses_preserve_enabled_engines_in_fresh_session(
        _isolated_hermes_home, codex, expected):
    engines = {"gemini": {"enabled": True, "paused": "true"}}
    if codex is not None:
        engines["codex"] = codex
    _save("engines", engines)
    script = f"""
from hermes_control import build_schema
from hermes_control.engines.registry import ENGINE_NAMES
assert ENGINE_NAMES == {expected!r}, ENGINE_NAMES
assert build_schema({{}})['parameters']['properties']['tasks']['items']['properties']['engine']['enum'] == list(ENGINE_NAMES)
print('invalid pauses preserved enabled engines and schema')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "HERMES_HOME": str(_isolated_hermes_home)}, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
def test_hold_and_description_skip_usage_for_paused_engine(monkeypatch, engine):
    _save("engines", {engine: {"enabled": True}})
    settings.save_paused(engine, True)
    usage = Mock(side_effect=AssertionError("paused engine must not read usage"))
    monkeypatch.setattr(limits, "usage", usage)
    assert limits.engine_hold(engine, refresh=True) == (f"{engine} is paused on the dashboard", None)
    row = limits.describe(engine, caps={engine: 80}, refresh=True)
    assert row["paused"] is True
    assert row["pause_problem"] is None
    assert row["held_back"] == f"{engine} is paused on the dashboard"
    assert row["held_until"] is None
    usage.assert_not_called()


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
@pytest.mark.parametrize("value", BAD_BOOLS)
def test_invalid_live_pause_surfaces_before_usage(monkeypatch, engine, value):
    _save("engines", {engine: {"paused": value}})
    usage = Mock(side_effect=AssertionError("invalid pause must not read usage"))
    monkeypatch.setattr(limits, "usage", usage)
    problem = f"{engine} pause setting is invalid (engines.{engine}.paused must be true or false)"
    assert limits.engine_hold(engine, refresh=True) == (problem, None)
    row = limits.describe(engine, caps={engine: 80}, refresh=True)
    assert row["paused"] is False
    assert row["pause_problem"] == row["held_back"] == problem
    assert row["held_until"] is None
    usage.assert_not_called()


@pytest.mark.parametrize("spec", [None, True, 1, "bad", []])
def test_non_mapping_engine_spec_holds_only_that_engine(monkeypatch, spec):
    _save("engines", {"gemini": spec, "claude": {"paused": True}})
    usage = Mock(side_effect=lambda engine, **kw: limits.Usage(engine))
    monkeypatch.setattr(limits, "usage", usage)
    assert settings.read_paused() == {"codex": False, "gemini": None}
    row = limits.describe("gemini", caps={})
    assert row["paused"] is False and "pause setting is invalid" in row["pause_problem"]
    assert limits.engine_hold("gemini") == (row["pause_problem"], None)
    usage.assert_not_called()
    for engine in ("claude", "codex"):
        assert limits.engine_hold(engine) is None
        row = limits.describe(engine, caps={})
        assert row["paused"] is False and row["pause_problem"] is None
    assert usage.call_count == 4


def test_unreadable_config_still_raises(monkeypatch):
    monkeypatch.setattr(settings, "_read_settings", Mock(side_effect=ValueError("unreadable config")))
    with pytest.raises(ValueError, match="unreadable config"):
        limits.engine_hold("codex")
    with pytest.raises(ValueError, match="unreadable config"):
        limits.describe("codex", caps={})


def test_unpaused_description_retains_usage_hold(monkeypatch):
    monkeypatch.setattr(limits, "usage", lambda *a, **k: limits.Usage(
        "codex", windows=(limits.Window("weekly", 95, None),)))
    row = limits.describe("codex", caps={"codex": 80})
    assert row["paused"] is False
    assert row["pause_problem"] is None
    assert "95%" in row["held_back"]


@pytest.fixture
def fake_engines(monkeypatch):
    class ScriptedGemini(ScriptedEngine):
        efforts = GeminiAdapter.efforts
        model_id = GeminiAdapter.model_id

    names = ("claude", "codex", "gemini")
    for module in (registry, routing, dispatch, model_policy, parallel, limits):
        monkeypatch.setattr(module, "ENGINE_NAMES", names)
    engines = {name: (ScriptedGemini if name == "gemini" else ScriptedEngine)(name, "ok", "ok")
               for name in names}
    monkeypatch.setattr(dispatch, "get_adapter", engines.__getitem__)
    monkeypatch.setattr(dispatch, "find_sandbox", lambda: None)
    _save("engines", {"codex": {"enabled": True}, "gemini": {"enabled": True}})
    _save("models", {"gemini": {"default": "gemini-test"}})
    # Claude is signed in but excluded by this task's configured destinations.
    _save("routing", {"fallback": {"codex": ["gemini"]}})
    return engines


def _dispatch(repo, run_id, engine="codex"):
    result = dispatch.dispatch([{"id": "task", "engine": engine, "policy": "write",
                                 "goal": "add a line", "why": "writer"}],
                               parent_cwd=str(repo), run_id=run_id)
    assert "error" not in result, result
    return result["tasks"][0]


def test_live_toggle_changes_routing_between_dispatches_without_reimport(repo, fake_engines):
    settings.save_paused("codex", True)
    moved = _dispatch(repo, "paused-codex")
    assert moved["engine"] == "gemini" and moved["ok"] and moved["commit"], moved
    assert "paused on the dashboard" in moved["rerouted"]["reason"]
    assert fake_engines["codex"].tasks == []
    assert len(fake_engines["gemini"].tasks) == 1
    assert fake_engines["claude"].tasks == []

    settings.save_paused("codex", False)
    direct = _dispatch(repo, "resumed-codex")
    assert direct["engine"] == "codex" and direct["ok"] and direct["commit"]
    assert len(fake_engines["codex"].tasks) == 1


def test_all_destinations_paused_then_unpaused_destination_is_ready(repo, fake_engines):
    settings.save_paused("codex", True)
    settings.save_paused("gemini", True)
    entry = _dispatch(repo, "all-paused")
    assert entry["paused"] and not entry["ok"] and entry["worktree"] == ""
    record = pauses.load(entry["paused"]["id"])
    assert record.resets_at is None
    assert "paused on the dashboard" in record.reason
    assert dispatch.resume_blocker(record)[1] is None
    assert all(not engine.tasks for engine in fake_engines.values())

    settings.save_paused("gemini", False)
    assert dispatch.resume_blocker(record) == ("", "gemini")
    resumed = dispatch.resume_paused(record.id)
    assert resumed["engine"] == "gemini" and resumed["ok"] and resumed["commit"], resumed
    assert len(fake_engines["gemini"].tasks) == 1
    assert fake_engines["codex"].tasks == fake_engines["claude"].tasks == []
    assert pauses.load(record.id) is None


def test_mid_run_limit_with_all_fallbacks_paused_keeps_resumable_worktree(
        repo, fake_engines, monkeypatch):
    _save("routing", {"fallback": {"claude": ["codex", "gemini"]}})
    fake_engines["claude"].outcomes = ["limit"]
    settings.save_paused("codex", True)
    settings.save_paused("gemini", True)
    entry = _dispatch(repo, "limit-with-paused-fallbacks", engine="claude")
    assert entry["paused"] and not entry["ok"] and entry["commit"] == "", entry
    assert entry["unproven_reason"].startswith("paused:")
    record = pauses.load(entry["paused"]["id"])
    assert record is not None and record.engine == "claude"
    assert record.session_id == "claude-session"
    assert str(record.worktree_path()) == entry["worktree"]
    assert (record.worktree_path() / "work.txt").read_text(encoding="utf-8") == "claude\n"
    assert "paused on the dashboard" in record.reason
    assert fake_engines["codex"].tasks == fake_engines["gemini"].tasks == []

    # The original engine is still over its real usage limit.
    _reading(monkeypatch, "claude", blocked=True)
    assert dispatch.resume_blocker(record)[1] is None
    settings.save_paused("gemini", False)
    assert dispatch.resume_blocker(record) == ("", "gemini")
    resumed = dispatch.resume_paused(record.id)
    assert resumed["engine"] == "gemini" and resumed["ok"] and resumed["commit"], resumed
    assert len(fake_engines["gemini"].tasks) == 1
    assert pauses.load(record.id) is None


@pytest.fixture
def api():
    path = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("hc_pause_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes_control")
    with TestClient(app) as client:
        yield client, module


def _put(client, engine, body):
    return client.put(f"/api/plugins/hermes_control/engines/{engine}/paused", json=body)


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
def test_pause_endpoint_round_trip(api, monkeypatch, engine):
    client, module = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    _save("engines", {engine: {"enabled": True}})
    for paused in (True, False):
        response = _put(client, engine, {"paused": paused})
        assert response.status_code == 200
        assert response.json() == {"engine": engine, "paused": paused}
        assert settings.read_paused()[engine] is paused


@pytest.mark.parametrize("engine", ["claude", "unknown", "gemini"])
def test_pause_endpoint_rejects_unknown_non_optional_or_disabled_engine(api, engine):
    client, _ = api
    assert _put(client, engine, {"paused": True}).status_code == 400


def test_pause_endpoint_rejects_disabled_codex(api, monkeypatch):
    _save("engines", {"codex": {"enabled": False}})
    client, module = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("claude", "gemini"))
    assert _put(client, "codex", {"paused": True}).status_code == 400


@pytest.mark.parametrize("engine", settings.OPTIONAL_ENGINES)
def test_pause_endpoint_can_pause_session_engine_disabled_for_next_session(api, monkeypatch, engine):
    client, module = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    _save("engines", {engine: {"enabled": False, "paused": "bad"}})
    assert _put(client, engine, {"paused": True}).status_code == 200
    assert settings.read_paused()[engine] is True
    assert engine not in settings.read_engines()


@pytest.mark.parametrize("value", [True, False])
@pytest.mark.parametrize("spec", [{"enabled": True, "paused": "true"}, "bad"])
def test_pause_endpoint_repairs_invalid_value(api, monkeypatch, value, spec):
    client, module = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("claude", "gemini"))
    _save("engines", {"codex": {"enabled": False},
                      "gemini": spec})
    assert _put(client, "gemini", {"paused": value}).status_code == 200
    row = client.get("/api/plugins/hermes_control/limits").json()["engines"]["gemini"]
    assert row["paused"] is value and row["pause_problem"] is None


@pytest.mark.parametrize("spec", [{"enabled": True, "paused": "true"}, "bad"])
def test_invalid_gemini_pause_reroutes_without_breaking_claude_or_dashboard(
        repo, fake_engines, api, monkeypatch, spec):
    client, module = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    _save("engines", {"codex": {"enabled": True}, "gemini": spec})
    _save("routing", {"fallback": {"gemini": ["codex"], "codex": ["gemini"]}})
    moved = _dispatch(repo, "invalid-gemini", engine="gemini")
    assert moved["engine"] == "codex" and moved["ok"] and moved["commit"], moved
    assert "pause setting is invalid" in moved["rerouted"]["reason"]
    direct = _dispatch(repo, "claude-with-invalid-gemini", engine="claude")
    assert direct["engine"] == "claude" and direct["ok"] and direct["commit"], direct
    assert fake_engines["gemini"].tasks == []
    row = limits.describe("gemini", caps={})
    assert row["paused"] is False and "pause setting is invalid" in row["pause_problem"]
    response = client.get("/api/plugins/hermes_control/limits?refresh=true")
    assert response.status_code == 200
    assert response.json()["engines"]["gemini"]["pause_problem"] == row["pause_problem"]

    settings.save_paused("codex", True)
    paused = _dispatch(repo, "invalid-gemini-no-fallback")
    record_id = paused["paused"]["id"]
    response = client.get("/api/plugins/hermes_control/paused")
    assert response.status_code == 200
    [record] = response.json()["paused"]
    assert record["id"] == record_id and record["ready"] is False
    assert "pause setting is invalid" in record["not_ready"]
    response = client.post(f"/api/plugins/hermes_control/paused/{record_id}/resume")
    assert response.status_code == 409
    assert "pause setting is invalid" in response.json()["detail"]


@pytest.mark.parametrize("body", [{}, *({"paused": value} for value in BAD_BOOLS)])
def test_pause_endpoint_rejects_non_bool_without_writing(api, monkeypatch, body):
    writer = Mock()
    monkeypatch.setattr("hermes_cli.plugins_state.save_plugin_setting", writer)
    client, _ = api
    assert _put(client, "codex", body).status_code == 400
    writer.assert_not_called()


def test_pause_endpoint_permission_error_is_403(api, monkeypatch):
    writer = Mock(side_effect=PermissionError("managed setting"))
    monkeypatch.setattr("hermes_cli.plugins_state.save_plugin_setting", writer)
    client, _ = api
    response = _put(client, "codex", {"paused": True})
    assert response.status_code == 403
    assert "managed setting" in response.json()["detail"]
