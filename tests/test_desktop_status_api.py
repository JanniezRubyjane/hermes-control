"""The desktop chip contract, through HTTP with no real usage or run reads."""

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


URL = "/api/plugins/hermes_control/status"


def reading(percent=None, **overrides):
    return {"windows": [] if percent is None else [
                {"label": "weekly", "used_percent": percent, "resets_at": 300.0}],
            "cap": 80, "paused": False, "blocked": False, "blocked_until": None,
            "held_back": None, "held_until": None, "report_only": False,
            "account": "someone@example.com", **overrides}


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location(f"desktop_api_{id(tmp_path)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rows = {name: reading() for name in module.ENGINE_NAMES}
    recent = []
    caps = {name: 80 for name in rows}
    calls = []
    monkeypatch.setattr(module.limits, "read_caps", lambda: caps)

    def describe(name, *, caps, refresh):
        calls.append((name, caps, refresh))
        return rows[name]

    def list_runs(*, limit):
        assert limit == 5
        return recent

    def forbidden(*args, **kwargs):
        pytest.fail("status must not probe engine state or adapters")

    monkeypatch.setattr(module.limits, "describe", describe)
    monkeypatch.setattr(module.runs, "list_runs", list_runs)
    monkeypatch.setattr(module, "engine_state", forbidden)
    monkeypatch.setattr(module, "get_adapter", forbidden)
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/hermes_control")
    return module, TestClient(app), rows, recent, caps, calls


def chip(kind="idle", **values):
    return {"kind": kind, "engine": None, "percent": None, "window": None,
            "resets_at": None, "run_id": None, "task_id": None,
            "started_at": None, **values}


def test_idle_fullest_window_and_response_allowlist(api):
    module, client, rows, _, caps, calls = api
    rows.update(claude=reading(12.0), codex=reading(35.0, windows=[
        {"label": "short", "used_percent": 35.0, "resets_at": 200.0},
        {"label": "weekly", "used_percent": 22.0, "resets_at": 300.0}]))
    response = client.get(URL)
    assert response.status_code == 200
    assert response.json() == {
        "chip": chip(engine="codex", percent=35.0),
        "engines": [
            {"name": "claude", "percent": 12.0, "window": "weekly", "cap": 80,
             "report_only": False, "level": "ok", "resets_at": 300.0},
            {"name": "codex", "percent": 35.0, "window": "short", "cap": 80,
             "report_only": False, "level": "ok", "resets_at": 200.0}],
        "last_run": None, "proof": None, "problem": None}
    assert '"account"' not in response.text
    assert "@" not in response.text
    assert calls == [(name, caps, False) for name in module.ENGINE_NAMES]


def test_report_only_over_cap_does_not_influence_chip(api):
    _, client, rows, *_ = api
    rows.update(claude=reading(10.0), codex=reading(86.0, report_only=True))
    body = client.get(URL).json()
    assert body["chip"] == chip(engine="claude", percent=10.0)
    assert body["engines"][1]["level"] == "over"


@pytest.mark.parametrize(("percent", "level"), [(59.99, "ok"), (60.0, "near"), (80.0, "over")])
def test_cap_boundaries(api, percent, level):
    _, client, rows, *_ = api
    rows["codex"] = reading(percent)
    body = client.get(URL).json()
    assert body["engines"][1]["level"] == level
    assert body["chip"] == (chip(engine="codex", percent=percent) if level == "ok" else
                            chip("near", engine="codex", percent=percent,
                                 window="weekly", resets_at=300.0))


@pytest.mark.parametrize("percent, level", [(40.0, "near"), (50.0, "over")])
def test_each_engine_uses_its_own_cap(api, percent, level):
    _, client, rows, *_ = api
    rows.update(claude=reading(percent, cap=50), codex=reading(40.0, cap=80))
    body = client.get(URL).json()
    assert [(row["cap"], row["level"]) for row in body["engines"]] == [
        (50, level), (80, "ok")]
    assert body["chip"] == chip("near", engine="claude", percent=percent,
                                window="weekly", resets_at=300.0)


@pytest.mark.parametrize("percent", [0.0, 35.0])
@pytest.mark.parametrize("reverse", [False, True])
def test_fullest_window_ignores_unknown_percent(api, percent, reverse):
    _, client, rows, *_ = api
    windows = [
        {"label": "unknown", "used_percent": None, "resets_at": 999.0},
        {"label": "short", "used_percent": percent, "resets_at": 200.0}]
    rows["codex"] = reading(windows=windows[::-1] if reverse else windows)
    body = client.get(URL).json()
    row = body["engines"][1]
    assert (row["percent"], row["window"], row["resets_at"]) == (percent, "short", 200.0)
    assert body["chip"] == chip(engine="codex", percent=percent)


@pytest.mark.parametrize("other_percent", [None, 20.0, 65.0])
def test_all_unknown_windows_do_not_influence_chip(api, other_percent):
    _, client, rows, *_ = api
    rows.update(claude=reading(windows=[
        {"label": "short", "used_percent": None, "resets_at": 200.0},
        {"label": "weekly", "used_percent": None, "resets_at": 300.0}]),
        codex=reading(other_percent))
    body = client.get(URL).json()
    row = body["engines"][0]
    assert row["percent"] is row["window"] is row["resets_at"] is None
    assert row["level"] == "ok"
    expected = chip() if other_percent is None else chip(engine="codex", percent=other_percent)
    if other_percent == 65.0:
        expected = chip("near", engine="codex", percent=65.0,
                        window="weekly", resets_at=300.0)
    assert body["chip"] == expected


@pytest.mark.parametrize("overrides, reset", [
    ({"blocked": True, "blocked_until": 400.0, "held_until": 500.0}, 400.0),
    ({"held_back": "cap reached", "held_until": 500.0}, 500.0),
    ({"blocked": True}, 300.0),
    ({"held_back": "" , "held_until": 0.0}, 0.0),
])
def test_blocked_beats_near_and_uses_block_reset(api, overrides, reset):
    _, client, rows, *_ = api
    rows.update(claude=reading(95.0), codex=reading(20.0, **overrides))
    body = client.get(URL).json()
    assert body["chip"] == chip("blocked", engine="codex", resets_at=reset)
    assert body["engines"][1]["level"] == "blocked"
    assert body["engines"][1]["resets_at"] == reset


def test_first_blocked_engine_wins(api):
    _, client, rows, *_ = api
    rows.update(claude=reading(10.0, blocked=True), codex=reading(90.0, blocked=True))
    assert client.get(URL).json()["chip"] == chip("blocked", engine="claude", resets_at=300.0)


@pytest.mark.parametrize("blocked", [False, True])
def test_paused_never_triggers_blocked_or_near(api, blocked):
    _, client, rows, *_ = api
    rows.update(claude=reading(90.0, paused=True, blocked=blocked, held_back="paused"),
                codex=reading(65.0))
    body = client.get(URL).json()
    assert body["engines"][0]["level"] == "paused"
    assert body["chip"] == chip("near", engine="codex", percent=65.0,
                                window="weekly", resets_at=300.0)


def test_near_picks_highest_percent(api):
    _, client, rows, *_ = api
    rows.update(claude=reading(61.0), codex=reading(85.0))
    assert client.get(URL).json()["chip"] == chip(
        "near", engine="codex", percent=85.0, window="weekly", resets_at=300.0)


@pytest.mark.parametrize("statuses, task_id, engine", [
    (["done", "running"], "b", "codex"),
    (["waiting", "paused"], "a", "claude"),
    ([], None, None),
])
def test_running_beats_blocked_and_last_run_stays_newest(api, statuses, task_id, engine):
    _, client, rows, recent, *_ = api
    rows["claude"] = reading(blocked=True)
    recent.extend([
        {"run_id": "newest", "state": "finished", "updated_at": 200.0,
         "tasks": [{"id": "z"}, {"id": "y"}], "account": "someone@example.com"},
        {"run_id": "active", "state": "running", "started_at": 100.0,
         "tasks": [{"id": name, "engine": eng, "status": status}
                   for name, eng, status in zip(["a", "b"], ["claude", "codex"], statuses)]},
        {"run_id": "later-active", "state": "running", "started_at": 150.0, "tasks": []}])
    response = client.get(URL)
    body = response.json()
    assert body["chip"] == chip("running", run_id="active", task_id=task_id,
                                engine=engine, started_at=100.0)
    assert body["last_run"] == {"run_id": "newest", "state": "finished",
                                "updated_at": 200.0, "task_ids": ["z", "y"]}
    assert "@" not in response.text and '"account"' not in response.text


def test_unknown_usage_and_no_runs(api):
    _, client, *_ = api
    body = client.get(URL).json()
    assert body["chip"] == chip()
    assert body["last_run"] is None
    for row in body["engines"]:
        assert row["percent"] is row["window"] is row["resets_at"] is None
        assert row["level"] == "ok"


def test_bad_caps_use_defaults_and_report_problem(api, monkeypatch):
    module, client, _, _, _, calls = api

    def bad_caps():
        raise ValueError("invalid cap")

    defaults = {"claude": 80, "codex": 80}

    def parse_caps(raw):
        assert raw is None
        return defaults

    monkeypatch.setattr(module.limits, "read_caps", bad_caps)
    monkeypatch.setattr(module.limits, "parse_caps", parse_caps)
    assert client.get(URL).json()["problem"] == "invalid cap"
    assert all(caps is defaults for _, caps, _ in calls)


@pytest.mark.parametrize("caps_error, runs_error", [
    (None, OSError), (OSError, None), (OSError, OSError),
    (RuntimeError, RuntimeError), (ValueError, OSError),
])
def test_read_failures_keep_status_available(api, monkeypatch, caplog, caps_error, runs_error):
    module, client, _, _, _, calls = api
    defaults = {"claude": 80, "codex": 80}
    expected_problems = []

    def parse_caps(raw):
        assert raw is None
        return defaults

    def bad_caps():
        raise caps_error("invalid cap")

    def bad_runs(*, limit):
        assert limit == 5
        raise runs_error("unreadable logs")

    if caps_error is not None:
        monkeypatch.setattr(module.limits, "read_caps", bad_caps)
        monkeypatch.setattr(module.limits, "parse_caps", parse_caps)
        expected_problems.append("invalid cap" if caps_error is ValueError else
                                 f"could not read the usage caps: {caps_error.__name__}")
    if runs_error is not None:
        monkeypatch.setattr(module.runs, "list_runs", bad_runs)
        expected_problems.append(f"could not read the run logs: {runs_error.__name__}")

    response = client.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert body["last_run"] is None
    assert body["chip"] == chip()
    assert body["problem"] == "; ".join(expected_problems)
    if caps_error is not None:
        assert len(calls) == len(module.ENGINE_NAMES)
        assert all(caps is defaults for _, caps, _ in calls)
    warnings = [record for record in caplog.records if record.name == module.logger.name]
    assert [record.getMessage() for record in warnings] == [
        problem for problem in expected_problems if problem != "invalid cap"]
    assert all(record.levelname == "WARNING" and record.exc_info for record in warnings)


@pytest.mark.parametrize("report_only", [False, True])
def test_engines_follow_registry_even_for_blocked_report_only(api, monkeypatch, report_only):
    module, client, rows, _, caps, calls = api
    monkeypatch.setattr(module, "ENGINE_NAMES", ("future", "another"))
    rows.update(future=reading(86.0, blocked=True, report_only=report_only), another=reading(20.0))
    body = client.get(URL).json()
    assert [row["name"] for row in body["engines"]] == ["future", "another"]
    assert calls == [("future", caps, False), ("another", caps, False)]
    assert body["engines"][0]["level"] == "blocked"
    assert body["chip"] == (chip(engine="another", percent=20.0) if report_only else
                            chip("blocked", engine="future", resets_at=300.0))
