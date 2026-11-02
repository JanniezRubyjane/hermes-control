import copy
import json
import os
import subprocess
import sys

import pytest

from hermes_control import build_schema, limits, model_policy, parallel
from hermes_control.engines import registry, settings
from hermes_control.engines.settings import parse_engines


@pytest.mark.parametrize("raw,expected", [
    (None, ("codex",)), ({}, ("codex",)), ({"gemini": {}}, ("codex",)),
    ({"gemini": {"enabled": False}}, ("codex",)),
    ({"gemini": {"enabled": True}}, ("codex", "gemini")),
])
def test_parse_engines(raw, expected):
    assert parse_engines(raw) == expected


@pytest.mark.parametrize("raw", [
    False, [], "gemini", {"claude": {}}, {"unknown": {}}, {"gemini": True},
    {"gemini": None}, {"gemini": []}, {"gemini": {"enabled": "true"}},
    {"gemini": {"enabled": 1}}, {"gemini": {"enabled": None}},
    {"gemini": {"enabled": True, "typo": False}},
])
def test_parse_engines_rejects_invalid_values(raw):
    with pytest.raises(ValueError):
        parse_engines(raw)


def test_default_planner_text_and_schema_are_unchanged():
    assert registry.ENGINE_NAMES == ("claude", "codex")
    props = build_schema({})["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["engine"]["enum"] == ["claude", "codex"]
    assert model_policy.describe({}) == (
        "The models the user allows on each engine and what each is for. When a "
        "listed use fits the task, choose that engine and model; otherwise leave this "
        "empty for the engine's default. claude: any model the CLI accepts; empty uses the CLI's own "
        "default. codex: any model the CLI accepts; empty uses the CLI's own default.",
        "Optional effort: raise it for hard tasks, lower it for routine ones; empty "
        "uses the model's configured effort, else the CLI default. "
        "claude: low|medium|high|xhigh|max. codex: low|medium|high|xhigh|max|ultra.")
    assert all("gemini" not in text for text in model_policy.describe({}))


def test_disabled_engine_cannot_dispatch(monkeypatch):
    from hermes_control.dispatch import parse_tasks
    from hermes_control.engines.gemini import GeminiAdapter

    with pytest.raises(KeyError, match="unknown engine 'gemini'"):
        registry.get_adapter("gemini")
    monkeypatch.setattr(GeminiAdapter, "probe", lambda _: pytest.fail("disabled engine probed"))
    for name in ("claude", "codex"):
        monkeypatch.setattr(registry.get_adapter(name), "probe", lambda: None)
    assert registry.available_engines() == {}
    with pytest.raises(ValueError, match="gemini"):
        parse_tasks([{"id": "t", "goal": "g", "engine": "gemini"}])


def test_enabled_engine_can_be_available_without_probing_real_cli(monkeypatch):
    from hermes_control.engines.base import EngineInfo

    monkeypatch.setattr(registry, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    for name in ("claude", "codex"):
        monkeypatch.setattr(registry.get_adapter(name), "probe", lambda: None)
    info = EngineInfo("gemini", "1.3.5", "/stub/agy")
    monkeypatch.setattr(registry.get_adapter("gemini"), "probe", lambda: info)
    monkeypatch.setattr(registry.get_adapter("gemini"), "auth_problem", lambda _: None)
    assert registry.available_engines() == {"gemini": info}


def test_read_engines_uses_plugin_settings(monkeypatch):
    import hermes_cli.config

    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {
            "settings": {"engines": {"gemini": {"enabled": True}}}}}}})
    assert settings.read_engines() == ("codex", "gemini")


@pytest.mark.parametrize("spec,expected", [
    (None, ["claude", "codex"]),
    ({"gemini": {"enabled": True}}, ["claude", "codex", "gemini"]),
    ({"gemini": {"enabled": "yes"}}, ["claude", "codex"]),
])
def test_registry_resolves_opt_in_once_in_fresh_process(tmp_path, spec, expected):
    home = tmp_path / "fresh-home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps({"plugins": {"entries": {
        "hermes_control": {"settings": {"engines": spec}}}}}))
    script = """
import json
from hermes_control.engines.registry import ENGINE_NAMES
from hermes_control import build_schema, limits, model_catalog, model_policy
assert list(ENGINE_NAMES) == build_schema({})['parameters']['properties']['tasks']['items']['properties']['engine']['enum']
assert set(limits.parse_caps(None)) == set(ENGINE_NAMES)
assert set(model_catalog._FETCHERS) == set(ENGINE_NAMES)
assert ('gemini' in ' '.join(model_policy.describe({}))) == ('gemini' in ENGINE_NAMES)
print(json.dumps(ENGINE_NAMES))
"""
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                          env={**os.environ, "HERMES_HOME": str(home)}, timeout=20)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == expected
    if spec and spec["gemini"]["enabled"] == "yes":
        assert "gemini stays disabled" in proc.stderr


def test_registry_read_failure_warns_and_keeps_gemini_off(monkeypatch, caplog):
    import importlib

    def failed():
        raise OSError("unreadable config")

    with monkeypatch.context() as patch:
        patch.setattr(settings, "read_engines", failed)
        importlib.reload(registry)
        assert registry.ENGINE_NAMES == ("claude", "codex")
        assert "gemini stays disabled" in caplog.text
    importlib.reload(registry)


@pytest.fixture(params=[False, True], ids=["gemini-off", "gemini-on"])
def gemini_enabled(request, monkeypatch):
    names = ("claude", "codex", *parse_engines({"gemini": {"enabled": request.param}}))
    for module in (registry, model_policy, limits, parallel):
        monkeypatch.setattr(module, "ENGINE_NAMES", names)
    return request.param


@pytest.mark.parametrize("section,parser,reader,raw,expected", [
    ("models", model_policy.parse_policy, model_policy.load_policy,
     {"claude": {"default": "sonnet"}, "codex": {"default": "gpt-5"},
      "gemini": {"default": "flash"}},
     {"claude": model_policy.EnginePolicy("sonnet"),
      "codex": model_policy.EnginePolicy("gpt-5")}),
    ("limits", limits.parse_caps, limits.read_caps,
     {"claude": {"max_percent": 61}, "codex": 72, "gemini": {"max_percent": 53}},
     {"claude": 61, "codex": 72}),
    ("parallel", parallel.parse_parallel, parallel.read_parallel,
     {"max_tasks": 4, "per_engine": {"claude": 2, "codex": 3, "gemini": 5}},
     {"max_tasks": 4, "per_engine": {"claude": 2, "codex": 3}}),
])
def test_optional_engine_settings_keep_configured_values(
        gemini_enabled, monkeypatch, caplog, section, parser, reader, raw, expected):
    monkeypatch.setattr(limits, "_warned_disabled_engines", set())
    monkeypatch.setattr(model_policy, "_warned_disabled_engines", set())
    expected = copy.deepcopy(expected)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {
            "engines": {"gemini": {"enabled": gemini_enabled}}, section: raw}}}}})
    if gemini_enabled:
        if section == "models":
            expected["gemini"] = model_policy.EnginePolicy("flash")
        elif section == "limits":
            expected["gemini"] = 53
        else:
            expected["per_engine"]["gemini"] = 5
    assert parser(raw) == expected
    assert reader() == expected
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    if gemini_enabled:
        assert not warnings
    else:
        assert len(warnings) == (1 if section in ("limits", "models") else 2)
        assert all("gemini" in record.message and "disabled" in record.message
                   for record in warnings)


@pytest.mark.parametrize("parser,raw,message", [
    (model_policy.parse_policy, {"unknown": {}},
     "unknown engine 'unknown' in models settings; engines are claude, codex"),
    (limits.parse_caps, {"unknown": {}},
     "limits: unknown engine 'unknown'; known: claude, codex"),
    (parallel.parse_parallel, {"per_engine": {"unknown": 2}},
     "parallel.per_engine: unknown key 'unknown'"),
])
def test_optional_engine_handling_keeps_unknown_engine_errors(parser, raw, message):
    with pytest.raises(ValueError) as exc:
        parser(raw)
    assert str(exc.value) == message


@pytest.mark.parametrize("parser,raw", [
    (model_policy.parse_policy, {"gemini": {"allowed": False}}),
    (limits.parse_caps, {"gemini": {"max_percent": 0}}),
    (parallel.parse_parallel, {"per_engine": {"gemini": 0}}),
])
def test_disabled_optional_blocks_are_skipped_before_validation(gemini_enabled, parser, raw):
    if gemini_enabled:
        with pytest.raises(ValueError, match="gemini"):
            parser(raw)
    else:
        assert parser(raw) == parser(None)


@pytest.mark.parametrize("include_gemini", [False, True])
@pytest.mark.parametrize("section,save,raw,stored_gemini", [
    ("models", model_policy.save_settings,
     {"claude": {"default": "sonnet"}, "codex": {"default": "gpt-5"}},
     {"default": "flash", "allowed": [{"name": "flash", "use_for": "quick", "effort": None}]}),
    ("limits", limits.save_caps,
     {"claude": {"max_percent": 61}, "codex": {"max_percent": 72}}, {"max_percent": 53}),
])
def test_saving_preserves_disabled_optional_blocks(
        gemini_enabled, include_gemini, section, save, raw, stored_gemini,
        _isolated_hermes_home):
    import yaml

    config_path = _isolated_hermes_home / "config.yaml"
    config_path.write_text(json.dumps({"plugins": {"entries": {"hermes_control": {
        "settings": {"engines": {"gemini": {"enabled": gemini_enabled}},
                     section: {"gemini": stored_gemini}}}}}}))
    submitted = copy.deepcopy(raw)
    if include_gemini:
        submitted["gemini"] = ({"default": "pro"} if section == "models"
                               else {"max_percent": 44})
    saved = save(submitted)
    stored = yaml.safe_load(config_path.read_text())["plugins"]["entries"]["hermes_control"]["settings"][section]
    assert {name: stored[name] for name in raw} == {name: saved[name] for name in raw}
    if gemini_enabled:
        assert stored["gemini"] == saved["gemini"]
        if include_gemini:
            assert stored["gemini"] != stored_gemini
    else:
        assert "gemini" not in saved
        assert stored["gemini"] == stored_gemini
