"""Regression tests for strict fail-closed guard nits and diagnostic messages."""

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_control import dispatch, worktree
from hermes_control.engines.base import ControlTask
from hermes_control.policy import ExecPolicy
from hermes_control.worktree import commit_worktree, create_task_worktree


def _task() -> ControlTask:
    return ControlTask(
        "t1",
        "fix it",
        "gemini",
        ExecPolicy(write=True),
        verify="/bin/sh scripts/sandbox-check.sh",
        model="gemini-4-argon",
    )


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "a.txt"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


@pytest.mark.parametrize("truthy_non_bool", ["yes", 1, ["ok"], {"allowed": True}, Mock()])
def test_with_check_guidance_requires_exact_true(truthy_non_bool):
    false_adapter = SimpleNamespace(headless_commands_allowed=lambda: False)
    false_goal = dispatch.with_check_guidance(_task(), adapter=false_adapter).goal
    assert "do not run any shell command, including tests and git" in false_goal
    assert "you may run the check command" not in false_goal

    non_bool_adapter = SimpleNamespace(headless_commands_allowed=lambda: truthy_non_bool)
    non_bool_goal = dispatch.with_check_guidance(_task(), adapter=non_bool_adapter).goal
    assert non_bool_goal == false_goal

    true_adapter = SimpleNamespace(headless_commands_allowed=lambda: True)
    true_goal = dispatch.with_check_guidance(_task(), adapter=true_adapter).goal
    assert "you may run the check command above yourself" in true_goal
    assert "do not run any shell command" not in true_goal


def test_with_check_guidance_handles_get_adapter_raising(monkeypatch):
    false_adapter = SimpleNamespace(headless_commands_allowed=lambda: False)
    expected_goal = dispatch.with_check_guidance(_task(), adapter=false_adapter).goal

    def raising_get_adapter(engine):
        raise RuntimeError(f"lookup failed for {engine}")

    monkeypatch.setattr(dispatch, "get_adapter", raising_get_adapter)
    prepared = dispatch.with_check_guidance(_task())
    assert prepared.goal == expected_goal
    assert "do not run any shell command, including tests and git" in prepared.goal
    assert "you may run the check command" not in prepared.goal


def test_blocked_commit_logs_warning_when_reset_fails_without_stderr(repo, monkeypatch, caplog):
    wt = create_task_worktree(str(repo), "c1", run_id="r1")
    assert wt is not None
    (wt.path / "a.txt").write_text("two\n", encoding="utf-8")

    original_run_git = worktree._run_git
    leaked_stderr = "fatal: unable to reset index near SECRET_VALUE_98765"
    leaked_stdout = "STDOUT_SECRET_12345"

    def fake_run_git(args, cwd, timeout=60, **kwargs):
        if args and args[0] == "reset":
            return subprocess.CompletedProcess(args, 128, leaked_stdout, leaked_stderr)
        return original_run_git(args, cwd, timeout=timeout, **kwargs)

    monkeypatch.setattr(worktree, "_run_git", fake_run_git)
    block_reason = "not committed: staged change blocked by secret scan: a.txt:1 (aws-access-key)"

    with caplog.at_level(logging.WARNING, logger="hermes_control.worktree"):
        sha, reason = commit_worktree(
            wt,
            message="hermes-control(gemini): c1",
            before_commit=lambda _tree, _env: block_reason,
        )

    assert (sha, reason) == (None, block_reason)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert str(wt.path) in msg
    assert "128" in msg
    assert leaked_stderr not in msg
    assert "SECRET_VALUE_98765" not in msg
    assert leaked_stdout not in msg
    assert leaked_stderr not in reason


def test_sandbox_check_script_wording():
    script = (Path(__file__).resolve().parents[1] / "scripts" / "sandbox-check.sh").read_text(
        encoding="utf-8"
    )
    assert (
        'echo "sandbox-check: using agy copy because the primary verify dir is unreadable or missing; '
        '$deselected tests are left to the post-commit verify" >&2'
    ) in script
    assert (
        'echo "sandbox-check: not set up; run scripts/verify-setup.sh once, outside the sandbox" >&2'
    ) in script
