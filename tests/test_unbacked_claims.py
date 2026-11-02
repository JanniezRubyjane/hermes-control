"""Check worker command claims against in-memory tool events."""

import pytest

from hermes_control.events import ControlEvent
from hermes_control.review_pack import _unbacked_claims
from hermes_control.worker_report import parse


@pytest.mark.parametrize("tool_input,claim,backed", [
    ({"CommandLine": "/bin/sh scripts/sandbox-check.sh tests/test_verify_consumers.py"},
     "/bin/sh scripts/sandbox-check.sh tests/test_verify_consumers.py", True),
    ({"command": "pytest -q"}, "pytest -q", True),
    ({"command": "pytest -q"}, "ruff check", False),
    ({"command": "python3 other.py"}, "python3 -c …", False),
    ({"command": "pytest -q"}, "…", False),
    ({"CommandLine": ["pytest", "-q"]}, "pytest -q", False),
    ({"command": "pytest -q"}, "pytest -q missing.py", False),
    ({"command": "pytest -q", "CommandLine": "ruff check"}, "ruff check", False),
    ({"command": None, "CommandLine": "pytest -q"}, "pytest -q", False),
])
def test_command_evidence(tool_input, claim, backed):
    assert_unbacked(tool_input, claim, backed)


@pytest.mark.parametrize("ellipsis", ["…", "..."])
@pytest.mark.parametrize("prefix", ["python3 -c ", "python3 -c"])
def test_ellipsis_ends_claim_inside_shell_command(ellipsis, prefix):
    assert_unbacked(
        {"command": '/bin/zsh -lc "python3 -c \'import runpy; runpy.run_path(\"tests/test_rooms.py\")\'"'},
        f"{prefix}{ellipsis} (runner)", True)


@pytest.mark.parametrize("claim", ["…", "...", "pytest…", "pytest..."])
def test_ellipsis_in_first_token_matches_nothing(claim):
    assert_unbacked({"command": "pytest -q"}, claim, False)


def assert_unbacked(tool_input, claim, backed):
    event = ControlEvent(run_id="r", task_id="t", engine="gemini" if "CommandLine" in tool_input
                         else "codex", seq=0, ts=0, kind="tool_start",
                         payload={"name": "run_command", "input": tool_input})
    report = parse(f"RESULT: PASS\nRAN: {claim} -> exit 0: passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert _unbacked_claims(report, [event], "t") == ([] if backed else [claim])
