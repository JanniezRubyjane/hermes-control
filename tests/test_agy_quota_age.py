"""Snapshot ages from synthetic payloads; no discovery or server calls."""

from datetime import datetime, timezone

import pytest

from hermes_control.agy_quota import parse_quota
from hermes_control.limits import Window

NOW = 1_924_992_000.0


def bucket(window, reset, description=None):
    result = {"window": window, "remainingFraction": 1,
              "resetTime": datetime.fromtimestamp(reset, timezone.utc).isoformat()}
    if description is not None:
        result["description"] = description
    return result


def payload(*buckets):
    return {"response": {"groups": [{"displayName": "Pool", "buckets": list(buckets)}]}}


@pytest.mark.parametrize("description", [None, ""])
@pytest.mark.parametrize("exact_first", [False, True])
def test_unused_five_hour_age_overrides_coarse_countdown(description, exact_first):
    exact = bucket("5h", NOW - 7 * 60 + 5 * 3600, description)
    coarse = bucket("weekly", NOW + 6 * 86400, "It will fully refresh in 6 days.")
    buckets = [exact, coarse] if exact_first else [coarse, exact]
    reading = parse_quota(payload(*buckets), NOW)
    assert reading.source == (
        "the Antigravity app's language server, snapshot about 7 min old; "
        "report only, never holds work back")
    assert reading.windows[buckets.index(exact)] == Window("Pool 5h", 0, None)
    assert reading.report_only


def test_unused_five_hour_age_survives_expired_reset():
    reading = parse_quota(payload(bucket("5h", NOW - 8 * 3600 + 5 * 3600)), NOW)
    assert "snapshot about 8 h old;" in reading.source
    assert reading.windows == (Window("Pool 5h", None, None),)


@pytest.mark.parametrize("description", ["Used", "It will fully refresh in 2 hours."])
def test_used_five_hour_bucket_is_not_an_exact_age_source(description):
    reading = parse_quota(payload(bucket("5h", NOW - 7 * 60 + 5 * 3600, description)), NOW)
    if description == "Used":
        assert "snapshot age unknown;" in reading.source
    else:
        assert "snapshot about 0 min old (countdown in whole hours);" in reading.source


@pytest.mark.parametrize("countdown, duration, note", [
    ("6 days", 6 * 86400, " (countdown in whole days)"),
    ("1 day", 86400, " (countdown in whole days)"),
    ("4 days, 17 hours", 4 * 86400 + 17 * 3600, " (countdown in whole hours)"),
    ("1 hour", 3600, " (countdown in whole hours)"),
    ("2 hours, 7 minutes", 2 * 3600 + 7 * 60, ""),
    ("1 minute", 60, ""),
])
def test_countdown_age_reports_smallest_unit(countdown, duration, note):
    reading = parse_quota(payload(bucket(
        "weekly", NOW - 7 * 60 + duration,
        f"It will fully refresh in {countdown}.")), NOW)
    assert reading.source == (
        f"the Antigravity app's language server, snapshot about 7 min old{note}; "
        "report only, never holds work back")
