"""Quota checks are offline and never persist provider identity or error text."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench.quota import QuotaGuard
from hermes_control import limits


@pytest.fixture(autouse=True)
def fresh_report(_isolated_hermes_home):
    path = _isolated_hermes_home / "control" / "usage-claude.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"at": time.time(), "info": {}}))
    return path


def reading(monkeypatch, windows=(), **kwargs):
    calls = []
    def fake(engine, *, refresh=False):
        assert refresh is False
        calls.append((engine, refresh))
        return limits.Usage(engine, windows=tuple(limits.Window(k, v, None) for k, v in windows),
                            account="secret-user@example.invalid", **kwargs)
    monkeypatch.setattr(limits, "usage", fake)
    return calls


@pytest.mark.parametrize("engine", ["codex", "claude"])
@pytest.mark.parametrize("label,used,allowed", [
    ("5-hour", 89.99, True), ("5-hour", 90, False), ("5h", 91, False),
    ("weekly", 79.99, True), ("weekly", 80, False), ("weekly", 100, False)])
def test_thresholds(monkeypatch, engine, label, used, allowed):
    calls = reading(monkeypatch, [(label, used)])
    guard = QuotaGuard(reserve=0)
    assert guard(engine) is allowed
    assert calls == [(engine, False)]
    assert guard.last_windows == {label: used}
    assert (label in guard.last_reason) is (not allowed)


def test_custom_thresholds_and_all_counted_windows(monkeypatch):
    reading(monkeypatch, [("primary 5-hour", 30), ("secondary 5-hour", 60), ("weekly", 20)])
    guard = QuotaGuard(max_5h=60, max_weekly=70, reserve=0)
    assert not guard("codex")
    assert "secondary 5-hour" in guard.last_reason
    guard.max_5h = 60.1
    assert guard("codex")


@pytest.mark.parametrize("model,blocked_bucket", [
    ("gemini-4-argon", "Gemini 4 Argon weekly"),
    ("gemini-4-argon-high", "Gemini Models 5h"),
    ("gemini-4-argon", "Gemini Models weekly"),
    ("claude-opus-4", "Claude and GPT models 5h"),
    ("gpt-6", "Claude and GPT models weekly")])
def test_gemini_bucket_matching(monkeypatch, model, blocked_bucket):
    labels = ["Gemini 4 Argon weekly", "Gemini Models 5h", "Gemini Models weekly",
              "Claude and GPT models 5h", "Claude and GPT models weekly"]
    guard = QuotaGuard()
    reading(monkeypatch, [(label, 100 if label == blocked_bucket else 1) for label in labels])
    assert not guard({"engine": "gemini", "model": model})
    assert blocked_bucket in guard.last_reason
    # Exhaust the other bucket; the lane's own bucket remains available.
    argon = model.startswith("gemini")
    reading(monkeypatch, [(label, 1 if ("Claude and GPT" not in label) == argon else 100)
                         for label in labels])
    assert guard("gemini", model)
    assert all(("Claude and GPT" not in label) == argon for label in guard.last_windows)


@pytest.mark.parametrize("windows,kwargs,reason", [
    ([], {}, "no quota windows"),
    ([("5-hour", 0)], {"error": "private-account@example.invalid failed"}, "error"),
    ([("weekly", 0)], {"blocked": True}, "blocked"),
    ([("weekly", None)], {}, "unknown usage"),
    ([("weekly", float("nan"))], {}, "unknown usage"),
    ([("weekly", True)], {}, "unknown usage"),
    ([(None, 1)], {}, "no matching quota windows"),
    ([("daily", 1)], {}, "no matching quota windows")])
def test_fail_closed_without_account_or_error_retention(monkeypatch, windows, kwargs, reason, capsys):
    reading(monkeypatch, windows, **kwargs)
    guard = QuotaGuard()
    assert not guard("claude")
    assert reason in guard.last_reason
    saved = json.dumps(guard.__dict__, allow_nan=False) + capsys.readouterr().out
    assert "private-account" not in saved and "secret-user" not in saved
    assert "account" not in guard.__dict__


def test_gemini_missing_matching_bucket_fails_closed(monkeypatch):
    reading(monkeypatch, [("Gemini Models weekly", 1)])
    guard = QuotaGuard()
    assert not guard("gemini", "claude-opus-4")
    assert "no matching" in guard.last_reason


def test_provider_exception_is_redacted(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("secret-user@example.invalid")
    monkeypatch.setattr(limits, "usage", fail)
    guard = QuotaGuard()
    assert not guard("codex")
    assert guard.last_reason == "codex: quota read error"


@pytest.mark.parametrize("value", [-1, 101, float("inf"), float("nan"), "90"])
def test_invalid_threshold(value):
    with pytest.raises(ValueError, match="max_5h"):
        QuotaGuard(max_5h=value)


def test_stale_claude_fails_closed_unless_allowed(monkeypatch, fresh_report):
    monkeypatch.setattr(time, "time", lambda: 10000)
    fresh_report.write_text(json.dumps({"at": 6400, "info": {}}))
    reading(monkeypatch, [("5-hour", 10), ("weekly", 20)])
    guard = QuotaGuard()
    assert not guard("claude")
    assert guard.last_reason == "claude reading is 60 min old"
    assert QuotaGuard(allow_stale_claude=True)("claude")
    assert QuotaGuard(max_age=3600)("claude")
    assert not QuotaGuard(max_age=3599)("claude")


@pytest.mark.parametrize("report", [None, {}, {"at": True}, {"at": "today"}, {"at": float("nan")}])
def test_unknown_claude_age_fails_closed(monkeypatch, fresh_report, report):
    reading(monkeypatch, [("weekly", 10)])
    if report is None:
        fresh_report.unlink()
    else:
        fresh_report.write_text(json.dumps(report))
    guard = QuotaGuard()
    assert not guard("claude")
    assert guard.last_reason == "claude reading age unknown"


def test_each_check_forgets_cache_without_renewing_login(monkeypatch):
    calls = []
    monkeypatch.setattr(limits, "forget", lambda engine: calls.append(("forget", engine)))
    def usage(engine, **kwargs):
        assert not kwargs
        calls.append(("usage", engine))
        return limits.Usage(engine, windows=(limits.Window("weekly", 10, None),))
    monkeypatch.setattr(limits, "usage", usage)
    guard = QuotaGuard()
    assert guard("codex") and guard.snapshot({"engine": "codex"}) == {"weekly": 10}
    assert calls == [("forget", "codex"), ("usage", "codex")] * 2


def test_claude_report_read_explicitly_uses_utf8(monkeypatch, fresh_report):
    reading(monkeypatch, [('weekly', 10)])
    fresh_report.write_text(json.dumps({'at': time.time(), 'info': {}, 'label': 'café'},
                                     ensure_ascii=False), encoding='utf-8')
    original = Path.read_text
    def read(path, *args, **kwargs):
        if path == fresh_report:
            assert kwargs.get('encoding') == 'utf-8'
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', read)
    assert QuotaGuard()('claude')
