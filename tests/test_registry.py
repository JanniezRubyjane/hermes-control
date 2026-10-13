import pytest

from hermes_control.engines.base import EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.engines.registry import (
    ENGINE_NAMES,
    available_engines,
    get_adapter,
)


def test_both_engines_are_registered():
    assert ENGINE_NAMES == ("claude", "codex")


def test_get_adapter_returns_the_right_type():
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)


def test_get_adapter_rejects_an_unknown_engine_by_name():
    with pytest.raises(KeyError, match="nope"):
        get_adapter("nope")


def test_available_engines_omits_engines_that_fail_to_probe(monkeypatch):
    monkeypatch.setattr(ClaudeAdapter, "probe", lambda self: None)
    monkeypatch.setattr(CodexAdapter, "probe", lambda self: None)
    assert available_engines() == {}


def test_available_engines_omits_an_installed_engine_that_is_signed_out(monkeypatch):
    info = EngineInfo(name="x", version="1", path="/bin/x")
    monkeypatch.setattr(ClaudeAdapter, "probe", lambda self: info)
    monkeypatch.setattr(CodexAdapter, "probe", lambda self: info)
    monkeypatch.setattr(ClaudeAdapter, "auth_problem", lambda self, i: "not signed in")
    monkeypatch.setattr(CodexAdapter, "auth_problem", lambda self, i: None)

    assert set(available_engines()) == {"codex"}


def test_engine_state_pairs_what_was_found_with_why_it_cannot_run(monkeypatch):
    from hermes_control.engines.registry import NOT_INSTALLED, engine_state

    info = EngineInfo(name="claude", version="9", path="/bin/claude")
    monkeypatch.setattr(ClaudeAdapter, "probe", lambda self: info)
    monkeypatch.setattr(ClaudeAdapter, "auth_problem", lambda self, i: "not signed in")
    monkeypatch.setattr(CodexAdapter, "probe", lambda self: None)

    assert engine_state(ClaudeAdapter()) == (info, "not signed in")
    assert engine_state(CodexAdapter()) == (None, NOT_INSTALLED)


def test_each_engine_names_the_command_that_signs_it_in():
    assert get_adapter("claude").sign_in_command == "claude auth login"
    assert get_adapter("codex").sign_in_command == "codex login"
