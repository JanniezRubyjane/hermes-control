"""Opt-out setting for codex as a worker engine while keeping codex sandbox."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_control
from hermes_control import (
    dispatch,
    lessons,
    limits,
    model_catalog,
    model_policy,
    parallel,
    review_pack,
    routing,
    runs,
    verify,
    watch,
)
from hermes_control.engines import registry, settings as engine_settings
from hermes_control.engines.base import ControlTask, EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import ControlResult, RawEvent, stamp
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "hermes_control" / "dashboard"


# --- 1. settings.py parsing & read_engines -------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (None, ("codex",)),
    ({}, ("codex",)),
    ({"codex": {}}, ("codex",)),
    ({"codex": {"enabled": True}}, ("codex",)),
    ({"codex": {"enabled": False}}, ()),
    ({"gemini": {"enabled": True}}, ("codex", "gemini")),
    ({"gemini": {"enabled": False}}, ("codex",)),
    ({"codex": {"enabled": False}, "gemini": {"enabled": True}}, ("gemini",)),
    ({"gemini": {"enabled": True}, "codex": {"enabled": True}}, ("codex", "gemini")),
    ({"codex": {"enabled": False}, "gemini": {"enabled": False}}, ()),
])
def test_parse_engines_defaults_codex_on_and_preserves_order(raw, expected):
    assert engine_settings.parse_engines(raw) == expected


@pytest.mark.parametrize("raw,match", [
    ([], "engines must be a mapping"),
    ("codex", "engines must be a mapping"),
    (False, "engines must be a mapping"),
    ({"claude": {"enabled": True}}, "unknown engine 'claude'"),
    ({"claude": {"enabled": False}}, "unknown engine 'claude'"),
    ({"unknown": {"enabled": True}}, "unknown engine 'unknown'"),
    ({"codex": False}, "engines.codex must be a mapping"),
    ({"codex": {"enabled": False, "other": 1}}, "engines.codex must be a mapping"),
    ({"codex": {"enabled": 1}}, "engines.codex.enabled must be a bool"),
    ({"codex": {"enabled": 0}}, "engines.codex.enabled must be a bool"),
    ({"codex": {"enabled": "false"}}, "engines.codex.enabled must be a bool"),
    ({"codex": {"enabled": "true"}}, "engines.codex.enabled must be a bool"),
    ({"codex": {"enabled": None}}, "engines.codex.enabled must be a bool"),
    ({"codex": {"enabled": []}}, "engines.codex.enabled must be a bool"),
])
def test_parse_engines_rejects_malformed_values_and_claude(raw, match):
    with pytest.raises(ValueError, match=match):
        engine_settings.parse_engines(raw)


def test_read_engines_from_isolated_config(_isolated_hermes_home):
    assert engine_settings.read_engines() == ("codex",)
    (_isolated_hermes_home / "config.yaml").write_text(json.dumps({
        "plugins": {"entries": {"hermes_control": {"settings": {
            "engines": {"codex": {"enabled": False}, "gemini": {"enabled": True}},
        }}}},
    }), encoding="utf-8")
    assert engine_settings.read_engines() == ("gemini",)


# --- 2. ENGINE_NAMES, schema enum/description, model_policy.describe -----------

def test_Disabled_codex_omitted_from_schema_description_and_model_policy(monkeypatch):
    names = ("claude", "gemini")
    for mod in (registry, hermes_control, routing, model_policy, limits, parallel):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)
    monkeypatch.setattr(routing, "_warn_disabled", routing._warn_disabled.__wrapped__)

    raw_models = {
        "claude": {"default": "sonnet", "allowed": {"sonnet": {"use_for": "reviews", "effort": "medium"}}},
        "codex": {"default": "gpt-6.1-sol", "allowed": {"gpt-6.1-sol": {"use_for": "writes", "effort": "high"}}},
        "gemini": {"default": "gemini-4-argon", "allowed": {"gemini-4-argon": {"use_for": "writes", "effort": "medium"}}},
    }
    policy = model_policy.parse_policy(raw_models)
    assert set(policy) == {"claude", "gemini"}

    # Even if a caller passes a policy dictionary containing a codex entry, describe omits codex.
    policy_with_codex = {
        **policy,
        "codex": model_policy.EnginePolicy(
            default="gpt-6.1-sol",
            allowed={"gpt-6.1-sol": model_policy.ModelChoice("gpt-6.1-sol", "writes", "high")},
        ),
    }
    model_text, effort_text = model_policy.describe(policy_with_codex)
    assert "codex" not in model_text.lower()
    assert "gpt-6.1-sol" not in model_text
    assert "codex" not in effort_text.lower()
    assert "ultra" not in effort_text
    assert "claude:" in model_text and "gemini:" in model_text

    fallback = routing.parse_fallback({
        "codex": ["gemini", "claude"],
        "claude": ["gemini", "codex/gpt-6.1-sol"],
        "gemini": ["codex", "claude"],
    }, policy)
    assert fallback == {"codex": ["gemini", "claude"], "claude": ["gemini"], "gemini": ["claude"]}

    schema = hermes_control.build_schema(policy_with_codex, fallback)
    props = schema["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["engine"]["enum"] == ["claude", "gemini"]
    assert "codex" not in props["model"]["description"].lower()
    assert "gpt-6.1-sol" not in props["model"]["description"]
    assert "codex" not in props["effort"]["description"].lower()

    desc = schema["description"]
    assert "@claude, @gemini" in desc
    assert "@codex" not in desc
    assert "'codex'" not in desc
    assert "claude -> gemini; gemini -> claude" in desc
    assert "codex" not in desc.lower()

    # Raw fallback with codex passed directly to build_description also omits codex.
    raw_desc = hermes_control.build_description({"claude": ["codex", "gemini"], "gemini": ["codex", "claude"]})
    assert "codex" not in raw_desc.lower()
    assert "claude -> gemini; gemini -> claude" in raw_desc


# --- 3. routing of a codex-planned or pinned task when codex is disabled -------

def test_routing_moves_codex_planned_task_to_fallback_destination_when_disabled(monkeypatch):
    names = ("claude", "gemini")
    for mod in (registry, hermes_control, routing, model_policy, dispatch, parallel):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)

    policy = model_policy.parse_policy({
        "claude": {"default": "sonnet", "allowed": {"sonnet": {"effort": "medium"}}},
        "gemini": {"default": "gemini-4-argon", "allowed": {"gemini-4-argon": {"effort": "high"}}},
    })
    fallback = routing.parse_fallback({
        "codex": ["gemini", "claude"],
        "claude": ["codex", "gemini"],
        "gemini": ["codex", "claude"],
    }, policy)

    # Even if status omits codex (as engine_status does) or marks codex None, codex is not usable.
    for status in ({"claude": None, "gemini": None},
                   {"claude": None, "codex": None, "gemini": None}):
        planned = ControlTask(
            id="t1", goal="implement feature", engine="codex",
            policy=ExecPolicy(write=True), why="user pinned",
            model="gpt-6.1-sol", effort="xhigh",
        )
        routed = routing.route([planned], status, fallback=fallback, policy=policy)
        moved, note = routed[0]
        assert moved.engine == "gemini"
        assert moved.model == "gemini-4-argon"
        assert moved.effort == "high"
        assert note is not None
        assert note["from"] == "codex"
        assert "disabled" in note["reason"]
        assert note["dropped_model"] == "gpt-6.1-sol"
        assert note["dropped_effort"] == "xhigh"


def test_disabled_codex_fallback_list_is_plain_data_that_survives_copies(monkeypatch):
    # The disabled source's list must be ordinary mapping data: a copy, a JSON
    # round trip or a rebuilt dict must still route codex tasks the same way.
    names = ("claude", "gemini")
    for mod in (registry, hermes_control, routing, model_policy, dispatch, parallel):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)
    policy = model_policy.parse_policy({
        "claude": {"default": "sonnet", "allowed": {"sonnet": {"effort": "medium"},
                                                   "opus": {"effort": "high"}}},
        "gemini": {"default": "gemini-4-argon", "allowed": {"gemini-4-argon": {"effort": "high"}}},
    })
    fallback = routing.parse_fallback({"codex": ["gemini", "claude/opus"]}, policy)
    assert type(fallback) is dict
    assert fallback == {"codex": ["gemini", "claude/opus"]}

    for copied in (dict(fallback), json.loads(json.dumps(fallback)), {**fallback}):
        planned = ControlTask(id="t1", goal="g", engine="codex", policy=ExecPolicy(write=True),
                              why="pinned")
        moved, note = routing.route([planned], {"claude": None, "gemini": "signed out"},
                                    fallback=copied, policy=policy)[0]
        assert (moved.engine, moved.model) == ("claude", "opus")
        assert note is not None and note["from"] == "codex" and "disabled" in note["reason"]
        assert routing.fallback_order("codex", copied) == ["gemini", "claude"]


def test_disabled_source_list_does_not_change_the_legacy_description(monkeypatch):
    # gemini disabled (the default): a gemini list in settings is kept as data,
    # but the planner text stays the legacy one because no enabled engine has a list.
    monkeypatch.setattr(routing, "ENGINE_NAMES", ("claude", "codex"))
    monkeypatch.setattr(hermes_control, "ENGINE_NAMES", ("claude", "codex"))
    fallback = routing.parse_fallback({"gemini": ["claude"]}, {})
    assert fallback == {"gemini": ["claude"]}
    assert hermes_control.build_description(fallback) == hermes_control.build_description({})


def test_dispatch_routes_codex_pinned_task_when_codex_disabled(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "init"], check=True, capture_output=True)

    names = ("claude",)
    for mod in (registry, hermes_control, routing, model_policy, dispatch, parallel, limits):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)

    ran_on = []

    class StubClaude(ClaudeAdapter):
        def probe(self):
            return EngineInfo("claude", "1.0", "/bin/true")

        def auth_problem(self, info):
            return None

        def build_argv(self, task, *, cwd):
            ran_on.append((task.engine, task.model))
            return [sys.executable, "-c", 'print(\'{"type": "result", "result": "ok"}\')']

    monkeypatch.setattr(dispatch, "get_adapter", lambda name: StubClaude() if name == "claude" else CodexAdapter())
    monkeypatch.setattr(dispatch, "find_sandbox", lambda: None)

    out = dispatch.dispatch([{
        "id": "pinned", "goal": "inspect repo", "engine": "codex",
        "why": "user pinned", "policy": "read-only",
    }], parent_cwd=str(repo), run_id="codex-disabled-dispatch")

    assert "error" not in out, out
    entry = out["tasks"][0]
    assert entry["engine"] == "claude"
    assert entry["rerouted"]["from"] == "codex"
    assert "disabled" in entry["rerouted"]["reason"]
    assert ran_on == [("claude", None)]


# --- 4. limits, model_catalog, dashboard, historical logs & get_adapter --------

def test_limits_and_dashboard_skip_codex_when_disabled_and_keep_sandbox(tmp_path, monkeypatch):
    names = ("claude",)
    for mod in (registry, hermes_control, routing, model_policy, limits, model_catalog, watch):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)
    monkeypatch.setattr(limits, "_warned_disabled_engines", set())
    limits._cache.clear()
    model_catalog._cache.clear()

    # Caps omit codex and skip configured codex caps without error.
    assert limits.parse_caps(None) == {"claude": 80}
    assert limits.parse_caps({"codex": {"max_percent": 95}, "claude": {"max_percent": 70}}) == {"claude": 70}

    # Neither limits._codex nor limits.usage("codex") may spawn codex app-server when disabled.
    monkeypatch.setattr(limits.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("codex app-server must not spawn when disabled"))
    monkeypatch.setitem(limits._FETCHERS, "codex", limits._codex)
    reading = limits.usage("codex", refresh=True)
    assert reading.engine == "codex" and reading.error and "disabled" in reading.error
    assert limits._codex(refresh=True).error and "disabled" in limits._codex(refresh=True).error
    assert limits.engine_hold("codex") is None

    # get_adapter('codex') and verify.find_sandbox() still work when codex is disabled.
    assert isinstance(registry.get_adapter("codex"), CodexAdapter)
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo("codex", "0.161.0", "/stub/codex"))
    assert verify.find_sandbox() == "/stub/codex"

    # Dashboard plugin_api neither shows codex in /engines, /limits, /models, /models/available
    # nor spawns codex app-server / codex debug models, while keeping sandbox available.
    spec = importlib.util.spec_from_file_location(
        f"hc_plugin_api_codex_off_{id(tmp_path)}", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENGINE_NAMES", names)
    monkeypatch.setattr(ClaudeAdapter, "probe",
                        lambda self: EngineInfo("claude", "2.1", "/stub/claude"))
    monkeypatch.setattr(ClaudeAdapter, "auth_problem", lambda self, info: None)
    monkeypatch.setattr(model_catalog.subprocess, "run",
                        lambda *a, **k: pytest.fail("codex debug models must not run when disabled"))

    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes_control")
    client = TestClient(app)

    engines_body = client.get("/api/plugins/hermes_control/engines").json()
    assert [row["name"] for row in engines_body["engines"]] == ["claude"]
    assert engines_body["sandbox"] == {"available": True, "path": "/stub/codex"}

    limits_body = client.get("/api/plugins/hermes_control/limits").json()
    assert set(limits_body["engines"]) == {"claude"}

    models_body = client.get("/api/plugins/hermes_control/models").json()
    assert set(models_body["settings"]) == {"claude"}
    assert set(models_body["efforts"]) == {"claude"}

    avail_body = client.get("/api/plugins/hermes_control/models/available?refresh=true").json()
    assert set(avail_body) == {"claude"}


def test_historical_codex_runs_still_display_when_codex_is_disabled(monkeypatch):
    names = ("claude",)
    for mod in (registry, hermes_control, limits, watch):
        monkeypatch.setattr(mod, "ENGINE_NAMES", names)

    now = time.time()
    run_id = "histcodex"
    log = RunLog(run_id)
    log.append(stamp([RawEvent(kind="plan", payload={
        "tasks": [{"id": "t1", "engine": "codex", "why": "historical writer",
                   "policy": "write", "depends_on": [], "verify": "true",
                   "model": "gpt-6.1-sol", "effort": "medium", "rerouted": None}],
        "repo": "/tmp/repo", "planner": None,
    })], run_id=run_id, task_id="", engine="", start_seq=0, now=now - 10))
    log.append(stamp([
        RawEvent(kind="session", payload={"session_id": "s1", "model": "gpt-6.1-sol", "effort": "medium"}),
        RawEvent(kind="tool_start", payload={"tool_id": "c1", "name": "command", "input": {"command": "pytest"}}),
        RawEvent(kind="tool_end", payload={"tool_id": "c1", "is_error": True, "exit_code": 1, "stderr": "failed"}),
        RawEvent(kind="text", payload={"text": "fixed bug"}),
        RawEvent(kind="result", payload={"ok": True, "usage": {"input_tokens": 100, "output_tokens": 50}}),
        RawEvent(kind="commit", payload={"sha": "abcdef1234567890"}),
        RawEvent(kind="verify", payload={"command": "true", "passed": True, "exit_code": 0, "output_tail": "ok"}),
    ], run_id=run_id, task_id="t1", engine="codex", start_seq=1, now=now - 5))
    log.append(stamp([RawEvent(kind="summary", payload={
        "tasks": [{"task_id": "t1", "engine": "codex", "ok": True, "commit": "abcdef1234567890",
                   "branch": "b", "worktree": "/tmp/wt", "base_commit": "0000000",
                   "text": "fixed bug", "unproven_reason": None, "rerouted": None,
                   "model": "gpt-6.1-sol", "effort": "medium", "verdict": "tests-passed"}],
        "overlaps": [],
    })], run_id=run_id, task_id="", engine="", start_seq=8, now=now - 1))

    rendered = watch.frame_text(run_id, now=now, usage=True)
    assert "codex" in rendered and "gpt-6.1-sol" in rendered
    assert "codex usage" not in rendered

    pack = review_pack.build(run_id)
    assert pack is not None and pack["tasks"][0]["actual_engine"] == "codex"

    stat_rows = runs.run_stats(limit=5, run_ids=[run_id])
    assert len(stat_rows) == 1 and stat_rows[0]["engine"] == "codex"

    lesson_classes = lessons.build(since=30, min_count=1, now=now)
    assert any(c["key"].startswith("codex_exit:") for c in lesson_classes)


def test_subprocess_import_with_codex_disabled_and_enabled(tmp_path):
    script = '''
import subprocess
from hermes_control import build_schema, limits, model_catalog, verify
from hermes_control.engines import codex, registry
from hermes_control.engines.base import EngineInfo
from hermes_control.dashboard import plugin_api

assert isinstance(registry.get_adapter("codex"), codex.CodexAdapter)
for adapter in registry._ADAPTERS.values():
    adapter.probe = lambda: None
registry._ADAPTERS["codex"].probe = lambda: EngineInfo("codex", "0.161.0", "/stub/codex")
assert verify.find_sandbox() == "/stub/codex"

schema = build_schema({})
enum = schema["parameters"]["properties"]["tasks"]["items"]["properties"]["engine"]["enum"]
assert enum == list(registry.ENGINE_NAMES)
assert ("codex" in enum) is ("codex" in registry.ENGINE_NAMES)
assert ("codex" in schema["description"].lower()) is ("codex" in registry.ENGINE_NAMES)
assert set(limits.parse_caps(None)) == set(registry.ENGINE_NAMES)
for name in registry.ENGINE_NAMES:
    model_catalog._FETCHERS[name] = lambda: {"source": "fake", "models": []}
    limits._FETCHERS[name] = lambda refresh=False, n=name: limits.Usage(n, error="unknown")
assert set(model_catalog.available(refresh=True)) == set(registry.ENGINE_NAMES)
assert [row["name"] for row in plugin_api.engines()["engines"]] == list(registry.ENGINE_NAMES)
assert plugin_api.engines()["sandbox"] == {"available": True, "path": "/stub/codex"}
assert set(plugin_api.get_limits()["engines"]) == set(registry.ENGINE_NAMES)
'''
    cases = [
        ({"codex": {"enabled": False}}, ("claude",)),
        ({"codex": {"enabled": False}, "gemini": {"enabled": True}}, ("claude", "gemini")),
        ({"codex": {"enabled": True}, "gemini": {"enabled": True}}, ("claude", "codex", "gemini")),
        ({}, ("claude", "codex")),
    ]
    for index, (engines_cfg, expected_names) in enumerate(cases):
        home = tmp_path / f"case-{index}"
        home.mkdir()
        (home / "config.yaml").write_text(json.dumps({
            "plugins": {"entries": {"hermes_control": {"settings": {"engines": engines_cfg}}}},
        }), encoding="utf-8")
        check = script + f"\nassert registry.ENGINE_NAMES == {expected_names!r}, registry.ENGINE_NAMES\n"
        proc = subprocess.run(
            [sys.executable, "-c", check], text=True, capture_output=True, cwd=ROOT,
            env={**os.environ, "HOME": str(home), "HERMES_HOME": str(home)}, timeout=20,
        )
        assert proc.returncode == 0, proc.stderr
