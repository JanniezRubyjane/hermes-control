"""Settled runs, cross-process resumes, and shared slot regressions."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hermes_control.dispatch as dispatch
from hermes_control import runs, slots
from hermes_control.runlog import RunLog, run_log_dir
from tests.scripted_engine import _reading, repo  # noqa: F401
from tests.test_dispatch_parallel import configure, engines, gate, task  # noqa: F401
from tests.test_resume_summary import append, entry, save_pause  # noqa: F401


@pytest.mark.parametrize("background", [False, True])
def test_failed_summary_still_settles_and_resumes_without_waiting(repo, monkeypatch, engines, background):
    _reading(monkeypatch, "codex", blocked=True)
    _reading(monkeypatch, "claude", blocked=True)
    original = RunLog.append_next

    def fail_summary(log, task_id, raws, **kwargs):
        if any(raw.kind == "summary" for raw in raws):
            raise OSError("summary unavailable")
        return original(log, task_id, raws, **kwargs)

    monkeypatch.setattr(RunLog, "append_next", fail_summary)
    result = (dispatch.start_dispatch([task("a")], parent_cwd=str(repo), wait=10)
              if background else dispatch.dispatch([task("a")], parent_cwd=str(repo)))
    events = RunLog(result["run_id"]).read_all()
    assert not any(e.kind == "summary" for e in events)
    settled = next(e for e in events if e.kind == "planner" and e.payload == {"phase": "settled"})
    returned = next(e for e in events if e.kind == "planner" and e.payload["phase"] == "returned")
    assert settled.seq < returned.seq
    # The dashboard owns neither of the Hermes process's registries.
    dispatch._FINISHED.clear()
    dispatch._THREADS.clear()
    monkeypatch.setattr(RunLog, "append_next", original)
    _reading(monkeypatch, "codex", 5)
    assert dispatch.resume_paused(result["tasks"][0]["paused"]["id"])["commit"]


@pytest.mark.parametrize("evidence", ["settled", "returned", "memory", "empty"])
def test_resume_accepts_finished_evidence(repo, monkeypatch, engines, evidence):
    record = save_pause(repo)
    log = RunLog(record.run_id)
    if evidence != "empty":
        append(log, "plan", {"tasks": [task("a")]})
    if evidence in ("settled", "returned"):
        append(log, "planner", {"phase": evidence})
    elif evidence == "memory":
        dispatch._FINISHED[record.run_id] = {"tasks": [], "_summary_seq": None}
    else:
        log.path.touch()
    assert dispatch.resume_paused(record.id)["commit"]


def test_status_polls_do_not_extend_worker_staleness(repo, monkeypatch, engines):
    record = save_pause(repo)
    log = RunLog(record.run_id)
    old = time.time() - runs.STALE_SECONDS - 1
    append(log, "plan", {"tasks": [task("a")]}, now=old)
    append(log, "paused", {"id": record.id}, task_id="a", now=old)
    for _ in range(3):
        dispatch.control_status(record.run_id)
    assert dispatch.resume_paused(record.id)["commit"]


def test_live_background_run_refuses_resume(repo, monkeypatch, engines):
    started, release = gate(engines["codex"], "b")
    result = dispatch.start_dispatch([task("b")], parent_cwd=str(repo), wait=0)
    record = save_pause(repo, result["run_id"])
    try:
        assert started.wait(5)
        with pytest.raises(dispatch.NotReady, match="still in progress"):
            dispatch.resume_paused(record.id)
        assert all(t.id != "a" for t in engines["codex"].tasks)
    finally:
        release.set()
        dispatch.control_status(result["run_id"], wait=10)


def test_unreadable_resume_log_is_not_ready(repo, monkeypatch, engines):
    record = save_pause(repo)

    def fail(log):
        raise OSError("unreadable")

    monkeypatch.setattr(RunLog, "read_all", fail)
    with pytest.raises(dispatch.NotReady, match="^could not read its run log: unreadable$"):
        dispatch.resume_paused(record.id)
    assert engines["codex"].tasks == []


@pytest.mark.parametrize("append_fails", [False, True])
def test_finished_tracks_persisted_summary_sequence(repo, monkeypatch, engines, append_fails):
    original_open = Path.open

    def fail_log_open(path, mode="r", *args, **kwargs):
        if append_fails and path.suffix == ".jsonl" and mode == "a":
            raise OSError("disk unavailable")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_log_open)
    result = dispatch.start_dispatch([task("a")], parent_cwd=str(repo), wait=10)
    summaries = [e for e in RunLog(result["run_id"]).read_all() if e.kind == "summary"]
    assert dispatch._FINISHED[result["run_id"]]["_summary_seq"] == (
        None if append_fails else summaries[-1].seq)
    assert "_summary_seq" not in result
    assert "_summary_seq" not in dispatch.control_status(result["run_id"])


def test_status_refreshes_after_dashboard_resume(repo, monkeypatch, engines):
    configure(monkeypatch, max_tasks=1)
    _reading(monkeypatch, "claude", blocked=True)
    engines["codex"].by_task["b"] = "limit"
    result = dispatch.start_dispatch([task("a"), task("b")], parent_cwd=str(repo), wait=10)
    run_id = result["run_id"]
    old = dispatch._FINISHED[run_id]
    assert old["tasks"][1]["paused"]
    _reading(monkeypatch, "codex", 5)
    with monkeypatch.context() as dashboard:
        dashboard.setattr(dispatch, "_FINISHED", {})
        dashboard.setattr(dispatch, "_THREADS", {})
        resumed = dispatch.resume_paused(result["tasks"][1]["paused"]["id"])
    assert dispatch._FINISHED[run_id] is old
    status = dispatch.control_status(run_id)
    assert status["tasks"][1]["paused"] is None
    assert status["tasks"][1]["commit"] == resumed["commit"]
    assert status["tasks"][1]["unproven_reason"] is None
    assert status["tasks"][1]["ok"]
    assert status["overlaps"][0]["tasks"] == ["a", "b"]
    assert dispatch._FINISHED[run_id]["tasks"] == status["tasks"]
    summaries = [e for e in RunLog(run_id).read_all() if e.kind == "summary"]
    assert dispatch._FINISHED[run_id]["_summary_seq"] == summaries[-1].seq
    assert "_summary_seq" not in status


def test_failed_first_resume_append_does_not_restore_older_results(monkeypatch):
    log = RunLog("two-resumes")
    dispatch._log_summary(log, log.run_id, [entry("a"), entry("b")], None)
    seq = log.read_all()[-1].seq
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a"), entry("b")], "_summary_seq": seq}
    original = log.append_next

    def fail(*args, **kwargs):
        raise OSError("first resume summary lost")

    monkeypatch.setattr(log, "append_next", fail)
    first = entry("a", ok=True, text="first resumed result", paused=None)
    dispatch._log_resume_summary(log, log.run_id, [first], None)
    monkeypatch.setattr(log, "append_next", original)
    second = entry("b", ok=True, text="second resumed result", paused=None)
    dispatch._log_resume_summary(log, log.run_id, [second], None)
    assert dispatch._FINISHED[log.run_id]["tasks"] == [first, second]
    assert [e["text"] for e in log.read_all()[-1].payload["tasks"]] == [first["text"], second["text"]]


@pytest.mark.parametrize("failure", ["open", "flock"])
def test_summary_lock_oserror_still_records_resume(monkeypatch, caplog, failure):
    fcntl = pytest.importorskip("fcntl")
    log = RunLog("lock-error")
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a")], "_summary_seq": None}
    original_open, original_flock = Path.open, fcntl.flock

    def open_file(path, *args, **kwargs):
        if failure == "open" and path.name.endswith(".summary.lock"):
            raise OSError("lock unavailable")
        return original_open(path, *args, **kwargs)

    def flock(handle, operation):
        if failure == "flock" and operation == fcntl.LOCK_EX:
            raise OSError("lock unavailable")
        return original_flock(handle, operation)

    monkeypatch.setattr(Path, "open", open_file)
    monkeypatch.setattr(fcntl, "flock", flock)
    resumed = entry("a", ok=True, text="resumed")
    dispatch._log_resume_summary(log, log.run_id, [resumed], None)
    assert dispatch._FINISHED[log.run_id]["tasks"] == [resumed]
    assert log.read_all()[-1].payload["tasks"][0]["text"] == "resumed"
    assert any(r.levelname == "WARNING" and "lock" in r.message for r in caplog.records)


def test_new_compact_summary_replaces_old_details_and_normalizes_pause():
    log = RunLog("compact")
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a", model="old", verify={"passed": True})],
                                      "_summary_seq": 0}
    append(log, "planner", {"phase": "settled"})
    compact = {key: value for key, value in entry("a").items()
               if key not in ("verify", "model", "diff_stat", "paused")}
    compact["paused_id"] = "new-pause"
    append(log, "summary", {"tasks": [compact]})
    dispatch._log_resume_summary(log, log.run_id, [entry("b")], None)
    current = dispatch.control_status(log.run_id)["tasks"][0]
    assert current == {key: value for key, value in compact.items() if key != "paused_id"} | {
        "paused": {"id": "new-pause"}}
    assert "paused_id" not in current


def test_summary_exception_updates_finished_in_finally(monkeypatch):
    log = RunLog("summary-error")
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a")], "_summary_seq": None}

    def fail(*args, **kwargs):
        raise OSError("summary failed")

    monkeypatch.setattr(dispatch, "_log_summary", fail)
    resumed = entry("a", ok=True, text="resumed")
    try:
        dispatch._log_resume_summary(log, log.run_id, [resumed], None)
    except OSError:
        pass
    assert dispatch._FINISHED[log.run_id]["tasks"] == [resumed]


def test_usage_limit_releases_source_slot_before_waiting_for_destination(repo, monkeypatch, engines):
    fcntl = pytest.importorskip("fcntl")
    configure(monkeypatch, codex=1, claude=1)
    engines["codex"].by_task["a"] = "limit"
    waiting = threading.Event()
    original_log = dispatch._log

    def log(ctx, task_id, engine, kind, payload):
        original_log(ctx, task_id, engine, kind, payload)
        if task_id == "a" and engine == "claude" and kind == "slot_wait":
            waiting.set()

    monkeypatch.setattr(dispatch, "_log", log)
    holder_started, release_holder = gate(engines["claude"], "holder")
    with ThreadPoolExecutor(2) as pool:
        holder = pool.submit(dispatch.dispatch, [task("holder", "claude")], parent_cwd=str(repo))
        try:
            assert holder_started.wait(5)
            switched = pool.submit(dispatch.dispatch, [task("a")], parent_cwd=str(repo))
            assert waiting.wait(5), "continuation should wait for the occupied Claude slot"
            # Nonblocking probe bounds failure even if a regression retains this lock.
            with (run_log_dir() / "slots/codex/slot-0.lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            release_holder.set()
        assert holder.result(timeout=10)["tasks"][0]["commit"]
        result = switched.result(timeout=10)
    assert result["tasks"][0]["engine"] == "claude"
    assert result["tasks"][0]["commit"]


def test_concurrent_runs_with_different_limits_share_slot_files(repo, monkeypatch, engines):
    pytest.importorskip("fcntl")
    configure(monkeypatch, codex=2)
    large_started, release_large = gate(engines["codex"], "large")
    small_started, release_small = gate(engines["codex"], "small-a")
    small_b_started, release_small_b = gate(engines["codex"], "small-b")
    waiting = threading.Event()
    original_slot = slots.engine_slot
    limits_seen = []

    def slot(engine, limit, *, on_wait):
        limits_seen.append(limit)

        def report(payload):
            on_wait(payload)
            if limit == 1 and payload["state"] == "waiting":
                waiting.set()

        return original_slot(engine, limit, on_wait=report, poll=0.01)

    monkeypatch.setattr(slots, "engine_slot", slot)
    with ThreadPoolExecutor(2) as pool:
        large = pool.submit(dispatch.dispatch, [task("large")], parent_cwd=str(repo))
        try:
            assert large_started.wait(5)
            configure(monkeypatch, codex=1)
            small = pool.submit(dispatch.dispatch, [task("small-a"), task("small-b")],
                                parent_cwd=str(repo))
            assert waiting.wait(5), "the smaller run must contend on the same slot-0 file"
            assert not small_started.is_set() and not small_b_started.is_set()
            release_large.set()
            assert small_started.wait(5)
            assert not small_b_started.wait(0.2)
            release_small.set()
            assert small_b_started.wait(5)
        finally:
            release_large.set()
            release_small.set()
            release_small_b.set()
        assert large.result(timeout=10)["tasks"][0]["commit"]
        assert all(e["commit"] for e in small.result(timeout=10)["tasks"])
    assert limits_seen == [2, 1, 1]


def test_status_rejects_invalid_run_id_before_reading_logs(monkeypatch):
    def unexpected_read(run_id):
        raise AssertionError("invalid run id reached the filesystem")

    monkeypatch.setattr(dispatch, "RunLog", unexpected_read)
    assert dispatch.control_status("../outside") == {"error": "no run ../outside"}
