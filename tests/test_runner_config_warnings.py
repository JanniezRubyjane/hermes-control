"""Runner configuration is advisory; committed Codex settings stay visible."""

import subprocess

import pytest

from hermes_control import landing, proof, worktree
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from hermes_control.verify import VerifyOutcome


@pytest.mark.parametrize("name", [
    "conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini",
    "sitecustomize.py", "usercustomize.py", "runner.pth",
])
@pytest.mark.parametrize("prefix", ["", "tests/", "nested/project/"])
@pytest.mark.parametrize("status", ["A", "M"])
@pytest.mark.parametrize("globs", [list(proof.DEFAULT_TEST_GLOBS), []])
def test_runner_configuration_warns(monkeypatch, name, prefix, status, globs):
    monkeypatch.setattr(proof, "_diff", lambda *_args, **_kwargs: "")
    task = ControlTask(id="t", goal="change runner", engine="codex",
                       policy=ExecPolicy(write=True), verify="pytest")
    path = prefix + name
    assert proof.advisory_warnings(task, None, "sha", {path: status}, globs) == [
        f"check_gaming: changed test-runner configuration {path}"]


@pytest.mark.parametrize("path,status", [
    ("app.py", "A"), ("app.py", "M"), ("pytest.ini.bak", "M"),
    ("pytest.ini", "D"), ("runner.pth", "D"),
])
def test_unrelated_or_deleted_configuration_does_not_warn(path, status):
    task = ControlTask(id="t", goal="edit project", engine="codex",
                       policy=ExecPolicy(write=True), verify="pytest")
    assert proof.advisory_warnings(task, None, "sha", {path: status}, []) == []


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "app.py").write_text("VALUE = 0\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    task = tmp_path / "wt"
    git(repo, "worktree", "add", str(task), "-b", "task", "HEAD")
    monkeypatch.setattr(proof, "read_settings", lambda: proof.parse_settings(None))
    monkeypatch.setattr(landing, "run_verify", lambda command, *_args, **_kwargs:
                        VerifyOutcome(command, True, 0, "passed", None))
    return worktree.TaskWorktree(task, "task", repo, base)


def finish(tree, verify):
    task = ControlTask(id="t", goal="edit project settings", engine="codex",
                       policy=ExecPolicy(write=True), verify=verify)
    git(tree.path, "add", "-A")
    result = ControlResult(task_id="t", engine="codex", ok=True, text="done",
                           session_id=None, diff=git(tree.path, "diff", "--cached"))
    landed = landing.land(task, result, tree, run_id="runner", sandbox=lambda: "/stub/codex")
    assert landed.commit == git(tree.path, "rev-parse", "HEAD")
    return landed


@pytest.mark.parametrize("verify", [None, "true"])
@pytest.mark.parametrize("directory", [".codex", ".CODEX"])
def test_committed_codex_settings_warn(tree, verify, directory):
    path = f"{directory}/config.toml"
    target = tree.path / path
    target.parent.mkdir()
    target.write_text("# project settings\n")
    landed = finish(tree, verify)
    warning = (f"warning: committed diff changes .codex/ ({path}); "
               "codex loads project .codex settings in trusted repos, including the "
               "post-commit verify; review them before merging")
    assert landed.unproven_reason == (warning if verify else
                                      f"{landing.NO_VERIFY_REASON}; {warning}")
    if verify:
        assert landed.verify.passed
        assert landed.events[-1].payload["unproven_reason"] == warning


@pytest.mark.parametrize("path", ["nested/.codex/config.toml", ".codex-extra/config.toml"])
def test_only_top_level_codex_settings_warn(tree, path):
    target = tree.path / path
    target.parent.mkdir(parents=True)
    target.write_text("# unrelated settings\n")
    assert finish(tree, "true").unproven_reason is None


def test_runner_warning_preserves_proven_verdict(tree):
    target = tree.path / "tests/conftest.py"
    target.parent.mkdir()
    target.write_text("# runner settings\n")
    landed = finish(tree, "true")
    assert landed.warnings == (
        "check_gaming: changed test-runner configuration tests/conftest.py",)
    assert landed.unproven_reason is None
    assert proof.task_verdict(landed.commit, landed.verify, "true", landed.unproven_reason,
                              landed.proof) == "tests-passed"
