"""Claude usage uses only the CLI's reports and public account status."""

import builtins
import http.client
import importlib
import json
import os
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_control import limits
from hermes_control.dashboard import plugin_api
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.claude import ClaudeAdapter

NOW = 1_790_160_000.0
LATER = NOW + 3600


@pytest.fixture
def official_sources(monkeypatch):
    """Fail on network or credential imports, and fake only the CLI boundary."""
    real_import = builtins.__import__
    real_import_module = importlib.import_module
    real_open = builtins.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def guarded_import_module(name, package=None):
        resolved = importlib.util.resolve_name(name, package) if name.startswith(".") else name
        if resolved == "agent.anthropic_credentials" or resolved.startswith("agent.anthropic_credentials."):
            pytest.fail("Claude usage must never import stored credentials")
        return real_import_module(name, package)

    def guard_path(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = os.fsdecode(path)
            if ".claude/.credentials" in name or "Claude Code-credentials" in name:
                pytest.fail("Claude usage must never open stored credentials")

    def guarded_open(file, *args, **kwargs):
        guard_path(file)
        return real_open(file, *args, **kwargs)

    def guarded_read_text(path, *args, **kwargs):
        guard_path(path)
        return real_read_text(path, *args, **kwargs)

    def guarded_read_bytes(path, *args, **kwargs):
        guard_path(path)
        return real_read_bytes(path, *args, **kwargs)

    def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "agent.anthropic_credentials" or name.startswith("agent.anthropic_credentials."):
            pytest.fail("Claude usage must never import stored credentials")
        if name == "agent" and "anthropic_credentials" in (fromlist or ()):
            pytest.fail("Claude usage must never import stored credentials")
        return real_import(name, globals, locals, fromlist, level)

    def no_network(*args, **kwargs):
        pytest.fail("Claude usage must never make a network call")

    status = {"email": "me@example.com", "subscriptionType": "pro"}
    calls = []

    def auth_status(argv, **kwargs):
        assert argv == ["/fake/claude", "auth", "status"]
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps(status), returncode=0)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(importlib, "import_module", guarded_import_module)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(Path, "read_text", guarded_read_text)
    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(http.client.HTTPConnection, "request", no_network)
    monkeypatch.setattr(ClaudeAdapter, "probe", lambda self: EngineInfo(
        name="claude", version="test", path="/fake/claude"))
    monkeypatch.setattr(limits.subprocess, "run", auth_status)
    monkeypatch.setattr(limits.time, "time", lambda: NOW)
    monkeypatch.setattr(limits, "_claude_account_cache", None)
    # conftest stubs every usage fetcher; restore the real Claude reader here.
    monkeypatch.setitem(limits._FETCHERS, "claude", limits._claude)
    return status, calls


def _record(percent, *, at=NOW):
    limits.record_claude_report({"status": "allowed", "unifiedWindows": {
        "five_hour": {"utilization": percent / 100, "resetsAt": LATER}}}, at=at)


@pytest.mark.parametrize("reader", ["report", "account", "cached"])
def test_reading_claude_usage_never_calls_network_or_imports_credentials(official_sources, reader):
    _record(27)

    if reader == "report":
        usage = limits._claude_usage(current_account="me@example.com")
    elif reader == "account":
        usage = limits._claude()
    else:
        usage = limits.usage("claude")

    assert usage.error is None
    assert usage.windows == (limits.Window("5-hour", 27.0, LATER),)


def test_claude_usage_comes_from_the_last_report_with_its_age(official_sources):
    _record(12, at=NOW - 60)
    assert limits.usage("claude").windows[0].used_percent == 12

    _record(43, at=NOW - 10)
    usage = limits.usage("claude")

    assert usage.windows == (limits.Window("5-hour", 43.0, LATER),)
    assert usage.source == f"claude's own report during a run at {limits.when(NOW - 10)}"
    assert (usage.account, usage.plan) == ("me@example.com", "pro")


def test_no_claude_report_is_unknown_and_never_held_back(official_sources):
    usage = limits.usage("claude")

    assert usage.error == "no reading yet: claude has not reported its usage in a run"
    assert usage.windows == ()
    assert usage.blocked is False
    assert (usage.account, usage.plan) == ("me@example.com", "pro")
    assert limits.held_back(usage, cap=1, now=NOW) is None


@pytest.mark.parametrize("has_report", [False, True])
def test_dashboard_refresh_rereads_auth_status_and_report_without_network(official_sources, has_report):
    status, calls = official_sources
    if has_report:
        _record(27)
    app = FastAPI()
    app.include_router(plugin_api.router, prefix="/api/plugins/hermes_control")
    with TestClient(app) as client:
        first = client.get("/api/plugins/hermes_control/limits").json()["engines"]["claude"]
        assert (first["account"], first["plan"]) == ("me@example.com", "pro")
        # Change the saved report without cache invalidation: Refresh must reread it.
        if has_report:
            report_path = limits._report_path()
            report = json.loads(report_path.read_text())
            report["info"]["unifiedWindows"]["five_hour"]["utilization"] = 0.43
            report_path.write_text(json.dumps(report))
        status["subscriptionType"] = "max"
        before_refresh = len(calls)

        response = client.get("/api/plugins/hermes_control/limits?refresh=true")

    assert response.status_code == 200
    row = response.json()["engines"]["claude"]
    assert len(calls) == before_refresh + 1
    assert (row["account"], row["plan"]) == ("me@example.com", "max")
    assert row["held_back"] is None
    if has_report:
        assert row["error"] is None
        assert row["windows"][0]["used_percent"] == 43
        assert row["source"] == f"claude's own report during a run at {limits.when(NOW)}"
    else:
        assert row["error"]
        assert row["windows"] == []
