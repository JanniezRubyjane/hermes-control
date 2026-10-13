"""Planner labels must come from config without credential or network access."""
import builtins
import socket
import sys
from types import ModuleType

SECRET = "sk-ant-oat01-must-never-leave"

import pytest

from hermes_control import planner_status


@pytest.fixture(autouse=True)
def no_credentials_or_network(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert name != "hermes_cli.runtime_provider", "must not import credential resolver"
        return original(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("must not resolve credentials or contact the network")

    resolver = ModuleType("hermes_cli.runtime_provider")
    resolver.resolve_runtime_provider = forbidden
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", resolver)
    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def test_configured_provider_and_model_without_sign_in_check(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "model": {"provider": "anthropic", "default": "claude-sonnet-5"}, "api_key": SECRET})
    result = planner_status.status()
    assert result == {"signed_in": None, "provider": "anthropic", "model": "claude-sonnet-5",
                      "source": "Hermes config", "detail": "sign-in state not checked"}
    assert SECRET not in repr(result)


@pytest.mark.parametrize("model", ["sonnet", None, [], {"provider": [], "default": {}}])
def test_other_config_shapes(monkeypatch, model):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"model": model})
    result = planner_status.status()
    assert result["signed_in"] is None
    assert result["model"] == (model if isinstance(model, str) else "")
    assert result["provider"] == ""
    assert result["detail"] == "sign-in state not checked"


def test_config_error_is_guarded_and_not_leaked(monkeypatch):
    def broken():
        raise RuntimeError(SECRET)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", broken)
    result = planner_status.status()
    assert result["signed_in"] is None
    assert result["provider"] == result["model"] == result["source"] == ""
    assert result["detail"] == "Hermes config unavailable; sign-in state not checked"
    assert SECRET not in repr(result)


def test_missing_readonly_loader(monkeypatch):
    monkeypatch.delattr("hermes_cli.config.load_config_readonly")
    assert "config unavailable" in planner_status.status()["detail"]
