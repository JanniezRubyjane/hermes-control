"""Regression tests for round-3 low findings A11, B3, and B5."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import hermes_control
from hermes_control import dispatch as dispatch_module, review_pack
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult, RawEvent, stamp
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worktree import TaskWorktree
from test_scripts_portable import executable, machine, run  # noqa: F401 - fixture


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


@pytest.mark.parametrize(
    ("raw_timeout", "raw_wait", "expected_timeout", "expected_wait"),
    [
        ("abc", "abc", None, 42.0),
        (True, -5, None, 42.0),
        (False, None, None, 42.0),
        (0, 0, None, 0.0),
        (-10, "15.5", None, 15.5),
        (120, 10, 120, 10.0),
        (600.0, 600, 600.0, 300.0),
        (1.5, float("inf"), 1.5, 300.0),
        (0.5, "inf", None, 300.0),
        (float("inf"), 10, None, 10.0),
        (float("nan"), 10, None, 10.0),
        ("600", 10, None, 10.0),
        (10**400, 10, None, 10.0),
        (120, 10**400, 120, 42.0),
        (10**400, 10**400, None, 42.0),
    ],
)
def test_control_dispatch_handler_validates_timeout_and_wait(
    monkeypatch, raw_timeout, raw_wait, expected_timeout, expected_wait
):
    captured: dict[str, object] = {}

    def fake_start_dispatch(tasks, *, parent_cwd, timeout=None, planner_session=None, wait):
        captured["tasks"] = tasks
        captured["parent_cwd"] = parent_cwd
        captured["timeout"] = timeout
        captured["planner_session"] = planner_session
        captured["wait"] = wait
        return {"run_id": "r1", "state": "finished", "tasks": []}

    monkeypatch.setattr(hermes_control, "_dispatch_wait_seconds", lambda: 42.0)
    monkeypatch.setattr(dispatch_module, "start_dispatch", fake_start_dispatch)

    args: dict[str, object] = {"tasks": [], "timeout": raw_timeout}
    if raw_wait is not None:
        args["wait"] = raw_wait
    result = json.loads(hermes_control.control_dispatch_handler(args, session_id="sess-1"))

    assert result["run_id"] == "r1"
    assert captured["timeout"] == expected_timeout
    assert captured["wait"] == expected_wait


@pytest.mark.parametrize("raw_setting", ["nan", float("inf"), -5])
def test_dispatch_wait_seconds_rejects_non_finite_or_negative_setting(
    monkeypatch, caplog, raw_setting
):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "plugins": {
                "entries": {
                    "hermes_control": {"settings": {"dispatch_wait_seconds": raw_setting}}
                }
            }
        },
    )
    assert hermes_control._dispatch_wait_seconds() == float(
        hermes_control.DEFAULT_DISPATCH_WAIT_SECONDS
    )
    assert "dispatch_wait_seconds setting is invalid" in caplog.text


def test_dispatch_wait_seconds_allows_finite_setting_above_clamp(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "plugins": {
                "entries": {
                    "hermes_control": {"settings": {"dispatch_wait_seconds": 900}}
                }
            }
        },
    )
    assert hermes_control._dispatch_wait_seconds() == 900.0


def test_control_dispatch_handler_clamps_configured_wait_above_max(monkeypatch):
    captured: dict[str, object] = {}

    def fake_start_dispatch(tasks, *, parent_cwd, timeout=None, planner_session=None, wait):
        captured["wait"] = wait
        return {"run_id": "r1", "state": "finished", "tasks": []}

    monkeypatch.setattr(hermes_control, "_dispatch_wait_seconds", lambda: 900)
    monkeypatch.setattr(dispatch_module, "start_dispatch", fake_start_dispatch)

    result = json.loads(hermes_control.control_dispatch_handler({"tasks": []}))
    assert result["run_id"] == "r1"
    assert captured["wait"] == 300.0


def test_run_task_engine_with_bad_timeout_starts_no_process(tmp_path, monkeypatch):
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    def tracking_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(dispatch_module.subprocess, "Popen", tracking_popen)
    engine = SimpleNamespace(
        name="codex",
        build_argv=lambda task, *, cwd: ["python3", "-c", "import time; time.sleep(5)"],
        parse_line=lambda line: [],
        extract_result=lambda task, raws, diff: ControlResult(
            task.id, "codex", False, "", None, diff=diff
        ),
        limit_hit=lambda raws: None,
    )
    task = ControlTask("t1", "goal", "codex", ExecPolicy(write=False), why="test")
    worktree = TaskWorktree(tmp_path, "branch", tmp_path, "base")
    log = RunLog("bad-timeout", directory=tmp_path / "logs")

    try:
        with pytest.raises(TypeError):
            dispatch_module._run_task_engine(
                task,
                run_id="bad-timeout",
                log=log,
                worktree=worktree,
                engine=engine,
                timeout="abc",  # type: ignore[arg-type]
            )
        assert spawned == []
    finally:
        for proc in spawned:
            proc.kill()
            proc.wait(timeout=5)


def test_review_pack_diff_masks_secrets_matching_secret_scan_rules(tmp_path):
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "config.py").write_text(" X = 1\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "task-branch")
    fake_secret = "ghp_" + "A" * 36
    (repo / "config.py").write_text(f'TOKEN = "{fake_secret}"\n', encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "add token")
    commit = git("rev-parse", "HEAD")

    run_id = "mask-diff"
    log = RunLog(run_id)

    def add(task_id: str, engine: str, kind: str, payload: dict) -> None:
        log.append(
            stamp(
                [RawEvent(kind=kind, payload=payload)],
                run_id=run_id,
                task_id=task_id,
                engine=engine,
                start_seq=log.next_seq(task_id),
                now=1790000000,
            )
        )

    add(
        "",
        "",
        "plan",
        {
            "repo": str(repo),
            "tasks": [{"id": "t1", "engine": "codex", "why": "test"}],
        },
    )
    add("t1", "codex", "session", {"model": "gpt-test", "effort": "low"})
    add("t1", "codex", "result", {"ok": True})
    add("t1", "codex", "commit", {"sha": commit, "branch": "task-branch"})
    add(
        "",
        "",
        "summary",
        {
            "tasks": [
                {
                    "task_id": "t1",
                    "worktree": str(repo / ".worktrees" / "t1"),
                    "branch": "task-branch",
                    "base_commit": base,
                    "text": "done",
                    "unproven_reason": None,
                }
            ]
        },
    )

    pack = review_pack.build(run_id, include_diff=True)
    assert pack is not None
    diff_text = pack["tasks"][0]["diff"]
    assert diff_text is not None
    assert fake_secret not in diff_text
    assert "«masked:github-token»" in diff_text

    formatted = review_pack.format_text(pack)
    assert fake_secret not in formatted
    assert "«masked:github-token»" in formatted


def test_uninstall_purge_removes_reviews_diagrams_and_gemini_slots_while_keeping_user_files(
    machine,
):
    _, bindir, _, env = machine
    executable(bindir / "hermes", "#!/bin/sh\nexit 0\n")
    assert run("install.sh", env).returncode == 0

    home = Path(env["HERMES_HOME"])
    (home / "config.yaml").touch()
    control = home / "control"

    review_file = control / "reviews/run123/task-a.json"
    review_tmp = control / "reviews/run123/.task-a-0123456789abcdef.tmp"
    diagram_file = control / "diagrams/diag123.json"
    diagram_tmp = control / "diagrams/.diagram-abc123.tmp"
    gemini_slot = control / "slots/gemini/slot-0.lock"
    claude_slot = control / "slots/claude/slot-0.lock"
    codex_slot = control / "slots/codex/slot-0.lock"
    run_log = control / "run-12345678.jsonl"

    for path in (
        review_file,
        review_tmp,
        diagram_file,
        diagram_tmp,
        gemini_slot,
        claude_slot,
        codex_slot,
        run_log,
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("plugin data", encoding="utf-8")

    backup_file = control / "settings-backups/2026-10-22.yaml"
    backup_file.parent.mkdir(parents=True, exist_ok=True)
    backup_file.write_text("backup: true\n", encoding="utf-8")

    user_file = control / "my-goal.md"
    user_file.write_text("keep user goal\n", encoding="utf-8")

    result = run("uninstall.sh", env, "--purge")
    assert result.returncode == 0, result.stderr

    for removed in (
        review_file,
        review_tmp,
        control / "reviews/run123",
        control / "reviews",
        diagram_file,
        diagram_tmp,
        control / "diagrams",
        gemini_slot,
        claude_slot,
        codex_slot,
        control / "slots/gemini",
        control / "slots/claude",
        control / "slots/codex",
        control / "slots",
        run_log,
    ):
        assert not removed.exists(), f"expected {removed} to be removed by --purge"

    assert backup_file.read_text(encoding="utf-8") == "backup: true\n"
    assert user_file.read_text(encoding="utf-8") == "keep user goal\n"


def test_uninstall_purge_skips_review_dir_symlink_and_narrows_tmp_glob(
    machine, tmp_path
):
    _, bindir, _, env = machine
    executable(bindir / "hermes", "#!/bin/sh\nexit 0\n")
    assert run("install.sh", env).returncode == 0

    home = Path(env["HERMES_HOME"])
    (home / "config.yaml").touch()
    control = home / "control"

    external_dir = tmp_path / "external-reviews"
    external_dir.mkdir(parents=True, exist_ok=True)
    keep_json = external_dir / "keep.json"
    keep_tmp = external_dir / ".x-ab.tmp"
    keep_json.write_text('{"keep": true}\n', encoding="utf-8")
    keep_tmp.write_text("temp\n", encoding="utf-8")

    reviews_dir = control / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    linked_dir = reviews_dir / "linked"
    linked_dir.symlink_to(external_dir)

    run1_dir = reviews_dir / "run1"
    run1_dir.mkdir(parents=True, exist_ok=True)
    not_ours_tmp = run1_dir / ".notours.tmp"
    ours_tmp = run1_dir / ".task-0123abcd.tmp"
    ours_json = run1_dir / "task.json"
    not_ours_tmp.write_text("keep me\n", encoding="utf-8")
    ours_tmp.write_text("remove me\n", encoding="utf-8")
    ours_json.write_text("{}\n", encoding="utf-8")

    result = run("uninstall.sh", env, "--purge")
    assert result.returncode == 0, result.stderr

    assert keep_json.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert keep_tmp.read_text(encoding="utf-8") == "temp\n"
    assert linked_dir.is_symlink()
    assert not_ours_tmp.read_text(encoding="utf-8") == "keep me\n"
    assert not ours_tmp.exists()
    assert not ours_json.exists()

