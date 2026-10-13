"""The controller's own check of a task, run inside an OS sandbox or not at all."""

import os
import shlex
import shutil
import subprocess

import pytest

from hermes_control.verify import (check_script_path, check_script_text, run_verify,
                                   write_check_script)


def test_check_script_uses_the_guarded_sandbox_argv_and_clean_environment(tmp_path):
    command = "printf '%s' \"$HOME\""
    text = check_script_text("/x/codex", tmp_path / "work tree", command)

    assert text.startswith("#!/bin/sh\n")
    assert "Hermes-Control" in text
    assert "env -i" in text
    assert "sandbox -P :workspace -C" in text
    assert "/bin/sh -c" in text
    assert shlex.quote(command) in text


def test_write_check_script_is_owner_read_execute_and_outside_worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    path = check_script_path("run", "task")
    assert path == tmp_path / "hermes-home/control/checks/run-task.sh"

    written = write_check_script("/x/codex", tmp_path, "exit 0", path=path)

    assert written == path
    assert path.stat().st_mode & 0o777 == 0o500
    assert path.read_text(encoding="utf-8") == check_script_text("/x/codex", tmp_path,
                                                                   "exit 0")


def test_write_check_script_replaces_a_stale_path_without_following_symlinks(tmp_path):
    path = tmp_path / "checks/check.sh"
    path.parent.mkdir()
    other = tmp_path / "other.txt"
    other.write_text("leave this alone", encoding="utf-8")
    path.symlink_to(other)

    write_check_script("/x/codex", tmp_path, "exit 0", path=path)

    assert not path.is_symlink()
    assert other.read_text(encoding="utf-8") == "leave this alone"
    assert path.stat().st_mode & 0o777 == 0o500
    write_check_script("/x/codex", tmp_path, "exit 1", path=path)
    assert "exit 1" in path.read_text(encoding="utf-8")


def test_check_script_preserves_quoted_command_and_strips_worker_credentials(tmp_path):
    fake_codex = tmp_path / "fake codex"
    fake_codex.write_text("#!/bin/sh\nprintf 'ARG:%s\\n' \"$@\"\nprintf 'ENV\\n'\nenv\n",
                          encoding="utf-8")
    fake_codex.chmod(0o700)
    command = "printf '%s' \"$HOME\"; echo 'single quote'"
    script = write_check_script(str(fake_codex), tmp_path / "work tree", command,
                                path=tmp_path / "checks/check.sh")

    proc = subprocess.run([str(script)], capture_output=True, text=True, check=True,
                          env={**os.environ, "OPENAI_API_KEY": "sk-should-not-leak",
                               "GITHUB_TOKEN": "ghp-should-not-leak"})
    lines = proc.stdout.splitlines()
    assert lines[:9] == ["ARG:sandbox", "ARG:-P", "ARG::workspace", "ARG:-C",
                         f"ARG:{tmp_path / 'work tree'}", "ARG:--", "ARG:/bin/sh",
                         "ARG:-c", f"ARG:{command}"]
    assert "ENV" in lines
    assert "should-not-leak" not in proc.stdout
    assert any(line.startswith("HOME=") for line in lines)
    assert any(line.startswith("PATH=") for line in lines)


def test_no_sandbox_means_not_verified_and_the_command_never_runs(tmp_path):
    outcome = run_verify("touch ran.txt", tmp_path, codex_path=None)

    assert outcome.passed is False
    assert outcome.exit_code is None
    assert outcome.unproven_reason.startswith("not verified: no sandbox available")
    assert not (tmp_path / "ran.txt").exists()


@pytest.mark.parametrize("impostor", ["/bin/echo", "/usr/bin/true"])
def test_a_binary_that_does_not_run_the_command_is_not_taken_for_a_sandbox(tmp_path, impostor):
    """`echo` exits 0 and even prints the argv it was given; `true` exits 0 silently.
    Neither ran anything, so neither may turn into a pass."""
    if not shutil.which(impostor):
        pytest.skip(f"{impostor} not present")
    outcome = run_verify("exit 0", tmp_path, codex_path=impostor)

    assert outcome.passed is False
    assert outcome.unproven_reason.startswith("not verified: the sandbox did not start")


needs_codex = pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")


@pytest.mark.integration
@needs_codex
def test_a_command_that_exits_zero_in_the_sandbox_passes(tmp_path):
    (tmp_path / "check.py").write_text("print('all good')\n", encoding="utf-8")

    outcome = run_verify("python3 check.py", tmp_path, codex_path=shutil.which("codex"))

    assert outcome.passed is True
    assert outcome.exit_code == 0
    assert outcome.unproven_reason is None
    assert "all good" in outcome.output_tail


@pytest.mark.integration
@needs_codex
def test_a_failing_command_reports_its_exit_code(tmp_path):
    outcome = run_verify("echo boom; exit 3", tmp_path, codex_path=shutil.which("codex"))

    assert outcome.passed is False
    assert outcome.exit_code == 3
    assert outcome.unproven_reason == "verification failed (exit 3)"
    assert "boom" in outcome.output_tail


@pytest.mark.integration
@needs_codex
@pytest.mark.live_system_guard_bypass  # run_verify genuinely SIGTERMs its own child
def test_a_command_that_outlives_its_budget_is_stopped(tmp_path):
    outcome = run_verify("echo $$ > pid.txt; exec sleep 60", tmp_path,
                         codex_path=shutil.which("codex"), timeout=3)

    assert outcome.passed is False
    assert outcome.unproven_reason.startswith("verification timed out")
    # Stopped, not abandoned: the command itself is gone, not just the sandbox wrapper.
    pid = int((tmp_path / "pid.txt").read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.integration
@needs_codex
def test_the_command_does_not_see_the_operators_secrets(tmp_path, monkeypatch):
    """It runs code an LLM just wrote; provider keys and GitHub tokens stay out."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")

    outcome = run_verify("env", tmp_path, codex_path=shutil.which("codex"))

    assert outcome.exit_code == 0
    assert "should-not-leak" not in outcome.output_tail


@pytest.mark.integration
@needs_codex
def test_the_command_cannot_write_outside_the_worktree(tmp_path):
    """$HOME is outside the worktree and outside the temp dir the sandbox allows."""
    from pathlib import Path

    target = Path.home() / f".hermes-control-verify-probe-{tmp_path.name}"
    try:
        outcome = run_verify(f"echo x > '{target}'", tmp_path,
                             codex_path=shutil.which("codex"))
        assert outcome.passed is False
        assert not target.exists()
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.integration
@needs_codex
def test_the_output_reaches_the_planner_without_terminal_colour_codes(tmp_path):
    """Test runners colour their verdict when FORCE_COLOR is set; to the model the
    escape codes are noise wrapped around the one line it needs."""
    outcome = run_verify(r"printf '\033[32m6 passed\033[0m\n'", tmp_path,
                         codex_path=shutil.which("codex"))

    assert outcome.output_tail == "6 passed\n"
