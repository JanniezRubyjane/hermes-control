"""Kept apart from test_dispatch_parallel.py so parallel branches merge cleanly."""
import threading

import pytest

import hermes_control.dispatch as dispatch_module  # noqa: F401
from hermes_control.dispatch import control_status, dispatch  # noqa: F401
from hermes_control.overlap import find_overlaps
from tests.scripted_engine import _engines, _git, repo  # noqa: F401
from tests.test_dispatch_parallel import (  # noqa: F401
    configure, engines, events, gate, task)


@pytest.mark.parametrize("same_file", [True, False])
def test_committed_file_overlap_is_reported(repo, monkeypatch, engines, same_file):
    configure(monkeypatch)
    base = _git(["rev-parse", "HEAD"], repo)

    def build_argv(task, *, cwd):
        name = "shared.txt" if same_file else f"{task.id}.txt"
        return ["python3", "-c", f"open({name!r}, 'a').write({task.id!r});"
                "print('{\"k\": \"ok\"}')"]

    monkeypatch.setattr(engines["codex"], "build_argv", build_argv)
    tasks = [task("a"), task("b")]
    for item in tasks:
        item["verify"] = "test -s shared.txt" if same_file else f"test -s {item['id']}.txt"
    result = dispatch(tasks, parent_cwd=str(repo))
    assert all(entry["commit"] and entry["verify"]["passed"] for entry in result["tasks"])
    expected = ([{"tasks": ["a", "b"], "base_commit": base, "files": ["shared.txt"]}]
                if same_file else [])
    summary_event = next(e for e in events(result) if e["kind"] == "summary")
    assert summary_event["payload"]["overlaps"] == expected
    status = control_status(result["run_id"])
    if same_file:
        assert result["overlaps"] == expected
        assert status["overlaps"] == expected
    else:
        assert "overlaps" not in result
        assert "overlaps" not in status


def test_uncommitted_task_does_not_count_as_overlap(repo, monkeypatch, engines):
    configure(monkeypatch)

    def build_argv(task, *, cwd):
        script = "" if task.id == "b" else "open('shared.txt', 'a').write('a');"
        return ["python3", "-c", script + "print('{\"k\": \"ok\"}')"]

    monkeypatch.setattr(engines["codex"], "build_argv", build_argv)
    a, b = task("a"), task("b")
    a["verify"] = "test -s shared.txt"
    b["policy"] = "read-only"
    b.pop("verify")
    result = dispatch([a, b], parent_cwd=str(repo))
    assert result["tasks"][0]["commit"]
    assert not result["tasks"][1]["commit"]
    assert "overlaps" not in result
    assert next(e for e in events(result) if e["kind"] == "summary")["payload"]["overlaps"] == []


def test_bad_commit_is_ignored(repo, caplog):
    base = _git(["rev-parse", "HEAD"], repo)
    entries = [{"task_id": "a", "base_commit": base, "commit": "not-a-commit"},
               {"task_id": "b", "base_commit": base, "commit": "also-invalid"}]
    assert find_overlaps(repo, entries) == []
    assert "git diff failed" in caplog.text


@pytest.mark.parametrize("wait", [0, 10])
@pytest.mark.parametrize("same_file", [True, False])
def test_background_overlap_survives_log_loss(repo, monkeypatch, engines, wait, same_file):
    configure(monkeypatch)
    started, release = gate(engines["codex"], "a")
    if wait:
        release.set()
    original = engines["codex"].build_argv

    def argv(t, *, cwd):
        args = original(t, cwd=cwd)
        if not same_file:
            args[-1] = args[-1].replace("work.txt", f"{t.id}.txt")
        return args

    monkeypatch.setattr(engines["codex"], "build_argv", argv)
    calls = []
    original_find = dispatch_module.find_overlaps

    def find(repo, entries):
        calls.append(True)
        return original_find(repo, entries)

    monkeypatch.setattr(dispatch_module, "find_overlaps", find)
    tasks = [task("a"), task("b")]
    if not same_file:
        for t in tasks:
            t["verify"] = f"test -s {t['id']}.txt"
    try:
        result = dispatch_module.start_dispatch(tasks, parent_cwd=str(repo), wait=wait)
        assert started.wait(5)
        if not wait:
            assert result["state"] == "running"
    finally:
        release.set()
    status = control_status(result["run_id"], wait=10)
    expected = [{"tasks": ["a", "b"], "base_commit": _git(["rev-parse", "HEAD"], repo),
                 "files": ["work.txt"]}] if same_file else []
    assert status.get("overlaps", []) == expected
    if wait:
        assert result.get("overlaps", []) == expected
    assert calls == [True]
    # A process-owned result must not depend on the log remaining readable.
    from hermes_control.runlog import RunLog
    monkeypatch.setattr(RunLog, "read_all", lambda self: [])
    status = control_status(result["run_id"])
    assert status.get("overlaps", []) == expected
    assert ("overlaps" in status) == same_file


@pytest.mark.parametrize("shared", [True, False])
@pytest.mark.parametrize("background", [False, True])
def test_resume_summary_keeps_all_latest_tasks(repo, monkeypatch, engines, shared, background):
    from tests.scripted_engine import _reading
    from hermes_control.runlog import RunLog

    configure(monkeypatch, max_tasks=1)
    _reading(monkeypatch, "claude", blocked=True)
    engines["codex"].by_task["c"] = "limit"
    original = engines["codex"].build_argv

    def argv(t, *, cwd):
        args = original(t, cwd=cwd)
        if t.id == "c" and not shared:
            args[-1] = args[-1].replace("work.txt", "other.txt")
        return args

    monkeypatch.setattr(engines["codex"], "build_argv", argv)
    tasks = [task(name) for name in ("a", "b", "c")]
    tasks[-1]["verify"] = "test -s work.txt" if shared else "test -s other.txt"
    result = (dispatch_module.start_dispatch(tasks, parent_cwd=str(repo), wait=10)
              if background else dispatch(tasks, parent_cwd=str(repo)))
    pause_id = result["tasks"][-1]["paused"]["id"]
    _reading(monkeypatch, "codex", 5)
    # Pause again first: both resume branches must retain the earlier entries.
    engines["codex"].by_task["c"] = "limit"
    again = dispatch_module.resume_paused(pause_id)
    summaries = [e.payload for e in RunLog(result["run_id"]).read_all() if e.kind == "summary"]
    assert [e["task_id"] for e in summaries[-1]["tasks"]] == ["a", "b", "c"]
    assert summaries[-1]["overlaps"] == result["overlaps"]
    _reading(monkeypatch, "codex", 5)
    resumed = dispatch_module.resume_paused(again["paused"]["id"])
    summaries = [e.payload for e in RunLog(result["run_id"]).read_all() if e.kind == "summary"]
    assert [e["task_id"] for e in summaries[-1]["tasks"]] == ["a", "b", "c"]
    assert summaries[-1]["tasks"][-1]["commit"] == resumed["commit"]
    assert summaries[-1]["tasks"][-1]["paused_id"] is None
    assert control_status(result["run_id"])["overlaps"] == [{
        "tasks": ["a", "b", "c"] if shared else ["a", "b"],
        "base_commit": result["tasks"][0]["base_commit"], "files": ["work.txt"]}]


def test_find_overlaps_transitive_bases_and_missing_repo(monkeypatch, caplog):
    from types import SimpleNamespace
    import hermes_control.overlap as overlap

    calls = []
    paths = {"a": "x\0", "b": "x\0y\0", "c": "y\0"}

    def diff(args, *, cwd):
        calls.append(cwd)
        return SimpleNamespace(returncode=0, stdout=paths[args[4]], stderr="")

    monkeypatch.setattr(overlap, "_run_git", diff)
    entries = [{"task_id": name, "base_commit": "base", "commit": name} for name in paths]
    assert find_overlaps("repo", entries) == [
        {"tasks": ["a", "b", "c"], "base_commit": "base", "files": ["x", "y"]}]
    assert find_overlaps("repo", [{**e, "base_commit": e["task_id"]} for e in entries]) == []
    calls.clear()
    assert find_overlaps(None, entries) == []
    assert not calls
    assert "repo" in caplog.text.lower()
