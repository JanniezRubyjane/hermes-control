"""Synthetic quota snapshots only: no real processes, sockets or accounts."""

import io
import json
import logging
import ssl
import subprocess
import urllib.error
from dataclasses import asdict
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_control import agy_quota, limits, watch

NOW = 1_924_992_000.0  # fabricated clock and quota values throughout
TOKEN = "fabricated_csrf_secret-123"
EXECUTABLE = "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
PS = f" 42 {EXECUTABLE} --csrf_token {TOKEN} --app_data_dir antigravity\n"
LSOF = "language 42 user 1u IPv4 0t0 TCP 127.0.0.1:43210 (LISTEN)\n"


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def bucket(window="weekly", remaining=0.5, reset=NOW + 3600, description=None):
    result = {"window": window, "remainingFraction": remaining, "resetTime": iso(reset)}
    if description is not None:
        result["description"] = description
    return result


def payload(*buckets):
    return {"response": {"groups": [{"displayName": "Example pool", "buckets": list(buckets)}]}}


def full_example():
    return {"ignored": True, "response": {"description": "Example", "groups": [
        {"displayName": "Gemini Models", "description": "Example models", "buckets": [
            {**bucket(remaining=0.82, reset=NOW - 7200 + 5 * 86400 + 14 * 3600,
                      description="It will fully refresh in 5 days, 14 hours."),
             "bucketId": "gemini-weekly", "displayName": "Weekly Limit Remaining"},
            {**bucket("5h", 0.61, NOW + 420,
                      "It will fully refresh in 2 hours, 7 minutes."),
             "bucketId": "gemini-5h"}]},
        {"displayName": "Claude and GPT models", "buckets": [
            {**bucket(remaining=0.72), "bucketId": "3p-weekly"},
            {**bucket("5h", 0.43, description="It will fully refresh in 13 minutes."),
             "bucketId": "3p-5h"}]},
        {"displayName": "Gemini 4 Argon", "buckets": [
            {**bucket(remaining=0.24), "bucketId": "gemini-argon-wtu-weekly"}]}]}}


@pytest.fixture(autouse=True)
def local_server(monkeypatch):
    """Stub both discovery commands and every urllib HTTP/HTTPS attempt."""
    state = SimpleNamespace(ps=PS, lsof=LSOF, body=full_example(), status=200,
                            ps_error=None, lsof_error=None, http_error=None,
                            commands=[], requests=[], openers=[])

    def run(args, **kwargs):
        state.commands.append((args, kwargs))
        kind = "ps" if args[0] == "ps" else "lsof"
        assert args[0] in ("ps", "lsof")
        error = getattr(state, kind + "_error")
        if error:
            raise error
        return SimpleNamespace(stdout=getattr(state, kind))

    def open_stub(opener, request, *, timeout):
        state.requests.append((request, timeout))
        state.openers.append(opener)
        if state.http_error:
            raise state.http_error
        raw = state.body if isinstance(state.body, bytes) else json.dumps(state.body).encode()
        reply = io.BytesIO(raw)
        reply.status = state.status
        return reply

    monkeypatch.setattr(agy_quota.subprocess, "run", run)
    monkeypatch.setattr(agy_quota.urllib.request.OpenerDirector, "open", open_stub)
    monkeypatch.setattr(agy_quota.time, "time", lambda: NOW)
    return state


def test_full_response_maps_all_five_buckets_in_order():
    reading = agy_quota.parse_quota(full_example(), NOW)
    assert reading.error is None
    assert reading.engine == "gemini" and reading.report_only
    assert [w.label for w in reading.windows] == [
        "Gemini Models weekly", "Gemini Models 5h", "Claude and GPT models weekly",
        "Claude and GPT models 5h", "Gemini 4 Argon weekly"]
    assert [w.used_percent for w in reading.windows] == pytest.approx([18, 39, 28, 57, 76])
    assert reading.windows[1].resets_at == NOW + 420
    assert reading.source == ("the Antigravity app's language server, snapshot about 2 h old"
                              " (countdown in whole hours); "
                              "report only, never holds work back")


@pytest.mark.parametrize("fields, expected", [
    ({"remainingFraction": 0}, 100), ({"remainingFraction": 0.5}, 50),
    ({"remainingFraction": 1}, 0), ({"remainingFraction": 2}, None), ({}, None),
    ({"remainingFraction": None}, None), ({"remainingFraction": "0.5"}, None),
    ({"remainingFraction": True}, None), ({"remainingFraction": False}, None),
    ({"remainingFraction": -0.1}, None), ({"remainingFraction": float("nan")}, None),
    ({"remainingFraction": float("inf")}, None),
])
def test_remaining_fraction_validation(fields, expected):
    reading = agy_quota.parse_quota(payload({"window": "weekly", **fields}), NOW)
    assert reading.windows[0].used_percent == expected


@pytest.mark.parametrize("window", ["5h", "weekly"])
def test_past_reset_is_unknown(window):
    reading = agy_quota.parse_quota(payload(bucket(window, 0, NOW - 1, "Used")), NOW)
    assert reading.windows == (limits.Window(f"Example pool {window}", None, None),)


def test_unused_five_hour_bucket_has_no_reset():
    reading = agy_quota.parse_quota(payload(bucket("5h", 1, NOW + 5 * 3600)), NOW)
    assert reading.windows == (limits.Window("Example pool 5h", 0, None),)
    assert "snapshot about 0 min old" in reading.source


@pytest.mark.parametrize("countdown, duration", [
    ("5 days, 14 hours", 5 * 86400 + 14 * 3600),
    ("6 days, 21 hours", 6 * 86400 + 21 * 3600),
    ("2 hours, 7 minutes", 2 * 3600 + 7 * 60), ("13 minutes", 13 * 60),
    ("1 day, 1 hour, 1 minute", 86400 + 3600 + 60),
])
def test_snapshot_age_from_countdown(countdown, duration):
    description = f"You have used some quota; it will fully refresh in {countdown}."
    reading = agy_quota.parse_quota(
        payload(bucket(reset=NOW - 7 * 60 + duration, description=description)), NOW)
    assert "snapshot about 7 min old" in reading.source


def test_snapshot_age_uses_first_parsable_bucket_even_when_reset_is_past():
    reading = agy_quota.parse_quota(payload(
        bucket(description="No countdown"),
        bucket(reset=NOW - 3600, description="Will fully refresh in 13 minutes."),
        bucket(reset=NOW + 3600, description="Will fully refresh in 2 hours.")), NOW)
    assert "snapshot about 1 h old" in reading.source
    assert reading.windows[1].used_percent is None


@pytest.mark.parametrize("reset", [None, "invalid", "2031-01-01T00:00:00", False])
def test_invalid_reset_does_not_invent_a_time_or_age(reset):
    reading = agy_quota.parse_quota(payload({
        **bucket(description="Will fully refresh in 13 minutes."), "resetTime": reset}), NOW)
    assert reading.windows[0].resets_at is None
    assert "snapshot age unknown" in reading.source


def test_unrecognized_group_and_window_are_not_filtered():
    data = payload(bucket("future-window", 0.75))
    data["response"]["groups"][0]["displayName"] = "Future pool"
    assert agy_quota.parse_quota(data, NOW).windows[0].label == "Future pool future-window"


@pytest.mark.parametrize("flag", [f"--csrf_token={TOKEN}", f"--csrf_token {TOKEN}"])
def test_token_flag_forms_and_candidate_order(flag):
    lines = [f"{pid} {EXECUTABLE} {flag} --app_data_dir antigravity"
             for pid in (9, 33, 11, 22)]
    lines += [f"99 /fake/other {flag}", f"88 {EXECUTABLE} --csrf_token=bad!token",
              f"77 {EXECUTABLE}", f"not-a-pid {EXECUTABLE} --csrf_token=abc"]
    assert agy_quota._candidates("\n".join(lines)) == [(33, TOKEN), (22, TOKEN), (11, TOKEN)]


@pytest.mark.parametrize("executable", [
    EXECUTABLE,
    "/Volumes/Antigravity/Antigravity.app/Contents/Resources/bin/language_server",
    "/fake/language_server",
])
@pytest.mark.parametrize("flag", ["--app_data_dir antigravity", "--app_data_dir=antigravity"])
def test_candidates_accept_antigravity_app_data(executable, flag):
    assert agy_quota._candidates(
        f"42 {executable} --standalone --csrf_token {TOKEN} {flag}") == [(42, TOKEN)]


@pytest.mark.parametrize("executable", [
    EXECUTABLE,
    "/Volumes/Antigravity/Antigravity.app/Contents/Resources/bin/language_server",
    "/Applications/ANTIGRAVITY.APP/Contents/Resources/bin/language_server",
])
def test_candidates_accept_app_path_without_app_data(executable):
    assert agy_quota._candidates(f"42 {executable} --csrf_token={TOKEN}") == [(42, TOKEN)]


@pytest.mark.parametrize("command", [
    "/bin/zsh -c grep language_server --csrf_token X",
    "/bin/zsh -c language_server --csrf_token X --app_data_dir antigravity",
    "/Applications/Windsurf.app/Contents/Resources/app/extensions/windsurf/bin/"
    "language_server --csrf_token X --app_data_dir windsurf",
    "/Applications/Antigravity.app/Contents/Resources/bin/language_server_macos_arm "
    "--csrf_token X --app_data_dir antigravity",
    "/fake/language_server --csrf_token X",
    "/fake/language_server --csrf_token X --app_data_dir antigravity-other",
    "/fake/language_server --csrf_token X --other=--app_data_dir=antigravity",
    "/Applications/Antigravity.app.other/bin/language_server --csrf_token X",
])
def test_candidates_reject_other_processes(command):
    assert agy_quota._candidates(f"42 {command}") == []


@pytest.mark.parametrize("pid", ["0", "-1", "not-a-pid"])
def test_candidates_require_positive_numeric_pid(pid):
    assert agy_quota._candidates(f"{pid} {EXECUTABLE} --csrf_token {TOKEN}") == []


def test_lsof_only_returns_loopback_listeners_in_order():
    lines = [LSOF, "x TCP *:4444 (LISTEN)", "x TCP [::1]:5555 (LISTEN)",
             "x TCP 127.0.0.1:43211 (LISTEN)", "x TCP 127.0.0.1:5555 (ESTABLISHED)",
             "x TCP 127.0.0.1:65536 (LISTEN)", LSOF]
    assert agy_quota._ports("\n".join(lines)) == [43210, 43211]


def test_fetch_uses_https_post_with_required_headers_and_timeouts(local_server):
    reading = limits._gemini(refresh=True)
    assert reading.windows == agy_quota.parse_quota(full_example(), NOW).windows
    assert local_server.commands[0][0] == ["ps", "-ax", "-o", "pid=,command="]
    assert local_server.commands[1][0] == [
        "lsof", "-a", "-p", "42", "-iTCP", "-sTCP:LISTEN", "-P", "-n"]
    for _, options in local_server.commands:
        assert options["timeout"] == 3 and options["check"]
        assert options["stderr"] == subprocess.DEVNULL
    request, timeout = local_server.requests[0]
    assert request.full_url == "https://127.0.0.1:43210" + agy_quota._ENDPOINT
    assert request.get_method() == "POST" and request.data == b"{}" and timeout == 3
    assert dict((k.lower(), v) for k, v in request.header_items()) == {
        "content-type": "application/json", "connect-protocol-version": "1",
        "x-codeium-csrf-token": TOKEN}
    handlers = local_server.openers[0].handlers
    https = next(h for h in handlers if isinstance(h, agy_quota.urllib.request.HTTPSHandler))
    assert https._context.verify_mode == ssl.CERT_NONE and not https._context.check_hostname
    assert not any(isinstance(h, agy_quota.urllib.request.ProxyHandler) and h.proxies for h in handlers)
    assert TOKEN not in repr(reading)


@pytest.mark.parametrize("newurl", ["http://127.0.0.1:43210/", "https://example.invalid/"])
def test_redirects_cannot_send_token_to_http_or_another_host(newurl):
    handler = agy_quota._NoRedirect()
    assert handler.redirect_request(None, None, 302, "redirect", {}, newurl) is None


@pytest.mark.parametrize("failure", [
    "ps_error", "no_process", "no_token", "lsof_error", "no_port", "timeout",
    "http_error", "non_200", "bad_json", "no_groups", "empty_groups", "no_buckets",
])
def test_each_failure_returns_secret_free_error_usage(failure, local_server, caplog):
    caplog.set_level(logging.DEBUG)
    if failure == "ps_error":
        local_server.ps_error = subprocess.TimeoutExpired(["ps", TOKEN], 3, output=TOKEN)
    elif failure == "no_process":
        local_server.ps = ""
    elif failure == "no_token":
        local_server.ps = f"42 {EXECUTABLE} --other flag --app_data_dir antigravity"
    elif failure == "lsof_error":
        local_server.lsof_error = OSError(TOKEN)
    elif failure == "no_port":
        local_server.lsof = "x TCP *:43210 (LISTEN)"
    elif failure == "timeout":
        local_server.http_error = TimeoutError(TOKEN)
    elif failure == "http_error":
        local_server.http_error = urllib.error.HTTPError("https://127.0.0.1/", 503, TOKEN, {}, None)
    elif failure == "non_200":
        local_server.status = 503
    elif failure == "bad_json":
        local_server.body = ("not json " + TOKEN).encode()
    elif failure == "no_groups":
        local_server.body = {"response": {"description": TOKEN}}
    elif failure == "empty_groups":
        local_server.body = {"response": {"groups": []}}
    elif failure == "no_buckets":
        local_server.body = payload()
    reading = limits._gemini()
    assert reading.engine == "gemini" and reading.error and not reading.windows
    assert TOKEN not in repr(reading) + reading.source + reading.error
    assert all(TOKEN not in record.getMessage() for record in caplog.records)
    assert all(req.full_url.startswith("https://127.0.0.1:") for req, _ in local_server.requests)
    if failure in ("no_process", "no_token", "ps_error"):
        assert len(local_server.commands) == 1 and not local_server.requests


@pytest.mark.parametrize("first", [503, b"invalid JSON", TimeoutError(TOKEN)])
def test_tries_ports_then_newest_candidates_until_first_valid_json(first, local_server, monkeypatch):
    local_server.ps = (f"41 {EXECUTABLE} --csrf_token={TOKEN} --app_data_dir antigravity\n"
                       + PS)
    local_server.lsof += "x TCP 127.0.0.1:43211 (LISTEN)\n"
    calls = []

    def open_stub(opener, request, *, timeout):
        calls.append(request.full_url)
        index = len(calls)
        if isinstance(first, Exception) and index < 3:
            raise first
        raw = first if isinstance(first, bytes) and index < 3 else json.dumps(full_example()).encode()
        reply = io.BytesIO(raw)
        reply.status = first if isinstance(first, int) and index < 3 else 200
        return reply

    monkeypatch.setattr(agy_quota.urllib.request.OpenerDirector, "open", open_stub)
    assert limits._gemini().error is None
    assert [args[3] for args, _ in local_server.commands if args[0] == "lsof"] == ["42", "41"]
    assert calls == [f"https://127.0.0.1:{port}" + agy_quota._ENDPOINT
                     for port in (43210, 43211, 43210)]


def test_first_valid_json_wins_even_when_groups_are_missing(local_server):
    local_server.body = {}
    local_server.lsof += "x TCP 127.0.0.1:43211 (LISTEN)\n"
    assert limits._gemini().error == "no Antigravity quota groups"
    assert len(local_server.requests) == 1


def test_report_only_never_holds_and_normal_over_cap_still_does():
    window = limits.Window("weekly", 100, NOW + 3600)
    report = limits.Usage("gemini", (window,), blocked=True, report_only=True)
    assert limits.held_back(report, cap=80, now=NOW) is None
    normal = limits.Usage("codex", (window,))
    assert limits.held_back(normal, cap=80, now=NOW) == (
        f"codex is at 100% of its weekly limit (cap 80%); resets {limits.when(NOW + 3600)}",
        NOW + 3600)


@pytest.mark.parametrize("refresh", [False, True])
def test_engine_hold_skips_report_only_fetcher(monkeypatch, refresh):
    monkeypatch.delitem(limits._cache, "gemini", raising=False)
    monkeypatch.setattr(limits, "read_paused", lambda: {})
    fetcher = Mock(side_effect=lambda **kwargs: pytest.fail("gemini hold must not fetch usage"))
    monkeypatch.setitem(limits._FETCHERS, "gemini", fetcher)
    assert limits.engine_hold("gemini", refresh=refresh) is None
    fetcher.assert_not_called()


@pytest.mark.parametrize("paused, reason", [
    (True, "gemini is paused on the dashboard"),
    (None, "gemini pause setting is invalid (engines.gemini.paused must be true or false)"),
])
def test_engine_hold_checks_pause_before_report_only(monkeypatch, paused, reason):
    monkeypatch.setattr(limits, "read_paused", lambda: {"gemini": paused})
    fetcher = Mock(side_effect=lambda **kwargs: pytest.fail("paused engine must not fetch usage"))
    monkeypatch.setitem(limits._FETCHERS, "gemini", fetcher)
    assert limits.engine_hold("gemini") == (reason, None)
    fetcher.assert_not_called()


@pytest.mark.parametrize("engine", ["claude", "codex"])
@pytest.mark.parametrize("blocked", [False, True])
def test_engine_hold_fetches_enforced_usage(monkeypatch, engine, blocked):
    monkeypatch.delitem(limits._cache, engine, raising=False)
    monkeypatch.setattr(limits, "read_paused", lambda: {})
    reset = NOW + 3600
    fetcher = Mock(return_value=limits.Usage(
        engine, (limits.Window("weekly", 90, reset),), blocked=blocked, blocked_until=reset))
    monkeypatch.setitem(limits._FETCHERS, engine, fetcher)
    reason = (f"{engine} has reached its usage limit; resets {limits.when(reset)}" if blocked else
              f"{engine} is at 90% of its weekly limit (cap 80%); resets {limits.when(reset)}")
    assert limits.engine_hold(engine, caps={engine: 80}, refresh=True) == (reason, reset)
    fetcher.assert_called_once_with(refresh=True)


@pytest.mark.parametrize("refresh", [False, True])
def test_describe_still_fetches_report_only_usage(monkeypatch, refresh):
    monkeypatch.delitem(limits._cache, "gemini", raising=False)
    monkeypatch.setattr(limits, "read_paused", lambda: {})
    reading = limits.Usage("gemini", (limits.Window("weekly", 100, NOW + 3600),),
                           report_only=True)
    fetcher = Mock(return_value=reading)
    monkeypatch.setitem(limits._FETCHERS, "gemini", fetcher)
    row = limits.describe("gemini", caps={"gemini": 1}, refresh=refresh)
    assert row["report_only"] and row["held_back"] is None
    assert row["windows"] == asdict(reading)["windows"]
    fetcher.assert_called_once_with(refresh=refresh)


def test_dashboard_serializes_all_windows_and_report_only(local_server, monkeypatch):
    monkeypatch.setitem(limits._FETCHERS, "gemini", limits._gemini)
    monkeypatch.setattr(limits, "_pause_state", lambda engine: (False, None))
    reading = limits.describe("gemini", caps={"gemini": 1})
    assert reading["report_only"] and reading["held_back"] is None
    assert len(reading["windows"]) == 5 and "snapshot about 2 h old" in reading["source"]


def test_watch_shows_report_only_and_never_red_hold(monkeypatch):
    reading = {**asdict(limits.Usage("gemini", (limits.Window("Argon weekly", 100, None),),
                                   report_only=True)), "cap": 80, "held_back": "ignored"}
    monkeypatch.setattr(watch, "ENGINE_NAMES", ("gemini",))
    monkeypatch.setattr(limits, "read_caps", lambda: {"gemini": 80})
    monkeypatch.setattr(limits, "describe", Mock(return_value=reading))
    line = watch._usage_line()
    assert line.plain == "usage  gemini Argon weekly 100% (report only)"
    assert all(span.style != "red" for span in line.spans)
