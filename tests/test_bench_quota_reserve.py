"""Reserve and learned attempt margins use only offline quota readings."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_controller, arm_bare, arm_full
from bench.quota import QuotaGuard, after
from hermes_control import limits


@pytest.fixture
def usage(monkeypatch):
    values = {"codex": {"weekly": 20}, "gemini": {"weekly": 20}}
    monkeypatch.setattr(limits, "forget", lambda engine: None)
    monkeypatch.setattr(limits, "usage", lambda engine: limits.Usage(
        engine, windows=tuple(limits.Window(label, value, None)
                              for label, value in values[engine].items())))
    return values


@pytest.mark.parametrize("used,allowed", [(76, False), (75, False), (74, True)])
def test_default_reserve(usage, used, allowed):
    usage["codex"]["weekly"] = used
    guard = QuotaGuard()
    assert guard("codex") is allowed
    if not allowed:
        assert guard.last_reason == f"codex: weekly at {used}% + margin 5 (limit 80%)"


@pytest.mark.parametrize("value", [-1, 101, float("inf"), float("nan"), "5", None, True])
def test_invalid_reserve(value):
    with pytest.raises(ValueError, match="reserve"):
        QuotaGuard(reserve=value)


def test_zero_reserve_allows_old_boundary(usage):
    usage["codex"]["weekly"] = 79.99
    assert QuotaGuard(reserve=0)("codex")
    usage["codex"]["weekly"] = 80
    assert not QuotaGuard(reserve=0)("codex")
    assert QuotaGuard(reserve=100).reserve == 100


def test_learns_largest_increase_and_keeps_it_after_reset(usage):
    guard = QuotaGuard()
    assert guard("codex")
    usage["codex"]["weekly"] = 30.4
    assert after(guard, {"engine": "codex"}) == {"weekly": 30.4}
    usage["codex"]["weekly"] = 70
    assert not guard("codex")
    assert "margin 10.4" in guard.last_reason
    usage["codex"]["weekly"] = 2
    guard.snapshot({"engine": "codex"})
    assert guard("codex")
    usage["codex"]["weekly"] = 3
    guard.snapshot({"engine": "codex"})
    usage["codex"]["weekly"] = 70
    assert not guard("codex")
    assert "margin 10.4" in guard.last_reason


@pytest.mark.parametrize("end", [2, None, "unknown", float("nan"), float("inf")])
def test_reset_and_non_numbers_not_learned(usage, end):
    guard = QuotaGuard()
    usage["codex"]["weekly"] = 60
    assert guard("codex")
    usage["codex"]["weekly"] = end
    guard.snapshot({"engine": "codex"})
    usage["codex"]["weekly"] = 74
    assert guard("codex")


def test_snapshot_without_preceding_call_for_engine_learns_nothing(usage):
    guard = QuotaGuard()
    assert guard("gemini")
    usage["codex"]["weekly"] = 60
    assert guard.snapshot({"engine": "codex"}) == {"weekly": 60}
    assert guard._increases["codex"] == {}
    usage["codex"]["weekly"] = 74
    assert guard("codex")


@pytest.mark.parametrize("start", [None, "unknown", float("nan"), float("inf"), True])
def test_non_numeric_earlier_reading_learns_nothing(usage, start):
    guard = QuotaGuard()
    usage["codex"]["weekly"] = start
    assert not guard("codex")
    usage["codex"]["weekly"] = 60
    assert guard.snapshot({"engine": "codex"}) == {"weekly": 60}
    assert guard._increases["codex"] == {}
    usage["codex"]["weekly"] = 74
    assert guard("codex")


def test_engines_and_windows_learn_separately(usage):
    guard = QuotaGuard()
    usage["codex"] = {"weekly": 20, "5h": 20}
    usage["gemini"]["weekly"] = 60
    assert guard("codex")
    assert guard("gemini")
    usage["codex"] = {"weekly": 30.4, "5h": 22}
    guard.snapshot({"engine": "codex"})
    usage["gemini"]["weekly"] = 62
    guard.snapshot({"engine": "gemini"})
    usage["codex"] = {"weekly": 20, "5h": 84}
    assert guard("codex")
    usage["codex"]["weekly"] = 70
    assert not guard("codex")
    usage["gemini"]["weekly"] = 74
    assert guard("gemini")


@pytest.mark.parametrize("arm", [arm_controller, arm_bare, arm_full])
@pytest.mark.parametrize("option,expected", [([], 5), (["--reserve", "12.5"], 12.5)])
def test_cli_reserve_reaches_guard(monkeypatch, tmp_path, arm, option, expected):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *args, **kwargs: calls.append(kwargs))
    args = ["--suite", str(tmp_path / "suite"), "--out", str(tmp_path / "results")]
    if arm is not arm_full:
        args += ["--lanes", '[{"engine":"codex"}]']
    assert arm.main(args + option) == 0
    assert calls[0]["quota_guard"].reserve == expected
