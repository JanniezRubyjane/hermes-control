"""How much of each engine's subscription is used, and when to hold work back."""

import io
import json
import contextlib
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
import yaml

from hermes_control import limits
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.codex import CodexAdapter

NOW = 1_790_160_000.0
LATER = NOW + 3600

# `account/rateLimits/read` from `codex app-server` (codex-cli 0.155.1), trimmed.
CODEX_RATE_LIMITS = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {"limitId": "codex", "planType": "free", "rateLimitReachedType": None,
                   "primary": {"usedPercent": 41, "windowDurationMins": 43200,
                               "resetsAt": int(LATER)},
                   "secondary": None},
}

# claude's `rate_limit_event` payload (claude-code 2.1.280).
CLAUDE_REPORT = {"status": "allowed", "resetsAt": int(LATER), "rateLimitType": "five_hour",
                 "unifiedWindows": {"five_hour": {"utilization": 0.27, "resetsAt": int(LATER)},
                                    "seven_day": {"utilization": 0.08, "resetsAt": int(LATER)}}}


@pytest.fixture(autouse=True)
def _clear_claude_account_cache(monkeypatch):
    monkeypatch.setattr(limits, "_claude_account_cache", None)


# --- the cap setting -------------------------------------------------------------

def test_every_engine_has_a_cap_of_80_percent_by_default():
    assert limits.parse_caps(None) == {"claude": 80, "codex": 80}


def test_a_cap_can_be_set_per_engine():
    assert limits.parse_caps({"codex": {"max_percent": 95}}) == {"claude": 80, "codex": 95}


@pytest.mark.parametrize("raw, message", [
    ({"gemini": {"max_percent": 50}}, "unknown engine"),
    ({"claude": {"max_percent": 0}}, "1 and 100"),
    ({"claude": {"max_percent": 101}}, "1 and 100"),
    ({"claude": {"max_percent": "lots"}}, "1 and 100"),
    ({"claude": {"max_percent": 50, "burst": 1}}, "burst"),
])
def test_bad_caps_are_refused_with_the_reason(raw, message):
    with pytest.raises(ValueError, match=message):
        limits.parse_caps(raw)


def test_caps_are_saved_where_hermes_keeps_plugin_settings(_isolated_hermes_home):
    limits.save_caps({"claude": {"max_percent": 70}})

    config = yaml.safe_load((_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8"))
    saved = config["plugins"]["entries"]["hermes_control"]["settings"]["limits"]
    assert saved == {"claude": {"max_percent": 70}, "codex": {"max_percent": 80}}
    assert limits.read_caps() == {"claude": 70, "codex": 80}


# --- reading usage ---------------------------------------------------------------

def test_codex_usage_comes_from_codex_itself():
    usage = limits.codex_usage_from_rate_limits(CODEX_RATE_LIMITS)

    assert usage.windows == (limits.Window("30-day", 41.0, LATER),)
    assert usage.blocked is False
    assert usage.plan == "free"


def test_codex_says_when_its_limit_is_reached():
    blocked = json.loads(json.dumps(CODEX_RATE_LIMITS))
    blocked["rateLimits"]["rateLimitReachedType"] = "rate_limit_reached"
    blocked["rateLimits"]["primary"]["usedPercent"] = 100

    usage = limits.codex_usage_from_rate_limits(blocked)

    assert usage.blocked is True
    assert usage.blocked_until == LATER


def test_codex_block_uses_only_exhausted_windows_reset():
    result = {"rateLimits": {"rateLimitReachedType": "rate_limit_reached",
                             "primary": {"usedPercent": 100, "resetsAt": NOW + 60},
                             "secondary": {"usedPercent": 5, "resetsAt": NOW + 604800}}}
    usage = limits.codex_usage_from_rate_limits(result)

    assert usage.blocked_until == NOW + 60
    assert limits.held_back(usage, cap=80, now=NOW + 61) is None


@pytest.mark.parametrize("blocked_field", ["rateLimitReachedType", "ordinaryUsageAllowed"])
def test_codex_provider_block_without_full_window_uses_first_known_reset(blocked_field):
    result = {"rateLimits": {"primary": {"usedPercent": 40, "resetsAt": NOW + 60},
                             "secondary": {"usedPercent": 5, "resetsAt": LATER}}}
    if blocked_field == "rateLimitReachedType":
        result["rateLimits"][blocked_field] = "rate_limit_reached"
    else:
        result[blocked_field] = False

    assert limits.codex_usage_from_rate_limits(result).blocked_until == NOW + 60


def test_codex_provider_block_without_known_reset_has_unknown_end():
    result = {"ordinaryUsageAllowed": False, "rateLimits": {"primary": {"usedPercent": 20}}}

    assert limits.codex_usage_from_rate_limits(result).blocked_until is None


def test_codex_missing_percentage_is_unknown_and_does_not_hold_work():
    result = {"rateLimits": {"primary": {"resetsAt": LATER},
                             "secondary": {"usedPercent": 85, "resetsAt": NOW + 60}}}
    usage = limits.codex_usage_from_rate_limits(result)

    assert usage.windows[0].used_percent is None
    assert limits.held_back(usage, cap=80, now=NOW)[1] == NOW + 60
    assert limits.held_back(usage, cap=80, now=NOW + 61) is None


def test_claude_usage_from_its_own_report_in_a_run():
    usage = limits.claude_usage_from_report(CLAUDE_REPORT, at=NOW)

    assert [(w.label, w.used_percent) for w in usage.windows] == [("5-hour", 27.0), ("weekly", 8.0)]
    assert usage.blocked is False
    assert "claude's own report" in usage.source


def test_claude_report_missing_utilization_is_unknown():
    report = {**CLAUDE_REPORT, "unifiedWindows": {
        "five_hour": {"resetsAt": LATER}}}

    reading = limits.claude_usage_from_report(report, at=NOW)

    assert reading.windows[0].used_percent is None
    assert limits.held_back(reading, cap=1, now=NOW) is None


def test_a_rejected_claude_report_means_blocked_until_its_reset():
    usage = limits.claude_usage_from_report({**CLAUDE_REPORT, "status": "rejected"}, at=NOW)

    assert usage.blocked is True
    assert usage.blocked_until == LATER


def test_claude_report_records_current_account(monkeypatch):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: ("old@example.com", "pro"))

    limits.record_claude_report(CLAUDE_REPORT, at=NOW)

    assert json.loads(limits._report_path().read_text())["account"] == "old@example.com"


def test_claude_report_survives_account_lookup_failure(monkeypatch):
    def broken_auth():
        raise RuntimeError("probe failed")

    monkeypatch.setattr(limits, "_claude_auth_status", broken_auth)
    limits.record_claude_report(CLAUDE_REPORT, at=NOW)

    assert json.loads(limits._report_path().read_text())["account"] is None


def test_claude_report_survives_write_failure(monkeypatch):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: ("me@example.com", "pro"))
    monkeypatch.setattr(limits, "_report_path", lambda: (_ for _ in ()).throw(RuntimeError("disk failed")))

    limits.record_claude_report(CLAUDE_REPORT, at=NOW)


def test_claude_report_reuses_recent_account(monkeypatch):
    calls = []
    monkeypatch.setattr(limits, "_claude_auth_status",
                        lambda: calls.append(1) or ("me@example.com", "pro"))

    limits.record_claude_report(CLAUDE_REPORT, at=NOW)
    limits.record_claude_report(CLAUDE_REPORT, at=NOW + 1)

    assert calls == [1]


def test_claude_report_replaces_file_atomically(monkeypatch):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: ("me@example.com", "pro"))
    path = limits._report_path()
    path.write_text("old report")
    real_replace = limits.os.replace

    def inspect_replace(source, target):
        assert path.read_text() == "old report"
        assert source.parent == target.parent == path.parent
        assert json.loads(source.read_text())["account"] == "me@example.com"
        real_replace(source, target)

    monkeypatch.setattr(limits.os, "replace", inspect_replace)
    limits.record_claude_report(CLAUDE_REPORT, at=NOW)

    assert json.loads(path.read_text())["at"] == NOW
    assert sorted(path.parent.glob("usage-claude.json.*")) == []


@pytest.mark.parametrize("report_account,current_account,accepted", [
    ("old@example.com", "new@example.com", False),
    ("old@example.com", None, False),
    ("old@example.com", "old@example.com", True),
    (None, "new@example.com", True),
])
def test_claude_report_is_bound_to_account_or_legacy_unknown_account(
        monkeypatch, report_account, current_account, accepted):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: (current_account, "pro"))
    monkeypatch.setattr(limits.time, "time", lambda: NOW)
    report = {"at": NOW, "info": CLAUDE_REPORT}
    if report_account is not None:
        report["account"] = report_account
    limits._report_path().write_text(json.dumps(report))

    usage = limits._claude()

    assert (usage.error is None) is accepted
    if not accepted:
        assert limits.held_back(usage, cap=1, now=NOW) is None


@pytest.mark.parametrize("account", [None, "me@example.com"])
def test_accountless_claude_report_expires_after_five_hours_even_with_weekly_reset(
        monkeypatch, account):
    monkeypatch.setattr(limits.time, "time", lambda: NOW + 5 * 3600 + 1)
    report = {"at": NOW, "account": account, "info": {"status": "rejected",
              "unifiedWindows": {"seven_day": {"utilization": 1,
                                              "resetsAt": NOW + 7 * 86400}}}}
    limits._report_path().write_text(json.dumps(report))

    reading = limits._claude_usage(current_account="me@example.com")

    assert (reading.error is not None) is (account is None)


@pytest.mark.parametrize("info,age", [
    ({"status": "rejected", "resetsAt": NOW + 60}, 61),
    ({"status": "rejected", "unifiedWindows": {
        "five_hour": {"utilization": 1, "resetsAt": NOW + 60},
        "seven_day": {"utilization": 0.05, "resetsAt": LATER}}}, 3601),
    ({"status": "rejected"}, 5 * 3600 + 1),
])
def test_claude_stale_report_is_unknown_and_does_not_hold_work(monkeypatch, info, age):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: ("me@example.com", "pro"))
    monkeypatch.setattr(limits.time, "time", lambda: NOW + age)
    limits._report_path().write_text(json.dumps({"at": NOW, "info": info}))

    usage = limits._claude()

    assert usage.error and "stale" in usage.error
    assert limits.held_back(usage, cap=1, now=NOW + age) is None


def test_claude_report_without_reset_is_valid_for_five_hours(monkeypatch):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: (None, None))
    monkeypatch.setattr(limits.time, "time", lambda: NOW + 5 * 3600)
    limits._report_path().write_text(json.dumps({"at": NOW, "info": {"status": "rejected"}}))

    assert limits._claude().blocked is True


def test_readings_are_cached_until_a_refresh(monkeypatch):
    calls = []
    monkeypatch.setitem(limits._FETCHERS, "codex",
                        lambda refresh=False: calls.append(refresh) or limits.Usage(engine="codex"))

    limits.usage("codex")
    limits.usage("codex")
    limits.usage("codex", refresh=True)

    # A refresh also tells the reader to renew the engine's sign-in (a plan change shows).
    assert calls == [False, True]


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize("refill", [False, True])
def test_forget_during_fetch_does_not_restore_stale_usage(monkeypatch, engine, refill):
    started, release = threading.Event(), threading.Event()
    old = limits.Usage(engine=engine)
    new = limits.Usage(engine=engine, blocked=True)

    def fetch(refresh=False):
        started.set()
        assert release.wait(5)
        return old

    monkeypatch.setitem(limits._FETCHERS, engine, fetch)
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(limits.usage, engine)
        try:
            assert started.wait(5)
            limits.forget(engine)
            monkeypatch.setitem(limits._FETCHERS, engine, lambda refresh=False: new)
            if refill:
                assert limits.usage(engine) is new
        finally:
            release.set()
        assert future.result(timeout=5) is old
    assert limits.usage(engine) is new


def test_codex_names_its_account_and_the_plan_on_it():
    """Seen for real: codex signed in to one ChatGPT account, Plus bought on another."""
    usage = limits.codex_usage_from_rate_limits(
        CODEX_RATE_LIMITS, account={"type": "chatgpt", "email": "me@example.com", "planType": "plus"})

    assert (usage.account, usage.plan) == ("me@example.com", "plus")


def test_claude_names_its_account_and_subscription_from_its_auth_status():
    status = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "me@example.com",
                         "subscriptionType": "max", "orgName": "x"})

    assert limits.claude_account(status) == ("me@example.com", "max")
    assert limits.claude_account("not json") == (None, None)


def test_claude_usage_carries_its_account(monkeypatch, tmp_path):
    from hermes_control.engines.claude import ClaudeAdapter

    script = tmp_path / "claude"
    script.write_text('#!/bin/sh\necho \'{"loggedIn": true, "email": "me@example.com", '
                      '"subscriptionType": "pro"}\'\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(ClaudeAdapter, "probe",
                        lambda self: EngineInfo(name="claude", version="t", path=str(script)))
    monkeypatch.setattr(limits.time, "time", lambda: NOW)
    limits.record_claude_report(CLAUDE_REPORT, at=NOW)

    usage = limits._claude()

    assert (usage.account, usage.plan) == ("me@example.com", "pro")


# --- holding work back -------------------------------------------------------------

def _usage(*percents, blocked=False):
    return limits.Usage(engine="claude", blocked=blocked, blocked_until=LATER if blocked else None,
                        windows=tuple(limits.Window(f"w{i}", p, LATER)
                                      for i, p in enumerate(percents)))


def test_under_the_cap_the_engine_takes_work():
    assert limits.held_back(_usage(79.0, 10.0), cap=80, now=NOW) is None


def test_at_the_cap_the_engine_is_held_back_until_that_window_resets():
    reason, until = limits.held_back(_usage(12.0, 85.0), cap=80, now=NOW)

    assert "85% of its w1 limit (cap 80%)" in reason
    assert until == LATER


def test_every_window_over_the_cap_is_named_and_the_last_reset_given():
    """Only when every window over the cap has reset can the engine take work: naming
    one window beside another's reset time misleads."""
    reading = limits.Usage(engine="claude", windows=(
        limits.Window("5-hour", 49.0, NOW + 60), limits.Window("weekly", 15.0, LATER)))

    reason, until = limits.held_back(reading, cap=10, now=NOW)

    assert "49% of its 5-hour limit and 15% of its weekly limit (cap 10%)" in reason
    assert "the last of them resets" in reason and until == LATER


def test_a_window_that_has_already_reset_does_not_count():
    stale = limits.Usage(engine="codex", windows=(limits.Window("5-hour", 99.0, NOW - 1),))

    assert limits.held_back(stale, cap=80, now=NOW) is None


def test_a_blocked_engine_is_held_back_whatever_the_cap():
    reason, until = limits.held_back(_usage(50.0, blocked=True), cap=100, now=NOW)

    assert "usage limit" in reason and until == LATER


def test_usage_that_could_not_be_read_never_holds_work_back():
    unknown = limits.Usage(engine="codex", error="app-server did not answer")

    assert limits.held_back(unknown, cap=1, now=NOW) is None


def test_reset_time_includes_server_time_zone():
    rendered = limits.when(LATER)

    assert rendered.startswith(time.strftime("%Y-%m-%d %H:%M", time.localtime(LATER)))
    assert rendered.endswith(time.strftime("%z", time.localtime(LATER)))


# --- the real codex ------------------------------------------------------------

@pytest.mark.integration
def test_the_real_codex_reports_its_rate_limits():
    import shutil

    if not shutil.which("codex"):
        pytest.skip("codex not installed")

    usage = limits._codex()

    assert usage.error is None, usage.error
    assert usage.windows and all(0 <= w.used_percent <= 100 for w in usage.windows)
    assert usage.account and "@" in usage.account and usage.plan


@pytest.mark.live_system_guard_bypass  # _codex signals the fake app-server's real process group
def test_codex_keeps_its_account_when_the_answers_come_back_out_of_order(monkeypatch, tmp_path):
    """Seen on the dashboard: with a sign-in renewal, codex answered the rate limits
    before the account, and the reading lost its account."""
    script = tmp_path / "codex"
    script.write_text(f"""#!{sys.executable}
import json, sys, time
RATE_LIMITS = json.loads({json.dumps(json.dumps(CODEX_RATE_LIMITS))})
pending = {{}}
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("id") == 1:
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    elif msg.get("id") in (2, 3):
        pending[msg["id"]] = msg
    if len(pending) == 2:
        print(json.dumps({{"id": 3, "result": RATE_LIMITS}}), flush=True)
        time.sleep(0.3)
        print(json.dumps({{"id": 2, "result": {{"account": {{"type": "chatgpt", "email": "me@example.com", "planType": "plus"}}}}}}), flush=True)
        pending.clear()
""", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo(name="codex", version="t", path=str(script)))

    usage = limits._codex(refresh=True)

    assert (usage.account, usage.plan) == ("me@example.com", "plus")


def test_codex_stops_its_group_when_reader_thread_cannot_start(monkeypatch):
    process = Mock(pid=12345)
    stopped = Mock()
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo(name="codex", version="t", path="fake-codex"))
    monkeypatch.setattr(limits, "engine_env", lambda: {})
    monkeypatch.setattr(limits.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(limits, "stop_group", stopped)

    def fail_start(self):
        raise OSError("thread cannot start")

    monkeypatch.setattr(limits.threading.Thread, "start", fail_start)
    reading = limits._codex()
    assert reading.error == "codex app-server: thread cannot start"
    stopped.assert_called_once_with(process, 2)


def test_codex_returns_reading_when_group_stop_raises_oserror(monkeypatch):
    answers = [
        {"id": 2, "result": {"account": {"type": "chatgpt", "email": "me@example.com",
                                         "planType": "plus"}}},
        {"id": 3, "result": CODEX_RATE_LIMITS},
    ]
    process = Mock(pid=12345, stdout=io.StringIO("".join(json.dumps(a) + "\n" for a in answers)),
                   stdin=io.StringIO())
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo(name="codex", version="t", path="fake-codex"))
    monkeypatch.setattr(limits, "engine_env", lambda: {})
    monkeypatch.setattr(limits.subprocess, "Popen", lambda *args, **kwargs: process)
    stopped = Mock(side_effect=OSError("killpg failed"))
    monkeypatch.setattr(limits, "stop_group", stopped)
    real_join = limits.threading.Thread.join
    joined = []

    def join(self, timeout=None):
        joined.append(self)
        return real_join(self, timeout)

    monkeypatch.setattr(limits.threading.Thread, "join", join)

    reading = limits._codex()

    assert reading.error is None
    assert reading.account == "me@example.com"
    stopped.assert_called_once_with(process, 2)
    assert len(joined) == 1


def _codex_with_background_child(monkeypatch, tmp_path, *, answers):
    child_pid_file = tmp_path / "child.pid"
    launcher_pid_file = tmp_path / "launcher.pid"
    script = tmp_path / "codex"
    script.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])
open({str(child_pid_file)!r}, 'w').write(str(child.pid))
open({str(launcher_pid_file)!r}, 'w').write(str(os.getpid()))
for _ in range(4):
    sys.stdin.readline()
if {answers!r}:
    print(json.dumps({{'id': 3, 'result': {{'rateLimits': {{'primary': {{'usedPercent': 41}}}}}}}}), flush=True)
time.sleep(300)
""", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setattr(CodexAdapter, "probe",
                        lambda self: EngineInfo(name="codex", version="t", path=str(script)))
    monkeypatch.setattr(limits, "_CODEX_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(limits, "_ACCOUNT_GRACE_SECONDS", 0.05)
    return child_pid_file, launcher_pid_file


def _assert_codex_child_stopped(child_pid_file, launcher_pid_file):
    assert child_pid_file.is_file()
    child_pid = int(child_pid_file.read_text())
    launcher_pid = int(launcher_pid_file.read_text())
    try:
        for _ in range(20):
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("codex app-server's background child survived the reading")
        with pytest.raises(ProcessLookupError):
            os.killpg(launcher_pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(child_pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            os.kill(launcher_pid, signal.SIGKILL)


@pytest.mark.live_system_guard_bypass  # signals the fake app-server and its own child
def test_codex_usage_stops_app_server_child_after_reply(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX process groups are required")
    child_pid_file, launcher_pid_file = _codex_with_background_child(
        monkeypatch, tmp_path, answers=True)

    usage = limits._codex()

    _assert_codex_child_stopped(child_pid_file, launcher_pid_file)
    assert usage.error is None
    assert usage.windows[0].used_percent == 41


@pytest.mark.live_system_guard_bypass  # signals the silent fake app-server and its own child
def test_codex_usage_stops_app_server_child_after_timeout(monkeypatch, tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX process groups are required")
    child_pid_file, launcher_pid_file = _codex_with_background_child(
        monkeypatch, tmp_path, answers=False)

    usage = limits._codex()

    _assert_codex_child_stopped(child_pid_file, launcher_pid_file)
    assert usage.error == "codex app-server did not answer account/rateLimits/read"
