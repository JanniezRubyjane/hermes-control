"""Regressions for the round-three guard review fixes."""

import dataclasses
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import Mock

import pytest

from hermes_control import dispatch, landing, proof
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import ControlResult, RawEvent
from hermes_control.runlog import RunLog
from hermes_control.verify import VerifyOutcome
from test_gemini_r3 import settings, task, tree  # noqa: F401 - shared fixtures


def context(tree):
    return dispatch._Context("fixes", RunLog("fixes"), str(tree.repo_root), ["gemini"], {}, {},
                             None, parallel_settings={"max_tasks": 1, "per_engine": {"gemini": 1}})


@pytest.mark.parametrize("directory", [".agents", ".gemini"])
@pytest.mark.parametrize("verify", [None, "true"])
def test_settings_read_failure_preserves_committed_agy_warning(tree, monkeypatch, directory, verify):
    target = tree.path / directory / "hooks.json"
    target.parent.mkdir()
    target.write_text("{}")
    changed_paths = proof.changed_paths
    inspected = []

    def changed(*args):
        inspected.append(True)
        return changed_paths(*args)

    def broken_settings():
        assert inspected, "changed paths must be computed before reading settings"
        raise OSError("settings unreadable")

    monkeypatch.setattr(proof, "changed_paths", changed)
    monkeypatch.setattr(proof, "read_settings", broken_settings)
    monkeypatch.setattr(landing, "run_verify", lambda *a, **k:
                        VerifyOutcome("true", True, 0, "passed", None))
    result = landing.land(task(verify=verify), ControlResult("t", "gemini", True, "done", None,
                          diff="diff"), tree, run_id="fixes", sandbox=lambda: "/stub/codex")
    assert result.commit
    assert f"warning: committed diff changes {directory}/" in result.unproven_reason
    if verify:
        assert result.verify.passed


@pytest.mark.parametrize("refusal", ["repository", "snapshot", "model", "effort"])
def test_prelaunch_refusals_report_guard(tree, settings, monkeypatch, refusal):
    adapter = GeminiAdapter()
    chosen = task()
    if refusal == "repository":
        settings.write_text("{}")
    elif refusal == "snapshot":
        monkeypatch.setattr(adapter, "permissions_snapshot", Mock(side_effect=OSError("unreadable")))
    elif refusal == "model":
        chosen = task(model=None)
    else:
        chosen = task(effort="bad")
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("agy started"))
    result = dispatch.run_task(chosen, run_id="fixes", log=RunLog("fixes"), worktree=tree,
                               adapter=adapter)
    assert not result.ok and result.failure_mode == "guard" and result.limit is None
    assert result.unproven_reason


@pytest.mark.parametrize("failed_check", [False, True])
def test_continued_worktree_refusal_reports_guard(tree, monkeypatch, failed_check):
    if failed_check:
        monkeypatch.setattr(dispatch, "changed_gemini_paths", lambda _: ([], "git unavailable"))
    else:
        target = tree.path / ".agents/hooks.json"
        target.parent.mkdir()
        target.write_text("{}")
    monkeypatch.setattr(dispatch, "run_task", lambda *a, **k: pytest.fail("worker started"))
    result = dispatch._run_guarded(context(tree), task(resume_session="session"), tree,
                                   sandbox=lambda: None, continued=True)
    assert not result.ok and result.failure_mode == "guard" and result.limit is None


@pytest.mark.parametrize("kind", ["directory", "non-json", "symlink", "broken-symlink", "unreadable"])
@pytest.mark.parametrize("continued", [False, True])
def test_unsafe_project_entries_refuse_real_continuations(tree, settings, monkeypatch, kind, continued):
    path = settings.parents[1] / "config/projects/unsafe.json"
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    elif kind == "non-json":
        path = path.with_suffix(".txt")
        path.write_text("{}")
    elif "symlink" in kind:
        target = path.parent.parent / "outside.json"
        if kind == "symlink":
            target.write_text("{}")
        path.symlink_to(target)
    else:
        path.write_text("{}")
        read = Path.read_bytes

        def unreadable(candidate):
            if candidate == path:
                raise PermissionError("unreadable")
            return read(candidate)

        monkeypatch.setattr(Path, "read_bytes", unreadable)
    monkeypatch.setattr(dispatch, "get_adapter", lambda _: GeminiAdapter())
    monkeypatch.setattr(dispatch, "_run_task_engine", lambda *a, **k: pytest.fail("agy started"))
    result = dispatch._run_guarded(context(tree), task(resume_session="session" if continued else None),
                                   tree, sandbox=lambda: None, continued=continued)
    assert not result.ok and result.failure_mode == "guard"
    assert str(path) in result.unproven_reason


@pytest.mark.parametrize("message,limited", [("Exhausted", False), ("retries exhausted", False),
    ("RESOURCE_EXHAUSTED", True), ("QUOTA EXHAUSTED", True)])
def test_exhaustion_only_identifies_resource_or_quota_limits(message, limited):
    assert (GeminiAdapter().limit_hit([RawEvent("error", {"message": message})]) is not None) == limited


@pytest.mark.parametrize("name,kind,safe", [
    (".DS_Store", "file", True), (".ds_store", "file", False),
    (".DS_Store.json", "file", False), (".DS_Store.extra", "file", False),
    (".DS_Store", "directory", False), (".DS_Store", "symlink", False),
    (".DS_Store", "broken-symlink", False),
])
def test_only_exact_regular_ds_store_is_ignored(tree, settings, monkeypatch, name, kind, safe):
    path = settings.parents[1] / "config/projects" / name
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    elif "symlink" in kind:
        target = path.parent.parent / "outside"
        if kind == "symlink":
            target.write_text("metadata")
        path.symlink_to(target)
    else:
        path.write_text("metadata")

    def worker(*a, **k):
        assert safe, "unsafe entry allowed agy to start"
        path.write_text("changed Finder metadata")
        return ControlResult("t", "gemini", True, "done", "session")

    monkeypatch.setattr(dispatch, "_run_task_engine", worker)
    result = dispatch.run_task(task(), run_id="fixes", log=RunLog("fixes"), worktree=tree,
                               adapter=GeminiAdapter())
    assert result.ok == safe
    assert result.failure_mode == (None if safe else "guard")
    if not safe:
        assert str(path) in result.unproven_reason


@pytest.mark.parametrize("planned,actual", [("codex", "gemini"), ("gemini", "codex")])
def test_process_cwd_uses_actual_adapter(tree, tmp_path, monkeypatch, planned, actual):
    link = tmp_path / "link"
    link.symlink_to(tree.path, target_is_directory=True)
    captured = {}

    def cannot_start(*a, **kwargs):
        captured.update(kwargs)
        raise OSError("test start failure")

    adapter = Mock()
    adapter.name = actual
    adapter.build_argv.return_value = ["/stub/cli"]
    adapter.engine_env.return_value = {}
    monkeypatch.setattr(dispatch.subprocess, "Popen", cannot_start)
    dispatch._run_task_engine(dataclasses.replace(task(), engine=planned), run_id="fixes",
                              log=RunLog("fixes"), worktree=dataclasses.replace(tree, path=link),
                              engine=adapter, timeout=1)
    assert captured["cwd"] == str(link.resolve() if actual == "gemini" else link)


def test_permission_cache_and_model_metadata_use_distinct_thread_values(tree, settings):
    adapter = GeminiAdapter()
    first_checked = Event()
    both_checked = Barrier(2)
    contents = ['{"enableTerminalSandbox": true}', '{"enableTerminalSandbox": true}\n']

    def run(index):
        if index:
            assert first_checked.wait(timeout=10)
        settings.write_text(contents[index])
        assert adapter.repository_problem(tree) is None
        adapter.build_argv(task(effort=("low", "high")[index]), cwd=tree.path)
        if not index:
            first_checked.set()
        both_checked.wait(timeout=10)
        snapshot = adapter.permissions_snapshot()
        model = adapter.parse_line('{"event":"init", "conversation_id":"s"}')[0].payload["model"]
        return snapshot[settings], model

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (0, 1)))
    assert results == [(hashlib.sha256(content.encode()).hexdigest(), f"gemini-4-argon-{effort}")
                       for content, effort in zip(contents, ("low", "high"))]
