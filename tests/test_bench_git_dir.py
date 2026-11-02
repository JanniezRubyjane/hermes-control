"""Caller-owned origin metadata can be found from inside its work tree."""

from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import grader


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, env=grader.git_env()).stdout.strip()


def test_subdir_of_normal_repo(tmp_path):
    git(tmp_path, "init")
    subdir = tmp_path / "nested" / "child"
    subdir.mkdir(parents=True)
    assert grader.real_git_dir(subdir) == (tmp_path / ".git").resolve()
    assert grader.git(subdir, "rev-parse", "--show-toplevel") == str(tmp_path.resolve())

    # The nearest repository wins when repositories are nested.
    git(subdir.parent, "init")
    assert grader.real_git_dir(subdir) == (subdir.parent / ".git").resolve()


@pytest.mark.parametrize("relative_pointer", [False, True])
def test_subdir_of_linked_worktree(tmp_path, relative_pointer):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init")
    git(origin, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-m", "initial")
    worktree = tmp_path / "linked"
    git(origin, "worktree", "add", "--detach", str(worktree))
    metadata = Path(git(worktree, "rev-parse", "--absolute-git-dir")).resolve()
    if relative_pointer:
        (worktree / ".git").write_text(
            "gitdir: ../origin/" + str(metadata.relative_to(origin)) + "\n")
    subdir = worktree / "nested" / "child"
    subdir.mkdir(parents=True)
    assert grader.real_git_dir(subdir) == metadata
    assert grader.git(subdir, "rev-parse", "--show-toplevel") == str(worktree.resolve())


@pytest.mark.parametrize("subdir", [".", "objects"])
def test_bare_repo(tmp_path, subdir):
    git(tmp_path, "init", "--bare")
    assert grader.real_git_dir(tmp_path / subdir) == tmp_path.resolve()


def test_no_repository(tmp_path):
    with pytest.raises(ValueError, match="^not inside a git repository$"):
        grader.real_git_dir(tmp_path)


def test_init_inside_existing_repo(tmp_path):
    git(tmp_path, "init")
    nested = tmp_path / "nested"
    nested.mkdir()
    grader.git(nested, "init")
    assert grader.real_git_dir(nested) == (nested / ".git").resolve()
