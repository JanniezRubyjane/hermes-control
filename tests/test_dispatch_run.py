import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import threading
import time

import pytest

from hermes_control.dispatch import dispatch, run_task
from hermes_control import dispatch as dispatch_module
from hermes_control import limits
from hermes_control.engines.base import ControlTask, EngineAdapter, EngineInfo
from hermes_control.events import ControlResult, RawEvent
from hermes_control.landing import Landing
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worktree import create_task_worktree
from hermes_control.worktree import VerifyCheckoutError
from hermes_control.verify import check_script_path
from hermes_control.verify import VerifyOutcome


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """dispatch() builds a RunLog with no directory, which resolves under the
    real HERMES_HOME the instant it is constructed (mkdir happens on path
    resolution, before any event is ever appended). Every test in this file
    must never be allowed to touch the operator's real ~/.hermes."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


class FakeAdapter(EngineAdapter):
    """Echoes JSON lines from a real subprocess, so the streaming path is exercised."""

    name = "fake"

    def __init__(self, lines=None, exit_code=0):
        self.lines = lines if lines is not None else ['{"k": "text"}', '{"k": "result"}']
        self.exit_code = exit_code

    def probe(self):
        return EngineInfo(name="fake", version="1", path="/bin/true")

    def auth_problem(self, info):
        return None

    def build_argv(self, task, *, cwd):
        script = "".join(f"print({line!r});" for line in self.lines)
        script += f"raise SystemExit({self.exit_code})"
        return ["python3", "-c", script]

    def parse_line(self, line):
        kind = json.loads(line)["k"]
        payload = {"text": "hi"} if kind == "text" else {"ok": True}
        return [RawEvent(kind=kind, payload=payload)]

    def extract_result(self, task, raws, diff):
        return ControlResult(task_id=task.id, engine=self.name,
                             ok=any(r.kind == "result" for r in raws),
                             text="hi", session_id=None, diff=diff,
                             unproven_reason=None if diff else "empty diff")


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(["add", "a.txt"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


def _task(task_id="t1"):
    return ControlTask(id=task_id, goal="g", engine="fake", policy=ExecPolicy(write=True))


def test_run_task_streams_events_into_the_run_log(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    run_task(_task(), run_id="r1", log=log, worktree=worktree, adapter=FakeAdapter())
    assert [e.kind for e in log.read_all()] == ["text", "result"]


def test_summary_records_result_text_and_worktree_base(repo, tmp_path):
    log = RunLog("summary-evidence", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="summary-evidence")
    result = ControlResult(task_id="t1", engine="fake", ok=True,
                           text="actual final answer", session_id=None, diff="")
    landing = Landing(commit="abc", verify=None, unproven_reason=None)
    done = dispatch_module._done_entry(_task(), result, worktree, landing, None)
    failed = dispatch_module._failed_entry(_task("failed"), "error")
    dispatch_module._log_summary(log, "summary-evidence", [done, failed], str(repo))
    entries = log.read_all()[0].payload["tasks"]
    assert entries[0]["text"] == "actual final answer"
    assert entries[0]["base_commit"] == worktree.base_commit
    assert entries[1]["text"] == ""
    assert entries[1]["base_commit"] == ""


def test_run_task_stamps_monotonic_sequence_numbers(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    run_task(_task(), run_id="r1", log=log, worktree=worktree, adapter=FakeAdapter())
    assert [e.seq for e in log.read_all()] == [0, 1]


def test_claude_reporting_failure_does_not_fail_completed_task(repo, tmp_path, monkeypatch, caplog):
    class ReportingClaude(FakeAdapter):
        name = "claude"

        def parse_line(self, line):
            if json.loads(line)["k"] == "usage":
                return [RawEvent(kind="usage", payload={"rate_limit_info": {"status": "allowed"}})]
            return super().parse_line(line)

    def broken_report(*_args, **_kwargs):
        raise RuntimeError("report failed")

    monkeypatch.setattr(limits, "record_claude_report", broken_report)
    log = RunLog("r-report", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r-report")

    result = run_task(_task(), run_id="r-report", log=log, worktree=worktree,
                      adapter=ReportingClaude(lines=['{"k": "usage"}', '{"k": "result"}']))

    assert result.ok is True
    assert "report failed" in caplog.text


def test_run_task_runs_the_engine_inside_the_worktree(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    adapter = FakeAdapter(lines=['{"k": "result"}'])
    run_task(_task(), run_id="r1", log=log, worktree=worktree, adapter=adapter)
    assert worktree.path.is_dir()


def test_claude_run_receives_a_live_preapproved_check_and_cleans_it_up(repo, tmp_path):
    log = RunLog("r-check", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r-check")
    script = check_script_path("r-check", "t1")

    class CheckingClaude(FakeAdapter):
        name = "claude"

        def build_argv(self, task, *, cwd):
            self.received = task
            code = ("from pathlib import Path; "
                    f"assert Path({str(script)!r}).is_file(); "
                    "print('{\"k\": \"result\"}')")
            return ["python3", "-c", code]

    adapter = CheckingClaude()
    task = dataclasses.replace(_task(), verify="python3 -m pytest")
    result = run_task(task, run_id="r-check", log=log, worktree=worktree,
                      adapter=adapter, sandbox=lambda: "/fake/codex")

    assert result.ok is True
    assert f"Bash({script})" in adapter.received.policy.allow_tools
    assert str(script) in adapter.received.goal
    assert "pre-approved" in adapter.received.goal
    assert not script.exists()


@pytest.mark.parametrize("engine,verify,sandbox", [
    ("claude", None, lambda: "/fake/codex"),
    ("claude", "exit 0", lambda: None),
    ("codex", "exit 0", lambda: "/fake/codex"),
])
def test_only_claude_with_verify_and_sandbox_gets_own_check(
        repo, tmp_path, engine, verify, sandbox):
    log = RunLog("r-no-check", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r-no-check")

    class RecordingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            self.received = task
            return super().build_argv(task, cwd=cwd)

    adapter = RecordingAdapter()
    adapter.name = engine
    task = dataclasses.replace(_task(), verify=verify)
    run_task(task, run_id="r-no-check", log=log, worktree=worktree,
             adapter=adapter, sandbox=sandbox)

    script = check_script_path("r-no-check", "t1")
    assert f"Bash({script})" not in adapter.received.policy.allow_tools
    assert str(script) not in adapter.received.goal
    assert not script.exists()


def test_run_task_reports_a_nonzero_exit_as_not_ok(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=FakeAdapter(lines=['{"k": "text"}'], exit_code=3))
    assert result.ok is False
    assert result.unproven_reason


def test_run_task_marks_an_untouched_worktree_unproven(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=FakeAdapter())
    assert result.unproven_reason == "empty diff"


def test_run_task_reports_a_diff_when_the_worker_edited_a_file(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class EditingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = ("import pathlib;"
                      "pathlib.Path('a.txt').write_text('two\\n', encoding='utf-8');"
                      "print('{\"k\": \"result\"}')")
            return ["python3", "-c", script]

    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=EditingAdapter())
    assert "a.txt" in result.diff
    assert result.unproven_reason is None


def test_run_task_reports_a_failed_spawn_without_raising(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class MissingBinaryAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            return ["definitely-not-a-real-binary-xyz"]

    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=MissingBinaryAdapter())
    assert result.ok is False
    assert result.unproven_reason == "engine failed to start"


def test_run_task_drains_oversized_stderr_without_deadlocking(repo, tmp_path):
    """A child that fills the OS pipe buffer on stderr (default ~64KB) while
    run_task only reads stdout would block forever on that write with nothing
    draining it. run_task must still complete, and the captured tail must reach
    the failing result. Runs the call on a watchdog thread so a regression here
    fails this test quickly instead of hanging the whole suite."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class NoisyFailingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            # Padding first, marker last: run_task keeps only the *tail* of
            # stderr, so the marker must survive truncation at the end.
            script = ("import sys;"
                      "sys.stderr.write('x' * 300000 + 'boom-marker-end');"
                      "raise SystemExit(1)")
            return ["python3", "-c", script]

    results: list = []
    watchdog = threading.Thread(
        target=lambda: results.append(
            run_task(_task(), run_id="r1", log=log, worktree=worktree,
                     adapter=NoisyFailingAdapter())),
        daemon=True)
    watchdog.start()
    watchdog.join(timeout=15)
    assert not watchdog.is_alive(), "run_task deadlocked draining oversized stderr"
    result = results[0]
    assert result.ok is False
    assert "boom-marker-end" in result.unproven_reason


def _run_task_bounded(task, *, run_id, log, worktree, adapter, timeout, join_timeout=20):
    """Run run_task on its own watchdog thread so a deadlock regression fails
    this test in `join_timeout` seconds instead of hanging the whole suite.
    Returns the ControlResult; asserts the watchdog actually finished."""
    results: list = []
    watchdog = threading.Thread(
        target=lambda: results.append(
            run_task(task, run_id=run_id, log=log, worktree=worktree,
                     adapter=adapter, timeout=timeout)),
        daemon=True)
    watchdog.start()
    watchdog.join(timeout=join_timeout)
    assert not watchdog.is_alive(), "run_task did not return within the watchdog window"
    return results[0]


def _assert_process_gone(pid_file):
    assert pid_file.is_file()
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.live_system_guard_bypass  # exercises real process-group termination
def test_normal_engine_exit_stops_background_child(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "background", run_id="r1")

    class BackgroundAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            child = ("import os, time; "
                     "open('child.pid', 'w').write(str(os.getpid())); "
                     "time.sleep(30)")
            script = ("import os, subprocess, sys, time; "
                      "open('engine.pid', 'w').write(str(os.getpid())); "
                      f"subprocess.Popen([sys.executable, '-c', {child!r}], "
                      "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
                      "\nwhile not os.path.exists('child.pid'): time.sleep(0.01)\n"
                      "print('{\"k\": \"result\"}')")
            return ["python3", "-u", "-c", script]

    try:
        result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                          adapter=BackgroundAdapter(), timeout=5)
        assert result.ok
        child_pid = int((worktree.path / "child.pid").read_text())
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("background child survived normal engine exit")
    finally:
        if os.name != "nt" and (worktree.path / "engine.pid").exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int((worktree.path / "engine.pid").read_text()), signal.SIGKILL)


@pytest.mark.live_system_guard_bypass  # exercises a real SIGTERM-ignoring descendant
def test_normal_engine_exit_kills_background_child_ignoring_sigterm(
        repo, tmp_path, monkeypatch):
    if os.name == "nt":
        pytest.skip("POSIX process groups are required")
    monkeypatch.setattr("hermes_control.dispatch._TERMINATE_GRACE_SECONDS", 0.2)
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "ignores-term", run_id="r1")

    class BackgroundAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            child = (
                "import os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "open('child.pid', 'w').write(str(os.getpid()))\n"
                "time.sleep(30)\n"
            )
            script = (
                "import os, subprocess, sys, time\n"
                "open('engine.pid', 'w').write(str(os.getpid()))\n"
                f"subprocess.Popen([sys.executable, '-c', {child!r}], "
                "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "while not os.path.exists('child.pid'): time.sleep(0.01)\n"
                "print('{\"k\": \"result\"}')\n"
            )
            return ["python3", "-u", "-c", script]

    try:
        result = _run_task_bounded(
            _task(), run_id="r1", log=log, worktree=worktree,
            adapter=BackgroundAdapter(), timeout=5, join_timeout=8)
        assert result.ok
        _assert_process_gone(worktree.path / "child.pid")
    finally:
        engine_pid_file = worktree.path / "engine.pid"
        if engine_pid_file.exists():
            with contextlib.suppress(ProcessLookupError):
                os.killpg(int(engine_pid_file.read_text()), signal.SIGKILL)


@pytest.mark.live_system_guard_bypass  # run_task genuinely SIGTERMs/SIGKILLs its own child
def test_run_task_times_out_a_chatty_child_and_leaves_no_process_running(repo, tmp_path):
    """The core safety mechanism this plugin exists to provide: a child that
    never stops producing stdout must still be killed once its timeout
    elapses, worktree preserved, no process left behind."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class ChattyForeverAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = (
                "import os, sys\n"
                "with open('pid.txt', 'w', encoding='utf-8') as f:\n"
                "    f.write(str(os.getpid()))\n"
                "while True:\n"
                "    print('{\"k\": \"text\"}')\n"
                "    sys.stdout.flush()\n"
            )
            return ["python3", "-u", "-c", script]

    result = _run_task_bounded(_task(), run_id="r1", log=log, worktree=worktree,
                               adapter=ChattyForeverAdapter(), timeout=1)
    assert result.ok is False
    assert "timed out" in result.unproven_reason
    assert worktree.path.is_dir()
    _assert_process_gone(worktree.path / "pid.txt")


@pytest.mark.live_system_guard_bypass  # run_task genuinely SIGTERMs/SIGKILLs its own child
def test_run_task_times_out_a_silent_child(repo, tmp_path):
    """The likelier failure mode in practice: an engine that hangs without ever
    writing anything to stdout. Before the fix, the deadline check lived inside
    `for raw_line in process.stdout:`, which blocks on readline() with no
    timeout of its own - a child that emits nothing would never let that check
    run at all, and run_task would hang forever. The watchdog below is what
    keeps that regression a fast test failure instead of a hung suite."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class SilentForeverAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = (
                "import os, time\n"
                "with open('pid.txt', 'w', encoding='utf-8') as f:\n"
                "    f.write(str(os.getpid()))\n"
                "time.sleep(3600)\n"
            )
            return ["python3", "-u", "-c", script]

    result = _run_task_bounded(_task(), run_id="r1", log=log, worktree=worktree,
                               adapter=SilentForeverAdapter(), timeout=1)
    assert result.ok is False
    assert "timed out" in result.unproven_reason
    assert worktree.path.is_dir()
    _assert_process_gone(worktree.path / "pid.txt")


def test_dispatch_returns_one_entry_per_task_in_dependency_order(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "b", "goal": "2", "engine": "claude", "depends_on": ["a"],
                     "why": "x"},
                    {"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="r2")
    assert [entry["task_id"] for entry in out["tasks"]] == ["a", "b"]
    assert out["run_id"] == "r2"
    assert out["run_log"].endswith("run-r2.jsonl")
    assert "error" not in out


def test_dispatch_recovers_from_adapter_exception_and_runs_next_task(repo, monkeypatch):
    class RaisingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            if task.id == "a":
                raise RuntimeError("argv exploded")
            return super().build_argv(task, cwd=cwd)

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: RaisingAdapter())
    out = dispatch([{"id": task_id, "goal": "work", "engine": "claude", "why": "x"}
                    for task_id in ("a", "b")], parent_cwd=str(repo), run_id="r-errors")

    first, second = out["tasks"]
    assert first["ok"] is False
    assert "RuntimeError: argv exploded" in first["unproven_reason"]
    assert second["ok"] is True
    assert any(e.kind == "error" and e.task_id == "a" and "argv exploded" in e.payload["message"]
               for e in RunLog("r-errors").read_all())


def test_failed_dependency_skips_whole_chain_without_starting_engines(repo, monkeypatch):
    started = []

    class RaisingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            started.append(task.id)
            raise RuntimeError("first task failed")

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: RaisingAdapter())
    tasks = [{"id": task_id, "goal": "work", "engine": "claude", "why": "x",
              "depends_on": [previous] if previous else []}
             for task_id, previous in (("a", None), ("b", "a"), ("c", "b"))]

    out = dispatch(tasks, parent_cwd=str(repo), run_id="r-chain")

    a, b, c = out["tasks"]
    assert started == ["a"]
    assert a["ok"] is False
    for entry, dependency in ((b, "a"), (c, "b")):
        assert entry["ok"] is False
        assert entry["worktree"] == ""
        assert entry["unproven_reason"] == f"skipped: depends on {dependency}, which failed"
    assert not (repo / ".worktrees" / "subagent-control-r-chain-b").exists()
    assert not (repo / ".worktrees" / "subagent-control-r-chain-c").exists()
    assert any(e.kind == "error" and e.task_id == "c" and "depends on b" in e.payload["message"]
               for e in RunLog("r-chain").read_all())


def test_unverified_success_does_not_block_dependent(repo, monkeypatch):
    started = []

    class RecordingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            started.append(task.id)
            return super().build_argv(task, cwd=cwd)

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: RecordingAdapter())
    out = dispatch([{"id": "a", "goal": "work", "engine": "claude", "why": "x"},
                    {"id": "b", "goal": "work", "engine": "claude", "why": "x",
                     "depends_on": ["a"]}], parent_cwd=str(repo), run_id="r-unverified")

    assert started == ["a", "b"]
    assert all(entry["ok"] is True for entry in out["tasks"])
    assert out["tasks"][0]["verify"] is None


def test_dispatch_reports_each_task_branch_and_worktree(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="r3")
    entry = out["tasks"][0]
    # Run-scoped: the run id is in the branch precisely so a second dispatch
    # with the same task ids does not collide with this run's branch.
    assert entry["branch"] == "hermes-subagent/subagent-control-r3-a"
    assert entry["worktree"].endswith("subagent-control-r3-a")


def test_dispatch_summary_omits_raw_engine_chatter(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="r4")
    assert set(out["tasks"][0]) == {"task_id", "engine", "ok", "text", "diff_stat",
                                    "unproven_reason", "branch", "base_commit", "commit", "worktree",
                                    "why", "verify", "verdict", "rerouted", "model", "effort", "paused"}


def test_dispatch_reports_a_validation_error_without_running_anything(repo):
    out = dispatch([{"id": "a", "goal": "1", "engine": "gemini"}],
                   parent_cwd=str(repo), run_id="r5")
    assert out["error"]
    assert out["tasks"] == []


def test_dispatch_records_a_failure_when_there_is_no_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(tmp_path), run_id="r6")
    entry = out["tasks"][0]
    assert entry["ok"] is False
    assert entry["unproven_reason"] == "no worktree (not a git repo)"


def test_dispatch_logs_a_plan_event_before_any_task_runs(repo, monkeypatch):
    """Design doc D2a: the planner's engine choice and its `why` must be
    logged, not just returned in the summary - the summary dies with the
    conversation, the run log doesn't."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "b", "goal": "2", "engine": "claude", "depends_on": ["a"],
                     "why": "needs a's output"},
                    {"id": "a", "goal": "1", "engine": "codex", "why": "pattern work"}],
                   parent_cwd=str(repo), run_id="r7")

    events = RunLog(out["run_id"]).read_all()
    plan_events = [e for e in events if e.kind == "plan"]
    assert len(plan_events) == 1
    assert events[0].kind == "plan"  # first event in the log, plan-then-execution

    plan_event = plan_events[0]
    assert plan_event.task_id == ""  # sentinel: a run-level event, not a task's
    assert plan_event.engine == ""   # ditto - not any one engine's event
    tasks_in_plan = plan_event.payload["tasks"]
    assert [t["id"] for t in tasks_in_plan] == ["a", "b"]  # dependency order
    assert {t["id"]: t["engine"] for t in tasks_in_plan} == {"a": "codex", "b": "claude"}
    assert {t["id"]: t["why"] for t in tasks_in_plan} == {
        "a": "pattern work", "b": "needs a's output"}


def test_dispatch_plan_event_payload_round_trips_through_the_run_log(repo, monkeypatch):
    """Proves the payload actually survived a write-to-disk-and-read-back cycle
    (not just that dispatch() built the right dict in memory) - which is also
    why `policy` must be the read-only/write string, not the ExecPolicy object:
    that object isn't JSON-serializable, so RunLog.append would have raised."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    out = dispatch([{"id": "a", "goal": "1", "engine": "codex", "why": "x",
                     "policy": "write"},
                    {"id": "b", "goal": "2", "engine": "claude", "why": "y",
                     "depends_on": ["a"]}],
                   parent_cwd=str(repo), run_id="r8")

    events = RunLog(out["run_id"]).read_all()
    plan_event = next(e for e in events if e.kind == "plan")
    tasks_by_id = {t["id"]: t for t in plan_event.payload["tasks"]}

    assert tasks_by_id["a"]["policy"] == "write"
    assert isinstance(tasks_by_id["a"]["policy"], str)
    assert tasks_by_id["b"]["policy"] == "read-only"
    assert tasks_by_id["b"]["depends_on"] == ["a"]
    assert isinstance(tasks_by_id["b"]["depends_on"], list)


# --- credential scoping (design §6) -----------------------------------------

def test_engine_subprocess_does_not_inherit_tier_one_secrets(repo, tmp_path, monkeypatch):
    """Design §6 promises engine subprocesses are spawned through
    `hermes_subprocess_env`; the Popen carried no `env=` at all, so the worker
    inherited everything. GH_TOKEN is in `_ALWAYS_STRIP_KEYS`
    (tools/environments/local_env_policy.py) - the Tier-1 set stripped even under
    `inherit_credentials=True` - so it must never reach a worker CLI."""
    monkeypatch.setenv("GH_TOKEN", "ghp-parent-secret-must-not-leak")
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class EnvDumpingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = ("import os, pathlib;"
                      "pathlib.Path('env.txt').write_text("
                      "repr([os.environ.get('GH_TOKEN'), bool(os.environ.get('PATH'))]),"
                      " encoding='utf-8');"
                      "print('{\"k\": \"result\"}')")
            return ["python3", "-c", script]

    run_task(_task(), run_id="r1", log=log, worktree=worktree,
             adapter=EnvDumpingAdapter())
    dumped = (worktree.path / "env.txt").read_text(encoding="utf-8")
    # PATH present is the positive control: the child got a real environment,
    # so GH_TOKEN's absence is stripping and not an empty env.
    assert dumped == "[None, True]", dumped


# --- process lifecycle ------------------------------------------------------

@pytest.mark.live_system_guard_bypass  # run_task genuinely SIGTERMs/SIGKILLs its own child
def test_run_task_bounds_the_wait_when_a_child_closes_stdout_but_keeps_running(
        repo, tmp_path):
    """The third hang path (I1). EOF on stdout is not exit: the reader loop ends
    normally, and the unbounded `process.wait()` that followed never consulted
    the deadline again, so run_task hung forever on a child that closed stdout
    and kept going."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class ClosesStdoutThenHangsAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = (
                "import os, sys, time\n"
                "with open('pid.txt', 'w', encoding='utf-8') as f:\n"
                "    f.write(str(os.getpid()))\n"
                "print('{\"k\": \"result\"}')\n"
                "sys.stdout.flush()\n"
                "os.close(1)\n"
                "time.sleep(3600)\n"
            )
            return ["python3", "-u", "-c", script]

    result = _run_task_bounded(_task(), run_id="r1", log=log, worktree=worktree,
                               adapter=ClosesStdoutThenHangsAdapter(), timeout=1)
    assert result.ok is False
    assert "timed out" in result.unproven_reason
    _assert_process_gone(worktree.path / "pid.txt")


@pytest.mark.live_system_guard_bypass  # run_task genuinely SIGTERMs/SIGKILLs its own child
def test_timeout_kills_engine_descendants_not_just_the_direct_child(repo, tmp_path):
    """I2. `claude` and `codex` spawn bash, node helpers and MCP servers. Killing
    only the direct child leaves those running - still writing into a worktree
    the summary has just called "preserved for human review"."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class SpawnsAChildAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            grandchild = (
                "import os, time\n"
                "open('grandchild.txt', 'w', encoding='utf-8').write(str(os.getpid()))\n"
                "time.sleep(3600)\n"
            )
            script = (
                "import os, subprocess, sys, time\n"
                "with open('pid.txt', 'w', encoding='utf-8') as f:\n"
                "    f.write(str(os.getpid()))\n"
                f"subprocess.Popen([sys.executable, '-u', '-c', {grandchild!r}])\n"
                "time.sleep(3600)\n"
            )
            return ["python3", "-u", "-c", script]

    result = _run_task_bounded(_task(), run_id="r1", log=log, worktree=worktree,
                               adapter=SpawnsAChildAdapter(), timeout=2)
    assert result.ok is False
    grandchild_pid_file = worktree.path / "grandchild.txt"
    assert grandchild_pid_file.is_file(), "the grandchild never started; test is vacuous"
    _assert_process_gone(worktree.path / "pid.txt")
    _assert_process_gone(grandchild_pid_file)


def test_a_successful_run_withholds_the_stderr_tail(repo, tmp_path):
    """Engines write real diagnostics to stderr in normal operation. The tail is
    a failure-path aid only; leaking it into every summary would put engine
    chatter in front of the planner on runs that went fine."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")

    class ChattyButSucceedingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            script = ("import pathlib, sys;"
                      "sys.stderr.write('noisy-marker');"
                      "pathlib.Path('a.txt').write_text('two\\n', encoding='utf-8');"
                      "print('{\"k\": \"result\"}')")
            return ["python3", "-c", script]

    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=ChattyButSucceedingAdapter())
    assert result.ok is True
    assert result.unproven_reason is None
    assert "noisy-marker" not in (result.text or "")


# --- files_hint (I4) --------------------------------------------------------

def test_files_hint_reaches_the_goal_the_worker_receives(repo, tmp_path):
    """The worker only ever sees `goal`. A hint parsed onto the dataclass and
    never rendered is a promise the schema and the skill both make."""
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    seen: list = []

    class GoalCapturingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            seen.append(task.goal)
            return super().build_argv(task, cwd=cwd)

    task = dataclasses.replace(_task(), files_hint=("agent/auth.py", "tests/test_auth.py"))
    run_task(task, run_id="r1", log=log, worktree=worktree,
             adapter=GoalCapturingAdapter())
    assert seen[0].startswith("g")
    assert "agent/auth.py" in seen[0] and "tests/test_auth.py" in seen[0]


def test_a_task_without_files_hint_keeps_its_goal_verbatim(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    seen: list = []

    class GoalCapturingAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            seen.append(task.goal)
            return super().build_argv(task, cwd=cwd)

    run_task(_task(), run_id="r1", log=log, worktree=worktree,
             adapter=GoalCapturingAdapter())
    assert seen == ["g"]


# --- diff capture failure (I10) ---------------------------------------------

def test_a_failed_diff_capture_is_not_reported_as_no_changes(repo, tmp_path, monkeypatch):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    monkeypatch.setattr("hermes_control.dispatch.capture_worktree_diff",
                        lambda wt: ("", "diff capture failed (boom) - inspect x"))

    result = run_task(_task(), run_id="r1", log=log, worktree=worktree,
                      adapter=FakeAdapter())
    assert result.unproven_reason != "empty diff"
    assert "diff capture failed" in result.unproven_reason


# --- validation (I3, I9) ----------------------------------------------------

def test_dispatch_rejects_a_task_with_no_why(repo):
    """Design D2a / plan deviation #5: `why` is required. The schema said so;
    dispatch never enforced it."""
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude"}],
                   parent_cwd=str(repo), run_id="r9")
    assert out["tasks"] == []
    assert "why" in out["error"]


def test_dispatch_reports_a_non_object_task_as_a_clean_error(repo):
    """`tasks: ["do the thing"]` used to raise AttributeError past dispatch's
    `except ValueError`, so the model got a traceback instead of a message it
    could repair its own call from."""
    out = dispatch(["do the thing"], parent_cwd=str(repo), run_id="r10")
    assert out["tasks"] == []
    assert "must be an object" in out["error"]


def test_dispatch_distinguishes_a_refused_worktree_from_a_non_repo(repo, monkeypatch):
    """C2: the summary said "not a git repo" about a repo Hermes was standing in."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    first = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                     parent_cwd=str(repo), run_id="rsame")
    assert first["tasks"][0]["unproven_reason"] != "no worktree (not a git repo)"
    # Same run id twice is the only remaining way to collide.
    second = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                      parent_cwd=str(repo), run_id="rsame")
    reason = second["tasks"][0]["unproven_reason"]
    assert "not a git repo" not in reason
    assert "worktree creation failed" in reason


def test_a_second_run_against_the_same_repo_succeeds(repo, monkeypatch):
    """The path C2 says is the expected one, not an edge case: the planner emits
    `task-1` again on the next invocation."""
    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    tasks = [{"id": "task-1", "goal": "1", "engine": "claude", "why": "x"}]
    first = dispatch(tasks, parent_cwd=str(repo), run_id="run-one")
    second = dispatch(tasks, parent_cwd=str(repo), run_id="run-two")
    for out in (first, second):
        entry = out["tasks"][0]
        assert entry["branch"], entry
        assert "no worktree" not in (entry["unproven_reason"] or "")
    assert first["tasks"][0]["branch"] != second["tasks"][0]["branch"]


@pytest.mark.live_system_guard_bypass  # deliberately leaves, then reaps, a stray child
def test_run_task_returns_even_when_a_descendant_keeps_the_pipe_open(
        repo, tmp_path, monkeypatch):
    """A fourth hang path, found while verifying the tree kill above.

    `process.stdout.close()` takes the buffered reader's lock, which the reader
    thread still holds while blocked in read(); closing under it blocks forever,
    with no deadline left. The reader stays blocked exactly when a descendant
    survives holding the pipe's write end - which is the `signal_tree` fallback
    path (killpg refused, or Windows `taskkill` failed), simulated here by
    downgrading the tree kill to a direct-child kill.
    """
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    monkeypatch.setattr("hermes_control.dispatch.stop_group",
                        lambda process, grace: process.terminate())

    class SpawnsASurvivingChildAdapter(FakeAdapter):
        def build_argv(self, task, *, cwd):
            grandchild = (
                "import os, time\n"
                "open('grandchild.txt', 'w', encoding='utf-8').write(str(os.getpid()))\n"
                "time.sleep(3600)\n"
            )
            script = (
                "import subprocess, sys, time\n"
                f"subprocess.Popen([sys.executable, '-u', '-c', {grandchild!r}])\n"
                "time.sleep(3600)\n"
            )
            return ["python3", "-u", "-c", script]

    try:
        result = _run_task_bounded(_task(), run_id="r1", log=log, worktree=worktree,
                                   adapter=SpawnsASurvivingChildAdapter(), timeout=2)
        assert result.ok is False
        assert "timed out" in result.unproven_reason
    finally:
        # This test intentionally orphans the grandchild; clean it up here.
        stray = worktree.path / "grandchild.txt"
        if stray.is_file():
            with contextlib.suppress(ValueError, OSError, ProcessLookupError):
                os.kill(int(stray.read_text(encoding="utf-8").strip()),
                        getattr(signal, "SIGKILL", signal.SIGTERM))


def test_an_adapter_that_crashes_on_one_line_does_not_kill_the_run(repo, tmp_path):
    """Spec section 7: an engine event the adapter cannot handle is never fatal.
    The second real Hermes run lost both of its tasks because claude's parser
    raised on one unexpected tool result and the exception escaped run_task."""

    class CrashingAdapter(FakeAdapter):
        def parse_line(self, line):
            if json.loads(line)["k"] == "boom":
                raise AttributeError("'str' object has no attribute 'get'")
            return super().parse_line(line)

    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    result = run_task(
        _task(), run_id="r1", log=log, worktree=worktree,
        adapter=CrashingAdapter(lines=['{"k": "text"}', '{"k": "boom"}', '{"k": "result"}']))

    events = log.read_all()
    assert [e.kind for e in events] == ["text", "error", "result"]
    assert "AttributeError" in events[1].payload["error"]
    assert '"boom"' in events[1].payload["line"]
    assert result.ok is True


def test_a_successful_dispatch_is_not_shown_to_hermes_as_a_failure(repo, monkeypatch):
    """Hermes treats any string result with `"error"` in its first 500 characters as a
    failed call (agent/display.py _detect_tool_failure) and hands that verdict to the
    loop guardrail. The first fully successful real run still showed
    `control_d ... [error]`, because the summary always carried `"error": null`."""
    from agent.display import _detect_tool_failure

    from hermes_control import control_dispatch_handler

    monkeypatch.setattr("hermes_control.dispatch.get_adapter",
                        lambda name: FakeAdapter())
    monkeypatch.chdir(repo)
    result = control_dispatch_handler(
        {"tasks": [{"id": "a", "goal": "1", "engine": "claude", "why": "x"}]})

    assert _detect_tool_failure("control_dispatch", result) == (False, "")


def test_a_rejected_dispatch_is_still_shown_to_hermes_as_a_failure(repo, monkeypatch):
    from agent.display import _detect_tool_failure

    from hermes_control import control_dispatch_handler

    monkeypatch.chdir(repo)
    result = control_dispatch_handler(
        {"tasks": [{"id": "a", "goal": "1", "engine": "gemini", "why": "x"}]})

    failed, _ = _detect_tool_failure("control_dispatch", result)
    assert failed is True


# --- landing: commit, then verify (the merge gap and the unproven gap) ------

class EditingAdapter(FakeAdapter):
    """Writes new.txt into its worktree, then reports like FakeAdapter."""

    def build_argv(self, task, *, cwd):
        argv = super().build_argv(task, cwd=cwd)
        argv[-1] = ("open('new.txt', 'w', encoding='utf-8').write('made by the worker\\n');"
                    + argv[-1])
        return argv


def test_failed_verify_blocks_dependent_even_when_engine_succeeded(repo, monkeypatch):
    started = []

    class RecordingAdapter(EditingAdapter):
        def build_argv(self, task, *, cwd):
            started.append(task.id)
            return super().build_argv(task, cwd=cwd)

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: RecordingAdapter())
    monkeypatch.setattr("hermes_control.dispatch.find_sandbox", lambda: None)
    out = dispatch([{"id": "a", "goal": "work", "engine": "claude", "why": "x",
                     "policy": "write", "verify": "true"},
                    {"id": "b", "goal": "work", "engine": "claude", "why": "x",
                     "depends_on": ["a"]}], parent_cwd=str(repo), run_id="r-verify-dep")

    a, b = out["tasks"]
    assert a["ok"] is True and a["verify"]["passed"] is False
    assert b["ok"] is False
    assert b["unproven_reason"] == "skipped: depends on a, which failed"
    assert b["worktree"] == ""
    assert started == ["a"]


def _dispatch_one(repo, monkeypatch, *, adapter, run_id, sandbox=None, **fields):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: adapter)
    monkeypatch.setattr("hermes_control.dispatch.find_sandbox", lambda: sandbox)
    task = {"id": "a", "goal": "1", "engine": "claude", "why": "x", "policy": "write",
            **fields}
    return dispatch([task], parent_cwd=str(repo), run_id=run_id)["tasks"][0]


def _rev(ref, cwd):
    return subprocess.run(["git", "rev-parse", ref], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_merging_a_task_branch_brings_back_the_workers_change(repo, monkeypatch):
    """The first real run: both workers edited, both branches pointed at the base
    commit, and `git merge <branch>` brought back nothing."""
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m1")

    assert entry["commit"] == _rev(entry["branch"], repo)
    _git(["merge", "-q", entry["branch"]], repo)
    assert (repo / "new.txt").read_text(encoding="utf-8") == "made by the worker\n"


def test_an_edit_nobody_checked_is_not_reported_proven(repo, monkeypatch):
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m2")

    assert entry["unproven_reason"] == "not verified: no verify command was given"
    assert entry["verify"] is None


def test_landing_warns_when_committed_diff_changes_claude_settings(repo, monkeypatch):
    class SettingsAdapter(EditingAdapter):
        def build_argv(self, task, *, cwd):
            argv = super().build_argv(task, cwd=cwd)
            argv[-1] = ("from pathlib import Path; Path('.claude').mkdir(exist_ok=True); "
                        "Path('.claude/settings.json').write_text('{}'); " + argv[-1])
            return argv

    entry = _dispatch_one(repo, monkeypatch, adapter=SettingsAdapter(), run_id="settings")

    assert entry["commit"]
    assert ".claude/settings.json" in entry["unproven_reason"]
    assert _rev(entry["branch"], repo) == entry["commit"]


def test_without_a_sandbox_the_verify_command_never_runs(repo, monkeypatch):
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m3",
                          sandbox=None, verify="touch verify-ran.txt")

    assert entry["unproven_reason"].startswith("not verified: no sandbox available")
    assert entry["verify"]["passed"] is False
    assert not os.path.exists(os.path.join(entry["worktree"], "verify-ran.txt"))
    assert entry["commit"]  # the change still reaches the branch for a human to judge


def test_a_failed_engine_run_is_not_committed(repo, monkeypatch):
    """Partial work from a failed run stays in the worktree for inspection; on the
    branch it would be one `git merge` away from the working branch."""
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(exit_code=1),
                          run_id="m4", verify="true")

    assert entry["commit"] == ""
    assert entry["verify"] is None
    assert _rev(entry["branch"], repo) == _rev("HEAD", repo)
    assert "engine exited 1" in entry["unproven_reason"]


def test_commit_and_verify_extend_the_tasks_run_log(repo, monkeypatch):
    _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m5",
                  verify="true")

    events = [e for e in RunLog("m5").read_all() if e.task_id == "a"]
    assert [e.kind for e in events][-2:] == ["commit", "verify"]
    assert [e.seq for e in events] == list(range(len(events)))


def _local_verify(command, checkout, *, codex_path):
    """Exercise landing's checkout choice without requiring the codex CLI."""
    proc = subprocess.run(["/bin/sh", "-c", command], cwd=checkout,
                          capture_output=True, text=True)
    reason = None if proc.returncode == 0 else f"verification failed (exit {proc.returncode})"
    return VerifyOutcome(command, reason is None, proc.returncode,
                         proc.stdout + proc.stderr, reason)


@pytest.mark.parametrize("timed_out", [False, True])
def test_ignored_worker_module_cannot_prove_commit(repo, monkeypatch, timed_out):
    class IgnoredModuleAdapter(EditingAdapter):
        def build_argv(self, task, *, cwd):
            argv = super().build_argv(task, cwd=cwd)
            argv[-1] = ("open('hidden_module.py', 'w').write('VALUE = 1\\n');" + argv[-1])
            return argv

    (repo / ".gitignore").write_text("hidden_module.py\n", encoding="utf-8")
    _git(["add", ".gitignore"], repo)
    _git(["commit", "-qm", "ignore generated module"], repo)
    def verify(command, checkout, *, codex_path):
        if timed_out:
            return VerifyOutcome(command, False, None, "", "verification timed out after 600s")
        return _local_verify(command, checkout, codex_path=codex_path)

    monkeypatch.setattr("hermes_control.landing.run_verify", verify)

    entry = _dispatch_one(repo, monkeypatch, adapter=IgnoredModuleAdapter(),
                          run_id="ignored-module", sandbox="/fake/codex",
                          verify="python3 -c 'import hidden_module; assert hidden_module.VALUE == 1'")

    assert entry["commit"] == _rev(entry["branch"], repo)
    assert entry["verify"]["passed"] is False
    if timed_out:
        assert entry["verify"]["exit_code"] is None
        assert entry["unproven_reason"].startswith("verification timed out")
    else:
        assert entry["verify"]["exit_code"] != 0
        assert entry["unproven_reason"].startswith("verification failed (exit ")
    assert "task worktree has files not in the commit: hidden_module.py" in entry["unproven_reason"]
    assert os.path.exists(os.path.join(entry["worktree"], "hidden_module.py"))


def test_successful_verify_uses_disposable_commit_checkout(repo, monkeypatch):
    seen = []

    def verify(command, checkout, *, codex_path):
        seen.append(checkout)
        return _local_verify(command, checkout, codex_path=codex_path)

    monkeypatch.setattr("hermes_control.landing.run_verify", verify)
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(),
                          run_id="clean-checkout", sandbox="/fake/codex",
                          verify="test -f new.txt && touch verify-artifact.txt")

    assert entry["unproven_reason"] is None
    assert entry["verify"]["passed"] is True
    assert len(seen) == 1
    assert seen[0] != entry["worktree"]
    assert not seen[0].exists()
    listed = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=repo,
                            check=True, capture_output=True, text=True).stdout
    assert str(seen[0]) not in listed
    assert not (seen[0] / "verify-artifact.txt").exists()
    assert not os.path.exists(os.path.join(entry["worktree"], "verify-artifact.txt"))


def test_checkout_creation_failure_keeps_commit_unproven(repo, monkeypatch):
    def fail_checkout(*args):
        raise VerifyCheckoutError("git worktree add denied")

    monkeypatch.setattr("hermes_control.landing.verify_checkout", fail_checkout)
    monkeypatch.setattr("hermes_control.landing.run_verify",
                        lambda *args, **kwargs: pytest.fail("verify must not run"))
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(),
                          run_id="checkout-failed", sandbox="/fake/codex", verify="true")

    assert entry["commit"] == _rev(entry["branch"], repo)
    assert entry["verify"] is None
    assert entry["unproven_reason"] == (
        f"not verified: could not create a clean checkout of {entry['commit']} "
        "(git worktree add denied)")


@pytest.mark.parametrize("failure_site", ["committed_claude_paths", "run_verify", "log_append"])
def test_post_commit_error_keeps_commit_and_marks_task_unproven(
        repo, monkeypatch, failure_site):
    if failure_site == "log_append":
        original = RunLog.append

        def fail_commit_event(self, events):
            if any(event.kind == "commit" for event in events):
                raise OSError("post-commit log full")
            return original(self, events)

        monkeypatch.setattr(RunLog, "append", fail_commit_event)
    else:
        monkeypatch.setattr(f"hermes_control.landing.{failure_site}",
                            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("post-commit failure")))
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(),
                          run_id=f"post-{failure_site}", verify="true")

    assert entry["commit"] == _rev(entry["branch"], repo)
    assert "OSError" in entry["unproven_reason"]


@pytest.mark.parametrize("failure_site", ["run_verify", "find_sandbox"])
def test_missing_verify_outcome_blocks_dependents(repo, monkeypatch, failure_site):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: EditingAdapter())
    monkeypatch.setattr("hermes_control.dispatch.find_sandbox", lambda: "/fake/codex")
    module = "landing" if failure_site == "run_verify" else "dispatch"
    monkeypatch.setattr(f"hermes_control.{module}.{failure_site}",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("check broke")))
    tasks = [{"id": "a", "goal": "work", "engine": "claude", "why": "x", "verify": "true"},
             {"id": "b", "goal": "work", "engine": "claude", "why": "x",
              "depends_on": ["a"]}]

    a, b = dispatch(tasks, parent_cwd=str(repo), run_id=f"verify-raises-{failure_site}")["tasks"]

    assert a["ok"] is True and a["commit"] and a["verify"] is None
    assert "check broke" in a["unproven_reason"]
    assert b["unproven_reason"] == "skipped: depends on a, which failed"
    assert b["worktree"] == ""


def test_post_commit_verify_error_keeps_claude_warning(repo, monkeypatch):
    class SettingsAdapter(EditingAdapter):
        def build_argv(self, task, *, cwd):
            argv = super().build_argv(task, cwd=cwd)
            argv[-1] = ("from pathlib import Path; Path('.claude').mkdir(exist_ok=True); "
                        "Path('.claude/settings.json').write_text('{}'); " + argv[-1])
            return argv

    monkeypatch.setattr("hermes_control.landing.run_verify",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("check broke")))
    entry = _dispatch_one(repo, monkeypatch, adapter=SettingsAdapter(),
                          run_id="settings-verify-error", verify="true")

    assert "check broke" in entry["unproven_reason"]
    assert ".claude/settings.json" in entry["unproven_reason"]


def test_unexpected_error_after_worktree_names_possible_commit(repo, monkeypatch):
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: EditingAdapter())
    monkeypatch.setattr("hermes_control.dispatch._execute",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("late failure")))
    entry = dispatch([{"id": "a", "goal": "work", "engine": "claude", "why": "x"}],
                     parent_cwd=str(repo), run_id="late-failure")["tasks"][0]

    assert entry["branch"]
    assert f"branch {entry['branch']} may already hold a commit" in entry["unproven_reason"]


def test_failed_dependency_wins_over_paused_dependency(repo, monkeypatch):
    from hermes_control import pauses

    class FailingAdapter(EditingAdapter):
        def build_argv(self, task, *, cwd):
            if task.id == "a":
                raise RuntimeError("worker failed")
            return super().build_argv(task, cwd=cwd)

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FailingAdapter())
    monkeypatch.setattr("hermes_control.dispatch.limits.engine_hold",
                        lambda engine, **kwargs: ("held", None) if engine == "claude" else None)
    # A fails in its worker; B is paused by a held engine; C depends on both.
    tasks = [{"id": "a", "goal": "work", "engine": "codex", "why": "x"},
             {"id": "b", "goal": "work", "engine": "claude", "why": "x"},
             {"id": "c", "goal": "work", "engine": "codex", "why": "x",
              "depends_on": ["a", "b"]}]
    monkeypatch.setattr("hermes_control.dispatch._free_engine", lambda ctx, besides: None)
    out = dispatch(tasks, parent_cwd=str(repo), run_id="mixed-deps")

    assert out["tasks"][1]["paused"]
    assert out["tasks"][2]["unproven_reason"] == "skipped: depends on a, which failed"
    assert pauses.load(pauses.pause_id("mixed-deps", "c")) is None


def test_failed_pause_logging_removes_saved_pause_record(repo, monkeypatch):
    from hermes_control import pauses

    monkeypatch.setattr("hermes_control.dispatch.get_adapter", lambda name: FakeAdapter())
    monkeypatch.setattr("hermes_control.dispatch.limits.engine_hold",
                        lambda engine, **kwargs: ("held", None))
    monkeypatch.setattr("hermes_control.dispatch._free_engine", lambda ctx, besides: None)
    original_log = __import__("hermes_control.dispatch", fromlist=["_log"])._log

    def fail_paused_log(ctx, task_id, engine, kind, payload):
        if kind == "paused":
            raise OSError("pause log full")
        return original_log(ctx, task_id, engine, kind, payload)

    monkeypatch.setattr("hermes_control.dispatch._log", fail_paused_log)
    out = dispatch([{"id": "a", "goal": "work", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="failed-pause")

    assert out["tasks"][0]["ok"] is False
    assert out["tasks"][0]["paused"] is None
    assert pauses.load(pauses.pause_id("failed-pause", "a")) is None


def test_the_worker_is_told_how_its_work_will_be_checked(repo, tmp_path):
    log = RunLog("r1", directory=tmp_path / "logs")
    worktree = create_task_worktree(str(repo), "t1", run_id="r1")
    seen: list = []

    class GoalCapturingAdapter(FakeAdapter):
        name = "codex"

        def build_argv(self, task, *, cwd):
            seen.append(task.goal)
            return super().build_argv(task, cwd=cwd)

    task = dataclasses.replace(_task(), engine="codex",
                               verify="python3 -m pytest -q tests/test_calc.py")
    run_task(task, run_id="r1", log=log, worktree=worktree, adapter=GoalCapturingAdapter())
    assert seen[0].startswith("g")
    assert "python3 -m pytest -q tests/test_calc.py" in seen[0]
    assert seen[0].count("How to work efficiently here:") == 1
    assert seen[0].index("Make that command pass.") < seen[0].index(
        "How to work efficiently here:")


needs_codex = pytest.mark.skipif(shutil.which("codex") is None,
                                 reason="codex CLI not installed")


@pytest.mark.integration
@needs_codex
def test_a_check_that_passes_against_the_commit_is_what_proves_a_task(repo, monkeypatch):
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m6",
                          sandbox=shutil.which("codex"),
                          verify="test -f new.txt && touch verify-artifact.txt")

    assert entry["unproven_reason"] is None
    assert entry["verify"]["passed"] is True and entry["verify"]["exit_code"] == 0
    _git(["merge", "-q", entry["branch"]], repo)
    assert (repo / "new.txt").is_file()
    # The check ran after the commit, so nothing it wrote rides along on the branch.
    assert not (repo / "verify-artifact.txt").exists()


@pytest.mark.integration
@needs_codex
def test_a_failing_check_leaves_the_task_committed_but_unproven(repo, monkeypatch):
    entry = _dispatch_one(repo, monkeypatch, adapter=EditingAdapter(), run_id="m7",
                          sandbox=shutil.which("codex"), verify="exit 4")

    assert entry["unproven_reason"] == "verification failed (exit 4)"
    assert entry["verify"]["exit_code"] == 4
    assert entry["commit"]


# --- routing around an engine that cannot run --------------------------------

class SignInFake(FakeAdapter):
    """FakeAdapter under a real engine name, signed in or not."""

    def __init__(self, name, problem=None):
        super().__init__()
        self.name = name
        self.problem = problem
        self.models_seen = []
        self.efforts_seen = []

    def auth_problem(self, info):
        return self.problem

    def build_argv(self, task, *, cwd):
        self.models_seen.append(task.model)
        self.efforts_seen.append(task.effort)
        return super().build_argv(task, cwd=cwd)


def _engines(monkeypatch, *, claude=None, codex=None):
    adapters = {"claude": SignInFake("claude", claude), "codex": SignInFake("codex", codex)}
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", adapters.__getitem__)
    return adapters


def test_when_one_engine_is_signed_out_every_task_runs_on_the_other(repo, monkeypatch):
    _engines(monkeypatch, codex="not signed in; run `codex login`")
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"},
                    {"id": "b", "goal": "2", "engine": "codex", "why": "user pinned"}],
                   parent_cwd=str(repo), run_id="s1")

    a, b = out["tasks"]
    assert (a["engine"], b["engine"]) == ("claude", "claude")
    assert a["rerouted"] is None
    assert b["rerouted"] == {"from": "codex", "reason": "not signed in; run `codex login`"}
    assert b["ok"] is True
    assert "error" not in out


def test_a_model_meant_for_the_signed_out_engine_is_not_sent_to_the_other(repo, monkeypatch):
    """Model names belong to one CLI; `claude --model gpt-5.5` would fail the task."""
    adapters = _engines(monkeypatch, codex="not signed in")
    out = dispatch([{"id": "b", "goal": "2", "engine": "codex", "why": "x",
                     "model": "gpt-5.5"}], parent_cwd=str(repo), run_id="s2")

    assert adapters["claude"].models_seen == [None]
    assert out["tasks"][0]["rerouted"]["dropped_model"] == "gpt-5.5"


def test_when_no_engine_can_run_nothing_starts_and_the_call_says_why(repo, monkeypatch):
    _engines(monkeypatch, claude="not signed in (claude)", codex="not installed (codex)")
    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="s3")

    assert out["tasks"] == []
    assert "not signed in (claude)" in out["error"]
    assert "not installed (codex)" in out["error"]
    worktrees = subprocess.run(["git", "worktree", "list"], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.splitlines()
    assert len(worktrees) == 1


def test_the_run_log_plan_records_where_a_task_was_moved_and_why(repo, monkeypatch):
    _engines(monkeypatch, claude="not signed in")
    dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
             parent_cwd=str(repo), run_id="s4")

    plan = next(e for e in RunLog("s4").read_all() if e.kind == "plan")
    assert plan.payload["tasks"][0]["engine"] == "codex"
    assert plan.payload["tasks"][0]["rerouted"] == {"from": "claude", "reason": "not signed in"}


# --- the model policy (plugins.entries.hermes_control.settings.models) ------------

POLICY = {"claude": {"default": "sonnet",
                     "allowed": {"sonnet": {"use_for": "routine", "effort": "medium"},
                                 "opus": {"use_for": "hard", "effort": "high"}}}}


def _with_policy(monkeypatch, settings):
    monkeypatch.setattr("hermes_control.model_policy.read_settings", lambda: settings)


def test_a_task_naming_no_model_runs_the_default_at_its_effort(repo, monkeypatch):
    adapters = _engines(monkeypatch)
    _with_policy(monkeypatch, POLICY)

    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="p1")

    assert (adapters["claude"].models_seen, adapters["claude"].efforts_seen) == (["sonnet"], ["medium"])
    assert (out["tasks"][0]["model"], out["tasks"][0]["effort"]) == ("sonnet", "medium")


def test_a_model_outside_the_list_stops_the_call_before_anything_runs(repo, monkeypatch):
    adapters = _engines(monkeypatch)
    _with_policy(monkeypatch, POLICY)

    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x", "model": "haiku"}],
                   parent_cwd=str(repo), run_id="p2")

    assert out["tasks"] == [] and "sonnet, opus" in out["error"]
    assert adapters["claude"].models_seen == []


def test_invalid_settings_are_reported_instead_of_ignored(repo, monkeypatch):
    _engines(monkeypatch)
    _with_policy(monkeypatch, {"claude": {"allowed": {"sonnet": {"effort": "ludicrous"}}}})

    out = dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x"}],
                   parent_cwd=str(repo), run_id="p3")

    assert out["tasks"] == [] and "model settings are invalid" in out["error"]


def test_a_rerouted_task_drops_the_other_clis_model_and_effort(repo, monkeypatch):
    """`ultra` is a codex effort claude does not have; the move lands on claude's
    default model at that model's effort."""
    adapters = _engines(monkeypatch, codex="not signed in")
    _with_policy(monkeypatch, POLICY)

    out = dispatch([{"id": "b", "goal": "2", "engine": "codex", "why": "x",
                     "model": "gpt-5.6-terra", "effort": "ultra"}],
                   parent_cwd=str(repo), run_id="p4")

    assert (adapters["claude"].models_seen, adapters["claude"].efforts_seen) == (["sonnet"], ["medium"])
    assert out["tasks"][0]["rerouted"]["dropped_model"] == "gpt-5.6-terra"
    assert out["tasks"][0]["rerouted"]["dropped_effort"] == "ultra"


def test_the_run_log_plan_records_each_tasks_model_and_effort(repo, monkeypatch):
    _engines(monkeypatch)
    _with_policy(monkeypatch, POLICY)

    dispatch([{"id": "a", "goal": "1", "engine": "claude", "why": "x", "model": "opus"}],
             parent_cwd=str(repo), run_id="p5")

    plan = next(e for e in RunLog("p5").read_all() if e.kind == "plan")
    assert (plan.payload["tasks"][0]["model"], plan.payload["tasks"][0]["effort"]) == ("opus", "high")
