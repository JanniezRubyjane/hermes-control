"""Eval copies stay private; all source homes are disposable test fixtures."""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

EVAL = Path(__file__).resolve().parents[1] / "scripts" / "eval"
sys.path.insert(0, str(EVAL))
import run as eval_run  # noqa: E402


@pytest.fixture(params=[0o000, 0o077])
def copy_umask(request):
    previous = os.umask(request.param)
    try:
        yield
    finally:
        os.umask(previous)


def test_profile_and_env_are_private(tmp_path, copy_umask):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("TEST_TOKEN=fixture\n")
    (source / ".env").chmod(0o644)

    home = eval_run.create_eval_home(source, tmp_path / "out")

    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / ".env").stat().st_mode) == 0o600
    assert (home / ".env").read_text() == "TEST_TOKEN=fixture\n"
    assert stat.S_IMODE((source / ".env").stat().st_mode) == 0o644


def test_evidence_directories_are_private(tmp_path, copy_umask):
    home = tmp_path / "home"
    home.mkdir()
    control = home / "control"
    control.mkdir()
    log = control / "run-test.jsonl"
    log.write_text("{}\n")
    out = tmp_path / "out"
    out.mkdir()

    evidence, _, logs = eval_run.copy_evidence(home, out, "test")

    for directory in (out / "evidence", evidence, evidence / "control"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert logs[0].read_text() == "{}\n"


@pytest.mark.parametrize("existing_out", [False, True])
def test_runner_output_permissions(tmp_path, monkeypatch, existing_out, copy_umask):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr(eval_run, "verify_command", lambda source: "true")
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps([{"id": "test", "expect": "test", "prompt": "hello"}]))
    monkeypatch.setattr(eval_run, "PROMPTS", prompts)
    monkeypatch.setattr(eval_run, "build", lambda project: project)
    monkeypatch.setattr(eval_run.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 0, "", ""))
    verify = source / "control/verify"
    verify.mkdir(parents=True)
    python_path = verify / "python-path"
    python_path.write_text(sys.executable + "\n")
    out = tmp_path / "out"
    if existing_out:
        out.mkdir()
        out.chmod(0o755)

    assert eval_run.main(["--live", "--out", str(out)]) == 0

    assert stat.S_IMODE(out.stat().st_mode) == (0o755 if existing_out else 0o700)
    for directory in (out / "evidence", out / "evidence/test"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
