"""Virtual time exercises silent workers without waiting for real deadlines."""
import io
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_control import dispatch as dispatch_module, review_pack, runs
from hermes_control.engines.base import ControlTask
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.events import ControlResult, RawEvent
from hermes_control.landing import Landing
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worktree import TaskWorktree


@pytest.fixture
def worker(monkeypatch, tmp_path):
    clock = SimpleNamespace(mono=0.0, wall=10000.0)
    process = SimpleNamespace(stdout=io.StringIO(), stderr=io.StringIO(), pid=123,
                              returncode=None)
    process.poll = lambda: process.returncode
    scheduled = []  # (monotonic arrival time, line); None means normal exit
    polls = []
    jumps = []  # (monotonic time, extra wall-clock seconds)

    def advance(seconds):
        clock.mono += seconds
        clock.wall += seconds
        while jumps and jumps[0][0] <= clock.mono:
            clock.wall += jumps.pop(0)[1]

    def get(*, timeout):
        polls.append(timeout)
        if scheduled and scheduled[0][0] <= clock.mono + timeout:
            at, line = scheduled.pop(0)
            advance(at - clock.mono)
            if line is None:
                process.returncode = 0
            return line
        advance(timeout)
        raise queue.Empty

    def stop(*args):
        process.returncode = -15 if process.returncode is None else process.returncode

    def wait(proc, timeout):
        if proc.returncode is None:
            advance(max(0, timeout))
        return proc.returncode

    stopped = Mock(side_effect=stop)
    monkeypatch.setattr(dispatch_module, "time", SimpleNamespace(
        time=lambda: clock.wall, monotonic=lambda: clock.mono))
    monkeypatch.setattr(dispatch_module, "queue", SimpleNamespace(
        Queue=lambda: SimpleNamespace(get=get), Empty=queue.Empty))
    monkeypatch.setattr(dispatch_module, "threading", SimpleNamespace(Thread=lambda **kw:
        SimpleNamespace(start=lambda: None, join=lambda **kw: None, is_alive=lambda: False)))
    monkeypatch.setattr(dispatch_module, "subprocess", SimpleNamespace(
        Popen=lambda *a, **kw: process, PIPE=-1, DEVNULL=-3,
        SubprocessError=dispatch_module.subprocess.SubprocessError))
    monkeypatch.setattr(dispatch_module, "engine_env", lambda: {})
    monkeypatch.setattr(dispatch_module, "stop_group", stopped)
    monkeypatch.setattr(dispatch_module, "wait_bounded", wait)
    monkeypatch.setattr(dispatch_module, "capture_worktree_diff", lambda wt: ("partial work", None))
    engine = SimpleNamespace(name="codex", build_argv=lambda *a, **kw: ["fake"],
        parse_line=lambda line: [] if line == "ignored" else [RawEvent("text", {"text": line})],
        extract_result=lambda task, raws, diff: ControlResult(
            task.id, "codex", True, "done", None, diff=diff), limit_hit=lambda raws: None)
    task = ControlTask("t1", "test", "codex", ExecPolicy(write=False), why="test")
    worktree = TaskWorktree(tmp_path, "branch", tmp_path, "base")
    log = RunLog("stall-test")

    def run(**kwargs):
        return dispatch_module.run_task(task, run_id=log.run_id, log=log,
            worktree=worktree, adapter=engine, **kwargs)

    return SimpleNamespace(run=run, clock=clock, scheduled=scheduled, jumps=jumps,
        polls=polls, stopped=stopped, task=task, worktree=worktree, log=log, engine=engine,
        process=process)


def test_silent_engine_stalls_and_preserves_work(worker):
    result = worker.run()
    assert not result.ok and result.failure_mode == "stall"
    assert result.unproven_reason == "stalled: no engine output for 1200 s"
    assert result.diff == "partial work" and worker.worktree.path.exists()
    assert worker.clock.mono == 1200
    assert max(worker.polls) <= 30
    worker.stopped.assert_called_once()


def test_chatty_engine_can_run_longer_than_stall_threshold(worker):
    worker.scheduled[:] = [(500, "one"), (1000, "two"), (1500, "three"), (1501, None)]
    result = worker.run()
    assert result.ok and result.failure_mode is None
    assert worker.clock.mono == 1501
    assert max(worker.polls) <= 30
    assert len(worker.log.read_all()) == 3


def test_forward_wall_step_logs_wake_without_shortening_silence(worker):
    worker.jumps[:] = [(30, 3600)]
    result = worker.run()
    assert not result.ok and result.failure_mode == "stall"
    assert worker.clock.mono == 1200
    assert result.unproven_reason == "stalled: no engine output for 1200 s after the machine woke"
    wakes = [e for e in worker.log.read_all() if e.raw_kind == "wake"]
    assert len(wakes) == 1
    assert wakes[0].kind == "wake" and wakes[0].task_id == "t1"
    assert wakes[0].payload["slept_seconds"] == 3600
    worker.stopped.assert_called_once()


def test_tool_silence_counts_only_awake_time_after_sleep(worker):
    worker.engine.parse_line = lambda line: [RawEvent("tool_start", {
        "tool_id": "test", "name": "Bash", "input": {"command": "sleep 600"}})]
    worker.scheduled[:] = [(30, "start test")]
    worker.jumps[:] = [(60, 90)]
    result = worker.run()
    assert result.failure_mode == "stall"
    assert worker.clock.mono == 1230
    assert result.unproven_reason == "stalled: no engine output for 1200 s after the machine woke"


def test_late_wake_extends_silence_deadline_by_full_grace(worker):
    worker.jumps[:] = [(1170, 3600)]
    result = worker.run()
    assert result.failure_mode == "stall"
    assert worker.clock.mono == 1470
    assert result.unproven_reason == "stalled: no engine output for 1470 s after the machine woke"


def test_backward_wall_step_does_not_delay_stall(worker):
    worker.jumps[:] = [(30, -3600)]
    result = worker.run()
    assert result.failure_mode == "stall"
    assert worker.clock.mono == 1200
    assert result.unproven_reason == "stalled: no engine output for 1200 s"
    assert not worker.log.read_all()


@pytest.mark.parametrize("adapter,line", [
    (CodexAdapter(), '{"type":"turn.started"}'),
    (CodexAdapter(), '{"type":"item.updated","item":{"type":"command_execution","id":"t"}}'),
    (ClaudeAdapter(), '{"type":"stream_event","event":{"type":"message_start"}}'),
])
def test_dropped_lines_count_as_output_and_clear_wake(worker, adapter, line):
    assert adapter.parse_line(line) == []
    worker.engine.parse_line = adapter.parse_line
    worker.jumps[:] = [(30, 3600)]
    worker.scheduled[:] = [(60, line), (600, line)]
    result = worker.run(timeout=2400)
    assert result.failure_mode == "stall"
    assert worker.clock.mono == 1800
    assert result.unproven_reason == "stalled: no engine output for 1200 s"


def test_wake_is_informational_in_run_view_and_trace(worker):
    worker.log.append_next("t1", [RawEvent("session", {"session_id": "s"})],
        engine="codex", now=worker.clock.wall)
    worker.jumps[:] = [(30, 3600)]
    worker.scheduled[:] = [(60, None)]
    assert worker.run().ok
    view = runs.run_view(worker.log.run_id, now=lambda: worker.clock.wall)
    attempt = view["tasks"][0]["attempts"][0]
    assert attempt["error"] is None
    assert not any(step["type"] == "error" for step in attempt["steps"])
    trace = runs.task_trace(worker.log.run_id, "t1")
    assert [entry["type"] for entry in trace] == ["session", "wake"]
    assert trace[-1]["data"]["slept_seconds"] == 3600


def test_engine_event_clears_wake_grace(worker):
    worker.jumps[:] = [(30, 3600)]
    worker.scheduled[:] = [(60, "awake"), (600, "working"), (1500, None)]
    result = worker.run()
    assert result.ok and result.failure_mode is None
    assert worker.clock.mono == 1500


def test_timeout_keeps_its_classification(worker):
    result = worker.run(timeout=90)
    assert not result.ok and result.failure_mode == "timeout"
    assert "timed out" in result.unproven_reason
    assert worker.clock.mono == 90
    worker.stopped.assert_called_once()


@pytest.mark.parametrize("failure", ["start-failure", "engine-exit", "adapter-failure"])
def test_other_engine_failures_are_classified(worker, monkeypatch, failure):
    if failure == "start-failure":
        monkeypatch.setattr(dispatch_module.subprocess, "Popen", Mock(side_effect=OSError("broken")))
    elif failure == "engine-exit":
        worker.process.returncode = 1
    else:
        worker.engine.extract_result = lambda task, raws, diff: ControlResult(
            task.id, "codex", False, "failed", None)
    if failure != "start-failure":
        # EOF without overwriting the requested exit code.
        monkeypatch.setattr(dispatch_module.queue, "Queue", lambda: SimpleNamespace(get=lambda **kw: None))
        if failure == "adapter-failure":
            worker.process.returncode = 0
    result = worker.run()
    assert not result.ok
    assert result.failure_mode == ("engine-exit" if failure == "adapter-failure" else failure)


def test_failure_mode_reaches_summary_and_review_pack(worker):
    worker.log.append_next("", [RawEvent("plan", {"repo": None, "tasks": [
        {"id": "t1", "engine": "codex", "why": "test", "policy": "read-only"}]})],
        engine="", now=worker.clock.wall)
    result = worker.run()
    entry = dispatch_module._done_entry(worker.task, result, worker.worktree,
        Landing(None, None, result.unproven_reason), None)
    dispatch_module._log_summary_unlocked(worker.log, worker.log.run_id, [entry], None)
    assert entry["failure_mode"] == "stall"
    assert worker.log.read_all()[-1].payload["tasks"][0]["failure_mode"] == "stall"
    pack = review_pack.build(worker.log.run_id)
    assert pack["tasks"][0]["failure_mode"] == "stall"
    assert "failure_mode: stall" in review_pack.format_text(pack)


@pytest.mark.parametrize("key,minimum,default", [
    ("stall_seconds", 660, 1200), ("wake_grace_seconds", 60, 300)])
@pytest.mark.parametrize("invalid", [True, "900", 0, None, float("nan"), float("inf")])
def test_invalid_settings_warn_and_fall_back(monkeypatch, caplog, key, minimum, default, invalid):
    from hermes_control import stall

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {key: invalid}}}}})
    assert stall.read_settings()[key] == default
    assert key in caplog.text and "default" in caplog.text
    assert stall.parse_settings({key: minimum})[key] == minimum
    assert stall.parse_settings({key: minimum - 1})[key] == default


@pytest.fixture
def prepared_worker(worker, monkeypatch):
    monkeypatch.setattr(dispatch_module, "engine_status", lambda _: {"codex": None})
    reads = Mock(return_value={"plugins": {"entries": {"hermes_control": {"settings": {
        "stall_seconds": 660, "wake_grace_seconds": 60}}}}})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", reads)
    # Isolate the other independent settings readers.
    monkeypatch.setattr(dispatch_module.model_policy, "load_policy", lambda: {})
    monkeypatch.setattr(dispatch_module.model_policy, "apply_policy", lambda task, policy: task)
    monkeypatch.setattr(dispatch_module, "_caps", lambda: {})
    monkeypatch.setattr(dispatch_module, "_planner", lambda _: {})
    monkeypatch.setattr(dispatch_module, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {"codex": 1}}, None))
    _, ctx = dispatch_module._prepare([
        dict(id="t1", engine="codex", goal="test", why="test", policy="read-only")],
        run_id=worker.log.run_id, log=worker.log, parent_cwd=None, timeout=None,
        planner_session=None)
    return ctx, reads


def test_plan_records_stall_settings(worker, prepared_worker):
    plan = next(event for event in worker.log.read_all() if event.kind == "plan")
    assert plan.payload["stall"] == {"stall_seconds": 660, "wake_grace_seconds": 60}


def test_dispatch_reads_stall_settings_once_and_passes_to_workers(worker, monkeypatch,
                                                                prepared_worker):
    ctx, reads = prepared_worker
    from contextlib import nullcontext
    monkeypatch.setattr("hermes_control.slots.engine_slot", lambda *a, **kw: nullcontext())
    monkeypatch.setattr(dispatch_module, "get_adapter", lambda _: worker.engine)
    for _ in range(2):
        result = dispatch_module._run_guarded(ctx, worker.task, worker.worktree,
            sandbox=lambda: None, continued=False)
        assert result.failure_mode == "stall"
        assert result.unproven_reason == "stalled: no engine output for 660 s"
    reads.assert_called_once()
