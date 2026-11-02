import os
from pathlib import Path

from hermes_control.engines import base
from hermes_control.engines.gemini import GeminiAdapter


def _stub_home(monkeypatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def test_drops_entries_equal_to_and_under_home_hermes_and_keeps_lookalikes_and_order(
    tmp_path, monkeypatch
):
    home = _stub_home(monkeypatch, tmp_path)
    raw_path = os.pathsep.join([
        str(home / ".hermes"),
        str(home / ".hermes" / "tools" / "python-3.12" / "bin"),
        str(home / "work" / "x" / ".hermes" / "bin"),
        "/usr/local/bin",
        str(home / ".hermesfoo" / "bin"),
        "",
        str(home / ".hermes" / "bin"),
        "/opt/homebrew/bin",
        "/usr/bin",
    ])
    monkeypatch.setattr(base, "engine_env", lambda: {"PATH": raw_path, "KEPT": "yes"})

    adapter = GeminiAdapter()
    env = adapter.engine_env()

    assert env == {
        "PATH": os.pathsep.join([
            str(home / "work" / "x" / ".hermes" / "bin"),
            "/usr/local/bin",
            str(home / ".hermesfoo" / "bin"),
            "",
            "/opt/homebrew/bin",
            "/usr/bin",
        ]),
        "KEPT": "yes",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
        "HERMES_CONTROL_CHECK_COPY": "1",
    }
    assert base.EngineAdapter.engine_env(adapter) == {"PATH": raw_path, "KEPT": "yes"}


def test_drops_entries_equal_to_and_under_hermes_home_outside_default(tmp_path, monkeypatch):
    home = _stub_home(monkeypatch, tmp_path)
    custom_hermes = tmp_path / "custom-hermes-root"
    raw_path = os.pathsep.join([
        str(custom_hermes),
        str(custom_hermes / "tools" / "node" / "bin"),
        str(home / ".hermes" / "bin"),
        str(tmp_path / "custom-hermes-root-extra" / "bin"),
        "/opt/homebrew/bin",
    ])
    monkeypatch.setattr(
        base,
        "engine_env",
        lambda: {"PATH": raw_path, "HERMES_HOME": str(custom_hermes)},
    )

    env = GeminiAdapter().engine_env()

    assert env["PATH"] == os.pathsep.join([
        str(tmp_path / "custom-hermes-root-extra" / "bin"),
        "/opt/homebrew/bin",
    ])
    assert env["HERMES_HOME"] == str(custom_hermes)
    assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "1"
    assert env["HERMES_CONTROL_CHECK_COPY"] == "1"


def test_empty_hermes_home_is_ignored(tmp_path, monkeypatch):
    home = _stub_home(monkeypatch, tmp_path)
    raw_path = os.pathsep.join([".", "/usr/bin", str(home / ".hermes" / "bin")])
    monkeypatch.setattr(
        base,
        "engine_env",
        lambda: {"PATH": raw_path, "HERMES_HOME": ""},
    )

    env = GeminiAdapter().engine_env()

    assert env["PATH"] == os.pathsep.join([".", "/usr/bin"])


def test_missing_path_key_does_not_error_or_add_path(tmp_path, monkeypatch):
    _stub_home(monkeypatch, tmp_path)
    monkeypatch.setattr(base, "engine_env", lambda: {"KEPT": "yes"})

    env = GeminiAdapter().engine_env()

    assert "PATH" not in env
    assert env == {
        "KEPT": "yes",
        "AGY_CLI_DISABLE_AUTO_UPDATE": "1",
        "HERMES_CONTROL_CHECK_COPY": "1",
    }
