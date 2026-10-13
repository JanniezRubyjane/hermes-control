"""Public repositories must not configure code execution or expose credentials."""

import dataclasses
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_control import dispatch, landing, review_pack, worktree
from hermes_control.engines import claude
from hermes_control.engines.base import ControlTask, EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.verify import VerifyOutcome


SECRET = "sk-" + "ant-api03-" + "A" * 95
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/merge_check.sh"


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "a.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    return repo


def _task(engine="claude", **kwargs):
    return ControlTask(id="t1", goal="change a file", engine=engine,
                       policy=ExecPolicy(write=True), **kwargs)


def _wt(repo):
    return worktree.TaskWorktree(repo, "test", repo, _git(repo, "rev-parse", "HEAD"))


@pytest.mark.parametrize("supported", [True, False])
def test_claude_restricted_detection_is_cached_and_controls_argv(
        monkeypatch, supported, _stub_claude_support_probe):
    calls = []

    def help_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="--restricted Restricted mode" if supported
                               else "--tools <tools>")

    monkeypatch.setattr(claude, "_supports_restricted", _stub_claude_support_probe)
    monkeypatch.setattr(claude, "engine_env", lambda: {"PATH": "/fake"})
    monkeypatch.setattr(claude.shutil, "which", lambda *a, **k: "/fake/claude")
    monkeypatch.setattr(claude.subprocess, "run", help_run)
    try:
        for _ in range(2):
            argv = ClaudeAdapter().build_argv(_task(), cwd=Path("/wt"))
            assert argv[0] == "/fake/claude"
            assert ("--restricted" in argv) is supported
            assert "--strict-mcp-config" in argv
            assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
        assert calls == [["/fake/claude", "--help"]]
    finally:
        claude._supports_restricted.cache_clear()


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_claude_help_failure_is_not_support(
        monkeypatch, failure, _stub_claude_support_probe):
    def help_run(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("claude", 20)
        return SimpleNamespace(returncode=1, stdout="--restricted")

    monkeypatch.setattr(claude, "_supports_restricted", _stub_claude_support_probe)
    monkeypatch.setattr(claude, "engine_env", lambda: {"PATH": "/fake"})
    monkeypatch.setattr(claude.subprocess, "run", help_run)
    try:
        assert not claude._supports_restricted("/fake/claude")
    finally:
        claude._supports_restricted.cache_clear()


@pytest.mark.parametrize("help_text,supported", [
    ("  --restricted Restricted mode\n", True),
    ("Options:\n\t--restricted\n", True),
    ("  --restricted-tools <tools>\n", False),
    ("Use --restricted for safety\n", False),
    ("  --restricted_tools <tools>\n", False),
    ("  --restricted2\n", False),
])
def test_claude_help_requires_exact_option(
        monkeypatch, help_text, supported, _stub_claude_support_probe):
    monkeypatch.setattr(claude, "engine_env", lambda: {"PATH": "/fake"})
    monkeypatch.setattr(claude.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=help_text))
    assert _stub_claude_support_probe("/fake/claude") is supported


def test_claude_probe_and_argv_use_the_unresolved_path_and_cache_on_realpath(
        tmp_path, monkeypatch, _stub_claude_support_probe):
    binary = tmp_path / "claude-real"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    bins = [tmp_path / "bin1", tmp_path / "bin2"]
    for folder in bins:
        folder.mkdir()
        (folder / "claude").symlink_to(binary)
    calls = []

    def help_run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="  --restricted\n")

    spawn_env = {"PATH": str(bins[0])}
    monkeypatch.setattr(claude, "engine_env", lambda: spawn_env.copy())
    monkeypatch.setattr(claude, "_supports_restricted", _stub_claude_support_probe)
    monkeypatch.setattr(claude.subprocess, "run", help_run)
    adapter = ClaudeAdapter()
    info = adapter.probe()
    # A shim launcher (volta) dispatches on its own name: never exec the resolved target.
    assert info.path == str(bins[0] / "claude")
    for folder in bins:
        spawn_env["PATH"] = str(folder)
        argv = adapter.build_argv(_task(), cwd=tmp_path)
        assert argv[0] == str(folder / "claude")
        assert "--restricted" in argv
    # Both links reach one binary, so --help is probed once.
    assert calls == [[str(bins[0] / "claude"), "--version"],
                     [str(bins[0] / "claude"), "--help"]]


@pytest.mark.parametrize("settings", [".claude/settings.json", ".claude/settings.local.json",
                                     ".CLAUDE/settings.json", ".Codex/config.toml",
                                     ".codex/config.toml", ".codex/hooks.toml",
                                     ".codex/rules/x.rules", ".codex/instructions.md"])
def test_committed_settings_refuse_worker_even_when_removed(repo, monkeypatch, settings):
    path = repo / settings
    path.parent.mkdir(parents=True)
    path.write_text("{}" if settings.endswith("json") else "[hooks]\n")
    _git(repo, "add", "-f", settings)
    _git(repo, "commit", "-qm", "settings")
    wt = _wt(repo)
    path.unlink()  # The base, not the working tree, determines the verdict.
    engine = "claude" if settings.casefold().startswith(".claude") else "codex"
    adapter = ClaudeAdapter() if engine == "claude" else CodexAdapter()
    monkeypatch.setattr(claude, "_supports_restricted", lambda _: False)
    monkeypatch.setattr(adapter, "probe", lambda: EngineInfo(engine, "old", "/fake/" + engine))
    monkeypatch.setattr(dispatch, "_run_task_engine",
                        lambda *a, **k: pytest.fail("unsafe worker started"))
    result = dispatch.run_task(_task(engine), run_id="blocked", log=RunLog("blocked"),
                               worktree=wt, adapter=adapter)
    assert not result.ok
    assert result.unproven_reason
    if engine == "claude":
        assert result.unproven_reason == (
            "claude old lacks --restricted and the repo commits .claude settings "
            "(hooks would run unsandboxed); update claude or use codex")
    else:
        assert ".codex" in result.unproven_reason
        assert "project" in result.unproven_reason


@pytest.mark.parametrize("engine,supported", [("claude", False), ("claude", True),
                                              ("codex", False)])
@pytest.mark.parametrize("location", ["directory", "nested", "blob"])
@pytest.mark.parametrize("directory", [".codex", ".claude", ".Codex", ".CLAUDE"])
def test_committed_settings_symlinks_refuse_worker(repo, monkeypatch, engine,
                                                 supported, location, directory):
    target = repo / "tracked-config"
    target.mkdir()
    filename = "settings.json" if directory.casefold() == ".claude" else "config.toml"
    (target / filename).write_text("{}")
    folder = repo / directory
    if location == "directory":
        folder.symlink_to("tracked-config", target_is_directory=True)
    else:
        folder.mkdir()
        if location == "nested":
            (folder / "nested").symlink_to("../tracked-config", target_is_directory=True)
        else:
            (folder / "instructions.md").symlink_to("../tracked-config/" + filename)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "symlinked settings")
    wt = _wt(repo)
    (folder if location == "directory" else next(folder.iterdir())).unlink()
    adapter = ClaudeAdapter() if engine == "claude" else CodexAdapter()
    monkeypatch.setattr(claude, "_supports_restricted", lambda _: supported)
    monkeypatch.setattr(claude.shutil, "which", lambda *a, **k: "/fake/claude")
    monkeypatch.setattr(adapter, "probe", lambda: EngineInfo(engine, "old", "/fake/" + engine))
    monkeypatch.setattr(dispatch, "_run_task_engine",
                        lambda *a, **k: pytest.fail("symlinked settings worker started"))
    result = dispatch.run_task(_task(engine), run_id="symlink", log=RunLog("symlink"),
                               worktree=wt, adapter=adapter)
    assert not result.ok
    assert result.unproven_reason


@pytest.mark.parametrize("engine,supported", [("claude", False), ("claude", True),
                                              ("codex", False)])
def test_safe_base_runs_worker(repo, monkeypatch, engine, supported):
    adapter = ClaudeAdapter() if engine == "claude" else CodexAdapter()
    if engine == "claude":
        (repo / ".claude").mkdir()
        (repo / ".claude/instructions.md").write_text("instructions\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "instructions")
    if supported:
        (repo / ".claude/settings.json").write_text("{}")
        _git(repo, "add", ".")
        _git(repo, "commit", "-qm", "settings")
    monkeypatch.setattr(claude, "_supports_restricted", lambda _: supported)
    monkeypatch.setattr(claude.shutil, "which", lambda *a, **k: "/fake/claude")
    expected = ControlResult("t1", engine, True, "ran", None)
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: expected)
    assert dispatch.run_task(_task(engine), run_id="safe", log=RunLog("safe"),
                             worktree=_wt(repo), adapter=adapter) is expected


@pytest.mark.parametrize("engine,supported", [("claude", False), ("claude", True),
                                              ("codex", False)])
def test_worker_refuses_uninspectable_base(repo, monkeypatch, engine, supported):
    adapter = ClaudeAdapter() if engine == "claude" else CodexAdapter()
    monkeypatch.setattr(claude, "_supports_restricted", lambda _: supported)
    monkeypatch.setattr(claude.shutil, "which", lambda *a, **k: "/fake/claude")
    wt = dataclasses.replace(_wt(repo), base_commit="missing-commit")
    monkeypatch.setattr(dispatch, "_run_task_engine",
                        lambda *a, **k: pytest.fail("worker started with unknown settings"))
    result = dispatch.run_task(_task(engine), run_id="bad-base", log=RunLog("bad-base"),
                               worktree=wt, adapter=adapter)
    assert not result.ok
    assert "could not check base" in result.unproven_reason


def test_codex_clean_continuation_runs(repo, monkeypatch):
    expected = ControlResult("t1", "codex", True, "ran", None)
    monkeypatch.setattr(dispatch, "run_task", lambda *a, **k: expected)
    ctx = dispatch._Context("clean", RunLog("clean"), str(repo), [], {}, {}, None)
    assert dispatch._run_guarded(ctx, _task("codex"), _wt(repo),
                                 sandbox=lambda: None, continued=True) is expected


@pytest.mark.parametrize("kind", ["untracked", "ignored", "committed", "symlink"])
def test_codex_continuation_refuses_changed_settings(repo, monkeypatch, tmp_path, kind):
    wt = _wt(repo)
    folder = repo / ".codex"
    if kind == "symlink":
        folder.symlink_to(tmp_path, target_is_directory=True)
    else:
        folder.mkdir()
        (folder / "config.toml").write_text("[hooks]\n")
        if kind == "ignored":
            (repo / ".gitignore").write_text(".codex/\n")
        elif kind == "committed":
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "worker settings")
    monkeypatch.setattr(dispatch, "run_task", lambda *a, **k: pytest.fail("worker started"))
    ctx = dispatch._Context("continue", RunLog("continue"), str(repo), [], {}, {}, None)
    result = dispatch._run_guarded(ctx, _task("codex"), wt,
                                   sandbox=lambda: None, continued=True)
    assert not result.ok
    assert "refused to run codex" in result.unproven_reason
    assert ".codex" in result.unproven_reason


@pytest.mark.parametrize("runner", ["worktree", "review"])
@pytest.mark.parametrize("driver", ["clean", "smudge", "process"])
def test_git_does_not_run_named_filter(repo, tmp_path, runner, driver):
    marker = tmp_path / "filter-ran"
    (repo / ".gitattributes").write_text("a.txt filter=hostile\n")
    _git(repo, "config", "filter.hostile." + driver,
         "touch " + shlex.quote(str(marker)) + "; cat")
    _git(repo, "config", "filter.hostile.required", "true")
    args = ["add", "a.txt"] if driver != "smudge" else ["checkout", "HEAD", "--", "a.txt"]
    if driver == "smudge":
        (repo / "a.txt").unlink()
    else:
        (repo / "a.txt").write_text("changed\n")
    if runner == "worktree":
        result = worktree._run_git(args, cwd=str(repo))
        assert result.returncode == 0, result.stderr
    else:
        review_pack._git(repo, *args)
    assert not marker.exists()


@pytest.mark.parametrize("runner", ["worktree", "review"])
def test_git_fails_closed_if_filter_discovery_fails(repo, monkeypatch, runner):
    monkeypatch.setattr(worktree, "noninteractive_repo_git_env", lambda cwd: None)
    monkeypatch.setattr(worktree.subprocess, "run",
                        lambda *a, **k: pytest.fail("git ran without hardened environment"))
    with pytest.raises((OSError, RuntimeError), match="harden|filter"):
        if runner == "worktree":
            worktree._run_git(["status"], cwd=str(repo))
        else:
            review_pack._git(repo, "status")


def test_goal_and_verify_red_tails_are_redacted_in_summary_and_log(repo):
    task = dataclasses.replace(_task(), goal="fix " + SECRET, verify="test", red="test")
    message = landing.commit_message(task, "safe")
    assert SECRET not in message
    assert "fix " in message and "Hermes-Control-Run: safe" in message
    outcome = VerifyOutcome("test", False, 1, "failure " + SECRET, "failed")
    evidence = {"verdict": "red-inconclusive", "red": {"output_tail": SECRET}}
    landed = landing.Landing("abc", outcome, "sandbox did not start: " + SECRET, proof=evidence)
    result = ControlResult("t1", "claude", True, "ran " + SECRET, None)
    entry = dispatch._done_entry(task, result, _wt(repo), landed, None)
    assert "sandbox did not start:" in entry["unproven_reason"]
    assert "ran " in entry["text"]
    assert SECRET not in json.dumps(entry)
    log = RunLog("tails")
    dispatch._log_summary(log, "tails", [entry], None)
    assert SECRET not in log.path.read_text()


def test_review_pack_redacts_tails_from_older_logs(monkeypatch):
    task = {"id": "t1", "attempts": [], "verify": {"output_tail": SECRET, "passed": False},
            "summary": {"proof": {"red": {"output_tail": SECRET}}}, "rerouted": {},
            "status": "failed", "status_reason": "failed", "why": "test",
            "planned_engine": "claude", "model": None, "limits": [], "paused": None}
    monkeypatch.setattr(review_pack.runs, "run_view", lambda *a, **k: {
        "tasks": [task], "repo": None, "state": "finished"})
    monkeypatch.setattr(review_pack.RunLog, "read_all", lambda *a: [])
    monkeypatch.setattr(review_pack.runs, "run_stats", lambda *a, **k: [])
    monkeypatch.setattr(review_pack, "_git_evidence", lambda *a, **k: {})
    pack = review_pack.build("old")
    assert SECRET not in json.dumps(pack)


def test_merge_check_uses_mktemp_directory(repo, tmp_path):
    path_dir = tmp_path / "bin"
    path_dir.mkdir()
    for program in ("git", "sh", "mktemp"):
        (path_dir / program).symlink_to(shutil.which(program))
    probe = tmp_path / "scratch-path"
    proc = subprocess.run(["/bin/sh", str(SCRIPT), "--unsandboxed",
                           "pwd > " + shlex.quote(str(probe)), "HEAD"], cwd=repo,
                          env={**os.environ, "PATH": str(path_dir), "TMPDIR": str(tmp_path)},
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    scratch = Path(probe.read_text().strip())
    assert scratch.parent == tmp_path.resolve()
    assert not scratch.exists()
    source = SCRIPT.read_text()
    assert '$(mktemp -d' in source
    assert "hc-merge-check-$$" not in source
