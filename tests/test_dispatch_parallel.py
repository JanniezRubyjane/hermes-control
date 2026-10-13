"""Event gates prove scheduling independently of machine/subprocess speed."""
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hermes_control.dispatch as dispatch_module
from hermes_control.dispatch import dispatch
from hermes_control.runlog import RunLog
from hermes_control.verify import VerifyOutcome
from tests.scripted_engine import ScriptedEngine, _engines, _git, _reading, repo  # noqa: F401


def task(name, engine="codex", depends_on=()):
    return dict(id=name, goal="write work", why="test", engine=engine,
                policy="write", verify="test -s work.txt", depends_on=list(depends_on))


def configure(monkeypatch, max_tasks=2, codex=2, claude=1):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {"parallel": {
            "max_tasks": max_tasks, "per_engine": {"codex": codex, "claude": claude}}}}}}})


@pytest.fixture
def engines(monkeypatch):
    engines = _engines(monkeypatch, ScriptedEngine("claude", *(["ok"] * 12)),
                       ScriptedEngine("codex", *(["ok"] * 12)))

    def verify(command, cwd, *, codex_path):
        result = subprocess.run(["/bin/sh", "-c", command], cwd=cwd, capture_output=True,
                                text=True)
        return VerifyOutcome(command, result.returncode == 0, result.returncode,
                             result.stdout + result.stderr, None)

    monkeypatch.setattr("hermes_control.landing.run_verify", verify)
    return engines


def gate(engine, name):
    started, release = threading.Event(), threading.Event()
    engine.block(name, started=started, release=release)
    return started, release


def events(summary):
    return [json.loads(line) for line in Path(summary["run_log"]).read_text().splitlines()]


def test_plan_records_parallel_settings(repo, monkeypatch, engines):
    configure(monkeypatch, max_tasks=2, codex=2, claude=1)
    summary = dispatch([task("a")], parent_cwd=str(repo))
    plan = next(event["payload"] for event in events(summary) if event["kind"] == "plan")
    assert plan["parallel"] == {
        "max_tasks": 2, "per_engine": {"claude": 1, "codex": 2}, "error": None}


def test_max_tasks_alone_allows_same_engine_overlap(repo, monkeypatch, engines):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {
            "parallel": {"max_tasks": 2}}}}}})
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b")], parent_cwd=str(repo))
        try:
            assert a.wait(5) and b.wait(5)
        finally:
            release_a.set()
            release_b.set()
        summary = future.result(timeout=10)
    plan = next(event["payload"] for event in events(summary) if event["kind"] == "plan")
    assert plan["parallel"] == {
        "max_tasks": 2, "per_engine": {"claude": 2, "codex": 2}, "error": None}


def test_plan_records_invalid_parallel_fallback(repo, monkeypatch, engines):
    configure(monkeypatch, max_tasks=9)
    summary = dispatch([task("a")], parent_cwd=str(repo))
    plan = next(event["payload"] for event in events(summary) if event["kind"] == "plan")
    assert plan["parallel"] == {
        "max_tasks": 1, "per_engine": {"claude": 1, "codex": 1},
        "error": "ValueError: parallel.max_tasks must be an integer between 1 and 8"}


def test_exception_in_failure_handler_preserves_results_and_summary(repo, monkeypatch, engines):
    configure(monkeypatch)
    original_result = engines["codex"].extract_result
    original_failed = dispatch_module._failed_entry

    def result(t, raws, diff):
        if t.id == "a":
            raise RuntimeError("broken task")
        return original_result(t, raws, diff)

    def failed(t, reason, **kwargs):
        if t.id == "a" and kwargs.get("worktree") is not None:
            raise RuntimeError("broken failure handler")
        return original_failed(t, reason, **kwargs)

    monkeypatch.setattr(engines["codex"], "extract_result", result)
    monkeypatch.setattr(dispatch_module, "_failed_entry", failed)
    summary = dispatch([task("a"), task("b"), task("c", depends_on=["a"]), task("d")],
                       parent_cwd=str(repo))

    by_id = {entry["task_id"]: entry for entry in summary["tasks"]}
    assert set(by_id) == {"a", "b", "c", "d"}
    assert by_id["a"]["unproven_reason"] == "RuntimeError: broken failure handler"
    assert not by_id["a"]["ok"]
    assert by_id["c"]["unproven_reason"] == "skipped: depends on a, which failed"
    assert all(by_id[name]["ok"] and by_id[name]["commit"] for name in ("b", "d"))
    summaries = [e for e in events(summary) if e["kind"] == "summary"]
    assert len(summaries) == 1
    assert [e["task_id"] for e in summaries[0]["payload"]["tasks"]] == [
        e["task_id"] for e in summary["tasks"]]


def test_slow_usage_check_does_not_block_worker_finally(repo, monkeypatch, engines):
    configure(monkeypatch)
    started, release_task = gate(engines["codex"], "a")
    checking, release_check, finished = (threading.Event() for _ in range(3))

    class ObservedPool(ThreadPoolExecutor):
        def submit(self, fn, index, item, admitted):
            future = super().submit(fn, index, item, admitted)
            if item[0].id == "a":
                future.add_done_callback(lambda _: finished.set())
            return future

    def hold(engine, **kwargs):
        if engine == "claude":
            checking.set()
            assert release_check.wait(10)
        return None

    monkeypatch.setattr(dispatch_module, "ThreadPoolExecutor", ObservedPool)
    monkeypatch.setattr(dispatch_module.limits, "engine_hold", hold)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b", "claude")],
                                 parent_cwd=str(repo))
        try:
            assert started.wait(5) and checking.wait(5)
            release_task.set()
            # A completed future proves run_one's finally released its slot and
            # settled its result while the other admission was still checking usage.
            assert finished.wait(5)
        finally:
            release_task.set()
            release_check.set()
        assert all(e["ok"] and e["commit"] for e in future.result(timeout=10)["tasks"])


@pytest.mark.parametrize("error", [ValueError, RuntimeError, OSError])
def test_parallel_settings_fallback_precedes_plan(repo, monkeypatch, engines, caplog, error):
    reads = []
    original_append = RunLog.append

    def read():
        reads.append(True)
        raise error("settings unavailable" + "x" * 350)

    def append(log, batch):
        if any(e.kind == "plan" for e in batch):
            assert reads == [True]
        original_append(log, batch)

    monkeypatch.setattr(dispatch_module.parallel, "read_parallel", read)
    monkeypatch.setattr(RunLog, "append", append)
    summary = dispatch([task("a"), task("b")], parent_cwd=str(repo))
    assert reads == [True]
    plan = next(e["payload"] for e in events(summary) if e["kind"] == "plan")
    assert plan["parallel"]["error"] == (f"{error.__name__}: settings unavailable" + "x" * 350)[:300]
    assert "parallel settings are invalid" in caplog.text
    assert all(e["ok"] and e["commit"] for e in summary["tasks"])
    assert [(e["task_id"], e["kind"]) for e in events(summary)] == [
        ("", "plan"), ("a", "result"), ("a", "commit"), ("a", "verify"),
        ("b", "result"), ("b", "commit"), ("b", "verify"),
        ("", "summary"), ("", "planner"), ("", "planner")]


@pytest.mark.parametrize("setting,second_engine,overlap", [
    (None, "codex", False), (1, "codex", False),
    (2, "codex", True), (1, "claude", True),
])
def test_slots_and_default_event_order(repo, monkeypatch, engines, setting, second_engine, overlap):
    if setting is not None:
        configure(monkeypatch, codex=setting)
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines[second_engine], "b")
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b", second_engine)],
                                 parent_cwd=str(repo), run_id="slots")
        try:
            assert a.wait(5)
            if overlap:
                assert b.wait(5)
            else:
                assert not b.wait(0.15)
            release_a.set()
            assert b.wait(5)
        finally:
            release_a.set()
            release_b.set()
        summary = future.result(timeout=10)
    assert [e["task_id"] for e in summary["tasks"]] == ["a", "b"]
    assert all(e["commit"] and e["verify"]["passed"] for e in summary["tasks"])
    if not overlap:
        assert [(e["task_id"], e["kind"]) for e in events(summary)] == [
            ("", "plan"), ("a", "result"), ("a", "commit"), ("a", "verify"),
            ("b", "result"), ("b", "commit"), ("b", "verify"),
            ("", "summary"), ("", "planner"), ("", "planner")]


def test_parallel_entries_equal_serial_even_when_second_finishes_first(repo, monkeypatch, engines):
    # Commit timestamps are the only nondeterministic part of these identical git objects.
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00Z")
    tasks = [task("a"), task("b")]
    serial = dispatch(tasks, parent_cwd=str(repo), run_id="compare")
    for entry in serial["tasks"]:
        _git(["worktree", "remove", "--force", entry["worktree"]], repo)
        _git(["branch", "-D", entry["branch"]], repo)
    Path(serial["run_log"]).unlink()
    configure(monkeypatch)
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    landed_b = threading.Event()
    original = RunLog.append

    def append(log, batch):
        original(log, batch)
        if any(e.task_id == "b" and e.kind == "verify" for e in batch):
            landed_b.set()

    monkeypatch.setattr(RunLog, "append", append)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, tasks, parent_cwd=str(repo), run_id="compare")
        try:
            assert a.wait(5) and b.wait(5)
            release_b.set()
            assert landed_b.wait(5)
        finally:
            release_a.set()
            release_b.set()
        parallel = future.result(timeout=10)
    assert parallel["tasks"] == serial["tasks"]
    rows = events(parallel)
    assert next(i for i, e in enumerate(rows) if e["kind"] == "commit" and e["task_id"] == "b") < next(
        i for i, e in enumerate(rows) if e["kind"] == "commit" and e["task_id"] == "a")
    for name in ["", "a", "b"]:
        seq = [e["seq"] for e in rows if e["task_id"] == name]
        assert seq == list(range(len(seq)))


@pytest.mark.parametrize("outcome", ["done", "failed", "paused", "raise"])
def test_dependencies_settle_and_siblings_survive(repo, monkeypatch, engines, outcome):
    configure(monkeypatch)
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    release_b.set()
    original = engines["codex"].extract_result

    def result(t, raws, diff):
        if t.id == "a":
            if outcome == "raise":
                raise RuntimeError("broken task")
            if outcome == "failed":
                from dataclasses import replace
                return replace(original(t, raws, diff), ok=False, unproven_reason="failed")
        return original(t, raws, diff)

    monkeypatch.setattr(engines["codex"], "extract_result", result)
    if outcome == "paused":
        engines["codex"].by_task["a"] = "limit"
        _reading(monkeypatch, "claude", blocked=True)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b", depends_on=["a"]), task("c")],
                                 parent_cwd=str(repo), run_id="deps")
        try:
            assert a.wait(5)
            assert not b.wait(0.15)
        finally:
            release_a.set()
        summary = future.result(timeout=10)
    by_id = {entry["task_id"]: entry for entry in summary["tasks"]}
    dependent, sibling = by_id["b"], by_id["c"]
    assert sibling["ok"] and sibling["commit"]
    if outcome == "done":
        assert b.is_set() and dependent["verify"]["passed"]
    elif outcome == "paused":
        assert not b.is_set()
        assert dependent["paused"]["reason"] == "waits for paused task a"
    else:
        assert not b.is_set()
        assert dependent["unproven_reason"] == "skipped: depends on a, which failed"
    summary_events = [e for e in events(summary) if e["kind"] == "summary"]
    assert len(summary_events) == 1
    assert [e["task_id"] for e in summary_events[0]["payload"]["tasks"]] == ["a", "c", "b"]


def test_cap_move_waits_for_destination_slot(repo, monkeypatch, engines):
    configure(monkeypatch, codex=1)
    _reading(monkeypatch, "claude", blocked=True)
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    release_b.set()
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b", "claude")],
                                 parent_cwd=str(repo), run_id="move")
        try:
            assert a.wait(5)
            assert not b.wait(0.15)
        finally:
            release_a.set()
        summary = future.result(timeout=10)
    assert all(e["engine"] == "codex" and e["commit"] for e in summary["tasks"])
    assert summary["tasks"][1]["rerouted"]


def test_total_slots_and_ready_tasks_bypass_a_busy_engine(repo, monkeypatch, engines):
    a, release_a = gate(engines["codex"], "a")
    b, release_b = gate(engines["codex"], "b")
    c, release_c = gate(engines["claude"], "c")
    d, release_d = gate(engines["claude"], "d")
    # Two claude slots make total capacity (rather than its engine cap) block d.
    configure(monkeypatch, codex=1, claude=2)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b"), task("c", "claude"),
                                            task("d", "claude")], parent_cwd=str(repo))
        try:
            assert a.wait(5) and c.wait(5)
            assert not b.is_set() and not d.wait(0.15)
            release_c.set()
            assert d.wait(5)
            assert not b.is_set()
            release_a.set()
            assert b.wait(5)
        finally:
            for event in [release_a, release_b, release_c, release_d]:
                event.set()
        assert all(e["commit"] for e in future.result(timeout=10)["tasks"])


def test_verify_commands_overlap_and_dependency_waits_for_verdict(repo, monkeypatch, engines):
    configure(monkeypatch)
    checking = threading.Barrier(2)
    release = threading.Event()
    both_checking = threading.Event()
    b, release_b = gate(engines["codex"], "b")
    release_b.set()

    def verify(command, cwd, *, codex_path):
        if "-b.verify-" not in str(cwd):
            checking.wait(timeout=5)
            both_checking.set()
            assert release.wait(5)
        return VerifyOutcome(command, True, 0, "", None)

    monkeypatch.setattr("hermes_control.landing.run_verify", verify)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(dispatch, [task("a"), task("b", depends_on=["a"]), task("c")],
                                 parent_cwd=str(repo))
        try:
            assert both_checking.wait(5)
            assert not b.is_set()
        finally:
            release.set()
        assert all(e["verify"]["passed"] for e in future.result(timeout=10)["tasks"])
