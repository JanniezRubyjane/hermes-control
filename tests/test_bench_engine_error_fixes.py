"""Regression checks for benchmark engine failures and missing grades."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_bare, arm_full, report
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.engines.gemini import GeminiAdapter


NOISE = "Reading additional input from stdin..."
FAILURE = {"type": "turn.failed", "error": {"message": "API connection failed"}}
LIMIT = "You've hit your usage limit. Please try again later."


@pytest.mark.parametrize("adapter,lines,expected", [
    (ClaudeAdapter(), [{"type": "result", "subtype": "error_max_turns",
                        "is_error": True, "result": "Reached max turns"}], None),
    (GeminiAdapter(), [{"type": "result", "result": {
        "status": "ERROR", "error": "Task could not be completed"}}], None),
    (CodexAdapter(), [FAILURE], "API connection failed"),
    (CodexAdapter(), [NOISE, {"type": "turn.completed", "usage": {}}], None),
    (CodexAdapter(), [NOISE, FAILURE], "API connection failed"),
    (CodexAdapter(), [FAILURE, NOISE], "API connection failed"),
    (CodexAdapter(), [NOISE], None),
    (CodexAdapter(), [FAILURE, {"type": "turn.completed"}], None),
    (CodexAdapter(), [FAILURE, {"type": "error", "message": "Task error"}], None),
    (CodexAdapter(), [{"type": "error", "message": "Task error"}], None),
    (ClaudeAdapter(), [{"type": "result", "subtype": "success",
                        "is_error": True, "result": LIMIT}], LIMIT),
    (ClaudeAdapter(), [{"type": "result", "subtype": "success",
                        "is_error": True, "result": LIMIT}, NOISE], LIMIT),
])
def test_engine_failure_classification(tmp_path, adapter, lines, expected):
    transcript = tmp_path / "cli.jsonl"
    transcript.write_text("\n".join(
        line if isinstance(line, str) else json.dumps(line) for line in lines) + "\n")
    assert arm_bare.engine_error(adapter, transcript) == expected


def dispatch_database(home):
    home.mkdir(exist_ok=True)
    db = sqlite3.connect(home / "state.db")
    with db:
        db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, "
                   "tool_name TEXT, content TEXT)")
        db.executemany("INSERT INTO messages VALUES (?, 'tool', 'control_dispatch', ?)", [
            ("planner", json.dumps({"error": "first refusal"})),
            ("planner", json.dumps({"error": "latest refusal"})),
            ("other", json.dumps({"error": "other session refusal"})),
            ("planner", json.dumps({"ok": True})),
        ])
    db.close()


@pytest.mark.parametrize("sessions,expected", [
    (["planner"], "latest refusal"),
    ([], "other session refusal"),
])
def test_latest_refused_dispatch(tmp_path, monkeypatch, sessions, expected):
    dispatch_database(tmp_path)
    connect = sqlite3.connect

    def reverse_unordered_reads(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.execute("PRAGMA reverse_unordered_selects = ON")
        return db

    monkeypatch.setattr(arm_full.sqlite3, "connect", reverse_unordered_reads)
    assert arm_full.planner_dispatch_error(tmp_path, sessions) == expected


def test_dispatch_database_uri_escapes_path(tmp_path):
    home = tmp_path / "planner home #?"
    dispatch_database(home)
    assert arm_full.planner_dispatch_error(home, ["planner"]) == "latest refusal"


def test_invalid_dispatch_database(tmp_path):
    (tmp_path / "state.db").write_text("this is not a sqlite database")
    assert arm_full.planner_dispatch_error(tmp_path, []) is None


def test_dispatch_database_connect_error(tmp_path, monkeypatch):
    (tmp_path / "state.db").touch()

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(arm_full.sqlite3, "connect", fail_connect)
    assert arm_full.planner_dispatch_error(tmp_path, []) is None


def test_dispatch_database_missing_table(tmp_path):
    sqlite3.connect(tmp_path / "state.db").close()
    assert arm_full.planner_dispatch_error(tmp_path, []) is None


@pytest.mark.parametrize("statuses,expected", [
    (["not-graded", "engine-error"], True),
    (["engine-error"], True),
    (["not-graded", "engine-error", "graded"], False),
    ([], False),
])
def test_report_ungraded(statuses, expected):
    current = [{"status": status, "engine": "codex"} for status in statuses]
    comparison = report.compare([], current)
    assert comparison["ungraded"] is expected
    for group in comparison["groups"]:
        assert group["ungraded"] is expected
