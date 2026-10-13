"""A live run is told by an OS lock on disk, not by a staleness guess."""

import select
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import hermes_control.dispatch as dispatch
from hermes_control import runs
from hermes_control.runlog import RunLog, run_log_dir
from tests.scripted_engine import _reading, repo  # noqa: F401
from tests.test_dispatch_parallel import engines, gate, task  # noqa: F401
from tests.test_resume_summary import append, entry, save_pause  # noqa: F401

fcntl = pytest.importorskip("fcntl")


def _two_hours_on(monkeypatch):
    real = time.time
    monkeypatch.setattr(dispatch.time, "time", lambda: real() + 7200)


def test_live_run_refuses_resume_even_two_hours_later(repo, monkeypatch, engines):
    started, release = gate(engines["codex"], "b")
    result = dispatch.start_dispatch([task("b")], parent_cwd=str(repo), wait=0)
    thread = dispatch._THREADS[result["run_id"]]
    record = save_pause(repo, result["run_id"])
    try:
        assert started.wait(5)
        dispatch._THREADS.clear()  # the dashboard is another process
        with monkeypatch.context() as late:
            _two_hours_on(late)
            with pytest.raises(dispatch.NotReady, match="still in progress"):
                dispatch.resume_paused(record.id)
        assert all(t.id != "a" for t in engines["codex"].tasks)
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    assert dispatch.resume_paused(record.id)["commit"]


def test_lock_held_by_another_process_refuses_resume(repo, monkeypatch, engines):
    record = save_pause(repo)
    path = run_log_dir() / f"run-{record.run_id}.live.lock"
    code = ("import fcntl, sys, time\n"
            f"h = open({str(path)!r}, 'a')\n"
            "fcntl.flock(h, fcntl.LOCK_EX)\n"
            "print('locked', flush=True)\n"
            "time.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert select.select([proc.stdout], [], [], 10)[0], "helper never locked the file"
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(dispatch.NotReady, match="^its run is still in progress"):
            dispatch.resume_paused(record.id)
    finally:
        proc.kill()
        proc.wait(10)
    assert dispatch.resume_paused(record.id)["commit"]


def test_dispatch_settles_even_when_the_run_raises(repo, monkeypatch, engines):
    def boom(ctx, routed):
        raise RuntimeError("boom")

    monkeypatch.setattr(dispatch, "_run_routed", boom)
    with pytest.raises(RuntimeError):
        dispatch.dispatch([task("a")], parent_cwd=str(repo), run_id="raises")
    phases = [e.payload.get("phase") for e in RunLog("raises").read_all() if e.kind == "planner"]
    assert "settled" in phases
    assert dispatch._run_is_live("raises") is False


def test_summary_seq_increases_in_file_order():
    first, second = RunLog("seqs"), RunLog("seqs")
    dispatch._log_summary(first, "seqs", [entry("a")], None)
    dispatch._log_summary(second, "seqs", [entry("a", text="dashboard")], None)
    dispatch._log_summary(first, "seqs", [entry("a", text="own")], None)
    seqs = [e.seq for e in RunLog("seqs").read_all() if e.kind == "summary"]
    assert len(seqs) == 3 and seqs == sorted(set(seqs))
    assert first.summary_seq == seqs[-1]


def test_status_refresh_keeps_finished_state_and_cached_fields(monkeypatch):
    log = RunLog("refresh")
    full_a = entry("a", diff_stat="1 file", verify={"passed": True}, ok=True)
    full_b = entry("b", diff_stat="2 files", verify={"passed": False}, ok=False)
    dispatch._FINISHED["refresh"] = {"tasks": [full_a, full_b], "_summary_seq": -1,
                                     "note": "kept"}
    compact = {key: full_a[key] for key in ("task_id", "engine", "ok", "commit", "branch",
                                            "worktree", "base_commit", "text",
                                            "unproven_reason", "rerouted")}
    compact.update(text="resumed", paused_id="p1")
    append(log, "summary", {"tasks": [compact]})
    monkeypatch.setattr(runs, "run_view", lambda run_id: {"state": "running", "tasks": []})
    status = dispatch.control_status("refresh")
    assert status["state"] == "finished" and status["note"] == "kept"
    by_id = {t["task_id"]: t for t in status["tasks"]}
    assert by_id["a"]["text"] == "resumed" and by_id["a"]["paused"] == {"id": "p1"}
    assert by_id["a"]["diff_stat"] == "1 file" and by_id["a"]["verify"] == {"passed": True}
    assert by_id["b"] == full_b
    seq = [e.seq for e in log.read_all() if e.kind == "summary"][-1]
    assert dispatch._FINISHED["refresh"]["_summary_seq"] == seq


def test_unreadable_log_does_not_hide_cached_result(monkeypatch):
    dispatch._FINISHED["cached"] = {"tasks": [entry("a")], "_summary_seq": None}

    def fail(log):
        raise OSError("unreadable")

    monkeypatch.setattr(RunLog, "read_all", fail)
    status = dispatch.control_status("cached")
    assert status["state"] == "finished" and status["tasks"] == [entry("a")]


def test_resume_summary_does_not_create_registry_entry():
    log = RunLog("dashboard")
    dispatch._log_resume_summary(log, "dashboard", [entry("a")], None)
    assert "dashboard" not in dispatch._FINISHED
    assert [e.kind for e in log.read_all()] == ["summary"]


def test_two_concurrent_probes_of_a_finished_run_both_see_it_over():
    # Two Resume clicks probe at once: one probe's lock must not make the other
    # read a finished run as live.
    with dispatch._live_lock_path("probes").open("a") as other:
        fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            assert dispatch._run_is_live("probes") is False
        finally:
            fcntl.flock(other, fcntl.LOCK_UN)


def test_summary_records_what_a_resumed_task_ran_and_proved():
    log = RunLog("fields")
    done = entry("a", ok=True, diff_stat="+3/-1", verify={"passed": True, "exit_code": 0},
                 model="claude-sonnet-5-5", effort="medium")
    dispatch._log_summary(log, "fields", [done], None)
    logged = next(e for e in log.read_all() if e.kind == "summary").payload["tasks"][0]
    assert logged["diff_stat"] == "+3/-1"
    assert logged["verify"] == {"passed": True, "exit_code": 0}
    assert (logged["model"], logged["effort"]) == ("claude-sonnet-5-5", "medium")


def test_compact_summary_omits_fields_an_older_entry_lacks():
    # A summary rewritten from an older log's compact entries must not write
    # explicit nulls that a process holding the full entry would merge over.
    log = RunLog("old-entry")
    old = {key: value for key, value in entry("a").items()
           if key not in ("diff_stat", "verify", "model", "effort")}
    dispatch._log_summary(log, "old-entry", [old], None)
    logged = next(e for e in log.read_all() if e.kind == "summary").payload["tasks"][0]
    assert not {"diff_stat", "verify", "model", "effort"} & set(logged)
