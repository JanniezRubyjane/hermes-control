"""Command evidence reaches lessons through isolated historical run logs."""

import json
import time

import pytest

from hermes_control import lessons, runlog


COMMAND = "/bin/sh scripts/sandbox-check.sh tests/test_cloud_guard.py"


def write_log(engine, tool_input, claim, *, error=None):
    def event(kind, payload, seq):
        return dict(run_id="agy-claims", task_id="cloud-guard", engine=engine,
                    ts=time.time(), seq=seq, kind=kind, payload=payload, raw_kind=None)

    events = [event("tool_start", {"name": "run_command", "tool_id": "shell",
                                   "input": tool_input}, 0)]
    if error is not None:
        events.append(event("tool_end", {"tool_id": "shell", "is_error": True,
                                          "exit_code": 1, "content": error}, 1))
    events.append(event("summary", {"tasks": [{"task_id": "cloud-guard", "text":
                  f"RESULT: PASS\nRAN: {claim} -> exit 0: passed\n"
                  "DEVIATIONS: none\nFOLLOW-UPS: none"}]}, 2))
    path = runlog.run_log_dir() / "run-agy-claims.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")


@pytest.mark.parametrize("engine,tool_input,claim,expected", [
    ("gemini", {"CommandLine": COMMAND}, COMMAND, []),
    ("gemini", {"CommandLine": COMMAND}, "ruff check", ["unbacked_claim:ruff"]),
    ("codex", {"command": COMMAND}, COMMAND, []),
    ("gemini", {"command": COMMAND, "CommandLine": "ruff check"}, COMMAND, []),
    ("gemini", {"command": "ruff check", "CommandLine": COMMAND}, COMMAND,
     ["unbacked_claim:sh"]),
    ("gemini", {"command": None, "CommandLine": COMMAND}, COMMAND,
     ["unbacked_claim:sh"]),
    ("gemini", {"command": "", "CommandLine": COMMAND}, COMMAND,
     ["unbacked_claim:sh"]),
    ("gemini", {"CommandLine": [COMMAND]}, COMMAND, ["unbacked_claim:sh"]),
])
def test_lessons_command_evidence(engine, tool_input, claim, expected):
    write_log(engine, tool_input, claim)
    assert [row["key"] for row in lessons.build(min_count=1)] == expected


@pytest.mark.parametrize("error", ["Error: command requires approval",
                                  "Permission denied", "command failed"])
def test_gemini_errors_do_not_create_engine_specific_classes(error):
    write_log("gemini", {"CommandLine": COMMAND}, COMMAND, error=error)
    assert lessons.build(min_count=1) == []


def test_codex_error_still_uses_command_head():
    write_log("codex", {"command": COMMAND}, COMMAND, error="command failed")
    assert [row["key"] for row in lessons.build(min_count=1)] == ["codex_exit:sh"]
