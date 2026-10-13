import subprocess
import os
import time

import pytest

from hermes_control import worktree as wt_mod
from hermes_control.worktree import (
    capture_worktree_diff,
    changed_claude_paths,
    committed_claude_paths,
    commit_worktree,
    create_task_worktree,
    create_task_worktree_with_reason,
    verify_checkout,
    uncommitted_worktree_paths,
    VerifyCheckoutError,
    worktree_diff,
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


def test_task_id_maps_to_upstream_naming(repo):
    result = create_task_worktree(str(repo), "t1", run_id="r1")
    assert result is not None
    assert result.path.name == "subagent-control-r1-t1"
    assert result.branch == "hermes-subagent/subagent-control-r1-t1"
    assert result.path.is_dir()


def test_repository_lock_is_shared_with_linked_worktrees(repo):
    linked = repo / "linked"
    _git(["worktree", "add", "--detach", str(linked)], repo)
    assert wt_mod._repository_lock(repo) is wt_mod._repository_lock(linked)


@pytest.mark.parametrize("failure", ["exit", "oserror", "timeout"])
def test_repository_lock_falls_back_to_resolved_path(tmp_path, monkeypatch, failure):
    def fail(*args, **kwargs):
        if failure == "oserror":
            raise OSError("git unavailable")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("git", 60)
        return subprocess.CompletedProcess(args, 1, "", "not a repository")

    monkeypatch.setattr(wt_mod, "_run_git", fail)
    assert wt_mod._repository_lock(tmp_path) is wt_mod._repository_lock(tmp_path / "child/..")


def test_returns_none_outside_a_git_repo(tmp_path):
    assert create_task_worktree(str(tmp_path), "t1", run_id="r1") is None


def test_the_same_task_id_in_a_second_run_gets_its_own_worktree(repo):
    """The failure C2 describes. Task ids are short, stable and reused across
    runs by design (`task-1`, `task-2`), so without the run id the second run's
    `git worktree add -b` hits run one's branch, git refuses, and the task gets
    no worktree at all."""
    first = create_task_worktree(str(repo), "task-1", run_id="run-aaa")
    second = create_task_worktree(str(repo), "task-1", run_id="run-bbb")
    assert first is not None and second is not None
    assert first.branch != second.branch
    assert first.path != second.path
    assert second.path.is_dir()


def test_a_repeated_run_id_and_task_id_still_fails_and_says_why(repo):
    """The residual collision (same run id twice) must not be reported as
    "not a git repo" - it is a repo, and it refused."""
    assert create_task_worktree(str(repo), "t1", run_id="r1") is not None
    worktree, reason = create_task_worktree_with_reason(str(repo), "t1", run_id="r1")
    assert worktree is None
    assert "not a git repo" not in reason
    assert "worktree creation failed" in reason
    assert "already exists" in reason  # git's own message, carried through


def test_a_genuine_non_repo_still_reports_the_non_repo_reason(tmp_path):
    worktree, reason = create_task_worktree_with_reason(
        str(tmp_path), "t1", run_id="r1")
    assert worktree is None
    assert reason == "no worktree (not a git repo)"


def test_diff_is_empty_for_an_untouched_worktree(repo):
    result = create_task_worktree(str(repo), "t2", run_id="r1")
    assert worktree_diff(result) == ""


def test_verify_checkout_creation_error_does_not_leave_a_worktree(repo):
    task = create_task_worktree(str(repo), "verify-error", run_id="r1")
    with pytest.raises(VerifyCheckoutError, match="not-a-commit"):
        with verify_checkout(task, "not-a-commit"):
            pytest.fail("invalid commit must not yield a checkout")
    assert not list(task.path.parent.glob(f"{task.path.name}.verify-*"))


def test_verify_checkout_is_removed_if_check_raises(repo):
    task = create_task_worktree(str(repo), "verify-raises", run_id="r1")
    with pytest.raises(RuntimeError, match="check failed"):
        with verify_checkout(task, task.base_commit) as checkout:
            assert (checkout / "a.txt").is_file()
            raise RuntimeError("check failed")
    assert not checkout.exists()


def test_verify_checkout_sweeps_only_old_matching_leftovers(repo):
    from hermes_control.verify import VERIFY_TIMEOUT_SECONDS

    task = create_task_worktree(str(repo), "verify-sweep", run_id="r1")
    old = task.path.with_name(f"{task.path.name}.verify-{'a' * 32}")
    fresh = task.path.with_name(f"{task.path.name}.verify-{'b' * 32}")
    unrelated = task.path.with_name(f"{task.path.name}.verify-not-hex")
    other_task = task.path.with_name(f"other.verify-{'c' * 32}")
    for path in (old, fresh, unrelated, other_task):
        path.mkdir()
    stale_time = time.time() - VERIFY_TIMEOUT_SECONDS - 301
    for path in (old, unrelated, other_task):
        os.utime(path, (stale_time, stale_time))

    with verify_checkout(task, task.base_commit) as checkout:
        assert checkout.is_dir()
        assert not old.exists()
        assert fresh.is_dir()
        assert unrelated.is_dir()
        assert other_task.is_dir()


def test_uncommitted_worktree_paths_include_pyc_and_limit_results(repo):
    task = create_task_worktree(str(repo), "diagnosis", run_id="r1")
    (task.path / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    for name in ("a.pyc", "b.txt", "c.txt", "d.txt", "e.txt", "f.txt"):
        (task.path / name).write_text("generated", encoding="utf-8")

    paths = uncommitted_worktree_paths(task)

    assert len(paths) == 5
    assert "a.pyc" in uncommitted_worktree_paths(task, limit=10)


def test_uncommitted_worktree_paths_swallows_git_errors(repo, monkeypatch):
    task = create_task_worktree(str(repo), "diagnosis-error", run_id="r1")

    def fail_git(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(wt_mod, "_run_git", fail_git)
    assert uncommitted_worktree_paths(task) == []


def test_diff_reports_a_tracked_file_change(repo):
    result = create_task_worktree(str(repo), "t3", run_id="r1")
    (result.path / "a.txt").write_text("two\n", encoding="utf-8")
    diff = worktree_diff(result)
    assert "a.txt" in diff and "+two" in diff


def test_diff_reports_a_new_untracked_file(repo):
    result = create_task_worktree(str(repo), "t4", run_id="r1")
    (result.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert "new.py" in worktree_diff(result)


def test_diff_returns_empty_string_when_git_fails(repo, monkeypatch):
    result = create_task_worktree(str(repo), "t5", run_id="r1")
    monkeypatch.setattr(wt_mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    assert worktree_diff(result) == ""


def test_worktree_diff_runs_hardened_git(repo, monkeypatch):
    """Verify that worktree_diff applies git hardening against malicious .git/config."""
    from hermes_cli._subprocess_compat import harden_git_argv

    result = create_task_worktree(str(repo), "t6", run_id="r1")

    # Capture the arguments passed to subprocess.run
    captured_calls = []

    def capture_run(*args, **kwargs):
        captured_calls.append((args, kwargs))
        # Return a mock result
        class MockResult:
            returncode = 0
            stdout = ""
        return MockResult()

    monkeypatch.setattr(wt_mod.subprocess, "run", capture_run)

    # Call worktree_diff
    worktree_diff(result)

    # Verify both git calls were made
    assert len(captured_calls) == 2, f"Expected 2 git calls, got {len(captured_calls)}"

    # Get the hardened flags for diff command
    expected_diff_flags = harden_git_argv(["diff", "--no-color"])

    # Check the second call (diff call)
    diff_call_args, diff_call_kwargs = captured_calls[1]
    assert len(diff_call_args) > 0
    argv = diff_call_args[0]

    # Verify the argv contains the hardened git command with external-driver flags
    assert "git" in argv
    assert set(expected_diff_flags) <= set(argv[1:]), (
        f"Expected diff argv to contain hardened flags {expected_diff_flags}, "
        f"but got {argv}"
    )

    # Verify env is hardened (not None)
    assert diff_call_kwargs.get("env") is not None
    assert diff_call_kwargs.get("stdin") is subprocess.DEVNULL


class _Failing:
    """A git result that exited non-zero, the way a broken index or a locked
    repo would - `worktree_diff` used to read this as "nothing changed"."""

    returncode = 128
    stdout = ""
    stderr = "fatal: not a git repository"


def test_capture_reports_a_failed_diff_instead_of_an_empty_one(repo, monkeypatch):
    """I10: `""` meant both "the worker changed nothing" and "git would not tell
    us", and downstream turned the second into the first."""
    result = create_task_worktree(str(repo), "t7", run_id="r1")
    monkeypatch.setattr(wt_mod, "_run_git", lambda *a, **k: _Failing())

    diff, failure = capture_worktree_diff(result)
    assert diff == ""
    assert failure is not None
    assert "diff capture failed" in failure
    assert str(result.path) in failure and result.branch in failure
    assert "before assuming no work" in failure


def test_capture_reports_a_failed_add_not_just_a_failed_diff(repo, monkeypatch):
    """`git add -AN` is what makes untracked files visible to `git diff`; its
    return code was never checked, so a failure there produced a confidently
    empty diff."""
    result = create_task_worktree(str(repo), "t8", run_id="r1")

    class _Ok:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_git(args, cwd, timeout=60):
        return _Failing() if args[0] == "add" else _Ok()

    monkeypatch.setattr(wt_mod, "_run_git", fake_git)
    diff, failure = capture_worktree_diff(result)
    assert diff == ""
    assert failure is not None and "git add -AN" in failure


def test_capture_reports_an_exception_as_a_failure_not_as_no_changes(repo, monkeypatch):
    result = create_task_worktree(str(repo), "t9", run_id="r1")
    monkeypatch.setattr(wt_mod.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("no git")))
    diff, failure = capture_worktree_diff(result)
    assert diff == ""
    assert failure is not None and "no git" in failure


def test_capture_reports_no_failure_for_a_genuinely_untouched_worktree(repo):
    result = create_task_worktree(str(repo), "t10", run_id="r1")
    assert capture_worktree_diff(result) == ("", None)


@pytest.mark.parametrize("change", ["tracked", "staged", "committed", "untracked", "ignored"])
def test_claude_settings_check_covers_every_worktree_change(repo, change):
    if change == "ignored":
        (repo / ".gitignore").write_text(".claude/extra.json\n")
        _git(["add", ".gitignore"], repo)
    settings = repo / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"base": true}')
    _git(["add", ".claude/settings.json"], repo)
    _git(["commit", "-qm", "base settings"], repo)
    wt = create_task_worktree(str(repo), f"settings-{change}", run_id="r1")
    assert changed_claude_paths(wt) == ([], None)
    if change in ("untracked", "ignored"):
        path = wt.path / ".claude" / "extra.json"
    else:
        path = wt.path / ".claude" / "settings.json"
    path.write_text('{"changed": true}')
    if change in ("staged", "committed"):
        _git(["add", str(path.relative_to(wt.path))], wt.path)
    if change == "committed":
        _git(["commit", "-qm", "worker settings"], wt.path)

    assert changed_claude_paths(wt) == ([str(path.relative_to(wt.path))], None)


def test_claude_settings_check_catches_case_variant_directory(repo):
    wt = create_task_worktree(str(repo), "case-settings", run_id="r1")
    path = wt.path / ".CLAUDE" / "settings.json"
    path.parent.mkdir()
    path.write_text("{}")

    paths, failure = changed_claude_paths(wt)
    assert failure is None
    assert ".CLAUDE" in paths or ".CLAUDE/settings.json" in paths


def test_committed_claude_check_catches_case_variant_directory(repo):
    wt = create_task_worktree(str(repo), "case-commit", run_id="r1")
    path = wt.path / ".CLAUDE" / "x"
    path.parent.mkdir()
    path.write_text("x")
    _git(["add", ".CLAUDE/x"], wt.path)
    _git(["commit", "-qm", "case variant"], wt.path)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt.path, check=True,
                         capture_output=True, text=True).stdout.strip()

    assert ".CLAUDE/x" in committed_claude_paths(wt, sha)


def test_claude_settings_check_refuses_symlink_in_base_when_target_changes(repo):
    settings = repo / ".claude"
    settings.mkdir()
    (repo / "target.json").write_text('{"base": true}')
    (settings / "settings.json").symlink_to("../target.json")
    _git(["add", ".claude/settings.json", "target.json"], repo)
    _git(["commit", "-qm", "linked settings"], repo)
    wt = create_task_worktree(str(repo), "linked-settings", run_id="r1")
    (wt.path / "target.json").write_text('{"changed": true}')

    paths, failure = changed_claude_paths(wt)
    assert failure is None
    assert ".claude/settings.json is a symlink" in paths


def test_claude_settings_check_refuses_symlinked_directory(repo):
    (repo / "settings").mkdir()
    (repo / "settings" / "settings.json").write_text("{}")
    (repo / ".claude").symlink_to("settings", target_is_directory=True)
    _git(["add", ".claude", "settings/settings.json"], repo)
    _git(["commit", "-qm", "linked directory"], repo)
    wt = create_task_worktree(str(repo), "linked-directory", run_id="r1")

    paths, failure = changed_claude_paths(wt)
    assert failure is None
    assert ".claude is a symlink" in paths


def test_committing_puts_the_workers_changes_on_the_task_branch(repo):
    """The merge gap: workers never commit, so `git merge <task branch>` used to bring
    back nothing at all. The controller now commits their changes for them."""
    wt = create_task_worktree(str(repo), "c1", run_id="r1")
    (wt.path / "a.txt").write_text("two\n", encoding="utf-8")
    (wt.path / "new.py").write_text("x = 1\n", encoding="utf-8")

    sha, failure = commit_worktree(wt, message="hermes-control(claude): c1")

    assert failure is None and sha
    _git(["merge", "-q", wt.branch], repo)  # the step a human takes to accept the work
    assert (repo / "a.txt").read_text(encoding="utf-8") == "two\n"
    assert (repo / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_a_worktree_with_no_changes_gets_no_commit(repo):
    wt = create_task_worktree(str(repo), "c2", run_id="r1")
    assert commit_worktree(wt, message="m") == (None, None)


def test_committing_does_not_run_repository_hooks(repo):
    """A hook would run with the controller's privileges over files a worker wrote.
    The hardened git environment has to keep it from firing."""
    marker = repo / "HOOK_RAN"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    wt = create_task_worktree(str(repo), "c3", run_id="r1")
    (wt.path / "a.txt").write_text("two\n", encoding="utf-8")

    sha, failure = commit_worktree(wt, message="m")

    assert failure is None and sha
    assert not marker.exists()


def test_the_commit_is_attributed_to_the_controller_not_the_repo_owner(repo):
    wt = create_task_worktree(str(repo), "c4", run_id="r1")
    (wt.path / "a.txt").write_text("two\n", encoding="utf-8")

    sha, _ = commit_worktree(wt, message="hermes-control(codex): c4")

    author, subject = subprocess.run(
        ["git", "log", "-1", "--format=%an|%s", sha], cwd=repo, capture_output=True,
        text=True, encoding="utf-8", check=True).stdout.strip().split("|", 1)
    assert author != "T" and "Hermes-Control" in author
    assert subject == "hermes-control(codex): c4"



def _write_bytecode_litter(root):
    for relative in ("__pycache__/calc.cpython-312.pyc", "pkg/__pycache__/m.cpython-312.pyc",
                     "legacy.pyc"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\x00bytecode")


def test_bytecode_a_worker_left_behind_is_not_committed(repo):
    """Found by the real run: codex ran pytest itself, the repo did not ignore
    __pycache__, and `git add -A` put .pyc files on the task branch."""
    wt = create_task_worktree(str(repo), "t1", run_id="r1")
    (wt.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    _write_bytecode_litter(wt.path)

    sha, failure = commit_worktree(wt, message="m")

    assert failure is None and sha
    files = subprocess.run(["git", "show", "--name-only", "--format=", sha], cwd=wt.path,
                           check=True, capture_output=True, text=True).stdout.split()
    assert files == ["new.py"]


def test_the_reported_diff_names_exactly_the_files_that_get_committed(repo):
    wt = create_task_worktree(str(repo), "t1", run_id="r1")
    (wt.path / "a.txt").write_text("two\n", encoding="utf-8")
    (wt.path / "new.py").write_text("x = 1\n", encoding="utf-8")
    _write_bytecode_litter(wt.path)

    diff, failure = capture_worktree_diff(wt)
    sha, _ = commit_worktree(wt, message="m")

    assert failure is None
    reported = {line.split(" b/", 1)[1] for line in diff.splitlines()
                if line.startswith("diff --git ")}
    committed = set(subprocess.run(["git", "show", "--name-only", "--format=", sha],
                                   cwd=wt.path, check=True, capture_output=True,
                                   text=True).stdout.split())
    assert reported == committed == {"a.txt", "new.py"}


def test_concurrent_worktrees_commits_and_verify_checkout_lock(repo, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    barrier = threading.Barrier(2)
    original = wt_mod._run_git
    active = 0
    guard = threading.Lock()

    def git(args, cwd, timeout=60):
        nonlocal active
        mutation = args[0] in {"add", "worktree"} or "commit" in args
        if not mutation:
            return original(args, cwd, timeout)
        with guard:
            active += 1
            assert active == 1, "repository mutations overlapped"
        try:
            return original(args, cwd, timeout)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(wt_mod, "_run_git", git)
    from tools import subagent_worktree
    original_create = subagent_worktree.create_subagent_worktree

    def create(*args, **kwargs):
        nonlocal active
        with guard:
            active += 1
            assert active == 1, "worktree creation overlapped a repository mutation"
        try:
            return original_create(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(subagent_worktree, "create_subagent_worktree", create)

    def work(name):
        barrier.wait(timeout=5)
        tree, reason = create_task_worktree_with_reason(str(repo), name, run_id="parallel")
        assert tree is not None, reason
        (tree.path / f"{name}.txt").write_text(name)
        barrier.wait(timeout=5)
        sha, reason = commit_worktree(tree, message=name)
        assert sha, reason
        with verify_checkout(tree, sha) as checkout:
            # Verify bodies must overlap; the repo lock covers setup/cleanup only.
            barrier.wait(timeout=5)
            assert (checkout / f"{name}.txt").read_text() == name
        return sha

    with ThreadPoolExecutor(2) as pool:
        commits = list(pool.map(work, ["a", "b"]))
    assert len(set(commits)) == 2
