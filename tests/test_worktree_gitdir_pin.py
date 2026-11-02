"""Task git uses the controller's registration even after a worker rewrites .git."""

from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from hermes_control import secret_scan, worktree


REFUSED = "not committed: the task worktree's git metadata changed (refused)"


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def task(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    path = tmp_path / "task"
    git(repo, "worktree", "add", "-b", "task", str(path), "HEAD")
    return worktree.TaskWorktree(path, "task", repo, git(repo, "rev-parse", "HEAD"))


def admin_dir(task):
    # Read the original pointer only in test setup, before the simulated worker runs.
    return Path((task.path / ".git").read_text().removeprefix("gitdir: ").strip())


def redirect(task, variant):
    common = Path(git(task.repo_root, "rev-parse", "--absolute-git-dir"))
    if variant == "main":
        (task.path / ".git").write_text(f"gitdir: {common}\n")
    else:
        evil = task.path / "evilgit"
        evil.mkdir()
        (evil / "commondir").write_text(f"{common}\n")
        (evil / "HEAD").write_text("ref: refs/heads/main\n")
        (task.path / ".git").write_text("gitdir: evilgit\n")


@pytest.mark.parametrize("variant", ["main", "evilgit"])
def test_rewritten_pointer_cannot_commit_to_main(task, variant):
    redirect(task, variant)
    (task.path / "a.txt").write_text("worker change\n")

    sha, reason = worktree.commit_worktree(task, message="task change")

    assert git(task.repo_root, "rev-parse", "main") == task.base_commit
    if sha:
        assert reason is None
        assert git(task.repo_root, "rev-parse", task.branch) == sha
        assert git(task.repo_root, "show", f"{sha}:a.txt") == "worker change"
    else:
        assert reason == REFUSED
        assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit


def test_normal_task_commits_on_its_branch(task):
    (task.path / "a.txt").write_text("normal change\n")

    sha, reason = worktree.commit_worktree(task, message="normal task")

    assert sha and reason is None
    assert git(task.repo_root, "rev-parse", task.branch) == sha
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit


@pytest.mark.parametrize("variant", ["normal", "main", "evilgit"])
def test_secret_scan_reads_the_pinned_staged_change(task, monkeypatch, variant):
    if variant != "normal":
        redirect(task, variant)
    monkeypatch.setattr(secret_scan, "read_settings",
                        lambda: {"enabled": True, "allow_paths": []})
    # Modify a tracked file to exercise the staged diff, blob reader and added lines.
    (task.path / "a.txt").write_text("base\n" + "AKIA" + "7Q" * 8 + "\n")

    sha, reason = worktree.commit_worktree(task, message="secret", before_commit=secret_scan.scan)

    assert sha is None
    assert "a.txt:2 (aws-access-key)" in reason
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit
    env = worktree._task_git_env(task)
    assert worktree._run_git(["diff", "--cached", "--name-only"],
                             cwd=str(task.path), env=env).stdout == ""
    assert git(task.repo_root, "diff", "--cached", "--name-only") == ""


@pytest.mark.parametrize("ref", ["refs/heads/main", None])
def test_changed_real_admin_head_is_refused(task, ref):
    admin = admin_dir(task)
    if ref is None:
        (admin / "HEAD").write_text(task.base_commit + "\n")
    else:
        git(task.repo_root, f"--git-dir={admin}", "symbolic-ref", "HEAD", ref)
    (task.path / "a.txt").write_text("must not reach main\n")

    assert worktree.commit_worktree(task, message="wrong HEAD") == (None, REFUSED)
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit
    assert not (admin / "index.lock").exists()


def test_missing_controller_registration_is_refused(task):
    (admin_dir(task) / "gitdir").unlink()
    (task.path / "a.txt").write_text("unregistered\n")

    assert worktree.commit_worktree(task, message="missing registration") == (None, REFUSED)
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit


def test_head_switched_during_guard_is_refused(task):
    admin = admin_dir(task)
    (task.path / "a.txt").write_text("guarded change\n")

    def switch_head(_task, _env):
        git(task.repo_root, f"--git-dir={admin}", "symbolic-ref", "HEAD", "refs/heads/main")

    assert worktree.commit_worktree(task, message="guard", before_commit=switch_head) == (None, REFUSED)
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit


def test_repo_root_can_itself_be_a_linked_worktree(task, tmp_path):
    root = tmp_path / "controller"
    git(task.repo_root, "worktree", "add", "-b", "controller", str(root), "HEAD")
    task = replace(task, repo_root=root)
    (task.path / "a.txt").write_text("linked controller\n")

    sha, reason = worktree.commit_worktree(task, message="linked controller")

    assert sha and reason is None
    assert git(root, "rev-parse", task.branch) == sha
    assert git(root, "rev-parse", "main") == task.base_commit


@pytest.mark.parametrize("variant", ["main", "evilgit"])
def test_task_inspection_uses_pinned_git_metadata(task, variant):
    redirect(task, variant)
    (task.path / "new.txt").write_text("new\n")
    (task.path / ".claude").mkdir()
    (task.path / ".claude" / "settings.json").write_text("{}\n")

    assert "new.txt" in worktree.uncommitted_worktree_paths(task, limit=20)
    paths, reason = worktree.changed_claude_paths(task)
    assert reason is None and ".claude/settings.json" in paths
    paths, reason = worktree.base_settings_paths(task, "claude")
    assert paths == [] and reason is None
    diff, reason = worktree.capture_worktree_diff(task)
    assert reason is None and "+new" in diff
    assert git(task.repo_root, "diff", "--cached", "--name-only") == ""
    assert git(task.repo_root, "diff", "--name-only") == ""

    sha, reason = worktree.commit_worktree(task, message="settings")
    assert sha and reason is None
    assert worktree.committed_claude_paths(task, sha) == [".claude/settings.json"]
    assert git(task.repo_root, "rev-parse", "main") == task.base_commit


def test_symlink_to_second_task_is_refused_without_changing_its_index(task, tmp_path):
    second_path = tmp_path / "second-task"
    git(task.repo_root, "worktree", "add", "-b", "second-task", str(second_path), "HEAD")
    second = replace(task, path=second_path, branch="second-task")
    second_admin = admin_dir(second)
    (second_path / "staged.txt").write_text("second task's staged work\n")
    git(second_path, "add", "staged.txt")
    staged_before = git(second_path, "diff", "--cached", "--name-only")
    files_before = git(task.repo_root, f"--git-dir={second_admin}", "ls-files")
    index_before = (second_admin / "index").read_bytes()
    (task.path / "new.txt").write_text("first task's work\n")
    (task.path / ".git").unlink()
    (task.path / ".git").symlink_to(second_path / ".git")

    diff, reason = worktree.capture_worktree_diff(task)
    assert diff == "" and REFUSED in reason
    assert worktree.uncommitted_worktree_paths(task) == []
    assert worktree.changed_claude_paths(task) == ([], REFUSED)
    assert worktree.base_settings_paths(task, "claude") == ([], REFUSED)
    assert worktree.commit_worktree(task, message="symlink redirect") == (None, REFUSED)

    assert git(second_path, "diff", "--cached", "--name-only") == staged_before
    assert git(task.repo_root, f"--git-dir={second_admin}", "ls-files") == files_before
    assert (second_admin / "index").read_bytes() == index_before


@pytest.mark.parametrize("variant", ["directory", "symlink"])
def test_nonregular_git_pointer_is_refused(task, variant):
    pointer = task.path / ".git"
    original = pointer.read_text()
    pointer.unlink()
    if variant == "directory":
        pointer.mkdir()
    else:
        saved = task.path / "saved-git-pointer"
        saved.write_text(original)
        pointer.symlink_to(saved)
    (task.path / "a.txt").write_text("must not commit\n")

    with pytest.raises(worktree._WorktreeGitMetadataError, match="git metadata changed"):
        worktree._task_git_env(task)
    assert worktree.commit_worktree(task, message="invalid git pointer") == (None, REFUSED)
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit


def test_secret_scan_reuses_supplied_pinned_env(task, monkeypatch):
    admin = admin_dir(task)
    resolve_env = secret_scan._task_git_env

    def reject_repinned_env(scanned_task, env):
        assert "GIT_DIR" not in env, "secret scan must reuse the pinned environment"
        return resolve_env(scanned_task, env)

    monkeypatch.setattr(secret_scan, "_task_git_env", reject_repinned_env)
    monkeypatch.setattr(secret_scan, "read_settings",
                        lambda: {"enabled": True, "allow_paths": []})
    (task.path / "a.txt").write_text("base\n" + "AKIA" + "7Q" * 8 + "\n")

    def scan_after_registration_change(scanned_task, env):
        assert env["GIT_DIR"] == str(admin)
        assert env["GIT_WORK_TREE"] == str(task.path)
        before = dict(env)
        (admin / "gitdir").unlink()
        reason = secret_scan.scan(scanned_task, env)
        assert env == before
        return reason

    sha, reason = worktree.commit_worktree(
        task, message="scan pinned index", before_commit=scan_after_registration_change)

    assert sha is None
    assert "a.txt:2 (aws-access-key)" in reason
    assert git(task.repo_root, "rev-parse", task.branch) == task.base_commit
