"""The Engines tab backend, through HTTP, plus the dashboard's own discovery and mount."""

import importlib.util
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_control import device_login
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.engines.registry import NOT_INSTALLED

DASHBOARD = Path(__file__).resolve().parents[1] / "hermes_control" / "dashboard"

FAKE_CLI = r"""#!/bin/sh
here=$(dirname "$0")
case "$1 $2" in
  "login status"|"auth status")
    if [ -f "$here/signed-in" ]; then echo "Logged in"; exit 0; fi
    echo "Not logged in"; exit 1 ;;
  "login --device-auth")
    echo "   https://auth.openai.com/codex/device"
    echo "   ABCD-EFGHI"
    while [ ! -f "$here/approve" ]; do sleep 0.1; done
    touch "$here/signed-in"; exit 0 ;;
esac
exit 2
"""


def _fake_cli(directory):
    directory.mkdir()
    script = directory / "cli"
    script.write_text(FAKE_CLI, encoding="utf-8")
    script.chmod(0o755)
    return script


def _adapter(base, script):
    class Fake(base):
        def probe(self):
            return EngineInfo(name=base.name, version=f"{base.name} 9.9", path=str(script)) \
                if script else None
    return Fake()


@pytest.fixture
def api(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        f"hc_plugin_api_{id(tmp_path)}", DASHBOARD / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    claude = _fake_cli(tmp_path / "claude")
    codex = _fake_cli(tmp_path / "codex")
    adapters = {"claude": _adapter(ClaudeAdapter, claude), "codex": _adapter(CodexAdapter, codex)}
    monkeypatch.setattr(module, "get_adapter", adapters.__getitem__)
    monkeypatch.setattr(module, "find_sandbox", lambda: str(codex))
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes_control")
    module.test_state = {"adapters": adapters, "claude": claude.parent, "codex": codex.parent}
    yield module, TestClient(app)
    for session_id, session in list(device_login._sessions.items()):
        if session.state == "pending":
            device_login.cancel_login(session_id)
    device_login._sessions.clear()


def _rows(client):
    body = client.get("/api/plugins/hermes_control/engines").json()
    return {row["name"]: row for row in body["engines"]}, body


def test_engine_rows_say_what_the_cli_says(api):
    module, client = api
    (module.test_state["claude"] / "signed-in").touch()

    rows, body = _rows(client)

    assert rows["claude"]["signed_in"] is True and rows["claude"]["problem"] is None
    assert rows["codex"]["signed_in"] is False
    assert rows["codex"]["problem"].startswith("not signed in")
    assert rows["claude"]["sign_in_command"] == "claude auth login"
    assert rows["codex"]["sign_in_command"] == "codex login"
    assert body["sandbox"]["available"] is True


def test_only_codex_signs_in_from_the_page(api):
    _, client = api
    rows, _ = _rows(client)

    assert rows["codex"]["sign_in_in_page"] is True
    assert rows["claude"]["sign_in_in_page"] is False


def test_reasoning_summary_get_returns_value_choices_and_codex_row(api):
    _, client = api

    response = client.get("/api/plugins/hermes_control/reasoning-summary")
    rows, _ = _rows(client)

    assert response.status_code == 200
    assert response.json() == {"reasoning_summary": "concise",
                               "choices": ["none", "concise", "detailed", "auto"]}
    assert rows["codex"]["reasoning_summary"] == "concise"
    assert "reasoning_summary" not in rows["claude"]


def test_reasoning_summary_put_round_trips_to_config_and_engines(api, _isolated_hermes_home):
    import yaml

    _, client = api
    response = client.put("/api/plugins/hermes_control/reasoning-summary",
                          json={"reasoning_summary": "auto"})

    assert response.status_code == 200
    assert response.json() == {"reasoning_summary": "auto",
                               "choices": ["none", "concise", "detailed", "auto"]}
    assert client.get("/api/plugins/hermes_control/reasoning-summary").json() == response.json()
    assert _rows(client)[0]["codex"]["reasoning_summary"] == "auto"
    config = yaml.safe_load((_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["plugins"]["entries"]["hermes_control"]["settings"]["reasoning_summary"] == "auto"


def test_reasoning_summary_put_rejects_invalid_value(api):
    _, client = api

    response = client.put("/api/plugins/hermes_control/reasoning-summary",
                          json={"reasoning_summary": "loud"})

    assert response.status_code == 400
    assert "none, concise, detailed, auto" in response.json()["detail"]


def test_reasoning_summary_put_reports_managed_setting(api, monkeypatch):
    module, client = api

    def denied(_value):
        raise PermissionError("administrator-managed setting")

    monkeypatch.setattr(module, "save_reasoning_summary", denied)
    response = client.put("/api/plugins/hermes_control/reasoning-summary",
                          json={"reasoning_summary": "none"})

    assert response.status_code == 403
    assert response.json()["detail"] == "administrator-managed setting"


def test_a_missing_engine_is_reported_as_not_installed(api, monkeypatch):
    module, client = api
    adapters = dict(module.test_state["adapters"], codex=_adapter(CodexAdapter, None))
    monkeypatch.setattr(module, "get_adapter", adapters.__getitem__)

    rows, _ = _rows(client)

    assert rows["codex"]["installed"] is False
    assert rows["codex"]["problem"] == NOT_INSTALLED


@pytest.mark.live_system_guard_bypass  # the login child is stopped when the test ends
def test_codex_signs_in_through_the_page_routes(api):
    module, client = api
    started = client.post("/api/plugins/hermes_control/engines/codex/login").json()
    assert started["state"] == "pending" and started["code"] == "ABCD-EFGHI"

    (module.test_state["codex"] / "approve").touch()
    deadline = time.monotonic() + 5
    while True:
        view = client.get(
            f"/api/plugins/hermes_control/engines/codex/login/{started['session_id']}").json()
        if view["state"] != "pending" or time.monotonic() > deadline:
            break
        time.sleep(0.05)

    assert view["state"] == "signed_in"
    rows, _ = _rows(client)
    assert rows["codex"]["signed_in"] is True


def test_a_signed_in_codex_cannot_start_another_sign_in(api):
    module, client = api
    (module.test_state["codex"] / "signed-in").touch()

    response = client.post("/api/plugins/hermes_control/engines/codex/login")

    assert response.status_code == 409
    assert "already signed in" in response.json()["detail"]


@pytest.mark.live_system_guard_bypass  # cancelling kills the login child
def test_a_sign_in_can_be_cancelled_over_http(api):
    _, client = api
    started = client.post("/api/plugins/hermes_control/engines/codex/login").json()

    cancelled = client.delete(
        f"/api/plugins/hermes_control/engines/codex/login/{started['session_id']}")

    assert cancelled.json()["state"] == "cancelled"


def test_an_unknown_sign_in_is_404(api):
    _, client = api
    assert client.get("/api/plugins/hermes_control/engines/codex/login/nope").status_code == 404
    assert client.delete("/api/plugins/hermes_control/engines/codex/login/nope").status_code == 404


@pytest.fixture
def linked_home(tmp_path, monkeypatch):
    """A Hermes home with this repo's plugin linked in and enabled - the state
    scripts/install.sh leaves the real ~/.hermes in."""
    home = tmp_path / "linked-home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "hermes_control").symlink_to(DASHBOARD.parent, target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes_control\n",
                                      encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_the_dashboard_discovers_the_linked_engines_tab(linked_home):
    from hermes_cli.web_server_dashboard import _discover_dashboard_plugins

    entry = next(p for p in _discover_dashboard_plugins() if p["name"] == "hermes_control")

    assert entry["source"] == "user"
    assert entry["tab"]["path"] == "/engines"
    assert entry["_api_file"] == "plugin_api.py"
    assert (DASHBOARD / "dist" / "index.js").is_file()
    assert (DASHBOARD / "dist" / "style.css").is_file()


def test_the_real_dashboard_mounts_the_linked_plugins_routes(linked_home):
    """Through Hermes's own discovery and mount, so the by-path import of
    plugin_api.py and its load of this package are what is exercised."""
    from hermes_cli import web_server, web_server_dashboard

    original = list(web_server.app.router.routes)
    web_server._dashboard_plugins_cache = None
    try:
        web_server._get_dashboard_plugins(force_rescan=True)
        web_server_dashboard._mount_plugin_api_routes()
        paths = {getattr(route, "path", "") for route in web_server.app.routes}
    finally:
        web_server.app.router.routes[:] = original
        web_server._dashboard_plugins_cache = None

    assert "/api/plugins/hermes_control/engines" in paths
    assert "/api/plugins/hermes_control/engines/codex/login/{session_id}" in paths


# --- model settings -------------------------------------------------------------

def test_the_models_route_lists_every_engine_and_its_effort_levels(api):
    _, client = api

    body = client.get("/api/plugins/hermes_control/models").json()

    assert set(body["settings"]) == {"claude", "codex"}
    assert body["settings"]["claude"]["allowed"] == []
    assert "max" in body["efforts"]["claude"] and "ultra" in body["efforts"]["codex"]
    assert body["problem"] is None


def test_saved_models_are_what_dispatch_then_enforces(api):
    from hermes_control import model_policy

    _, client = api
    settings = {"claude": {"default": "sonnet", "allowed": [
        {"name": "sonnet", "use_for": "routine", "effort": "medium"},
        {"name": "opus", "use_for": "hard", "effort": "high"}]}}

    response = client.put("/api/plugins/hermes_control/models", json={"settings": settings})

    assert response.status_code == 200
    assert set(model_policy.load_policy()["claude"].allowed) == {"sonnet", "opus"}
    listed = client.get("/api/plugins/hermes_control/models").json()["settings"]["claude"]
    assert [m["name"] for m in listed["allowed"]] == ["sonnet", "opus"]


def test_invalid_models_are_refused_and_nothing_is_saved(api):
    from hermes_control import model_policy

    _, client = api
    response = client.put("/api/plugins/hermes_control/models", json={"settings": {
        "claude": {"allowed": [{"name": "sonnet", "effort": "ludicrous"}]}}})

    assert response.status_code == 400
    assert "effort" in response.json()["detail"]
    assert model_policy.load_policy() == {}


def test_missing_models_settings_cannot_clear_existing_allow_list(api):
    from hermes_control import model_policy

    _, client = api
    model_policy.save_settings({"claude": {"allowed": {"sonnet": "routine"}}})

    response = client.put("/api/plugins/hermes_control/models", json={})

    assert response.status_code == 400
    assert "settings" in response.json()["detail"]
    assert set(model_policy.load_policy()["claude"].allowed) == {"sonnet"}


@pytest.mark.parametrize("settings", [None, {}])
def test_explicit_empty_models_settings_clear_allow_list(api, settings):
    from hermes_control import model_policy

    _, client = api
    model_policy.save_settings({"claude": {"allowed": {"sonnet": "routine"}}})

    response = client.put("/api/plugins/hermes_control/models", json={"settings": settings})

    assert response.status_code == 200
    assert model_policy.load_policy() == {}


def test_settings_already_broken_in_config_are_reported_not_hidden(api, _isolated_hermes_home):
    (_isolated_hermes_home / "config.yaml").write_text(
        "plugins:\n  entries:\n    hermes_control:\n      settings:\n        models:\n"
        "          claude: {allowed: {sonnet: {effort: ludicrous}}}\n", encoding="utf-8")
    _, client = api

    body = client.get("/api/plugins/hermes_control/models").json()

    assert "effort" in body["problem"]


def test_the_picker_gets_each_engines_model_list(api, monkeypatch):
    from hermes_control import model_catalog

    model_catalog._cache.clear()
    calls = []
    monkeypatch.setitem(model_catalog._FETCHERS, "claude", lambda: calls.append("claude") or {
        "source": "stub", "models": [model_catalog.CatalogModel(name="claude-opus-5-5")]})
    monkeypatch.setitem(model_catalog._FETCHERS, "codex", lambda: calls.append("codex") or {
        "source": "stub", "models": [model_catalog.CatalogModel(
            name="gpt-5.5", description="Legacy", efforts=("low", "high"))]})
    _, client = api

    body = client.get("/api/plugins/hermes_control/models/available").json()
    client.get("/api/plugins/hermes_control/models/available?refresh=true")

    assert body["claude"]["models"][0]["name"] == "claude-opus-5-5"
    assert body["codex"]["models"][0]["efforts"] == ["low", "high"]
    assert calls == ["claude", "codex", "claude", "codex"]  # refresh bypasses the cache
    model_catalog._cache.clear()


def test_the_planner_card_reports_config_without_checking_sign_in(api, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"model": {"provider": "anthropic", "default": "sonnet"},
                                 "api_key": "sk-secret"})
    _, client = api

    response = client.get("/api/plugins/hermes_control/planner")

    assert response.status_code == 200
    assert response.json()["signed_in"] is None
    assert response.json()["provider"] == "anthropic"
    assert response.json()["model"] == "sonnet"
    assert response.json()["detail"] == "sign-in state not checked"
    assert "sk-secret" not in response.text


# --- usage limits and paused tasks (docs/limits-and-resume.md §6) ---------------------

def test_the_limits_route_shows_each_engines_usage_and_cap(api, monkeypatch):
    from hermes_control import limits

    reading = limits.Usage(engine="codex", windows=(limits.Window("30-day", 85.0, time.time() + 60),),
                           source="codex app-server", plan="free")
    monkeypatch.setitem(limits._FETCHERS, "codex", lambda refresh=False: reading)
    _, client = api

    body = client.get("/api/plugins/hermes_control/limits").json()

    assert body["engines"]["claude"]["cap"] == 80
    assert body["engines"]["claude"]["held_back"] is None  # unknown usage never holds work back
    codex = body["engines"]["codex"]
    assert codex["windows"][0]["used_percent"] == 85.0
    assert "85% of its 30-day limit (cap 80%)" in codex["held_back"]


def test_a_cap_saved_on_the_page_is_what_dispatch_reads(api):
    from hermes_control import limits

    _, client = api

    response = client.put("/api/plugins/hermes_control/limits",
                          json={"limits": {"codex": {"max_percent": 95}}})

    assert response.status_code == 200
    assert limits.read_caps() == {"claude": 80, "codex": 95}


def test_a_bad_cap_is_refused(api):
    _, client = api

    response = client.put("/api/plugins/hermes_control/limits",
                          json={"limits": {"codex": {"max_percent": 0}}})

    assert response.status_code == 400 and "1 and 100" in response.json()["detail"]


def _paused_record(task_id="a", depends_on=()):
    from hermes_control import pauses

    record = pauses.Pause(
        id=pauses.pause_id("r9", task_id), run_id="r9",
        task={"id": task_id, "goal": "add a line", "engine": "claude", "policy": "write",
              "depends_on": list(depends_on), "why": "w"},
        repo="/repo", worktree=None, engine="claude", session_id=None,
        reason="claude hit its usage limit", resets_at=time.time() + 60, paused_at=time.time())
    pauses.save(record)
    return record


def test_paused_tasks_are_listed_with_whether_they_can_resume(api):
    _paused_record("a")
    _paused_record("b", depends_on=["a"])
    _, client = api

    items = {item["task_id"]: item for item in
             client.get("/api/plugins/hermes_control/paused").json()["paused"]}

    assert items["a"]["ready"] is True and items["a"]["engine"] == "claude"
    assert items["b"]["ready"] is False and "resume a first" in items["b"]["not_ready"]


def test_resume_runs_in_the_background_and_reports_its_outcome(api, monkeypatch):
    module, client = api
    record = _paused_record("a")
    monkeypatch.setattr(module, "resume_paused",
                        lambda record_id: {"task_id": "a", "commit": "abc", "paused": None})

    response = client.post(f"/api/plugins/hermes_control/paused/{record.id}/resume")

    assert response.status_code == 202
    for _ in range(50):
        resumes = client.get("/api/plugins/hermes_control/paused").json()["resumes"]
        if resumes and resumes[0]["state"] != "running":
            break
        time.sleep(0.05)
    assert resumes[0]["state"] == "done" and resumes[0]["entry"]["commit"] == "abc"


def test_resume_is_refused_with_the_reason_when_it_cannot_start(api):
    record = _paused_record("b", depends_on=["a"])
    _paused_record("a")
    _, client = api

    response = client.post(f"/api/plugins/hermes_control/paused/{record.id}/resume")

    assert response.status_code == 409 and "resume a first" in response.json()["detail"]


def test_a_paused_task_can_be_discarded(api):
    from hermes_control import pauses

    record = _paused_record("a")
    _, client = api

    assert client.delete(f"/api/plugins/hermes_control/paused/{record.id}").status_code == 200
    assert pauses.load(record.id) is None
    assert client.delete(f"/api/plugins/hermes_control/paused/{record.id}").status_code == 404


# --- the Runs view -----------------------------------------------------------------

def _logged_run(run_id):
    from hermes_control.events import RawEvent, stamp
    from hermes_control.runlog import RunLog

    log = RunLog(run_id)
    plan = {"tasks": [{"id": "a", "engine": "codex", "why": "pattern work", "policy": "write",
                       "depends_on": [], "verify": None, "model": None, "effort": None,
                       "rerouted": None}], "repo": "/repo", "planner": None}
    log.append(stamp([RawEvent(kind="plan", payload=plan)], run_id=run_id, task_id="",
                     engine="", start_seq=0, now=time.time()))
    log.append(stamp([RawEvent(kind="session", payload={"session_id": "T"})], run_id=run_id,
                     task_id="a", engine="codex", start_seq=0, now=time.time()))


def test_the_runs_route_lists_runs_with_their_tasks(api):
    _logged_run("run1")
    _, client = api

    listed = client.get("/api/plugins/hermes_control/runs").json()["runs"]

    assert listed[0]["run_id"] == "run1" and listed[0]["state"] == "running"
    assert listed[0]["tasks"] == [{"id": "a", "status": "running", "engine": "codex"}]


def test_one_run_is_served_whole_for_the_diagram(api):
    _logged_run("run2")
    _, client = api

    view = client.get("/api/plugins/hermes_control/runs/run2").json()

    assert view["repo"] == "/repo" and view["tasks"][0]["why"] == "pattern work"
    assert view["tasks"][0]["attempts"][0]["engine"] == "codex"


def test_task_trace_route_and_missing_run_or_task(api):
    _logged_run("trace-http")
    _, client = api

    response = client.get("/api/plugins/hermes_control/runs/trace-http/tasks/a/trace")
    assert response.status_code == 200
    body = response.json()
    assert [entry["type"] for entry in body["entries"]] == ["plan", "session"]
    assert body["last_seq"] == 0
    assert client.get("/api/plugins/hermes_control/runs/trace-http/tasks/a/trace?since=0").json() == {
        "entries": [], "last_seq": 0}

    missing_run = client.get("/api/plugins/hermes_control/runs/missing/tasks/a/trace")
    missing_task = client.get("/api/plugins/hermes_control/runs/trace-http/tasks/b/trace")
    assert missing_run.status_code == 404 and missing_run.json()["detail"] == "no such run"
    assert missing_task.status_code == 404 and missing_task.json()["detail"] == "no such task"


def test_finished_run_stream_starts_with_view(api):
    from hermes_control.events import RawEvent, stamp
    from hermes_control.runlog import RunLog

    _logged_run("stream-http")
    RunLog("stream-http").append(stamp(
        [RawEvent(kind="verify", payload={"passed": True})], run_id="stream-http",
        task_id="a", engine="codex", start_seq=1, now=time.time()))
    _, client = api

    with client.stream("GET", "/api/plugins/hermes_control/runs/stream-http/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert next(response.iter_lines()) == "event: view"


def test_unknown_run_stream_is_404(api):
    _, client = api
    response = client.get("/api/plugins/hermes_control/runs/no-such-run/stream")
    assert response.status_code == 404
    assert response.json()["detail"] == "no such run"


@pytest.mark.parametrize("run_id", ["nope", "..%2F..%2Fconfig"])
def test_an_unknown_run_is_404(api, run_id):
    _, client = api

    assert client.get(f"/api/plugins/hermes_control/runs/{run_id}").status_code == 404
