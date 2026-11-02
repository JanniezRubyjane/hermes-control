"""Review HTTP routes use private storage and the shared browser origin guard."""

import importlib.util
import json
from pathlib import Path
import subprocess
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog, run_log_dir

PREFIX = "/api/plugins/hermes_control"
URL = PREFIX + "/runs/RUN_ID/tasks/task/review"


@pytest.fixture
def api(diagram_repo, monkeypatch):
    from hermes_cli.dashboard_auth import prefix
    monkeypatch.setattr(prefix, "resolve_public_url", lambda: "")
    def git(*args):
        return subprocess.run(["git", "-C", str(diagram_repo), *args], check=True,
                              capture_output=True, text=True).stdout.strip()
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "task-branch")
    (diagram_repo / "app.py").write_text("def start():\n    return 2\n\nstart()\n")
    git("add", "app.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.org", "commit", "-qm", "change")
    sha = git("rev-parse", "HEAD")
    log = RunLog("RUN_ID")
    events = [("", "plan", {"repo": str(diagram_repo), "tasks": [{"id": "task", "engine": "codex"}]}),
              ("task", "commit", {"sha": sha, "branch": "task-branch"}),
              ("", "summary", {"tasks": [{"task_id": "task", "branch": "task-branch", "base_commit": base}]})]
    for task, kind, payload in events:
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id="RUN_ID", task_id=task,
                         engine="codex", start_seq=log.next_seq(task), now=1790000000))
    path = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("hc_review_notes_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    with TestClient(app) as client:
        yield client, sha, diagram_repo, git


def body(version=0, text="Fix this"):
    return {"version": version, "comments": [{"path": "app.py", "side": "new", "line": 2, "text": text}]}


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("run,task", [("missing", "task"), ("RUN_ID", "missing")])
def test_unknown_run_or_task_is_404(api, method, run, task):
    client, *_ = api
    response = client.request(method, PREFIX + f"/runs/{run}/tasks/{task}/review",
                              **({"json": body()} if method == "PUT" else {}))
    assert response.status_code == 404


def test_get_put_edit_delete_and_conflict(api):
    client, sha, *_ = api
    assert client.get(URL).json() == {"version": 0, "commit": sha, "comments": [], "note": ""}
    response = client.put(URL, json=body())
    assert response.status_code == 200
    result = response.json()
    assert result["version"] == 1 and result["commit"] == sha
    assert result["note"] == ("Review comments on hermes-control task task (run RUN_ID)\n"
                              f"branch task-branch at commit {sha[:12]} (not merged into main):\n"
                              "- app.py:2 (new) — Fix this")
    assert client.get(URL).json() == result
    assert client.put(URL, json=body()).status_code == 409
    assert client.get(URL).json() == result
    item = result["comments"][0]
    edited = body(1, "Edited")
    edited["comments"][0]["id"] = item["id"]
    response = client.put(URL, json=edited)
    assert response.status_code == 200
    assert response.json()["comments"][0]["created_at"] == item["created_at"]
    assert client.put(URL, json={"version": 2, "comments": []}).json() == {
        "version": 3, "commit": sha, "comments": [], "note": ""}
    assert (run_log_dir() / "reviews/RUN_ID/task.json").is_file()


def test_review_survives_branch_prune_and_fake_paths_are_refused(api):
    client, _, _, git = api
    assert client.put(URL, json=body()).status_code == 200
    git("checkout", "--detach")
    git("branch", "-D", "task-branch")
    assert client.get(URL.removesuffix("review") + "diff").json()["error"] == "branch gone"
    assert client.get(URL).json()["comments"]
    assert client.put(URL, json=body(1, "After prune")).status_code == 200
    fake = body(2)
    fake["comments"][0]["path"] = "fake.py"
    assert client.put(URL, json=fake).status_code == 422


@pytest.mark.parametrize("method", ["GET", "PUT"])
def test_foreign_origin_is_403(api, method):
    client, *_ = api
    response = client.request(method, URL, json=body(), headers={"Origin": "http://localhost:5173"})
    assert response.status_code == 403


@pytest.mark.parametrize("data", ["{", "null", "[]", '{"version":true,"comments":[]}',
                                   json.dumps(body(text="secret-input"), indent=2) + "x",
                                   " " * (256 * 1024) + "{}", "[" * 2000 + "0" + "]" * 2000])
def test_bad_or_oversized_raw_body_is_generic_422(api, data):
    client, *_ = api
    response = client.put(URL, content=data, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid review"}


def test_body_limit_counts_raw_bytes_even_with_valid_small_json(api):
    client, *_ = api
    data = " " * (256 * 1024) + json.dumps(body())
    assert client.put(URL, content=data).status_code == 422
    assert client.get(URL).json()["version"] == 0


@pytest.mark.parametrize("field,value", [("path", "fake.py"), ("side", "ctx"), ("line", True),
                                         ("line", 10_000_001), ("text", " "), ("text", "x" * 2001)])
def test_validation_maps_to_generic_422(api, field, value):
    client, *_ = api
    data = body()
    data["comments"][0][field] = value
    response = client.put(URL, json=data)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid review"}


def test_put_runs_off_the_event_loop_thread(api, monkeypatch):
    from hermes_control import review_notes
    client, *_ = api
    loop_thread = client.portal.call(threading.current_thread)
    threads = []
    original = review_notes.put

    def put(*args):
        threads.append(threading.current_thread())
        return original(*args)

    monkeypatch.setattr(review_notes, "put", put)
    assert client.put(URL, json=body()).status_code == 200
    assert len(threads) == 1
    assert threads[0] is not loop_thread


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("raw", ["{", "null", "[]", "{}",
    '{"version":1,"run_id":"RUN_ID","task_id":"task","commit":null,"comments":[{}]}'])
def test_damaged_review_file_is_generic_422(api, method, raw):
    client, *_ = api
    assert client.put(URL, json=body()).status_code == 200
    (run_log_dir() / "reviews/RUN_ID/task.json").write_text(raw)
    response = client.request(method, URL, **({"json": body(1)} if method == "PUT" else {}))
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid review"}
