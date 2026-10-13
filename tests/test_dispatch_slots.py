"""Kept apart from test_dispatch_parallel.py so parallel branches merge cleanly."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_control.dispatch as dispatch_module  # noqa: F401
from hermes_control.dispatch import control_status, dispatch  # noqa: F401
from tests.scripted_engine import _engines, _git, repo  # noqa: F401
from tests.test_dispatch_parallel import (  # noqa: F401
    configure, engines, events, gate, task)


@pytest.mark.parametrize("max_tasks", [1, 2])
def test_concurrent_dispatches_share_engine_slots(repo, monkeypatch, engines, max_tasks):
    pytest.importorskip("fcntl")
    configure(monkeypatch, max_tasks=max_tasks, codex=1)
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    entering_b = threading.Event()
    original = dispatch_module._run_guarded
    original_run_task = dispatch_module.run_task
    active = set()
    active_lock = threading.Lock()
    overlaps = []

    def run_task(t, **kwargs):
        with active_lock:
            if active:
                overlaps.append(t.id)
            active.add(t.id)
        try:
            return original_run_task(t, **kwargs)
        finally:
            with active_lock:
                active.remove(t.id)

    def guarded(ctx, t, worktree, **kwargs):
        if t.id == "b":
            entering_b.set()
        return original(ctx, t, worktree, **kwargs)

    monkeypatch.setattr(dispatch_module, "_run_guarded", guarded)
    monkeypatch.setattr(dispatch_module, "run_task", run_task)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(dispatch, [task("a")], parent_cwd=str(repo), run_id="first")
        second = None
        try:
            assert a.wait(5)
            second = executor.submit(dispatch, [task("b")], parent_cwd=str(repo),
                                     run_id="second")
            assert entering_b.wait(5)
            assert not b.wait(0.6)
            assert not b.wait(0.6)
            from hermes_control.runlog import RunLog
            waiting = [e.payload for e in RunLog("second").read_all() if e.kind == "slot_wait"]
            assert waiting == [{"engine": "codex", "state": "waiting"}]
            release_a.set()
            assert b.wait(5)
        finally:
            release_a.set()
            release_b.set()
        summaries = [first.result(timeout=10), second.result(timeout=10)]
    assert all(s["tasks"][0]["ok"] and s["tasks"][0]["commit"] for s in summaries)
    assert overlaps == []
    assert not [e for e in events(summaries[0]) if e["kind"] == "slot_wait"]
    waited = [e for e in events(summaries[1]) if e["kind"] == "slot_wait"]
    assert len(waited) == 2
    assert waited[0]["payload"] == {"engine": "codex", "state": "waiting"}
    event = waited[1]
    assert event["task_id"] == "b" and event["engine"] == "codex"
    seconds = event["payload"]["seconds"]
    assert isinstance(seconds, float) and seconds >= 1.0
    assert event["payload"] == {"engine": "codex", "state": "acquired", "seconds": round(seconds, 1)}


def test_resume_uses_configured_engine_slots(repo, monkeypatch, engines):
    from hermes_control.slots import engine_slot
    from tests.scripted_engine import _reading

    pytest.importorskip("fcntl")
    configure(monkeypatch, codex=2)
    _reading(monkeypatch, "claude", blocked=True)
    engines["codex"].by_task["a"] = "limit"
    paused = dispatch([task("a")], parent_cwd=str(repo))["tasks"][0]["paused"]["id"]
    _reading(monkeypatch, "codex", 5)
    started, release_task = gate(engines["codex"], "a")
    held, release_slot = threading.Event(), threading.Event()

    def holder():
        with engine_slot("codex", 1):
            held.set()
            assert release_slot.wait(10)

    with ThreadPoolExecutor(2) as executor:
        holding = executor.submit(holder)
        try:
            assert held.wait(5)
            resumed = executor.submit(dispatch_module.resume_paused, paused)
            assert started.wait(3), "resumed task ignored the available second slot"
        finally:
            release_slot.set()
            release_task.set()
        holding.result(timeout=5)
        assert resumed.result(timeout=10)["commit"]
