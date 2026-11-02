"""Round-three regressions for the opt-in agy engine (no live CLI probes)."""

import dataclasses
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from hermes_control import dispatch, landing, limits, proof, worktree
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult, LimitHit, RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
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
    (repo / "a.txt").write_text("base\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    task = tmp_path / "task"
    git(repo, "worktree", "add", "-b", "test", str(task), "HEAD")
    return worktree.TaskWorktree(task, "test", repo, git(repo, "rev-parse", "HEAD"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    home = tmp_path / "home"
    path = home / ".gemini/antigravity-cli/settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"enableTerminalSandbox": true}')
    monkeypatch.setattr(GeminiAdapter, "engine_env", lambda _: {"HOME": str(home)})
    monkeypatch.setattr(GeminiAdapter, "_path", lambda _: "/stub/agy")
    return path


def task(**kwargs):
    return ControlTask("t", "fix it", "gemini", ExecPolicy(write=True),
                       **{"model": "gemini-4-argon", **kwargs})


@pytest.mark.parametrize("data", [{}, {"id": "default", "name": "Default"},
                                   {"id": "default", "name": "Default", "projectResources": {}}])
def test_safe_project_metadata_is_accepted(tree, settings, data):
    project = settings.parents[1] / "config/projects/default-cli-project.json"
    project.parent.mkdir(parents=True)
    project.write_text(json.dumps(data))
    assert GeminiAdapter().repository_problem(tree) is None


@pytest.mark.parametrize("content,reason", [
    ('{"permissions": {}}', "permissions"), ('{"hooks": {}}', "hooks"),
    ('{"future": true}', "future"), ('{"projectResources": {"permission": true}}', "projectResources"),
    ('{"projectResources": []}', "projectResources"), ('{"projectResources": null}', "projectResources"),
    ('[]', "object"), ('null', "object"), ('broken json', "parse"),
])
def test_project_permission_files_fail_closed(tree, settings, content, reason):
    project = settings.parents[1] / "config/projects/default-cli-project.json"
    project.parent.mkdir(parents=True)
    project.write_text(content)
    problem = GeminiAdapter().repository_problem(tree)
    assert problem and str(project) in problem and reason in problem


@pytest.mark.parametrize("kind", ["directory", "non-json", "symlink", "broken-symlink", "unreadable"])
def test_unsafe_project_entries_refuse_initial_and_resume_runs(tree, settings, monkeypatch, kind):
    project = settings.parents[1] / "config/projects/unsafe.json"
    project.parent.mkdir(parents=True)
    if kind == "directory":
        project.mkdir()
    elif kind == "non-json":
        project = project.with_suffix(".txt")
        project.write_text("{}")
    elif "symlink" in kind:
        target = project.parent.parent / "outside.json"
        if kind == "symlink":
            target.write_text("{}")
        project.symlink_to(target)
    else:
        project.write_text("{}")
        original = Path.read_bytes

        def unreadable(path):
            if path == project:
                raise PermissionError("unreadable")
            return original(path)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("worker started"))
    for resume in (None, "session"):
        result = dispatch.run_task(task(resume_session=resume), run_id="unsafe", log=RunLog("unsafe"),
                                   worktree=tree, adapter=GeminiAdapter())
        assert not result.ok and str(project) in result.unproven_reason


@pytest.mark.parametrize("content,safe", [("", True), ("{}", True), (" \n{}\n", True),
                                          ('{"SessionStart": []}', False), ("[]", False),
                                          ("null", False), ("invalid", False)])
def test_user_hooks_are_empty_or_refused(tree, settings, content, safe):
    hooks = settings.with_name("hooks.json")
    hooks.write_text(content)
    problem = GeminiAdapter().repository_problem(tree)
    assert (problem is None) == safe
    if not safe:
        assert str(hooks) in problem


def test_unreadable_user_hooks_fail_closed(tree, settings, monkeypatch):
    hooks = settings.with_name("hooks.json")
    hooks.write_text("{}")
    original = Path.read_bytes

    def unreadable(path):
        if path == hooks:
            raise PermissionError("unreadable hooks")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert str(hooks) in GeminiAdapter().repository_problem(tree)


@pytest.mark.parametrize("relative", ["antigravity-cli/hooks.json", "antigravity-cli/cache/projects.json"])
def test_snapshot_tracks_extra_permission_state(settings, relative):
    adapter = GeminiAdapter()
    path = settings.parents[1] / relative
    assert adapter.permissions_snapshot()[path] is None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"{}")
    assert adapter.permissions_snapshot()[path] == hashlib.sha256(b"{}").hexdigest()
    path.unlink()
    assert adapter.permissions_snapshot()[path] is None


@pytest.mark.parametrize("relative,initial,changed", [
    ("antigravity-cli/settings.json", '{"enableTerminalSandbox": true}', '{"enableTerminalSandbox": false}'),
    ("config/projects/default.json", '{"projectResources": {}}', '{"permissions": {"allow": ["*"]}}'),
    ("antigravity-cli/hooks.json", '{}', '{"SessionStart": ["command"]}'),
])
def test_before_snapshot_uses_the_bytes_checked_by_repository_guard(tree, settings, relative,
                                                                  initial, changed):
    target = settings.parents[1] / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(initial)
    adapter = GeminiAdapter()
    assert adapter.repository_problem(tree) is None
    target.write_text(changed)
    before = adapter.permissions_snapshot()
    assert before[target] == hashlib.sha256(initial.encode()).hexdigest()
    assert adapter.permissions_snapshot()[target] == hashlib.sha256(changed.encode()).hexdigest()


def test_settings_are_read_once_for_guard_and_before_snapshot(tree, settings, monkeypatch):
    original = Path.read_bytes
    reads = []

    def counted(path):
        if path == settings:
            reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    adapter = GeminiAdapter()
    assert adapter.repository_problem(tree) is None
    adapter.permissions_snapshot()
    assert reads == [settings]


@pytest.mark.parametrize("model,effort,expected", [
    ("gemini-4-argon", None, "gemini-4-argon-medium"),
    ("gemini-4-argon", "high", "gemini-4-argon-high"),
    ("gemini-4-argon-low", None, "gemini-4-argon-low"),
    ("gemini-4-argon-low", "high", "gemini-4-argon-high"),
    ("claude-opus-5-5-high", "low", "claude-opus-5-5-low"),
    ("claude-sonnet-5-5", None, "claude-sonnet-5-5-medium"),
    ("gpt-oss-120b-medium", None, "gpt-oss-120b-medium"),
])
def test_model_id_always_encodes_effort(settings, model, effort, expected):
    argv = GeminiAdapter().build_argv(task(model=model, effort=effort), cwd=Path("/wt"))
    assert argv[argv.index("--model") + 1] == expected
    assert "--effort" not in argv
    assert GeminiAdapter.efforts == ("low", "medium", "high")


@pytest.mark.parametrize("resume", [None, "session"])
def test_missing_model_is_actionable_and_never_launches(tree, settings, monkeypatch, resume):
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("agy launched"))
    result = dispatch.run_task(task(model=None, resume_session=resume), run_id="missing-model",
                               log=RunLog("missing-model"), worktree=tree, adapter=GeminiAdapter())
    assert not result.ok
    assert "plugins.entries.hermes_control.settings.models.gemini.default" in result.unproven_reason


@pytest.mark.parametrize("message,limited", [
    ("quota", False), ("Insufficient QUOTA", False), ("quota configuration missing", False),
    ('invalid model selection: quota exceeded', False),
    ('invalid model selection: rate limit', False),
    ("QUOTA exceeded", True), ("Exhausted", False), ("RESOURCE_EXHAUSTED", True),
    ("HTTP 429", True), ("429 Too Many Requests", True),
])
def test_limit_detection_requires_a_real_limit_message(message, limited):
    hit = GeminiAdapter().limit_hit([RawEvent("error", {"message": message})])
    assert (hit is not None) == limited


@pytest.mark.parametrize("engine_name", ["gemini", "codex"])
def test_safety_refusal_overrides_limit_failure_mode(tree, settings, monkeypatch, engine_name):
    chosen = dataclasses.replace(task(), engine=engine_name, policy=ExecPolicy(write=False))
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: ControlResult(
        "t", "gemini", False, "limit", "s", diff="diff", limit=LimitHit("gemini", "quota exceeded"),
        failure_mode="limit"))
    result = dispatch.run_task(chosen, run_id="guard", log=RunLog("guard"),
                               worktree=tree, adapter=GeminiAdapter())
    assert result.limit is None and result.failure_mode == "guard"
    assert "read-only gemini" in result.unproven_reason
    entry = dispatch._done_entry(chosen, result, tree, landing.Landing(None, None, result.unproven_reason), None)
    assert entry["failure_mode"] == "guard"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_error_result_keeps_stderr_tail_and_run_log_names_model(tree, settings, monkeypatch, exit_code):
    adapter = GeminiAdapter()
    script = ('import json,sys; print(json.dumps({"event":"init", "conversation_id":"s"})); '
              'print(json.dumps({"event":"result", "result":{"status":"ERROR"}})); '
              'print("real provider diagnostic: quota configuration unavailable", file=sys.stderr); '
              f'sys.exit({exit_code})')
    build_argv = adapter.build_argv

    def fake_argv(chosen, **kwargs):
        return [sys.executable, "-c", script, *build_argv(chosen, **kwargs)[1:]]

    monkeypatch.setattr(adapter, "build_argv", fake_argv)
    result = dispatch.run_task(task(effort="high"), run_id="error", log=RunLog("error"),
                               worktree=tree, adapter=adapter, timeout=10)
    assert not result.ok and result.limit is None
    assert "real provider diagnostic: quota configuration unavailable" in result.unproven_reason
    assert any(event.kind in ("session", "result") and
               event.payload.get("model") == "gemini-4-argon-high" for event in RunLog("error").read_all())


@pytest.mark.parametrize("engine_name", ["claude", "codex", "gemini"])
def test_only_gemini_resolves_process_cwd(tree, tmp_path, monkeypatch, engine_name):
    link = tmp_path / "link"
    link.symlink_to(tree.path, target_is_directory=True)
    captured = {}

    def cannot_start(*args, **kwargs):
        captured.update(kwargs)
        raise OSError("test start failure")

    adapter = Mock(name=engine_name)
    adapter.name = engine_name
    adapter.build_argv.return_value = ["/stub/cli"]
    adapter.engine_env.return_value = {}
    monkeypatch.setattr(dispatch.subprocess, "Popen", cannot_start)
    dispatch._run_task_engine(dataclasses.replace(task(), engine=engine_name), run_id="cwd",
                              log=RunLog("cwd"), worktree=dataclasses.replace(tree, path=link),
                              engine=adapter, timeout=1)
    assert captured["cwd"] == str(link.resolve() if engine_name == "gemini" else link)


@pytest.mark.parametrize("engine_name", ["claude", "codex", "gemini"])
@pytest.mark.parametrize("inspection_fails", [False, True])
def test_advisory_git_failure_does_not_skip_verification(tree, monkeypatch, caplog,
                                                        engine_name, inspection_fails):
    (tree.path / "a.txt").write_text("edited\n")
    chosen = dataclasses.replace(task(verify="true"), engine=engine_name)
    changed = Mock(side_effect=OSError("git failed")) if inspection_fails else Mock(return_value={"a.txt": "M"})
    monkeypatch.setattr(proof, "changed_paths", changed)
    monkeypatch.setattr(proof, "advisory_warnings", lambda *a: [])
    verify = Mock(return_value=VerifyOutcome("true", True, 0, "passed", None))
    monkeypatch.setattr(landing, "run_verify", verify)
    result = ControlResult("t", engine_name, True, "done", None, diff="diff")
    landed = landing.land(chosen, result, tree, run_id="git-failure", sandbox=lambda: "/stub/codex")
    assert landed.commit and landed.verify and landed.verify.passed
    verify.assert_called_once()
    changed.assert_called_once()
    if inspection_fails:
        assert "warning" in caplog.text.lower() and "git failed" in caplog.text


def test_disabled_engine_warning_is_once_per_process(monkeypatch, caplog):
    monkeypatch.setattr(limits, "ENGINE_NAMES", ("claude", "codex"))
    monkeypatch.setattr(limits, "_warned_disabled_engines", set(), raising=False)
    for _ in range(3):
        assert limits.parse_caps({"gemini": {"invalid": True}}) == {"claude": 80, "codex": 80}
    messages = [record.message for record in caplog.records if "engine 'gemini' is disabled" in record.message]
    assert len(messages) == 1


@pytest.mark.parametrize("engine_name", ["claude", "codex", "gemini"])
def test_agy_warning_inspection_failure_is_advisory(tree, monkeypatch, caplog, engine_name):
    class FailedGitPaths(dict):
        def __iter__(self):
            raise subprocess.CalledProcessError(1, "git diff")

    (tree.path / "a.txt").write_text("edited\n")
    monkeypatch.setattr(proof, "changed_paths", lambda *a: FailedGitPaths())
    monkeypatch.setattr(proof, "advisory_warnings", lambda *a: [])
    monkeypatch.setattr(landing, "run_verify", lambda *a, **k: VerifyOutcome("true", True, 0, "passed", None))
    chosen = dataclasses.replace(task(verify="true"), engine=engine_name)
    landed = landing.land(chosen, ControlResult("t", engine_name, True, "done", None, diff="diff"),
                          tree, run_id="warning-failure", sandbox=lambda: "/stub/codex")
    assert landed.verify.passed and landed.unproven_reason is None
    assert "could not inspect task t for agy settings" in caplog.text


@pytest.mark.parametrize("relative,initial,changed", [
    ("antigravity-cli/settings.json", '{"enableTerminalSandbox": true}', '{"enableTerminalSandbox": false}'),
    ("config/projects/default.json", '{}', '{"permissions": {}}'),
    ("antigravity-cli/hooks.json", '{}', '{"SessionStart": []}'),
])
def test_check_to_snapshot_race_is_a_guard_failure(tree, settings, monkeypatch, relative, initial, changed):
    target = settings.parents[1] / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(initial)
    adapter = GeminiAdapter()
    snapshot = adapter.permissions_snapshot
    calls = []

    def racing_snapshot():
        if not calls:
            target.write_text(changed)
        calls.append(True)
        return snapshot()

    monkeypatch.setattr(adapter, "permissions_snapshot", racing_snapshot)
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: ControlResult(
        "t", "gemini", True, "done", "session"))
    result = dispatch.run_task(task(), run_id="race", log=RunLog("race"), worktree=tree, adapter=adapter)
    assert not result.ok and result.failure_mode == "guard"
    assert str(target) in result.unproven_reason


def test_permission_cache_and_model_metadata_are_thread_local(tree, settings):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    adapter = GeminiAdapter()
    barrier = Barrier(2)

    def run(effort):
        assert adapter.repository_problem(tree) is None
        adapter.build_argv(task(effort=effort), cwd=tree.path)
        barrier.wait(timeout=10)
        snapshot = adapter.permissions_snapshot()
        model = adapter.parse_line('{"event":"init", "conversation_id":"s"}')[0].payload["model"]
        return snapshot, model

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ("low", "high")))
    assert [model for _, model in results] == ["gemini-4-argon-low", "gemini-4-argon-high"]
    assert all(snapshot[settings] == hashlib.sha256(settings.read_bytes()).hexdigest()
               for snapshot, _ in results)


@pytest.mark.parametrize("relative", ["antigravity-cli/hooks.json", "antigravity-cli/cache/projects.json"])
@pytest.mark.parametrize("change", ["create", "edit", "remove"])
def test_extra_permission_state_changes_are_guard_failures(tree, settings, monkeypatch, relative, change):
    target = settings.parents[1] / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if change != "create":
        target.write_text("{}")

    def worker(*args, **kwargs):
        if change == "remove":
            target.unlink()
        else:
            target.write_text('{"grant": true}')
        return ControlResult("t", "gemini", False, "limit", "s", limit=LimitHit("gemini", "quota exceeded"),
                             failure_mode="limit")

    monkeypatch.setattr(dispatch, "_run_task_engine", worker)
    result = dispatch.run_task(task(), run_id="extra-state", log=RunLog("extra-state"),
                               worktree=tree, adapter=GeminiAdapter())
    assert not result.ok and result.limit is None and result.failure_mode == "guard"
    assert str(target) in result.unproven_reason
