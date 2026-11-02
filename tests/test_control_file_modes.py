"""Sensitive control data stays private even with a permissive process umask."""

import os
from pathlib import Path
import stat

import pytest

from hermes_control import pauses, runlog
from hermes_control.events import ControlEvent


@pytest.fixture(autouse=True)
def permissive_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_new_control_directory_is_private():
    directory = runlog.run_log_dir()
    assert directory == Path(os.environ["HERMES_HOME"]) / "control"
    assert _mode(directory) == 0o700


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o707, 0o700])
def test_existing_directory_is_tightened_only_when_needed(monkeypatch, mode):
    directory = Path(os.environ["HERMES_HOME"]) / "control"
    directory.mkdir(mode=mode)
    existing = directory / "existing.jsonl"
    existing.write_text("existing output\n", encoding="utf-8")
    existing.chmod(0o644)
    chmod = Path.chmod
    calls = []

    def capture(path, new_mode, **kwargs):
        calls.append((path, new_mode))
        return chmod(path, new_mode, **kwargs)

    monkeypatch.setattr(Path, "chmod", capture)
    assert runlog.run_log_dir() == directory
    assert _mode(directory) == 0o700
    assert calls == ([(directory, 0o700)] if mode & 0o077 else [])
    assert _mode(existing) == 0o644
    assert existing.read_text(encoding="utf-8") == "existing output\n"


@pytest.mark.parametrize("existing", [False, True])
def test_run_log_creation_and_existing_mode(existing):
    log = runlog.RunLog("private")
    if existing:
        log.path.touch(mode=0o644)
    event = ControlEvent(run_id="private", task_id="t1", engine="codex", seq=0,
                         ts=1.0, kind="text", payload={"text": "worker output café"})
    log.append([event])
    log.append([event])
    assert _mode(log.path) == (0o644 if existing else 0o600)
    assert log.read_all() == [event, event]


def test_pause_staging_and_saved_record_are_private(monkeypatch):
    record = pauses.Pause(id="private-t1", run_id="private", task={"goal": "goal café"},
                          repo=None, worktree=None, engine="codex", session_id=None,
                          reason="limit", resets_at=None, paused_at=1.0)
    replace = os.replace
    calls = []

    def capture(source, destination):
        assert source.suffix == ".tmp"
        assert _mode(source) == 0o600
        calls.append((source, destination))
        return replace(source, destination)

    monkeypatch.setattr(pauses.os, "replace", capture)
    pauses.save(record)
    assert len(calls) == 1
    staging, saved = calls[0]
    assert not staging.exists()
    assert _mode(saved) == 0o600
    assert pauses.load(record.id) == record


def test_directory_that_cannot_be_chmodded_still_works(monkeypatch, caplog):
    directory = Path(os.environ["HERMES_HOME"]) / "control"
    directory.mkdir(parents=True)
    directory.chmod(0o755)

    def refuse(self, mode):
        raise PermissionError("not the owner")

    monkeypatch.setattr(Path, "chmod", refuse)
    assert runlog.run_log_dir() == directory
    assert "could not restrict" in caplog.text
