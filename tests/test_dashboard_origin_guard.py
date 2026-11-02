"""Every plugin route rejects browser requests from unrelated web origins."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


PREFIX = "/api/plugins/hermes_control"


@pytest.fixture
def api(tmp_path, monkeypatch):
    from hermes_cli.dashboard_auth import prefix

    path = Path(__file__).resolve().parents[1] / "hermes_control/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location(f"hc_origin_api_{id(tmp_path)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "get_adapter", lambda name: SimpleNamespace(sign_in_command=""))
    monkeypatch.setattr(module, "engine_state", lambda adapter: (None, "not installed"))
    monkeypatch.setattr(module, "find_sandbox", lambda: None)
    # Usage readers and HERMES_HOME are isolated by the autouse conftest fixtures.
    monkeypatch.setattr(prefix, "resolve_public_url", lambda: "")
    app = FastAPI()
    app.include_router(module.router, prefix=PREFIX)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("origin", [None, "http://testserver", "http://testserver:80",
                                   "HTTP://TESTSERVER", "null", "file:///app", "app://desktop"])
def test_allowed_origins(api, origin):
    headers = {} if origin is None else {"Origin": origin}
    assert api.get(f"{PREFIX}/engines", headers=headers).status_code == 200


@pytest.mark.parametrize("method,path,kwargs", [
    ("GET", "/engines", {}),
    ("PUT", "/limits", {"json": {"limits": {}}}),
    ("POST", "/paused/x/resume", {}),
    ("GET", "/runs/missing/stream", {}),
])
def test_other_localhost_port_is_rejected_on_every_route_kind(api, method, path, kwargs):
    response = api.request(method, PREFIX + path, headers={"Origin": "http://localhost:5173"},
                           **kwargs)
    assert response.status_code == 403
    assert response.json() == {"detail": "cross-origin request refused"}


@pytest.mark.parametrize("origin", ["https://evil.example", "http://[::1",
                                   "http://testserver:bad", "http://testserver/path",
                                   "http://user@testserver", "http://testserver#fragment",
                                   "http://testserver?query", "http://testserver:99999",
                                   "http://testserver\t", "http://"])
def test_unrelated_or_malformed_web_origin_is_rejected(api, origin):
    response = api.get(f"{PREFIX}/engines", headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json()["detail"] == "cross-origin request refused"


@pytest.mark.parametrize("origin", ["https://hermes.example.com", "HTTPS://HERMES.EXAMPLE.COM:443"])
def test_public_dashboard_origin_is_allowed(api, monkeypatch, origin):
    from hermes_cli.dashboard_auth import prefix

    monkeypatch.setattr(prefix, "resolve_public_url", lambda: "https://hermes.example.com/dash")
    assert api.get(f"{PREFIX}/engines", headers={"Origin": origin}).status_code == 200


def test_public_url_lookup_failure_keeps_only_request_origin(api, monkeypatch):
    from hermes_cli.dashboard_auth import prefix

    def unavailable():
        raise RuntimeError("public URL unavailable")

    monkeypatch.setattr(prefix, "resolve_public_url", unavailable)
    assert api.get(f"{PREFIX}/engines", headers={"Origin": "http://testserver"}).status_code == 200
    assert api.get(f"{PREFIX}/engines", headers={"Origin": "https://evil.example"}).status_code == 403
