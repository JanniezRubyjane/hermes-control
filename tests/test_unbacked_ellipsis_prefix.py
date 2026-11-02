"""Ellipsis claims require the full tokens and any abbreviated token prefix."""

import pytest

from hermes_control.events import ControlEvent
from hermes_control.review_pack import _unbacked_claims


@pytest.mark.parametrize("ellipsis", ["…", "..."])
@pytest.mark.parametrize("input_key", ["command", "CommandLine"])
@pytest.mark.parametrize("claim,command,backed", [
    ("python3 -c{e}", "python3 other.py", False),
    ("python3 -c{e}", "python3 -c 'print(1)'", True),
    ("python3 -c{e}", "python3 other.py -c 'print(1)'", False),
    ("python3 -c{e}", "sh -c \"python3 -c 'print(1)'\"", True),
    ("python3 -c{e}", "zsh -lc 'python3 other.py'", False),
    ("git diff main{e}HEAD", "git diff main{e}HEAD", True),
    ("git diff main{e}HEAD", "git diff other", False),
    ("git diff main{e}HEAD", "git diff main{e}HEAD-extra", False),
    ("git diff main{e}HEAD --stat{e}", "git diff main{e}HEAD --stat=80", True),
    ("git diff main{e}HEAD --stat{e}", "git diff main{e}HEAD --name-only", False),
    ("git log main{e}", "git log main...HEAD", True),
    ("git log main{e}", "git log feature", False),
    ('echo "wait{e}"', 'echo "wait{e}"', True),
    ('echo "wait{e}"', "echo other", False),
    ("python3 {e}", "python3 x.py", True),
    ("python3 {e}ignored", "python3 x.py", True),
    ("python3; {e}", "python3 x.py", True),
    ("python3; -c{e}", "python3 -c 'print(1)'", True),
    ("{e}", "python3 x.py", False),
    ("pytest{e}", "pytest -q", False),
])
def test_ellipsis_claim(ellipsis, input_key, claim, command, backed):
    claim = claim.format(e=ellipsis)
    command = command.format(e=ellipsis)
    event = ControlEvent(
        run_id="r", task_id="t", engine="gemini" if input_key == "CommandLine" else "codex",
        seq=0, ts=0, kind="tool_start",
        payload={"name": "run_command", "input": {input_key: command}},
    )
    report = {"ran": [{"command": claim}]}
    assert _unbacked_claims(report, [event], "t") == ([] if backed else [claim])
