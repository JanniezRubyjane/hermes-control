"""Controller warnings remain visible even when a committed task's check passes."""

import pytest
from test_runs import DONE_TAIL, PLAN, T0, _session, _write

from hermes_control import runs


WARNING = "warning: committed diff changes .gemini/ settings"


@pytest.mark.parametrize(
    "verify_reason, summary, status, reason",
    [
        (WARNING, {"verdict": "unproven"}, "unproven", WARNING),
        (WARNING, None, "unproven", WARNING),
        (None, {"verdict": "unproven"}, "unproven", "not verified"),
        (None, {"verdict": "unproven", "unproven_reason": "controller warning"},
         "unproven", "controller warning"),
        (None, {"verdict": "proven", "unproven_reason": "controller warning"},
         "unproven", "controller warning"),
        (WARNING, {"verdict": "unproven", "unproven_reason": "controller warning"},
         "unproven", WARNING),
        (None, {"verdict": "proven", "unproven_reason": None}, "done", None),
        (None, None, "done", None),
    ],
)
def test_passed_check_respects_controller_verdict(verify_reason, summary, status, reason):
    events = [
        ("", "", "plan", PLAN),
        _session("gemini"),
        ("a", "gemini", "commit", DONE_TAIL[1][3]),
        ("a", "gemini", "verify", {**DONE_TAIL[2][3], "unproven_reason": verify_reason}),
    ]
    if summary is not None:
        events.append(("", "", "summary", {"tasks": [{"task_id": "a", **summary}]}))
    _write("status-verdict", *events)

    view = runs.run_view("status-verdict", now=lambda: T0 + 60)

    assert view is not None
    [task] = view["tasks"]
    assert (task["status"], task["status_reason"]) == (status, reason)


def test_plan_task_without_id_is_skipped():
    plan = {**PLAN, "tasks": [{"engine": "gemini", "goal": "missing id"}, *PLAN["tasks"]]}
    _write("missing-plan-id", ("", "", "plan", plan))

    view = runs.run_view("missing-plan-id", now=lambda: T0 + 60)

    assert view is not None
    assert [task["id"] for task in view["tasks"]] == ["a"]
