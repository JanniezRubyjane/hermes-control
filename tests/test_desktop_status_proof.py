"""Desktop proof results through HTTP, with synthetic usage and run views."""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


URL = "/api/plugins/hermes_control/status"


def reading(percent=None, **overrides):
    return {"windows": [] if percent is None else [
                {"label": "weekly", "used_percent": percent, "resets_at": 300.0}],
            "cap": 80, "paused": False, "blocked": False, "blocked_until": None,
            "held_back": None, "held_until": None, "report_only": False,
            "account": "hidden@invalid", **overrides}


def task(task_id, status, policy="write", verdict=None):
    return {"id": task_id, "policy": policy, "status": status,
            "engine": "codex",
            "summary": {"verdict": verdict} if verdict is not None else None}


def run(run_id="run-1", state="finished", tasks=()):
    return {"run_id": run_id, "state": state, "updated_at": 200.0,
            "started_at": 100.0, "tasks": list(tasks)}


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location(f"desktop_proof_api_{id(tmp_path)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = {name: reading() for name in module.ENGINE_NAMES}
    recent, views, calls = [], {}, []
    caps = {name: 80 for name in rows}
    monkeypatch.setattr(module.limits, "read_caps", lambda: caps)

    def describe(name, *, caps, refresh):
        assert refresh is False
        return rows[name]

    def list_runs(*, limit):
        assert limit == 5
        return recent

    def run_view(run_id):
        calls.append(run_id)
        return views[run_id]

    def forbidden(*args, **kwargs):
        pytest.fail("status must not probe engine state or adapters")

    monkeypatch.setattr(module.limits, "describe", describe)
    monkeypatch.setattr(module.runs, "list_runs", list_runs)
    monkeypatch.setattr(module.runs, "run_view", run_view)
    monkeypatch.setattr(module, "engine_state", forbidden)
    monkeypatch.setattr(module, "get_adapter", forbidden)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes_control")
    return SimpleNamespace(module=module, client=TestClient(app), rows=rows,
                           recent=recent, views=views, calls=calls)


def add_run(api, tasks, *, state="finished"):
    api.recent.append(run(state=state, tasks=tasks))
    api.views["run-1"] = {"tasks": tasks}


def unproven_chip():
    return {"kind": "unproven", "engine": None, "percent": None, "window": None,
            "resets_at": None, "run_id": "run-1", "task_id": "task-1",
            "started_at": None}


def test_no_runs(api):
    body = api.client.get(URL).json()
    assert body["proof"] is None
    assert body["last_run"] is None
    assert body["chip"]["kind"] == "idle"
    assert api.calls == []


def test_reviews_never_count(api):
    add_run(api, [task("review-1", "failed", "read-only"),
                  task("review-2", "done", "read-only", "red-green-proven"),
                  task("review-3", "unproven", "read-only"),
                  task("review-4", "stopped", "read-only")])
    body = api.client.get(URL).json()
    assert body["proof"] == {"run_id": "run-1", "written": 0, "proven": 0,
                             "red_green": 0, "not_proven": []}
    assert body["chip"]["kind"] == "idle"


@pytest.mark.parametrize("percent, kind", [(20.0, "idle"), (65.0, "near")])
def test_all_written_done_preserves_usage_chip(api, percent, kind):
    add_run(api, [task("task-1", "done", verdict="red-green-proven"),
                  task("task-2", "done", verdict="tests-passed")])
    api.rows["codex"] = reading(percent)
    body = api.client.get(URL).json()
    assert body["proof"] == {"run_id": "run-1", "written": 2, "proven": 2,
                             "red_green": 1, "not_proven": []}
    assert body["chip"] == {
        "kind": kind, "engine": "codex", "percent": percent,
        "window": "weekly" if kind == "near" else None,
        "resets_at": 300.0 if kind == "near" else None,
        "run_id": None, "task_id": None, "started_at": None}


def test_mixed_verdicts_counts_order_and_response_allowlist(api):
    tasks = [task("task-8", "done", verdict="red-green-proven"),
             task("task-9", "done", verdict="tests-passed"),
             task("task-1", "unproven"), task("task-3", "failed"),
             task("task-2", "stopped"), task("review-1", "failed", "read-only")]
    add_run(api, tasks)
    api.views["run-1"].update(account="hidden@invalid", private="hidden@invalid")
    api.views["run-1"]["tasks"][0]["summary"]["private"] = "hidden@invalid"
    api.rows["codex"] = reading(20.0)
    response = api.client.get(URL)
    body = response.json()
    assert body["proof"] == {"run_id": "run-1", "written": 5, "proven": 2,
                             "red_green": 1,
                             "not_proven": ["task-1", "task-3", "task-2"]}
    assert body["chip"] == unproven_chip()
    assert body["last_run"] == {"run_id": "run-1", "state": "finished",
                                "updated_at": 200.0,
                                "task_ids": [t["id"] for t in tasks]}
    assert set(body) == {"chip", "engines", "last_run", "proof", "problem"}
    assert "account" not in response.text
    assert "@" not in response.text
    assert "private" not in response.text


@pytest.mark.parametrize("status", ["running", "waiting", "paused"])
def test_unfinished_written_tasks_are_not_flagged(api, status):
    add_run(api, [task("task-1", status, verdict="red-green-proven")])
    body = api.client.get(URL).json()
    assert body["proof"] == {"run_id": "run-1", "written": 1, "proven": 0,
                             "red_green": 1, "not_proven": []}
    assert body["chip"]["kind"] == "idle"


def test_older_running_run_beats_blocked_and_unproven(api):
    add_run(api, [task("task-1", "unproven")])
    api.recent.append(run("run-2", "running", [task("task-2", "running")]))
    api.rows["codex"] = reading(blocked=True)
    body = api.client.get(URL).json()
    assert body["proof"]["not_proven"] == ["task-1"]
    assert body["chip"] == {
        "kind": "running", "engine": "codex", "percent": None, "window": None,
        "resets_at": None, "run_id": "run-2", "task_id": "task-2",
        "started_at": 100.0}


@pytest.mark.parametrize("overrides, kind", [
    ({"blocked": True}, "blocked"),
    ({"held_back": "cap reached"}, "blocked"),
    ({"blocked": True, "report_only": True}, "unproven"),
    ({"blocked": True, "paused": True}, "unproven"),
    ({}, "unproven"),
])
def test_priority_against_engine_usage(api, overrides, kind):
    add_run(api, [task("task-1", "failed")])
    api.rows["codex"] = reading(65.0, **overrides)
    body = api.client.get(URL).json()
    assert body["chip"]["kind"] == kind
    if kind == "unproven":
        assert body["chip"] == unproven_chip()
    else:
        assert body["chip"]["engine"] == "codex"


def test_newest_running_state_never_gives_unproven(api):
    add_run(api, [task("task-1", "stopped")], state="running")
    body = api.client.get(URL).json()
    assert body["proof"]["not_proven"] == ["task-1"]
    assert body["chip"]["kind"] == "running"


def test_missing_run_view(api):
    add_run(api, [])
    api.views["run-1"] = None
    body = api.client.get(URL).json()
    assert body["proof"] is None
    assert body["problem"] is None
    assert body["last_run"]["run_id"] == "run-1"


@pytest.mark.parametrize("bad_caps", [False, True])
def test_run_view_failure_is_logged_and_keeps_route_available(api, monkeypatch, caplog, bad_caps):
    add_run(api, [])

    def fail_view(run_id):
        raise RuntimeError("hidden@invalid")

    def fail_caps():
        raise ValueError("invalid cap")

    monkeypatch.setattr(api.module.runs, "run_view", fail_view)
    if bad_caps:
        monkeypatch.setattr(api.module.limits, "read_caps", fail_caps)
    with caplog.at_level(logging.WARNING, logger=api.module.__name__):
        response = api.client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["proof"] is None
    expected = "could not read the last run: RuntimeError"
    assert body["problem"] == ("invalid cap; " + expected if bad_caps else expected)
    assert any(record.levelno == logging.WARNING and record.message == expected
               and record.exc_info is not None for record in caplog.records)
    assert "account" not in response.text
    assert "@" not in response.text


def test_only_newest_run_view_is_read_once(api):
    add_run(api, [task("task-1", "unproven")])
    api.recent.append(run("run-2"))
    body = api.client.get(URL).json()
    assert api.calls == ["run-1"]
    assert body["proof"]["run_id"] == body["last_run"]["run_id"] == "run-1"
