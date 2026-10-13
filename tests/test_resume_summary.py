"""Regression coverage for resumed results and parallel run reporting."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import hermes_control.dispatch as dispatch
from hermes_control import overlap, pauses, review_pack, runs, slots
from hermes_control.events import RawEvent
from hermes_control.runlog import RunLog, run_log_dir
from tests.scripted_engine import _git, _reading, repo  # noqa: F401
from tests.test_dispatch_parallel import configure, engines, gate, task  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    monkeypatch.setattr(dispatch, "_THREADS", {})
    monkeypatch.setattr(dispatch, "_FINISHED", {})


def entry(name, **changes):
    return {**dispatch._failed_entry(pauses.task_from_dict(task(name)), "paused"), **changes}


def save_pause(repo, run_id="resume", name="a"):
    record = pauses.Pause(
        id=f"{run_id}-{name}", run_id=run_id, task=task(name), repo=str(repo),
        worktree=None, engine="codex", session_id=None, reason="quota",
        resets_at=None, paused_at=time.time())
    pauses.save(record)
    return record


def append(log, kind, payload, *, now=None, task_id=""):
    log.append_next(task_id, [RawEvent(kind=kind, payload=payload)], engine="codex",
                    now=time.time() if now is None else now)


def test_resume_refused_until_background_run_finishes(repo, monkeypatch, engines):
    configure(monkeypatch, max_tasks=1)
    started, release = gate(engines["codex"], "b")
    original_summary = dispatch._log_summary
    summary_written, settle = threading.Event(), threading.Event()

    def hold_summary(log, run_id, entries, repo):
        result = original_summary(log, run_id, entries, repo)
        if [e["task_id"] for e in entries] == ["b"]:
            summary_written.set()
            assert settle.wait(10)
        return result

    monkeypatch.setattr(dispatch, "_log_summary", hold_summary)
    result = dispatch.start_dispatch([task("b")], parent_cwd=str(repo), wait=0)
    record = save_pause(repo, result["run_id"])
    try:
        assert started.wait(5)
        # Even a summary on disk must not override a still-live local thread.
        release.set()
        assert summary_written.wait(5)
        with pytest.raises(dispatch.NotReady, match=(
                "^its run is still in progress; resume it after the run finishes$")):
            dispatch.resume_paused(record.id)
        assert pauses.load(record.id) is not None
        assert all(t.id != "a" for t in engines["codex"].tasks)
    finally:
        release.set()
        settle.set()
        dispatch.control_status(result["run_id"], wait=10)
    monkeypatch.setattr(dispatch, "_log_summary", original_summary)
    assert dispatch.resume_paused(record.id)["commit"]


def test_log_only_live_run_refuses_resume_but_stale_run_allows_it(repo, monkeypatch, engines):
    monkeypatch.setattr(dispatch, "fcntl", None)  # the staleness rule is the no-fcntl fallback
    record = save_pause(repo)
    log = RunLog(record.run_id)
    old = time.time() - runs.STALE_SECONDS - 60
    append(log, "plan", {"tasks": [task("a")]}, now=old)
    append(log, "paused", {"id": record.id}, task_id="a")
    assert record.run_id not in dispatch._THREADS
    with pytest.raises(dispatch.NotReady, match=(
            "^its run is still in progress; resume it after the run finishes$")):
        dispatch.resume_paused(record.id)
    assert pauses.load(record.id) is not None
    assert engines["codex"].tasks == []
    # Staleness depends on the last event, not on when the run began.
    log.path.unlink()
    append(log, "plan", {"tasks": [task("a")]}, now=old)
    append(log, "paused", {"id": record.id}, task_id="a", now=old)
    assert dispatch.resume_paused(record.id)["commit"]


def test_concurrent_resumes_merge_under_per_run_flock(repo, monkeypatch, engines):
    fcntl = pytest.importorskip("fcntl")
    configure(monkeypatch)
    _reading(monkeypatch, "claude", blocked=True)
    _reading(monkeypatch, "codex", blocked=True)
    result = dispatch.start_dispatch([task("a"), task("b")], parent_cwd=str(repo), wait=10)
    ids = [e["paused"]["id"] for e in result["tasks"]]
    _reading(monkeypatch, "codex", 5)
    first_read, release, second_waiting = (threading.Event() for _ in range(3))
    local = threading.local()
    original_read, original_summary, original_flock = (
        RunLog.read_all, dispatch._log_resume_summary, fcntl.flock)

    def resume_summary(log, run_id, entries, repo):
        local.task = entries[0]["task_id"]
        return original_summary(log, run_id, entries, repo)

    def read(log):
        events = original_read(log)
        if getattr(local, "task", None) == "a":
            first_read.set()
            assert release.wait(10)
        return events

    def flock(handle, operation):
        if (getattr(local, "task", None) == "b" and operation == fcntl.LOCK_EX
                and Path(handle.name).name == f"run-{result['run_id']}.summary.lock"):
            try:
                original_flock(handle, operation | fcntl.LOCK_NB)
            except BlockingIOError:
                second_waiting.set()
        return original_flock(handle, operation)

    monkeypatch.setattr(dispatch, "_log_resume_summary", resume_summary)
    monkeypatch.setattr(RunLog, "read_all", read)
    monkeypatch.setattr(fcntl, "flock", flock)
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(dispatch.resume_paused, ids[0])
        try:
            assert first_read.wait(5)
            b = pool.submit(dispatch.resume_paused, ids[1])
            waited = second_waiting.wait(5)
        finally:
            release.set()
        resumed = [a.result(timeout=10), b.result(timeout=10)]
    assert waited, "the second resume must wait on the run's summary file lock"
    assert (run_log_dir() / f"run-{result['run_id']}.summary.lock").exists()
    latest = [e.payload for e in RunLog(result["run_id"]).read_all() if e.kind == "summary"][-1]
    expected = {e["task_id"]: e["commit"] for e in resumed}
    assert all(expected.values())
    assert {e["task_id"]: e["commit"] for e in latest["tasks"]} == expected
    assert {e["task_id"]: e["commit"] for e in dispatch._FINISHED[result["run_id"]]["tasks"]} == expected


def test_same_paused_record_cannot_resume_twice_at_once(repo, engines):
    pytest.importorskip("fcntl")
    record = save_pause(repo)
    started, release = gate(engines["codex"], "a")
    with ThreadPoolExecutor(1) as pool:
        first = pool.submit(dispatch.resume_paused, record.id)
        try:
            assert started.wait(5)
            with pytest.raises(dispatch.NotReady, match="^this paused task is already resuming$"):
                dispatch.resume_paused(record.id)
            assert len(engines["codex"].tasks) == 1
        finally:
            release.set()
        assert first.result(timeout=10)["commit"]


def test_resume_preserves_memory_overlap_when_original_summary_was_lost(repo, monkeypatch, engines):
    configure(monkeypatch, max_tasks=1)
    _reading(monkeypatch, "claude", blocked=True)
    engines["codex"].by_task["c"] = "limit"
    original_append = RunLog.append_next

    def fail_summary(log, task_id, raws, **kwargs):
        if any(raw.kind == "summary" for raw in raws):
            raise OSError("summary unavailable")
        return original_append(log, task_id, raws, **kwargs)

    monkeypatch.setattr(RunLog, "append_next", fail_summary)
    tasks = [task(n) for n in ("a", "b", "c")]
    tasks[-1]["verify"] = "test -s other.txt"
    result = dispatch.start_dispatch(tasks, parent_cwd=str(repo), wait=10)
    expected = [{"tasks": ["a", "b"], "base_commit": _git(["rev-parse", "HEAD"], repo),
                 "files": ["work.txt"]}]
    assert result["overlaps"] == expected
    assert not any(e.kind == "summary" for e in RunLog(result["run_id"]).read_all())
    monkeypatch.setattr(RunLog, "append_next", original_append)
    assert any(e.kind == "planner" and e.payload == {"phase": "settled"}
               for e in RunLog(result["run_id"]).read_all())
    _reading(monkeypatch, "codex", 5)
    original_argv = engines["codex"].build_argv

    def unrelated(t, *, cwd):
        args = original_argv(t, cwd=cwd)
        args[-1] = "import os; os.unlink('work.txt');" + args[-1].replace("work.txt", "other.txt")
        return args

    monkeypatch.setattr(engines["codex"], "build_argv", unrelated)
    resumed = dispatch.resume_paused(result["tasks"][-1]["paused"]["id"])
    assert resumed["commit"]
    assert dispatch.control_status(result["run_id"])["overlaps"] == expected
    latest = [e.payload for e in RunLog(result["run_id"]).read_all() if e.kind == "summary"][-1]
    assert latest["overlaps"] == expected
    assert [e["task_id"] for e in latest["tasks"]] == ["a", "b", "c"]


@pytest.mark.parametrize("append_fails", [False, True])
def test_unreadable_log_still_updates_finished(monkeypatch, caplog, append_fails):
    log = RunLog("unreadable")
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a"), entry("b")]}

    def fail(*args, **kwargs):
        raise OSError("log unavailable")

    monkeypatch.setattr(log, "read_all", fail)
    if append_fails:
        monkeypatch.setattr(log, "append_next", fail)
    resumed = entry("a", ok=True, text="resumed result")
    dispatch._log_resume_summary(log, log.run_id, [resumed], None)
    assert dispatch._FINISHED[log.run_id]["tasks"] == [resumed, entry("b")]
    assert any(r.levelname == "WARNING" and "log" in r.message for r in caplog.records)


def test_merge_ignores_missing_task_id_and_overlays_log_then_resume():
    log = RunLog("old-summary")
    dispatch._FINISHED[log.run_id] = {"tasks": [entry("a", text="memory"), entry("b")]}
    append(log, "summary", {"tasks": [{"text": "legacy"}, entry("a", text="log"),
                                      entry("c")]})
    resumed = entry("b", ok=True, text="resumed")
    dispatch._log_resume_summary(log, log.run_id, [resumed], None)
    expected = [entry("a", text="log"), resumed, entry("c")]
    assert dispatch._FINISHED[log.run_id]["tasks"] == expected
    latest = log.read_all()[-1].payload["tasks"]
    assert [(e["task_id"], e["text"]) for e in latest] == [("a", "log"), ("b", "resumed"), ("c", "")]


def test_slot_freed_after_one_poll_reports_acquired(monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    directory = run_log_dir() / "slots" / "codex"
    directory.mkdir(parents=True)
    ticks = iter([0.0, 0.5])
    monkeypatch.setattr(slots, "monotonic", lambda: next(ticks))
    waits, polls = [], []
    with (directory / "slot-0.lock").open("a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)

        def free_slot(seconds):
            polls.append(seconds)
            fcntl.flock(holder, fcntl.LOCK_UN)

        monkeypatch.setattr(slots, "sleep", free_slot)
        with slots.engine_slot("codex", 1, on_wait=waits.append):
            assert polls == [0.5]
    assert waits == [{"engine": "codex", "state": "waiting"},
                     {"engine": "codex", "state": "acquired", "seconds": 0.5}]


def test_parallel_error_collapses_whitespace_before_truncation(monkeypatch):
    message = "bad config\n\t  next line\r\n" + "x" * 400

    def fail():
        raise ValueError(message)

    monkeypatch.setattr(dispatch.parallel, "read_parallel", fail)
    settings, error = dispatch._parallel()
    assert settings == dispatch.parallel.parse_parallel(None)
    assert error == ("ValueError: bad config next line " + "x" * 400)[:300]


def test_no_repo_warning_only_when_committed_entries_exist(caplog):
    for entries in ([], [{"task_id": "a"}], [{"base_commit": "base"}], [{"commit": "sha"}]):
        assert overlap.find_overlaps(None, entries) == []
    assert not caplog.records
    assert overlap.find_overlaps(None, [{"task_id": "a", "base_commit": "base", "commit": "sha"}]) == []
    assert "cannot find overlaps without a repo" in caplog.text


def test_review_pack_reuses_evidence_read_for_plan(monkeypatch):
    log = RunLog("review-once")
    parallel = {"max_tasks": 1, "per_engine": {"codex": 1, "claude": 1}}
    append(log, "plan", {"parallel": parallel, "tasks": [task("a")]})
    append(log, "text", {"text": "final result"}, task_id="a")
    # Exclude the view/stats readers so this counts only build's evidence reads.
    view = runs.run_view(log.run_id)
    monkeypatch.setattr(runs, "run_view", lambda run_id: view)
    monkeypatch.setattr(runs, "run_stats", lambda **kwargs: [])
    original_read, reads = RunLog.read_all, []

    def read(log):
        reads.append(log.run_id)
        return original_read(log)

    monkeypatch.setattr(RunLog, "read_all", read)
    pack = review_pack.build(log.run_id)
    assert pack["parallel"] == parallel
    assert pack["tasks"][0]["worker_final_text"] == "final result"
    assert reads == [log.run_id]
