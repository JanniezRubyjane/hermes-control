import dataclasses
import json
import subprocess
import sys
import time

import pytest

from hermes_control import dispatch, pauses, routing, worktree
from hermes_control.engines import registry
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult, LimitHit
from hermes_control.policy import ExecPolicy, assert_no_forbidden_flags, gemini_settings_problem
from hermes_control.runlog import RunLog


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


@pytest.fixture(autouse=True)
def enabled_gemini(monkeypatch):
    monkeypatch.setattr(registry, "ENGINE_NAMES", ("claude", "codex", "gemini"))


@pytest.fixture
def tree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    task = tmp_path / "task"
    git(repo, "worktree", "add", "-b", "test", str(task), "HEAD")
    return worktree.TaskWorktree(task, "test", repo, git(repo, "rev-parse", "HEAD"))


@pytest.fixture
def machine_settings(tmp_path, monkeypatch):
    home = tmp_path / "machine-home"
    path = home / ".gemini/antigravity-cli/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"enableTerminalSandbox": true}')
    monkeypatch.setattr(GeminiAdapter, "engine_env", lambda _: {"HOME": str(home)})
    return path


def task(engine="gemini"):
    return ControlTask("t", "fix it", engine, ExecPolicy(write=True),
                       model="gemini-4-argon" if engine == "gemini" else None)


@pytest.mark.parametrize("data", [
    {"enableTerminalSandbox": True},
    {"enableTerminalSandbox": True, "toolPermission": "request-review"},
    {"enableTerminalSandbox": True, "toolPermission": "strict"},
    {"toolPermission": "proceed-in-sandbox", "enableTerminalSandbox": True},
    {"enableTerminalSandbox": True, "permissions": {"allow": [], "deny": ["command(*)"]}},
])
def test_machine_settings_safe_values(data):
    assert gemini_settings_problem(data) is None


@pytest.mark.parametrize("data", [
    None, [], "settings", {"toolPermission": "always-proceed"},
    {"toolPermission": "future-unsafe-mode"}, {"toolPermission": True},
    {"toolPermission": None}, {"toolPermission": []},
    {"permissions": None}, {"permissions": []},
    {"permissions": {"allow": "command(*)"}}, {"permissions": {"allow": [None]}},
    *({"permissions": {"allow": [rule]}} for rule in (
        "unsandboxed(command(pwd))", "unsandboxed(read_file(src/a.py))",
        "write_file(*)", "read_file(*)", "command(*)", " Command( * ) ", "UNSANDBOXED(anything)")),
])
def test_machine_settings_unsafe_or_malformed_values_fail_closed(data):
    assert gemini_settings_problem(data)


@pytest.mark.parametrize("argv", [
    ["agy", "--remote-control"], ["agy", "--remote-control=true"],
    ["agy", "--dangerously-skip-permissions"], ["agy", "--dangerously-skip-permissions=true"],
    ["agy", "--mode", "auto"], ["agy", "--mode=auto"],
])
def test_forbidden_agy_flags_in_both_spellings(argv):
    with pytest.raises(ValueError, match="forbidden flag"):
        assert_no_forbidden_flags(argv)


@pytest.mark.parametrize("argv", [["agy", "--mode", "plan"], ["agy", "--mode=accept-edits"],
                                 ["codex", "--color", "auto"], ["claude", "--autocompact=auto"]])
def test_unrelated_auto_values_remain_allowed(argv):
    assert_no_forbidden_flags(argv)


@pytest.mark.parametrize("directory", [".agents", ".gemini", ".AGENTS", ".GEMINI"])
@pytest.mark.parametrize("kind", ["file", "directory-symlink", "nested-symlink"])
def test_committed_project_settings_or_symlinks_refuse_worker(tree, directory, kind,
                                                            machine_settings, monkeypatch):
    folder = tree.path / directory
    if kind == "directory-symlink":
        # git sees only the link text, not the settings behind it.
        folder.symlink_to("outside-settings", target_is_directory=True)
    else:
        folder.mkdir()
        path = folder / "hooks.json"
        if kind == "nested-symlink":
            path.symlink_to("../outside-settings.json")
        else:
            path.write_text('{"hooks":{"SessionStart":"touch /tmp/unsafe"}}')
    git(tree.path, "add", ".")
    git(tree.path, "commit", "-qm", "repo settings")
    tree = dataclasses.replace(tree, base_commit=git(tree.path, "rev-parse", "HEAD"))
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("worker started"))
    result = dispatch.run_task(task(), run_id="base", log=RunLog("base"), worktree=tree,
                               adapter=GeminiAdapter())
    assert not result.ok
    assert "refused to run gemini" in result.unproven_reason
    assert directory in result.unproven_reason


def test_unknown_base_is_refused(tree, machine_settings):
    tree = dataclasses.replace(tree, base_commit="missing-base")
    assert "could not check base" in GeminiAdapter().repository_problem(tree)


@pytest.mark.parametrize("directory", [".agents", ".gemini", ".AGENTS", ".GEMINI"])
def test_initial_run_refuses_untracked_settings_symlinks(tree, machine_settings, directory):
    (tree.path / directory).symlink_to("outside-settings", target_is_directory=True)
    assert directory in GeminiAdapter().repository_problem(tree)


@pytest.mark.parametrize("kind", ["missing", "directory", "invalid-json", "invalid-utf8", "unsafe"])
def test_machine_settings_read_errors_fail_closed(tree, machine_settings, kind):
    if kind == "missing":
        machine_settings.unlink()
    elif kind == "directory":
        machine_settings.unlink()
        machine_settings.mkdir()
    elif kind == "invalid-json":
        machine_settings.write_text("not JSON")
    elif kind == "invalid-utf8":
        machine_settings.write_bytes(b"\xff")
    else:
        machine_settings.write_text('{"toolPermission":"always-proceed"}')
    problem = GeminiAdapter().repository_problem(tree)
    assert "refused to run gemini" in problem
    assert "agy" in problem


def test_clean_base_and_safe_machine_settings_run(tree, machine_settings, monkeypatch):
    expected = ControlResult("t", "gemini", True, "ran", None)
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: expected)
    assert dispatch.run_task(task(), run_id="clean", log=RunLog("clean"), worktree=tree,
                             adapter=GeminiAdapter()) is expected


def ctx(tree):
    return dispatch._Context("guard", RunLog("guard"), str(tree.repo_root), ["gemini"], {}, {},
                             None, parallel_settings={"max_tasks": 1, "per_engine": {"gemini": 1}})


@pytest.mark.parametrize("directory", [".agents", ".gemini", ".AGENTS", ".GEMINI"])
@pytest.mark.parametrize("kind", ["untracked", "staged", "committed", "ignored", "symlink"])
def test_continuation_refuses_changed_settings(tree, directory, kind, monkeypatch):
    folder = tree.path / directory
    if kind == "symlink":
        folder.symlink_to("elsewhere", target_is_directory=True)
    else:
        folder.mkdir()
        (folder / "hooks.json").write_text("{}")
        if kind == "ignored":
            (tree.path / ".gitignore").write_text(directory + "/\n")
        elif kind in ("staged", "committed"):
            git(tree.path, "add", ".")
            if kind == "committed":
                git(tree.path, "commit", "-qm", "worker hooks")
    monkeypatch.setattr(dispatch, "run_task", lambda *a, **k: pytest.fail("worker started"))
    result = dispatch._run_guarded(ctx(tree), task(), tree, sandbox=lambda: None, continued=True)
    assert not result.ok
    assert "refused to run gemini" in result.unproven_reason
    assert directory in result.unproven_reason


@pytest.mark.parametrize("directory", [".agents", ".gemini"])
def test_continuation_refuses_existing_symlink_even_when_git_diff_is_clean(tree, directory):
    (tree.path / directory).symlink_to("outside", target_is_directory=True)
    git(tree.path, "add", ".")
    git(tree.path, "commit", "-qm", "base symlink")
    tree = dataclasses.replace(tree, base_commit=git(tree.path, "rev-parse", "HEAD"))
    paths, failure = worktree.changed_gemini_paths(tree)
    assert failure is None
    assert f"{directory} is a symlink" in paths


def test_continuation_check_errors_fail_closed(tree, monkeypatch):
    def failed(*args, **kwargs):
        raise OSError("git unavailable")

    monkeypatch.setattr(worktree, "_run_git", failed)
    result = dispatch._run_guarded(ctx(tree), task(), tree, sandbox=lambda: None, continued=True)
    assert not result.ok
    assert "could not check .agents/ and .gemini/" in result.unproven_reason


def test_clean_continuation_passes_guard(tree, monkeypatch):
    expected = ControlResult("t", "gemini", True, "ran", "session")
    monkeypatch.setattr(dispatch, "run_task", lambda *a, **k: expected)
    assert dispatch._run_guarded(ctx(tree), task(), tree, sandbox=lambda: None,
                                 continued=True) is expected


def test_usage_limit_handoff_checks_settings_before_gemini_starts(tree, monkeypatch):
    context = ctx(tree)
    context.parallel_settings["per_engine"]["codex"] = 1
    ran = []

    def worker(chosen, **kwargs):
        ran.append(chosen.engine)
        assert chosen.engine == "codex"
        (tree.path / ".agents").mkdir()
        (tree.path / ".agents/hooks.json").write_text("{}")
        return ControlResult("t", "codex", False, "limit", "session",
                             limit=LimitHit("codex", "usage limit"))

    monkeypatch.setattr(dispatch, "run_task", worker)
    outcome = dispatch._execute(context, task("codex"), task("codex"), None, tree,
                                sandbox=lambda: None)
    assert ran == ["codex"]
    assert outcome.task.engine == "gemini" and not outcome.result.ok
    assert ".agents/hooks.json" in outcome.result.unproven_reason


@pytest.mark.parametrize("same_engine", [True, False])
@pytest.mark.parametrize("unsafe", [".agents", ".gemini", "machine-settings", "base-settings"])
def test_resume_paused_reaches_continuation_and_repository_guards(
        tree, machine_settings, monkeypatch, same_engine, unsafe):
    previous = "gemini" if same_engine else "codex"
    if unsafe == "base-settings":
        folder = tree.path / ".agents"
        folder.mkdir()
        (folder / "hooks.json").write_text("{}")
        git(tree.path, "add", ".")
        git(tree.path, "commit", "-qm", "base hooks")
        tree = dataclasses.replace(tree, base_commit=git(tree.path, "rev-parse", "HEAD"))
    elif unsafe == "machine-settings":
        machine_settings.write_text(json.dumps({"permissions": {"allow": ["unsandboxed(command(*))"]}}))
    else:
        folder = tree.path / unsafe
        folder.mkdir()
        (folder / "hooks.json").write_text("{}")
    record = pauses.Pause("resume-t", "resume", pauses.task_to_dict(task(previous)),
                          str(tree.repo_root), pauses.worktree_to_dict(tree), previous,
                          "saved-session", "limit", None, time.time())
    pauses.save(record)
    monkeypatch.setattr(dispatch, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    monkeypatch.setattr(dispatch, "engine_status", lambda _: {
        "claude": "signed out", "codex": "signed out", "gemini": None})
    monkeypatch.setattr(dispatch, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {"claude": 1, "codex": 1, "gemini": 1}}, None))
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("worker started"))
    entry = dispatch.resume_paused(record.id)
    assert not entry["ok"]
    assert "refused to run gemini" in entry["unproven_reason"]
    assert entry["commit"] == "" and entry["paused"] is None
    assert pauses.load(record.id) is None


def test_clean_resume_preserves_session_and_sandbox_contract(tree, machine_settings, monkeypatch):
    record = pauses.Pause("resume-t", "resume", pauses.task_to_dict(task()), str(tree.repo_root),
                          pauses.worktree_to_dict(tree), "gemini", "saved-session", "limit", None,
                          time.time())
    pauses.save(record)
    monkeypatch.setattr(dispatch, "ENGINE_NAMES", ("claude", "codex", "gemini"))
    monkeypatch.setattr(dispatch, "engine_status", lambda _: {
        "claude": "signed out", "codex": "signed out", "gemini": None})
    monkeypatch.setattr(dispatch, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {"gemini": 1}}, None))
    seen = []

    def worker(chosen, **kwargs):
        seen.append(chosen)
        return ControlResult("t", "gemini", True, "ran", "saved-session", unproven_reason="empty diff")

    monkeypatch.setattr(dispatch, "_run_task_engine", worker)
    entry = dispatch.resume_paused(record.id)
    assert entry["ok"]
    assert len(seen) == 1 and seen[0].resume_session == "saved-session"


@pytest.mark.parametrize("value", [None, False, 0, 1, "true", [], {}])
def test_terminal_sandbox_must_be_exactly_true(value):
    data = {} if value is None else {"enableTerminalSandbox": value}
    problem = gemini_settings_problem(data)
    assert problem and "enableTerminalSandbox" in problem


@pytest.mark.parametrize("extra,key", [
    ({"futurePermission": True}, "futurePermission"),
    ({"hooks": {"SessionStart": "command"}}, "hooks"),
    ({"permissions": {"futurePermission": []}}, "futurePermission"),
    ({"permissions": {"deny": "command(*)"}}, "deny"),
    ({"permissions": {"deny": [1]}}, "deny"),
    ({"permissions": {"allow": ["command(python3 -m pytest -q)"]}}, "command(python3 -m pytest -q)"),
    ({"permissions": {"allow": ["read_file(src/*)"]}}, "read_file(src/*)"),
    ({"permissions": {"allow": [""]}}, "permissions.allow"),
    ({"trustedWorkspaces": "repo"}, "trustedWorkspaces"),
    ({"trustedWorkspaces": [None]}, "trustedWorkspaces"),
    ({"trustedWorkspaces": [{"repo": "path"}]}, "trustedWorkspaces"),
])
def test_unreviewed_settings_name_the_rejected_key_or_rule(extra, key):
    data = {"enableTerminalSandbox": True, **extra}
    original = json.dumps(data)
    problem = gemini_settings_problem(data)
    assert problem and key in problem
    assert json.dumps(data) == original  # The policy check is pure.
    assert gemini_settings_problem({"enableTerminalSandbox": True,
                                    "trustedWorkspaces": ["/repo"],
                                    "permissions": {"allow": [], "deny": []}}) is None


@pytest.mark.parametrize("directory", [".agents", ".gemini"])
@pytest.mark.parametrize("ignored", [False, True])
def test_first_run_refuses_untracked_and_ignored_hooks(
        tree, machine_settings, monkeypatch, directory, ignored):
    folder = tree.path / directory
    folder.mkdir()
    (folder / "hooks.json").write_text("{}")
    if ignored:
        (tree.path / ".gitignore").write_text(directory + "/\n")
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("worker started"))
    result = dispatch.run_task(task(), run_id="first", log=RunLog("first"), worktree=tree,
                               adapter=GeminiAdapter())
    assert not result.ok and directory in result.unproven_reason


@pytest.fixture
def fake_worker(tree, machine_settings, tmp_path, monkeypatch):
    """Run the real streaming/landing paths with Python standing in for agy."""
    script = tmp_path / "fake_agy.py"
    script.write_text('''
import json
import sys
from pathlib import Path

operation, target = sys.argv[1:]
Path("a.txt").write_text("worker edit\\n")
path = Path(target)
if operation == "write":
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed permission state")
elif operation == "unlink":
    path.unlink()
elif operation == "mkdir":
    path.mkdir(parents=True)
elif operation == "rmdir":
    path.rmdir()
print(json.dumps({"event": "init", "conversation_id": "saved-session"}))
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": "done"}}))
''')
    names = ("claude", "codex", "gemini")
    monkeypatch.setattr(dispatch, "ENGINE_NAMES", names)
    monkeypatch.setattr(routing, "ENGINE_NAMES", names)
    monkeypatch.setattr(dispatch, "engine_status", lambda _: {
        "claude": "signed out", "codex": "signed out", "gemini": None})
    monkeypatch.setattr(dispatch, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {name: 1 for name in names}}, None))
    monkeypatch.setattr(dispatch, "create_task_worktree_with_reason", lambda *a, **k: (tree, None))

    def configure(operation="none", target=machine_settings):
        monkeypatch.setattr(GeminiAdapter, "build_argv", lambda *a, **k: [
            sys.executable, str(script), operation, str(target)])

    return configure


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("change", [
    "read-only", "settings-edit", "settings-remove", "project-edit", "project-remove",
    "project-add", "projects-add", "projects-remove",
])
def test_dispatch_and_resume_never_commit_guard_violations(
        tree, machine_settings, monkeypatch, fake_worker, resume, change):
    projects = machine_settings.parents[1] / "config/projects"
    target = machine_settings
    operation = "write"
    if change == "read-only":
        operation = "none"
    elif change.startswith("project-"):
        projects.mkdir(parents=True)
        target = projects / "default-cli-project.json"
        if change != "project-add":
            target.write_text('{"id":"default-cli-project","projectResources":{}}')
        if change == "project-remove":
            operation = "unlink"
    elif change == "projects-add":
        target, operation = projects, "mkdir"
    elif change == "projects-remove":
        projects.mkdir(parents=True)
        target, operation = projects, "rmdir"
    elif change == "settings-remove":
        operation = "unlink"
    fake_worker(operation, target)
    chosen = dataclasses.replace(task(), model="gemini-4-argon",
                                 policy=ExecPolicy(write=change != "read-only"))
    run_id = "violation"
    if resume:
        record = pauses.Pause("violation-t", run_id, pauses.task_to_dict(chosen), str(tree.repo_root),
                              pauses.worktree_to_dict(tree), "gemini", "saved-session", "limit",
                              None, time.time())
        pauses.save(record)
        entry = dispatch.resume_paused(record.id, timeout=10)
        assert pauses.load(record.id) is None
    else:
        result = dispatch.dispatch([{
            "id": "t", "goal": "review files", "engine": "gemini", "why": "review",
            "policy": "read-only" if change == "read-only" else "write",
            "model": "gemini-4-argon",
        }], parent_cwd=str(tree.repo_root), run_id=run_id, timeout=10)
        assert "error" not in result, result
        entry = result["tasks"][0]
    assert entry["ok"] is False
    expected = "read-only gemini task changed files" if change == "read-only" else str(target)
    assert expected in entry["unproven_reason"]
    assert entry["commit"] == "" and entry["paused"] is None
    assert git(tree.path, "rev-parse", "HEAD") == tree.base_commit
    assert git(tree.path, "diff")  # The rejected edits remain available for inspection.
    events = RunLog(run_id).read_all()
    assert not any(event.kind == "commit" for event in events)
    summary = next(event for event in reversed(events) if event.kind == "summary")
    assert summary.payload["tasks"][0]["ok"] is False


def test_snapshot_hashes_permission_files_and_tracks_absence(machine_settings):
    import hashlib

    adapter = GeminiAdapter()
    projects = machine_settings.parents[1] / "config/projects"
    before = adapter.permissions_snapshot()
    assert before[machine_settings] == hashlib.sha256(machine_settings.read_bytes()).hexdigest()
    assert before[projects] is None
    projects.mkdir(parents=True)
    project = projects / "default-cli-project.json"
    project.write_text('{"projectResources":{}}')
    after = adapter.permissions_snapshot()
    assert after[projects] == "directory"
    assert after[project] == hashlib.sha256(project.read_bytes()).hexdigest()
    machine_settings.unlink()
    assert adapter.permissions_snapshot()[machine_settings] is None


@pytest.mark.parametrize("when", ["before", "after"])
def test_unreadable_permission_snapshot_fails_closed(tree, machine_settings, monkeypatch, when):
    projects = machine_settings.parents[1] / "config/projects"
    calls = []

    def snapshot(_):
        calls.append("snapshot")
        if when == "before" or len(calls) > 1:
            raise PermissionError(13, "unreadable", str(projects))
        return {}

    def worker(*args, **kwargs):
        assert when == "after", "worker started without a readable snapshot"
        return ControlResult("t", "gemini", True, "done", None, diff="diff")

    monkeypatch.setattr(GeminiAdapter, "permissions_snapshot", snapshot, raising=False)
    monkeypatch.setattr(dispatch, "_run_task_engine", worker)
    result = dispatch.run_task(task(), run_id="unreadable", log=RunLog("unreadable"),
                               worktree=tree, adapter=GeminiAdapter())
    assert not result.ok and str(projects) in result.unproven_reason


def test_guard_violation_stops_usage_limit_handoff(tree, machine_settings, monkeypatch):
    def worker(*args, **kwargs):
        machine_settings.write_text('{"enableTerminalSandbox": false}')
        return ControlResult("t", "gemini", False, "limit", "s", diff="diff",
                             limit=LimitHit("gemini", "quota"))

    monkeypatch.setattr(dispatch, "_run_task_engine", worker)
    chosen = dataclasses.replace(task(), policy=ExecPolicy(write=False))
    result = dispatch.run_task(chosen, run_id="limit-guard", log=RunLog("limit-guard"),
                               worktree=tree, adapter=GeminiAdapter())
    assert result.limit is None
    assert "read-only gemini task changed files" in result.unproven_reason
    assert str(machine_settings) in result.unproven_reason
