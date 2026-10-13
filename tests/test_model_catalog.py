"""The model lists the dashboard's picker offers, per engine."""

import http.client
import json
import shutil
import socket
import urllib.request

import pytest

from hermes_control import model_catalog
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.codex import CodexAdapter

# The shape `codex debug models` prints (codex-cli 0.155.1), trimmed to the fields read.
CODEX_CATALOG = {
    "fetched_at": "2026-09-23T17:55:39Z",
    "models": [
        {"slug": "gpt-5.6-terra", "display_name": "GPT-5.6-Terra", "visibility": "list",
         "description": "Balanced model.", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in
                                        ("low", "medium", "high", "xhigh", "max", "ultra")]},
        {"slug": "gpt-5.5", "display_name": "GPT-5.5", "visibility": "list",
         "description": "Legacy coding model.", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh")]},
        {"slug": "codex-auto-review", "display_name": "Codex Auto Review", "visibility": "hide",
         "supported_reasoning_levels": []},
    ],
}


@pytest.fixture(autouse=True)
def _fresh_cache():
    model_catalog._cache.clear()
    yield
    model_catalog._cache.clear()


def _fake_codex(monkeypatch, tmp_path, body):
    script = tmp_path / "codex"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo(name="codex", version="t", path=str(script)))


def _stub_claude(monkeypatch, static=("claude-opus-5-5", "claude-sonnet-5")):
    from hermes_cli import models

    monkeypatch.setitem(models._PROVIDER_MODELS, "anthropic", list(static))


def test_codex_lists_only_the_models_it_shows_with_their_own_effort_levels():
    models = model_catalog.parse_codex_catalog(json.dumps(CODEX_CATALOG))

    assert [m.name for m in models] == ["gpt-5.6-terra", "gpt-5.5"]
    assert models[0].efforts[-1] == "ultra" and "ultra" not in models[1].efforts
    assert models[1].description == "Legacy coding model."
    assert models[0].label == "GPT-5.6-Terra"


def test_the_codex_list_comes_from_codex_itself(monkeypatch, tmp_path):
    _fake_codex(monkeypatch, tmp_path, f"cat <<'JSON'\n{json.dumps(CODEX_CATALOG)}\nJSON")
    _stub_claude(monkeypatch)

    codex = model_catalog.available()["codex"]

    assert codex["error"] is None
    assert [m["name"] for m in codex["models"]] == ["gpt-5.6-terra", "gpt-5.5"]
    assert "codex debug models" in codex["source"]


def test_one_engine_failing_does_not_hide_the_other(monkeypatch, tmp_path):
    _fake_codex(monkeypatch, tmp_path, "echo 'not logged in' >&2; exit 1")
    _stub_claude(monkeypatch)

    result = model_catalog.available()

    assert result["codex"]["models"] == [] and "not logged in" in result["codex"]["error"]
    assert "claude-opus-5-5" in [m["name"] for m in result["claude"]["models"]]


def test_claude_offers_its_aliases_then_the_built_in_models(monkeypatch, tmp_path):
    _fake_codex(monkeypatch, tmp_path, "echo '{\"models\": []}'")
    _stub_claude(monkeypatch)

    claude = model_catalog.available()["claude"]

    names = [m["name"] for m in claude["models"]]
    assert names[:3] == list(model_catalog.CLAUDE_ALIASES)
    assert "claude-opus-5-5" in names
    assert names[3:] == ["claude-sonnet-5", "claude-opus-5-5"]
    assert claude["source"] == "Hermes's built-in model list (no account is contacted)"


def test_the_built_in_list_is_labelled_as_such(monkeypatch, tmp_path):
    _fake_codex(monkeypatch, tmp_path, "echo '{\"models\": []}'")
    _stub_claude(monkeypatch, static=("claude-sonnet-5",))

    claude = model_catalog.available()["claude"]

    assert "claude-sonnet-5" in [m["name"] for m in claude["models"]]
    assert claude["source"] == "Hermes's built-in model list (no account is contacted)"


def test_claude_catalog_never_calls_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Claude catalog must never contact an account or the network")

    monkeypatch.setattr("hermes_cli.models._fetch_anthropic_models", forbidden)
    monkeypatch.setattr("hermes_cli.models.provider_model_ids", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(http.client.HTTPConnection, "request", forbidden)

    claude = model_catalog._claude()

    assert [m.name for m in claude["models"]][:3] == list(model_catalog.CLAUDE_ALIASES)
    assert len(claude["models"]) > len(model_catalog.CLAUDE_ALIASES)
    assert claude["source"] == "Hermes's built-in model list (no account is contacted)"


def test_lists_are_cached_until_a_refresh_is_asked_for(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(model_catalog._FETCHERS, "codex",
                        lambda: calls.append(1) or {"source": "s", "models": []})
    _stub_claude(monkeypatch)

    model_catalog.available()
    model_catalog.available()
    assert len(calls) == 1
    model_catalog.available(refresh=True)
    assert len(calls) == 2


def test_a_failed_fetch_is_not_cached(monkeypatch, tmp_path):
    """A transient failure should not stick for the whole cache period."""
    calls = []

    def flaky():
        calls.append(1)
        raise RuntimeError("network down")

    monkeypatch.setitem(model_catalog._FETCHERS, "codex", flaky)
    _stub_claude(monkeypatch)

    model_catalog.available()
    model_catalog.available()
    assert len(calls) == 2


@pytest.mark.integration
def test_the_real_codex_lists_models_with_effort_levels():
    if not shutil.which("codex"):
        pytest.skip("codex not installed")

    codex = model_catalog.available(refresh=True)["codex"]

    assert codex["error"] is None, codex["error"]
    assert codex["models"] and all(m["efforts"] for m in codex["models"])


@pytest.mark.parametrize("built_in", [None, [], "anthropic", {},
                                     {"anthropic": "sonnet"}, {"anthropic": {}},
                                     {"anthropic": ["sonnet", 42]}])
def test_reshaped_built_in_list_keeps_aliases(monkeypatch, built_in):
    monkeypatch.setattr("hermes_cli.models._PROVIDER_MODELS", built_in)
    result = model_catalog._claude()
    assert [m.name for m in result["models"]] == list(model_catalog.CLAUDE_ALIASES)
    assert "built-in list was unavailable" in result["source"]


def test_missing_built_in_list_keeps_aliases(monkeypatch):
    monkeypatch.delattr("hermes_cli.models._PROVIDER_MODELS")
    result = model_catalog._claude()
    assert [m.name for m in result["models"]] == list(model_catalog.CLAUDE_ALIASES)
    assert "built-in list was unavailable" in result["source"]


def test_missing_models_module_keeps_aliases(monkeypatch):
    import builtins
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "hermes_cli" and "models" in kwargs.get("fromlist", args[2] if len(args) > 2 else ()):
            raise ImportError("models unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    result = model_catalog._claude()
    assert [m.name for m in result["models"]] == list(model_catalog.CLAUDE_ALIASES)
    assert "built-in list was unavailable" in result["source"]
