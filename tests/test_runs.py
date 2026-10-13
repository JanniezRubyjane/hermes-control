"""Run logs read back as the dashboard's run list and diagram."""

import json

import pytest
from scripted_engine import ScriptedEngine, _engines, _reading, repo  # noqa: F401 - fixture

from hermes_control import runs
from hermes_control.dispatch import dispatch
from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog

T0 = 1_790_000_000.0
PLAN = {"tasks": [{"id": "a", "engine": "claude", "why": "reads widely first", "policy": "write",
                   "depends_on": [], "verify": "python -m pytest -q", "model": "sonnet",
                   "effort": "low", "rerouted": None}],
        "repo": "/work/app", "planner": {"provider": "anthropic", "model": "claude-fable-5-1",
                                         "session_id": "S-1"}}


def _write(run_id, *events):
    """events: (task_id, engine, kind, payload), one second apart from T0."""
    log = RunLog(run_id)
    for offset, (task_id, engine, kind, payload) in enumerate(events):
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id=run_id, task_id=task_id,
                         engine=engine, start_seq=log.next_seq(task_id), now=T0 + offset))


def _session(engine, sid="X"):
    return ("a", engine, "session", {"session_id": sid, "model": f"{engine}-model"})


DONE_TAIL = (
    ("a", "claude", "result", {"ok": True, "usage": {"input_tokens": 1200, "output_tokens": 80},
                               "total_cost_usd": 0.02}),
    ("a", "claude", "commit", {"sha": "abc1234def", "branch": "hermes-subagent/x-a"}),
    ("a", "claude", "verify", {"command": "python -m pytest -q", "passed": True, "exit_code": 0,
                               "output_tail": "1 passed", "unproven_reason": None}),
    ("", "", "summary", {"tasks": [{"task_id": "a", "engine": "claude", "ok": True,
                                    "commit": "abc1234def", "branch": "hermes-subagent/x-a",
                                    "worktree": "/work/app/.worktrees/x-a", "unproven_reason": None,
                                    "rerouted": None, "paused_id": None}]}),
)


def _frame_view(frame):
    assert frame.startswith("event: view\ndata: ") and frame.endswith("\n\n")
    return json.loads(frame[len("event: view\ndata: "):-2])


def test_stream_starts_with_the_full_view_and_unknown_run_is_empty():
    _write("stream-initial", ("", "", "plan", PLAN), _session("claude"))
    stream = runs.stream_run("stream-initial", now=lambda: T0 + 10)

    assert _frame_view(next(stream)) == runs.run_view("stream-initial", now=lambda: T0 + 10)
    stream.close()
    assert list(runs.stream_run("missing-stream", now=lambda: T0 + 10)) == []


def test_task_trace_has_ordered_events_and_completed_tools():
    plan = {**PLAN, "tasks": [{**PLAN["tasks"][0], "goal": "g" * 700},
                               {**PLAN["tasks"][0], "id": "b"}]}
    _write("trace-shapes", ("", "", "plan", plan),
           ("", "", "planner", {"phase": "waiting", "wait_seconds": 3}),
           _session("claude"),
           ("a", "claude", "thinking", {"text": ""}),
           ("a", "claude", "thinking", {"text": "reasoning"}),
           ("a", "claude", "text", {"text": "hello"}),
           ("a", "claude", "tool_start", {"tool_id": "one", "name": "Bash",
                                           "input": {"command": "pytest"}}),
           ("a", "claude", "tool_end", {"tool_id": "one", "is_error": False,
                                         "stdout": "x" * 7000, "stderr": "warning", "exit_code": 0}),
           ("a", "claude", "tool_start", {"tool_id": "two", "name": "Read",
                                           "input": {"file_path": "a.py"}}),
           ("a", "claude", "tool_end", {"tool_id": "two", "is_error": True,
                                         "content": "denied"}),
           ("a", "claude", "commit", {"sha": "abc"}),
           ("a", "claude", "verify", {"command": "pytest", "exit_code": 1,
                                       "passed": False, "unproven_reason": "failed",
                                       "output_tail": "1 failed"}))

    entries = runs.task_trace("trace-shapes", "a")
    assert [e["type"] for e in entries] == ["plan", "planner", "session", "thinking",
                                            "text", "tool", "tool", "commit", "verify"]
    assert [(e["ts"], e["seq"]) for e in entries] == sorted(
        (e["ts"], e["seq"]) for e in entries)
    assert entries[0]["goal"].endswith("…") and len(entries[0]["goal"]) == 600
    assert entries[1]["phase"] == "waiting" and entries[1]["wait_seconds"] == 3
    assert entries[2]["how"] == "planned" and entries[2]["session_id"] == "X"
    assert entries[3]["text"] == "reasoning" and entries[4]["text"] == "hello"
    first, second = entries[5:7]
    assert first["tool_id"] == "one" and first["input"] == "pytest"
    assert json.loads(first["input_raw"]) == {"command": "pytest"}
    assert len(first["output"]) == 6000 and first["output_truncated"] is True
    assert first["stderr"] == "warning" and first["exit_code"] == 0
    assert first["duration_s"] == 1 and first["is_error"] is False
    assert second["output"] == "denied" and second["is_error"] is True
    assert entries[-2]["data"] == {"sha": "abc"}
    assert entries[-1]["data"]["output_tail"] == "1 failed"
    assert [e["type"] for e in runs.task_trace("trace-shapes", "b")] == ["plan", "planner"]
    assert [e["type"] for e in runs.task_trace("trace-shapes", "a", since_seq=first["seq"])] == [
        "tool", "tool", "commit", "verify"]
    assert runs.task_trace("missing-trace", "a") is None


def test_stream_sends_each_new_trace_entry_and_tool_completion():
    _write("trace-stream", ("", "", "plan", PLAN), _session("claude"))
    calls = 0

    def append(_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            _write("trace-stream", ("a", "claude", "tool_start",
                                    {"tool_id": "t", "name": "Bash", "input": {"command": "pwd"}}))
        else:
            _write("trace-stream", ("a", "claude", "tool_end",
                                    {"tool_id": "t", "content": "here", "is_error": False}),
                   ("", "", "planner", {"phase": "returned"}))

    stream = runs.stream_run("trace-stream", now=lambda: T0 + 10, sleep=append)
    next(stream)
    assert _frame_view(next(stream))["state"] == "running"
    pending = json.loads(next(stream).split("data: ", 1)[1])
    assert pending["task_id"] == "a" and pending["entry"]["tool_id"] == "t"
    assert "output" not in pending["entry"]
    assert _frame_view(next(stream))["state"] == "running"
    completed = json.loads(next(stream).split("data: ", 1)[1])
    planner = json.loads(next(stream).split("data: ", 1)[1])
    assert completed["task_id"] == "a" and completed["entry"]["seq"] == pending["entry"]["seq"]
    assert completed["entry"]["output"] == "here"
    assert planner["task_id"] == "" and planner["entry"]["phase"] == "returned"
    stream.close()


def test_stream_refreshes_after_an_append_and_ends_when_settled():
    _write("stream-append", ("", "", "plan", PLAN), _session("claude"))
    calls = 0

    def append_on_first_sleep(_seconds):
        nonlocal calls
        calls += 1
        if calls == 1:
            _write("stream-append", ("a", "claude", "text", {"text": "working"}))
        else:
            _write("stream-append", ("a", "claude", "verify", {"passed": True}))

    stream = runs.stream_run("stream-append", now=lambda: T0 + 10,
                             sleep=append_on_first_sleep)
    assert _frame_view(next(stream))["state"] == "running"
    changed = _frame_view(next(stream))
    assert changed["tasks"][0]["attempts"][0]["last_text"] == "working"
    assert changed == runs.run_view("stream-append", now=lambda: T0 + 10)
    assert json.loads(next(stream).split("data: ", 1)[1])["entry"]["text"] == "working"
    assert _frame_view(next(stream))["state"] == "finished"
    assert json.loads(next(stream).split("data: ", 1)[1])["entry"]["type"] == "verify"
    with pytest.raises(StopIteration):
        next(stream)


def test_stream_sends_keep_alive_after_idle_heartbeat():
    _write("stream-idle", ("", "", "plan", PLAN), _session("claude"))
    clock = [T0 + 10]

    def advance(seconds):
        clock[0] += seconds

    stream = runs.stream_run("stream-idle", poll_seconds=0.5, heartbeat_seconds=1,
                             now=lambda: clock[0], sleep=advance)
    next(stream)
    assert next(stream) == ": keep-alive\n\n"
    stream.close()


def test_idle_stream_sends_stopped_view_when_run_becomes_stale():
    _write("stream-stale", ("", "", "plan", PLAN), _session("claude"))
    clock = [T0 + 10]

    def advance(seconds):
        clock[0] += seconds

    stream = runs.stream_run("stream-stale", poll_seconds=1, heartbeat_seconds=1,
                             now=lambda: clock[0], sleep=advance)
    assert _frame_view(next(stream))["state"] == "running"
    assert next(stream) == ": keep-alive\n\n"
    clock[0] = T0 + 2 + runs.STALE_SECONDS
    stopped = _frame_view(next(stream))
    assert stopped == runs.run_view("stream-stale", now=lambda: clock[0])
    assert stopped["state"] == "stopped"
    with pytest.raises(StopIteration):
        next(stream)


def test_stream_sends_gone_when_log_disappears():
    _write("stream-gone", ("", "", "plan", PLAN), _session("claude"))
    stream = runs.stream_run("stream-gone", now=lambda: T0 + 10,
                             sleep=lambda _: RunLog("stream-gone").path.unlink())
    next(stream)
    assert next(stream) == "event: gone\ndata: {}\n\n"
    with pytest.raises(StopIteration):
        next(stream)


def test_a_finished_run_reads_from_plan_to_check():
    _write("r1", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "tool_start", {"tool_id": "t1", "name": "Read",
                                          "input": {"file_path": "/work/app/calc.py"}}),
           ("a", "claude", "tool_end", {"tool_id": "t1", "is_error": False, "content": "x" * 5000}),
           ("a", "claude", "text", {"text": "Added mul and its test."}), *DONE_TAIL)

    view = runs.run_view("r1", now=lambda: T0 + 60)

    assert view["state"] == "finished" and view["repo"] == "/work/app"
    assert view["planner"] == {"provider": "anthropic", "model": "claude-fable-5-1",
                               "session_id": "S-1"}
    [task] = view["tasks"]
    assert task["status"] == "done" and task["why"] == "reads widely first"
    [attempt] = task["attempts"]
    assert (attempt["engine"], attempt["model"], attempt["tools"]) == ("claude", "claude-model", 1)
    assert attempt["last_text"] == "Added mul and its test."
    assert attempt["result"]["cached_input_tokens"] is None
    assert attempt["result"]["cache_write_tokens"] is None
    assert attempt["result"]["reasoning_tokens"] is None
    assert attempt["steps"][0] == {"type": "tool", "name": "Read", "detail": "/work/app/calc.py",
                                   "is_error": False}
    assert task["commit"]["sha"] == "abc1234def" and task["verify"]["passed"] is True
    assert task["branch"] == "hermes-subagent/x-a"


def test_a_handover_after_a_limit_shows_both_attempts():
    _write("r2", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "error", {"message": "You've hit your usage limit."}),
           ("a", "claude", "limit", {"message": "You've hit your usage limit.", "resets_at": T0 + 99,
                                     "next": "codex"}),
           _session("codex", "Y"), *[(t, "codex" if e else e, k, p) for t, e, k, p in DONE_TAIL])

    [task] = runs.run_view("r2", now=lambda: T0 + 60)["tasks"]

    first, second = task["attempts"]
    assert first["engine"] == "claude" and first["limit"]["next"] == "codex"
    assert second["engine"] == "codex" and second["how"] == "continued"
    assert task["status"] == "done"


def test_a_move_shows_while_the_task_still_runs():
    _write("r13", ("", "", "plan", PLAN),
           ("a", "codex", "rerouted", {"from": "claude", "reason": "claude is at 90% ..."}),
           _session("codex"))

    [task] = runs.run_view("r13", now=lambda: T0 + 10)["tasks"]

    assert task["status"] == "running" and task["rerouted"]["from"] == "claude"


def test_a_paused_task_and_its_resume():
    paused = ("a", "claude", "paused", {"id": "r3-a", "reason": "claude hit its usage limit",
                                        "resets_at": T0 + 3600, "worktree": None})
    summary_paused = ("", "", "summary", {"tasks": [{"task_id": "a", "engine": "claude", "ok": False,
                                                     "commit": "", "paused_id": "r3-a",
                                                     "unproven_reason": "paused: ..."}]})
    _write("r3", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "limit", {"message": "limit", "resets_at": None, "next": "pause"}),
           paused, summary_paused)

    paused_view = runs.run_view("r3", now=lambda: T0 + 60)
    assert paused_view["state"] == "paused"
    assert paused_view["tasks"][0]["status"] == "paused"
    assert next(item for item in runs.list_runs(now=lambda: T0 + 60)
                if item["run_id"] == "r3")["state"] == "paused"
    stream = runs.stream_run("r3", now=lambda: T0 + 60)
    assert _frame_view(next(stream))["state"] == "paused"
    with pytest.raises(StopIteration):
        next(stream)

    _write("r3", ("a", "claude", "resumed", {"id": "r3-a", "engine": "claude", "session": "X"}),
           ("a", "claude", "tool_start", {"tool_id": "next", "name": "Read", "input": {}}))
    assert runs.run_view("r3", now=lambda: T0 + 60)["state"] == "running"

    _write("r3", _session("claude", "X"), *DONE_TAIL)
    [task] = runs.run_view("r3", now=lambda: T0 + 60)["tasks"]

    assert task["status"] == "done"
    assert task["attempts"][-1]["how"] == "resumed"
    assert task["paused"] is None and len(task["resumed"]) == 1


def test_failed_summary_overrides_old_pause_event():
    _write("failed-after-pause", ("", "", "plan", PLAN),
           ("a", "claude", "paused", {"id": "failed-after-pause-a", "reason": "limit"}),
           ("", "", "summary", {"tasks": [{"task_id": "a", "engine": "claude",
                                            "ok": False, "paused_id": None,
                                            "unproven_reason": "pause log failed"}]}))

    [task] = runs.run_view("failed-after-pause")["tasks"]
    assert task["status"] == "failed"
    assert task["paused"] is None
    assert task["status_reason"] == "pause log failed"


def test_resumed_settings_apply_to_next_attempt_and_old_events_keep_plan():
    _write("resolved-resume", ("", "", "plan", PLAN),
           ("a", "claude", "resumed", {"id": "p", "engine": "claude",
                                       "model": "new-model", "effort": "high"}),
           ("a", "claude", "session", {"session_id": "X"}),
           ("a", "claude", "resumed", {"id": "p", "engine": "claude"}),
           ("a", "claude", "session", {"session_id": "Y"}))
    _write("old-resume", ("", "", "plan", PLAN),
           ("a", "claude", "resumed", {"id": "p", "engine": "claude"}),
           ("a", "claude", "session", {"session_id": "X"}))

    attempts = runs.run_view("resolved-resume")["tasks"][0]["attempts"]
    old = runs.run_view("old-resume")["tasks"][0]["attempts"]
    assert [(a["model"], a["effort"]) for a in attempts] == [
        ("new-model", "high"), ("new-model", "high")]
    assert (old[0]["model"], old[0]["effort"]) == ("sonnet", "low")


def test_a_run_in_progress_and_a_task_not_started_yet():
    plan = {**PLAN, "tasks": PLAN["tasks"] + [{**PLAN["tasks"][0], "id": "b", "depends_on": ["a"]}]}
    _write("r4", ("", "", "plan", plan), _session("claude"),
           ("a", "claude", "tool_start", {"tool_id": "t1", "name": "Bash",
                                          "input": {"command": "pytest"}}))

    view = runs.run_view("r4", now=lambda: T0 + 30)

    assert view["state"] == "running"
    assert [t["status"] for t in view["tasks"]] == ["running", "waiting"]


def test_a_run_that_went_silent_long_ago_is_stopped_not_running():
    _write("r5", ("", "", "plan", PLAN), _session("claude"))

    view = runs.run_view("r5", now=lambda: T0 + 24 * 3600)

    assert view["state"] == "stopped" and view["tasks"][0]["status"] == "stopped"


def test_a_failed_check_is_unproven_and_says_why():
    _write("r6", ("", "", "plan", PLAN), _session("claude"), DONE_TAIL[0], DONE_TAIL[1],
           ("a", "claude", "verify", {"command": "pytest", "passed": False, "exit_code": 1,
                                      "output_tail": "1 failed", "unproven_reason": "verify failed (exit 1)"}),
           DONE_TAIL[3])

    [task] = runs.run_view("r6", now=lambda: T0 + 60)["tasks"]

    assert task["status"] == "unproven" and "exit 1" in task["status_reason"]


def test_an_engine_failure_without_a_commit_is_failed():
    _write("r7", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "error", {"subtype": "error_during_execution"}),
           ("", "", "summary", {"tasks": [{"task_id": "a", "ok": False, "commit": "",
                                           "unproven_reason": "engine reported failure"}]}))

    [task] = runs.run_view("r7", now=lambda: T0 + 60)["tasks"]

    assert task["status"] == "failed" and task["status_reason"] == "engine reported failure"


def test_a_log_from_before_summaries_is_settled_by_its_checks():
    """Runs logged before the summary event existed still read as finished."""
    _write("r8", ("", "", "plan", PLAN), _session("claude"), *DONE_TAIL[:3])

    view = runs.run_view("r8", now=lambda: T0 + 30)

    assert view["state"] == "finished" and view["tasks"][0]["status"] == "done"


def test_a_quiet_log_without_summaries_settles_tasks_that_got_a_result():
    _write("r10", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "error", {"subtype": "error_during_execution"}))

    view = runs.run_view("r10", now=lambda: T0 + 24 * 3600)

    assert view["state"] == "finished" and view["tasks"][0]["status"] == "failed"


def test_a_log_that_did_not_record_its_repo_finds_it_in_worktree_paths():
    plan = {k: v for k, v in PLAN.items() if k != "repo"}
    _write("r11", ("", "", "plan", plan), _session("claude"),
           ("a", "claude", "tool_start", {"tool_id": "t1", "name": "Read", "input": {
               "file_path": "/work/app/.worktrees/subagent-control-r11-a/calc.py"}}))

    assert runs.run_view("r11", now=lambda: T0 + 30)["repo"] == "/work/app"


def test_codex_runs_name_their_repo_in_what_a_command_printed():
    """codex's tool inputs are shell commands; its output (pwd) holds the path."""
    plan = {k: v for k, v in PLAN.items() if k != "repo"}
    _write("r12", ("", "", "plan", plan), _session("codex"),
           ("a", "codex", "tool_start", {"tool_id": "i1", "name": "command_execution",
                                         "input": {"command": "/bin/zsh -lc pwd"}}),
           ("a", "codex", "tool_end", {"tool_id": "i1", "is_error": False,
                                       "content": "/work/app/.worktrees/subagent-control-r12-a\n"}))

    assert runs.run_view("r12", now=lambda: T0 + 30)["repo"] == "/work/app"


def test_paths_inside_the_task_worktree_are_shown_relative_to_it():
    """Seen in the first real watch: every step began with the same long prefix."""
    wt = "/work/app/.worktrees/subagent-control-r14-a"
    _write("r14", ("", "", "plan", PLAN), _session("claude"),
           ("a", "claude", "tool_start", {"tool_id": "t1", "name": "Edit",
                                          "input": {"file_path": f"{wt}/src/calc.py"}}),
           ("a", "claude", "text", {"text": f"Created [README.md]({wt}/README.md)."}))

    attempt = runs.run_view("r14", now=lambda: T0 + 10)["tasks"][0]["attempts"][0]

    assert attempt["steps"][0]["detail"] == "src/calc.py"
    assert attempt["last_text"] == "Created [README.md](README.md)."


def test_runs_are_listed_newest_first():
    _write("old1", ("", "", "plan", PLAN), *DONE_TAIL)
    _write("new2", ("", "", "plan", PLAN), _session("claude"))
    import os
    os.utime(RunLog("old1").path, (T0, T0))
    os.utime(RunLog("new2").path, (T0 + 100, T0 + 100))

    listed = runs.list_runs(now=lambda: T0 + 120)

    assert [r["run_id"] for r in listed] == ["new2", "old1"]
    assert listed[0]["tasks"] == [{"id": "a", "status": "running", "engine": "claude"}]


@pytest.mark.parametrize("engine, usage, expected", [
    ("claude", {"input_tokens": 100, "output_tokens": 20,
                "cache_read_input_tokens": 2058100, "cache_creation_input_tokens": 300,
                "output_tokens_details": {"thinking_tokens": 12}},
     {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 2058100,
      "cache_write_tokens": 300, "reasoning_tokens": 12, "cost_usd": 0.02, "ok": True}),
    ("codex", {"input_tokens": 120, "output_tokens": 30,
               "cached_input_tokens": 500, "cache_write_input_tokens": 40,
               "reasoning_output_tokens": 15},
     {"input_tokens": 120, "output_tokens": 30, "cached_input_tokens": 500,
      "cache_write_tokens": 40, "reasoning_tokens": 15, "cost_usd": 0.02, "ok": True}),
])
def test_result_keeps_engine_usage_details(engine, usage, expected):
    _write("usage", ("", "", "plan", PLAN), _session(engine),
           ("a", engine, "result", {"ok": True, "usage": usage, "total_cost_usd": 0.02}))

    result = runs.run_view("usage")["tasks"][0]["attempts"][0]["result"]

    assert result == expected


def test_run_stats_has_one_row_per_attempt_in_recent_run_order():
    plan = {**PLAN, "tasks": [PLAN["tasks"][0],
                              {**PLAN["tasks"][0], "id": "b", "model": "planned-b"}]}
    _write("stats", ("", "", "plan", plan), _session("claude"),
           ("a", "claude", "tool_start", {"tool_id": "t1", "name": "Bash", "input": {}}),
           ("a", "claude", "tool_end", {"tool_id": "t1", "is_error": True}),
           ("a", "claude", "result", {"usage": {"input_tokens": 100, "output_tokens": 10}}),
           ("a", "claude", "limit", {"next": "codex"}),
           ("a", "codex", "session", {"session_id": "Y"}),
           ("a", "codex", "result", {"usage": {"input_tokens": 200,
                                                   "cached_input_tokens": 50,
                                                   "output_tokens": 20,
                                                   "reasoning_output_tokens": 3},
                                     "total_cost_usd": 0.04}),
           ("a", "codex", "verify", {"passed": False}),
           ("b", "codex", "rerouted", {"from": "claude", "reason": "capacity"}),
           ("b", "codex", "session", {"session_id": "Z", "model": "reported-b"}),
           ("b", "codex", "result", {"usage": {"output_tokens": 5}}))

    rows = runs.run_stats()

    assert len(rows) == 3
    assert rows[0] == {"run_id": "stats", "task_id": "a", "planned_engine": "claude",
                       "engine": "claude", "planned_model": "sonnet", "model": "claude-model",
                       "model_recorded": True,
                       "effort": "low", "policy": "write", "how": "planned", "tools": 1,
                       "tool_errors": 1, "seconds": 3, "input_tokens": 100,
                       "cached_input_tokens": None, "output_tokens": 10,
                       "reasoning_tokens": None, "cost_usd": None, "status": "unproven",
                       "verify_passed": False, "final_attempt": False, "moved": False}
    assert rows[1]["how"] == "continued" and rows[1]["moved"] is True
    assert rows[1]["model"] is None and rows[1]["effort"] is None
    assert rows[1]["cached_input_tokens"] == 50
    assert rows[1]["reasoning_tokens"] == 3 and rows[1]["cost_usd"] == 0.04
    assert rows[1]["final_attempt"] is True
    assert rows[2]["task_id"] == "b" and rows[2]["model"] == "reported-b"
    assert rows[2]["moved"] is True and rows[2]["verify_passed"] is None
    assert rows[2]["final_attempt"] is True


def test_reroute_settings_apply_only_to_later_attempts_and_reported_model_wins():
    _write("resolved-move", ("", "", "plan", PLAN),
           ("a", "claude", "session", {"session_id": "X"}),
           ("a", "codex", "rerouted", {"from": "claude", "reason": "limit",
                                      "model": "gpt-5.6-sol", "effort": "high"}),
           ("a", "codex", "session", {"session_id": "Y"}),
           ("a", "codex", "rerouted", {"from": "claude", "reason": "retry",
                                      "model": "gpt-6-sol", "effort": "medium"}),
           ("a", "codex", "session", {"session_id": "Z", "model": "reported-model"}))

    attempts = runs.run_view("resolved-move")["tasks"][0]["attempts"]
    rows = runs.run_stats()
    sessions = [entry for entry in runs.task_trace("resolved-move", "a")
                if entry["type"] == "session"]

    assert [(a["model"], a["effort"]) for a in attempts] == [
        ("sonnet", "low"), ("gpt-5.6-sol", "high"), ("reported-model", "medium")]
    assert [(r["model"], r["effort"]) for r in rows] == [
        ("sonnet", "low"), ("gpt-5.6-sol", "high"), ("reported-model", "medium")]
    assert [(e["model"], e["effort"]) for e in sessions] == [
        ("sonnet", "low"), ("gpt-5.6-sol", "high"), ("reported-model", "medium")]


def test_old_reroute_without_settings_does_not_borrow_planned_values():
    _write("old-move", ("", "", "plan", PLAN),
           ("a", "codex", "rerouted", {"from": "claude", "reason": "capacity"}),
           ("a", "codex", "session", {"session_id": "Y"}))

    attempt = runs.run_view("old-move")["tasks"][0]["attempts"][0]
    row = runs.run_stats()[0]
    session = next(e for e in runs.task_trace("old-move", "a") if e["type"] == "session")
    assert (attempt["model"], attempt["effort"]) == (None, None)
    assert (row["model"], row["effort"]) == (None, None)
    assert (session["model"], session["effort"]) == (None, None)


def test_run_stats_limits_recent_runs_not_attempts():
    _write("old", ("", "", "plan", PLAN), _session("claude"))
    _write("new", ("", "", "plan", PLAN), _session("claude"),
           ("a", "codex", "session", {"session_id": "Y"}))
    import os
    os.utime(RunLog("old").path, (T0, T0))
    os.utime(RunLog("new").path, (T0 + 100, T0 + 100))

    assert [(row["run_id"], row["engine"]) for row in runs.run_stats(1)] == [
        ("new", "claude"), ("new", "codex")]


@pytest.mark.parametrize("run_id", ["../etc", "a/b", "", "x" * 80])
def test_a_run_id_that_is_not_a_plain_name_is_never_read(run_id):
    assert runs.run_view(run_id) is None


# --- what dispatch records for the view ------------------------------------------

def test_dispatch_records_the_repo_the_planner_session_and_a_closing_summary(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "ok"), ScriptedEngine("codex"))

    dispatch([{"id": "a", "goal": "g", "engine": "claude", "why": "w", "policy": "write"}],
             parent_cwd=str(repo), run_id="r9", planner_session="S-9")

    events = RunLog("r9").read_all()
    plan = events[0].payload
    assert plan["repo"] == str(repo) and plan["planner"]["session_id"] == "S-9"
    # The summary settles the run; the planner's own "returned" note follows it.
    summary = next(e for e in reversed(events) if e.kind == "summary")
    assert summary.payload["tasks"][0]["task_id"] == "a"
    assert events[-1].kind == "planner" and events[-1].payload["phase"] == "returned"
    assert runs.run_view("r9")["tasks"][0]["status"] in ("unproven", "done")


def test_the_tool_passes_the_planners_session_to_dispatch(monkeypatch):
    from hermes_control import control_dispatch_handler

    seen = {}
    monkeypatch.setattr("hermes_control.dispatch.start_dispatch",
                        lambda tasks, **kw: seen.update(kw) or {"run_id": "x", "tasks": []})

    control_dispatch_handler({"tasks": []}, session_id="S-7", task_id="t")

    assert seen["planner_session"] == "S-7"


def test_task_trace_includes_slot_wait_notices():
    waiting = {"engine": "claude", "state": "waiting"}
    acquired = {"engine": "claude", "state": "acquired", "seconds": 1.2}
    _write("slot-notices", ("", "", "plan", PLAN),
           ("a", "claude", "slot_wait", waiting),
           ("a", "claude", "slot_wait", acquired))
    notices = [e for e in runs.task_trace("slot-notices", "a") if e["type"] == "slot_wait"]
    assert [e["data"] for e in notices] == [waiting, acquired]
