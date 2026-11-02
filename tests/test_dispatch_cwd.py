import builtins
import os
import sys
import types

from hermes_control import control_dispatch_handler


def test_dispatch_uses_session_cwd(tmp_path, monkeypatch):
    session_cwd = tmp_path / "session-repo"
    session_cwd.mkdir()
    assert str(session_cwd) != os.getcwd()
    runtime_cwd = types.ModuleType("agent.runtime_cwd")
    runtime_cwd.resolve_agent_cwd = lambda: session_cwd
    monkeypatch.setitem(sys.modules, "agent.runtime_cwd", runtime_cwd)
    captured = {}

    def start_dispatch(tasks, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("hermes_control.dispatch.start_dispatch", start_dispatch)

    control_dispatch_handler({"tasks": [], "wait": 0})

    assert captured["parent_cwd"] == str(session_cwd)


def test_dispatch_falls_back_when_runtime_cwd_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.runtime_cwd", None)
    imports = []
    original_import = builtins.__import__

    def track_import(name, *args, **kwargs):
        imports.append(name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", track_import)
    captured = {}

    def start_dispatch(tasks, **kwargs):
        captured.update(kwargs)
        return {}

    monkeypatch.setattr("hermes_control.dispatch.start_dispatch", start_dispatch)

    control_dispatch_handler({"tasks": [], "wait": 0})

    assert captured["parent_cwd"] == os.getcwd()
    # Prove this exercised compatibility fallback after attempting the runtime import.
    assert "agent.runtime_cwd" in imports
