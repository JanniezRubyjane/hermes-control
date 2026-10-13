"""Prune only safe Hermes task refs in real temporary git repositories."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from hermes_control import dispatch, pauses, prune, runs, watch, worktree
from tests.test_dispatch_parallel import engines, gate  # noqa: F401
from tests.test_resume_summary import save_pause


PREFIX = "hermes-subagent/subagent-control-"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "tracked.txt").write_text("base\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def task(repo, name, *, path=None):
    branch = PREFIX + name
    path = path or repo.parent / f"task {name}"
    git(repo, "worktree", "add", "-q", "-b", branch, str(path))
    return branch, path


def run(capsys, *args):
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    code = watch.run_cli(parser.parse_args(["prune", *map(str, args), "--json"]))
    output = capsys.readouterr()
    return code, {row["branch"]: row for row in json.loads(output.out)}, output.err


def test_dry_run_reports_task_worktrees_and_orphan_branches_without_changes(repo, capsys,
                                                                             monkeypatch):
    branch, path = task(repo, "merged", path=repo.parent / "task path with spaces")
    orphan = PREFIX + "orphan"
    git(repo, "branch", orphan)
    git(repo, "branch", "unrelated")
    verify = repo.parent / ("task.verify-" + "a" * 32)
    git(repo, "worktree", "add", "-q", "--detach", str(verify))

    code, rows, err = run(capsys, "--repo", repo)

    assert code == 0 and not err
    assert set(rows) == {branch, orphan}
    assert rows[branch]["path"] == str(path)
    assert rows[branch]["state"] == "clean"
    assert rows[branch]["hold"] is None
    assert rows[branch]["merged"] is True
    assert rows[branch]["size"] >= 0
    assert rows[branch]["age_days"] >= 0
    assert rows[orphan]["path"] is None
    assert rows[orphan]["hold"] is None
    assert path.is_dir() and verify.is_dir()
    assert git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    assert git(repo, "show-ref", "--verify", f"refs/heads/{orphan}")

    monkeypatch.chdir(repo)
    code, current_rows, err = run(capsys)
    assert code == 0 and set(current_rows) == set(rows) and not err


def test_apply_removes_merged_clean_and_orphan_then_is_idempotent(repo, capsys):
    branch, path = task(repo, "merged")
    orphan = PREFIX + "orphan"
    git(repo, "branch", orphan)
    git(repo, "branch", "unrelated")
    verify = repo.parent / ("task.verify-" + "b" * 32)
    git(repo, "worktree", "add", "-q", "--detach", str(verify))

    code, rows, err = run(capsys, "--repo", repo, "--apply")

    assert code == 0 and not err
    assert rows[branch]["action"] == "removed"
    assert rows[orphan]["action"] == "removed"
    assert not path.exists()
    assert branch not in git(repo, "branch", "--list", PREFIX + "*")
    assert orphan not in git(repo, "branch", "--list", PREFIX + "*")
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert verify.is_dir()
    assert git(repo, "show-ref", "--verify", "refs/heads/unrelated")

    code, rows, err = run(capsys, "--repo", repo, "--apply")
    assert code == 0 and rows == {} and not err


def test_apply_keeps_wip_scratch_unmerged_paused_and_live(repo, capsys, monkeypatch):
    entries = {name: task(repo, name + "-task" if name in ("live", "running") else name)
               for name in ("wip", "scratch", "unmerged", "paused", "live", "running")}
    (entries["wip"][1] / "tracked.txt").write_text("changed\n")
    (entries["scratch"][1] / "notes.txt").write_text("draft\n")
    (entries["unmerged"][1] / "new.txt").write_text("new\n")
    git(entries["unmerged"][1], "add", ".")
    git(entries["unmerged"][1], "commit", "-qm", "new work")
    monkeypatch.setattr(pauses, "all_paused", lambda: [SimpleNamespace(
        run_id="paused", task={"id": "paused"},
        worktree_path=lambda: entries["paused"][1])])
    monkeypatch.setattr(dispatch, "_run_is_live", lambda run_id: run_id == "live")
    original_view = runs.run_view
    monkeypatch.setattr(runs, "run_view", lambda run_id: {"state": "running"}
                        if run_id == "running" else original_view(run_id))
    from hermes_control.runlog import run_log_dir
    for run_id in ("live", "running"):
        (run_log_dir() / f"run-{run_id}.jsonl").touch()

    code, rows, err = run(capsys, "--repo", repo, "--apply")

    assert code == 0 and not err
    assert {name: rows[branch]["hold"] for name, (branch, _) in entries.items()} == {
        "wip": "wip", "scratch": "scratch", "unmerged": "not merged",
        "paused": "paused", "live": "live", "running": "live"}
    assert rows[entries["wip"][0]]["state"] == "wip:1"
    assert rows[entries["scratch"][0]]["state"] == "scratch:1"
    assert all(row["action"] == "kept" for row in rows.values())
    assert all(path.is_dir() for _, path in entries.values())


def test_apply_waits_for_another_process_creating_a_worktree(repo, capsys):
    pytest.importorskip("fcntl")
    branch, path = task(repo, "locked")
    lock_path = repo / ".git" / "hermes-control-worktrees.lock"
    code = ("import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(lock_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "locked"
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(run, capsys, "--repo", repo, "--apply")
            try:
                time.sleep(0.3)
                assert path.is_dir() and not future.done()
            finally:
                proc.kill()
                proc.wait(10)
            result, rows, err = future.result(timeout=10)
        assert result == 0 and not err
        assert rows[branch]["action"] == "removed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_apply_times_out_when_worktree_creation_lock_stays_busy(repo, capsys, monkeypatch):
    pytest.importorskip("fcntl")
    branch, path = task(repo, "locked-timeout")
    lock_path = repo / ".git" / "hermes-control-worktrees.lock"
    code = ("import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(2)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(lock_path)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "locked"
        monkeypatch.setattr(prune, "_WORKTREE_LOCK_TIMEOUT", 0, raising=False)
        result = prune.prune(str(repo), apply=True, as_json=True)
        err = capsys.readouterr().err
        assert result == 1
        assert "another Hermes process is creating a worktree; try again" in err
        assert path.is_dir() and git(repo, "show-ref", "--verify", f"refs/heads/{branch}")
    finally:
        proc.kill()
        proc.wait(10)


def test_apply_computes_sizes_before_worktree_lock(repo, capsys, monkeypatch):
    pytest.importorskip("fcntl")
    task(repo, "size-before-lock")
    lock_path = repo / ".git" / "hermes-control-worktrees.lock"
    code = ("import fcntl, sys, time\n"
            "handle = open(sys.argv[1], 'a')\n"
            "fcntl.flock(handle, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code, str(lock_path)],
                            stdout=subprocess.PIPE, text=True)
    sized = threading.Event()
    original_size = prune._size

    def size(path):
        sized.set()
        return original_size(path)

    monkeypatch.setattr(prune, "_size", size)
    try:
        assert select.select([proc.stdout], [], [], 10)[0]
        assert proc.stdout.readline().strip() == "locked"
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(run, capsys, "--repo", repo, "--apply")
            try:
                assert sized.wait(3)
            finally:
                proc.kill()
                proc.wait(10)
            assert future.result(timeout=10)[0] == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)


def test_apply_holds_resumed_task_worktree_while_it_runs(repo, capsys, engines):
    record = save_pause(repo, run_id="resume", name="a")
    started, release = gate(engines["codex"], "a")
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(dispatch.resume_paused, record.id)
        try:
            assert started.wait(5)
            branch = PREFIX + "resume-a"
            code, rows, err = run(capsys, "--repo", repo, "--apply")
            assert code == 0 and not err
            assert rows[branch]["hold"] == "paused"
            assert rows[branch]["action"] == "kept"
            assert rows[branch]["path"] and os.path.isdir(rows[branch]["path"])
            assert dispatch._run_is_live(record.run_id) is False
        finally:
            release.set()
        assert future.result(timeout=10)["commit"]


def test_main_worktree_uses_porcelain_for_submodule(repo):
    origin = repo.parent / "module-origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.name", "Test")
    git(origin, "config", "user.email", "test@example.com")
    (origin / "file.txt").write_text("base\n")
    git(origin, "add", ".")
    git(origin, "commit", "-qm", "base")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q",
        str(origin), "module")
    git(repo, "commit", "-qam", "module")
    module = repo / "module"
    _, path = task(module, "inside")
    first = git(path, "worktree", "list", "--porcelain").splitlines()[0]
    assert first.startswith("worktree ")
    assert prune._main_worktree(path) == Path(first[len("worktree "):]).resolve()


@pytest.mark.parametrize("failure", ["git", "open"])
def test_worktree_creation_continues_if_file_lock_setup_fails(repo, monkeypatch, caplog,
                                                              failure):
    pytest.importorskip("fcntl")
    real_run_git = worktree._run_git
    real_open = type(repo).open
    calls = 0

    def maybe_fail_git(*args, **kwargs):
        nonlocal calls
        calls += 1
        if failure == "git" and calls == 2:
            raise subprocess.TimeoutExpired("git", 60)
        return real_run_git(*args, **kwargs)

    def maybe_fail_open(path, *args, **kwargs):
        if failure == "open" and path.name == "hermes-control-worktrees.lock":
            raise OSError("lock unavailable")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(worktree, "_run_git", maybe_fail_git)
    monkeypatch.setattr(type(repo), "open", maybe_fail_open)
    created, reason = worktree.create_task_worktree_with_reason(str(repo), "lock-error",
                                                                run_id="test")
    assert created is not None and created.path.is_dir() and reason == ""
    assert "lock" in caplog.text.lower()


def test_pause_without_worktree_path_holds_matching_branch(repo, capsys, monkeypatch):
    branch, path = task(repo, "resume-task-a")
    monkeypatch.setattr(pauses, "all_paused", lambda: [SimpleNamespace(
        run_id="resume", task={"id": "task-a"}, worktree_path=lambda: None)])

    code, rows, err = run(capsys, "--repo", repo, "--apply")

    assert code == 0 and not err
    assert rows[branch]["hold"] == "paused"
    assert rows[branch]["action"] == "kept" and path.is_dir()


def test_missing_worktree_is_pruned_before_merged_branch_deleted(repo, capsys):
    branch, path = task(repo, "missing")
    shutil.rmtree(path)

    code, rows, err = run(capsys, "--repo", repo)
    assert code == 0 and not err
    assert rows[branch]["state"] == "missing"

    code, rows, err = run(capsys, "--repo", repo, "--apply")
    assert code == 0 and not err
    assert rows[branch]["action"] == "removed"
    assert not git(repo, "branch", "--list", branch)


def test_running_inside_task_worktree_holds_that_worktree(repo, capsys, monkeypatch):
    from hermes_control import worktree

    branch, path = task(repo, "current")
    other, other_path = task(repo, "other")
    original_run_git = worktree._run_git
    git_cwds = []

    def record_cwd(args, cwd, **kwargs):
        git_cwds.append(cwd)
        return original_run_git(args, cwd, **kwargs)

    monkeypatch.setattr(worktree, "_run_git", record_cwd)
    nested = path / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)

    code, rows, err = run(capsys, "--apply")

    assert code == 0 and not err
    assert rows[branch]["hold"] == "current"
    assert rows[branch]["action"] == "kept" and path.is_dir()
    assert rows[other]["action"] == "removed" and not other_path.exists()
    assert git_cwds[0] == str(path)
    assert set(git_cwds[1:]) == {str(repo)}


def test_apply_reports_worktree_prune_failure_with_rows(repo, capsys, monkeypatch):
    from hermes_control import prune

    branch, path = task(repo, "prune-fails")
    real_git = prune._git

    def fail_prune(root, *args):
        if args == ("worktree", "prune"):
            return SimpleNamespace(returncode=1, stderr="prune unavailable")
        return real_git(root, *args)

    monkeypatch.setattr(prune, "_git", fail_prune)
    code, rows, err = run(capsys, "--repo", repo, "--apply")

    assert code == 1 and "prune unavailable" in err
    assert rows[branch]["action"] == "kept"
    assert "prune unavailable" in rows[branch]["message"]
    assert path.is_dir()
