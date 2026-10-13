"""The merge helper must leave the caller's checkout and worktrees alone."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "merge_check.sh"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def merge_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.test")
    (repo / "shared.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    for branch, filename, value in (("left", "shared.txt", "left\n"),
                                    ("right", "right.txt", "right\n"),
                                    ("conflict", "shared.txt", "conflict\n")):
        _git(repo, "switch", "-c", branch, "main")
        (repo / filename).write_text(value)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", branch)
    _git(repo, "switch", "main")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    for program in ("git", "sh", "mktemp"):
        (path_dir / program).symlink_to(shutil.which(program))
    env = {**os.environ, "TMPDIR": str(scratch), "PATH": str(path_dir)}
    return repo, env


@pytest.mark.parametrize("args, code, message", [
    (["--unsandboxed", "true", "left", "right"], 0, "clean merge and check passed"),
    (["--unsandboxed", "exit 3", "left", "right"], 1,
     "merge_check: check failed (exit 3)"),
    (["true", "left", "conflict"], 1, "merge_check: conflict does not merge cleanly"),
    ([], 2, "usage: merge_check.sh"),
])
def test_merge_check_cleans_up_and_preserves_checkout(merge_repo, args, code, message):
    repo, env = merge_repo
    before = (_git(repo, "branch", "--show-current"), _git(repo, "rev-parse", "HEAD"),
              _git(repo, "status", "--porcelain"))
    result = subprocess.run(["sh", str(SCRIPT), *args], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert result.returncode == code, result.stdout + result.stderr
    assert message in result.stdout + result.stderr
    assert (_git(repo, "branch", "--show-current"), _git(repo, "rev-parse", "HEAD"),
            _git(repo, "status", "--porcelain")) == before
    worktrees = _git(repo, "worktree", "list", "--porcelain")
    assert worktrees.count("worktree ") == 1
    assert f"worktree {repo}" in worktrees


def test_merge_check_ignores_inherited_result_on_passing_check(merge_repo):
    repo, env = merge_repo
    result = subprocess.run(["sh", str(SCRIPT), "--unsandboxed", "true", "left"], cwd=repo,
                            env={**env, "result": "7"}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean merge and check passed" in result.stdout


def test_merge_check_without_codex_merges_but_does_not_run_check(merge_repo, tmp_path):
    repo, env = merge_repo
    marker = tmp_path / "check-ran"
    command = f"printf x > {shlex.quote(str(marker))}"
    result = subprocess.run(["sh", str(SCRIPT), command, "left", "right"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "merge_check: merged left" in result.stdout
    assert "merge_check: merged right" in result.stdout
    assert ("merge_check: codex not found; the check was not run "
            "(no sandbox available)") in result.stderr
    assert not marker.exists()


def test_merge_check_unsandboxed_runs_check_without_codex(merge_repo, tmp_path):
    repo, env = merge_repo
    marker = tmp_path / "check-ran"
    command = f"printf x > {shlex.quote(str(marker))}"
    result = subprocess.run(["sh", str(SCRIPT), "--unsandboxed", command, "right"],
                            cwd=repo, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "merge_check: codex not found; running the check outside a sandbox" in result.stderr
    assert marker.exists()


def test_merge_check_unsandboxed_conflict_still_exits_one(merge_repo, tmp_path):
    repo, env = merge_repo
    marker = tmp_path / "check-ran"
    command = f"printf x > {shlex.quote(str(marker))}"
    result = subprocess.run(["sh", str(SCRIPT), "--unsandboxed", command,
                             "left", "conflict"], cwd=repo, env=env,
                            capture_output=True, text=True)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "merge_check: conflict does not merge cleanly" in result.stderr
    assert not marker.exists()
