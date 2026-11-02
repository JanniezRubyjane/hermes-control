"""Committed agy settings stay visible as warnings, regardless of the task engine."""

import subprocess

import pytest

from hermes_control import landing, worktree
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from hermes_control.verify import VerifyOutcome


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "app.py").write_text("VALUE = 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    task = tmp_path / "task"
    git(repo, "worktree", "add", "-b", "test", str(task), "HEAD")
    return worktree.TaskWorktree(task, "test", repo, git(repo, "rev-parse", "HEAD"))


def finish(tree, engine, verify, monkeypatch):
    task = ControlTask(id="t", goal="edit project settings", engine=engine,
                       policy=ExecPolicy(write=True), verify=verify)
    monkeypatch.setattr(landing, "run_verify", lambda command, *_args, **_kwargs:
                        VerifyOutcome(command, True, 0, "passed", None))
    git(tree.path, "add", "-A")
    diff = git(tree.path, "diff", "--cached")
    result = ControlResult(task_id="t", engine=engine, ok=True, text="done",
                           session_id=None, diff=diff)
    landed = landing.land(task, result, tree, run_id="agents", sandbox=lambda: "/stub/codex")
    assert landed.commit == git(tree.path, "rev-parse", "HEAD")
    return landed


@pytest.mark.parametrize("engine", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("verify", [None, "true"])
@pytest.mark.parametrize("path", [
    ".agents/hooks.json", ".AGENTS/hooks.json", ".agents", ".gemini/settings.json",
    ".GEMINI/settings.json",
])
def test_committed_agy_project_paths_warn(tree, monkeypatch, engine, verify, path):
    target = tree.path / path
    if path == ".agents":
        # Git records the link itself; inspection must not follow it or require a directory.
        target.symlink_to("elsewhere", target_is_directory=True)
    else:
        target.parent.mkdir()
        target.write_text("{}\n")
    landed = finish(tree, engine, verify, monkeypatch)
    directory = path.split("/", 1)[0].casefold()
    detail = ("agy runs .agents/hooks.json and loads these settings" if directory == ".agents"
              else "agy loads these .gemini settings")
    warning = (f"warning: committed diff changes {directory}/ ({path}); "
               f"{detail} in any later agy session in this repo")
    assert landed.unproven_reason == (warning if verify else
                                      f"{landing.NO_VERIFY_REASON}; {warning}")
    if verify:
        assert landed.verify.passed
        assert landed.events[-1].payload["unproven_reason"] == warning


def test_multiple_project_warnings_preserve_claude_text(tree, monkeypatch):
    for path in (".claude/settings.json", ".agents/hooks.json", ".gemini/settings.json"):
        target = tree.path / path
        target.parent.mkdir()
        target.write_text("{}\n")
    landed = finish(tree, "codex", "true", monkeypatch)
    assert landed.unproven_reason == (
        "warning: committed diff changes .claude/ (.claude/settings.json); "
        "review Claude settings before merging; "
        "warning: committed diff changes .agents/ (.agents/hooks.json); "
        "agy runs .agents/hooks.json and loads these settings in any later agy session in this repo; "
        "warning: committed diff changes .gemini/ (.gemini/settings.json); "
        "agy loads these .gemini settings in any later agy session in this repo")


def test_unrelated_paths_do_not_warn(tree, monkeypatch):
    for path in ("nested/.agents/hooks.json", ".agents-extra/hooks.json", "app.py"):
        target = tree.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n")
    assert finish(tree, "codex", "true", monkeypatch).unproven_reason is None
