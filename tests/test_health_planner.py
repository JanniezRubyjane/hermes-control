import json
import sqlite3

import pytest

from hermes_control import health


@pytest.fixture
def planner_db():
    path = health.state_db_path()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sessions(id TEXT, started_at REAL, api_call_count INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_write_tokens INTEGER);
            CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, tool_name TEXT,
                tool_calls TEXT, active INTEGER, compacted INTEGER);
        """)

    def add(session, calls=5, tokens=(100, 20, 60, 20), tasks=2, active=1,
            compacted=0):
        dispatch = [{"id": f"call_{session}", "function": {"name": "control_dispatch", "arguments":
                    json.dumps({"tasks": [{} for _ in range(tasks)]})}}]
        with sqlite3.connect(path) as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                         (session, 10, calls, *tokens))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                         (session, "assistant", "", None, json.dumps(dispatch), active, compacted))
            for status, content in [(0, "compacted"), (1, "123456"), (None, "abcd")]:
                conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                             (session, "tool", content, "control_dispatch", None, status,
                              int(status == 0)))
    return path, add


@pytest.mark.parametrize("nested, has_id, expected", [
    (False, True, 5), (True, True, 5), (False, False, 14),
])
def test_dispatch_ids_deduplicate_with_active_rows_first(planner_db, nested, has_id, expected):
    path, add = planner_db
    add("duplicate", tasks=0)

    def dispatch(tasks, call_id):
        args = {"tasks": [{} for _ in range(tasks)]}
        name = "control_dispatch"
        if nested:
            name = "tool_call"
            args = {"calls": [{"name": "other", "arguments": {}},
                              {"name": "control_dispatch", "arguments": json.dumps(args)}]}
        call = {"function": {"name": name, "arguments": json.dumps(args)}}
        if has_id:
            call["id"] = call_id
        return json.dumps([call])

    with sqlite3.connect(path) as conn:
        # Put the inactive copy first, with a different count to test active precedence.
        for active, compacted, tasks, call_id in [
            (0, 0, 9, "shared"), (1, 0, 2, "shared"), (0, 1, 3, "unique"),
        ]:
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                         ("duplicate", "assistant", "", None, dispatch(tasks, call_id),
                          active, compacted))
    result = health._planner(0, 20, [])
    assert result["totals"]["dispatched_tasks"] == expected
    assert result["sessions"][0]["tool_result_characters"] == {"control_dispatch": 10}


def test_dispatch_ids_are_scoped_to_session(planner_db):
    path, add = planner_db
    add("one")
    add("two")
    with sqlite3.connect(path) as conn:
        calls = conn.execute("SELECT tool_calls FROM messages WHERE session_id = 'one' "
                             "AND role = 'assistant'").fetchone()[0]
        conn.execute("UPDATE messages SET tool_calls = ? WHERE session_id = 'two' "
                     "AND role = 'assistant'", (calls,))
    assert health._planner(0, 20, [])["totals"]["dispatched_tasks"] == 4


def test_dispatched_exposes_outer_ids_and_nested_indices():
    calls = [
        {"id": "direct", "function": {"name": "control_dispatch",
         "arguments": {"tasks": [{}, {}]}}},
        {"id": "outer", "function": {"name": "tool_call", "arguments": {"calls": [
            {"name": "other"},
            {"name": "control_dispatch", "arguments": {"tasks": [{}]}},
            {"name": "control_dispatch", "arguments": {"tasks": [{}, {}, {}]}},
        ]}}},
        {"function": {"name": "tool_call", "arguments": {"calls": [
            {"name": "control_dispatch", "arguments": {"tasks": [{}]}},
        ]}}},
    ]
    assert health._dispatched(json.dumps(calls)) == [
        ("direct", 2), ("outer#1", 1), ("outer#2", 3), (None, 1),
    ]


def test_compacted_dispatch_and_active_tool_characters(planner_db):
    path, add = planner_db
    add("compacted", active=0, tasks=3)
    before = path.read_bytes()
    result = health._planner(0, 20, [])
    assert result["totals"]["dispatched_tasks"] == 3
    assert result["sessions"][0]["tool_result_characters"] == {"control_dispatch": 10}
    assert "all messages" in result["source"]
    assert "active messages" in result["source"]
    assert path.read_bytes() == before


def test_ratios_long_sessions_and_per_call_fields(planner_db):
    _, add = planner_db
    add("long", calls=100, tokens=(1000, 200, 600, 200))
    add("short", calls=5)
    add("zero", calls=0, tokens=(0, 0, 0, 0), tasks=0)
    worker = [dict(run_id="r", task_id="t", engine="codex", input_tokens=80,
                   output_tokens=20)]
    result = health._planner(0, 20, worker)
    totals = result["totals"]
    assert health.LONG_SESSION_CALLS == 100
    assert totals["planner_to_worker_total_ratio"] == 22
    assert result["planner_to_worker_ratio"] == 5.5
    assert totals["long_sessions"] == 1
    assert totals["long_session_calls"] == 100
    assert totals["long_session_token_share"] == pytest.approx(2000 / 2200)
    rows = {row["id"]: row for row in result["sessions"]}
    assert rows["long"]["processed_per_call"] == 20
    assert rows["short"]["processed_per_call"] == 40
    assert rows["short"]["cache_read_per_call"] == 12
    assert rows["short"]["tool_result_characters_total"] == 10
    assert rows["short"]["tool_result_characters_per_call"] == 2
    for field in ("processed_per_call", "cache_read_per_call", "tool_result_characters_per_call"):
        assert rows["zero"][field] is None
    data = {key: health._section("test") for key, _ in health.SECTIONS}
    report = health.markdown(dict(data, since_days=30, planner=result))
    for field in ("processed_per_call", "cache_read_per_call", "tool_result_characters_total",
                  "tool_result_characters_per_call", "log_api_calls", "log_cache_read_tokens",
                  "log_cache_write_tokens", "log_big_writes", "log_big_write_tokens"):
        assert field.replace("_", " ") in report
    assert "per-task ratio (denominators differ: planner counts tasks dispatched in sessions, worker counts run-log tasks)" in report
    assert "total planner to worker total ratio: 22" in report
    assert "total long sessions: 1" in report
    assert "total long session calls: 100" in report
    assert "total long session token share:" in report


def log_line(session, call, read, write):
    return (f"2026-10-09 19:47:41,812 INFO [{session}] agent.conversation_loop: "
            f"API call #{call}: model=claude-opus-5-5 provider=anthropic in=54763 "
            f"out=2697 total=57460 latency=31.0s cache={read}/54763 (89%) "
            f"write={write} id=msg_{call}\n")


def test_agent_logs_deduplicate_rotations_and_report_missing(planner_db):
    path, add = planner_db
    add("logged")
    add("missing")
    logs = path.parent / "logs"
    logs.mkdir()
    first = log_line("logged", 1, 48636, 6125)
    big = log_line("logged", 2, 100, 30001)
    boundary = log_line("logged", 3, 200, 30000)
    (logs / "agent.log").write_text(first + "unrelated line\n" + big)
    (logs / "agent.log.1").write_text(first + boundary + log_line("other", 1, 1, 1))
    (logs / "other.log").write_text(log_line("missing", 1, 2, 3))
    before = {p: p.read_bytes() for p in logs.iterdir()}
    result = health._planner(0, 20, [])
    rows = {row["id"]: row for row in result["sessions"]}
    expected = dict(log_api_calls=3, log_cache_read_tokens=48936,
                    log_cache_write_tokens=66126, log_big_writes=1, log_big_write_tokens=30001)
    for field, value in expected.items():
        assert rows["logged"][field] == value
        assert rows["missing"][field] is None
    notes = [note for note in result["not_measured"] if "log lines" in note]
    assert len(notes) == 1 and "1 session" in notes[0]
    assert {p: p.read_bytes() for p in logs.iterdir()} == before


def test_no_logs_directory(planner_db):
    path, add = planner_db
    add("one")
    add("two")
    result = health._planner(0, 20, [])
    assert not (path.parent / "logs").exists()
    for row in result["sessions"]:
        for field in ("log_api_calls", "log_cache_read_tokens", "log_cache_write_tokens",
                      "log_big_writes", "log_big_write_tokens"):
            assert row[field] is None
    notes = [note for note in result["not_measured"] if "log lines" in note]
    assert len(notes) == 1 and "2 sessions" in notes[0]


def test_unmatched_api_call_lines_are_reported(planner_db):
    path, add = planner_db
    add("known")
    logs = path.parent / "logs"
    logs.mkdir()
    malformed = log_line("known", 2, 1, 1).replace("cache=1/54763", "cache=changed")
    (logs / "agent.log").write_text(
        log_line("known", 1, 2, 3) + malformed
        + malformed.replace("[known]", "[unknown]")
        + "INFO [known] agent.conversation_loop: unrelated line\n")
    result = health._planner(0, 20, [])
    assert result["sessions"][0]["log_api_calls"] == 1
    assert "1 agent.log API call lines did not match the expected format" in result["not_measured"]


def test_unmatched_lines_deduplicate_across_rotations(planner_db):
    path, add = planner_db
    add("known")
    logs = path.parent / "logs"
    logs.mkdir()
    malformed = log_line("known", 2, 1, 1).replace(" write=1", "")
    (logs / "agent.log").write_text(malformed)
    (logs / "agent.log.1").write_text(malformed)
    result = health._planner(0, 20, [])
    assert "1 agent.log API call lines did not match the expected format" in result["not_measured"]


def test_null_active_rows_take_precedence_like_active(planner_db):
    path, add = planner_db
    add("nullfirst", tasks=0)

    def dispatch(tasks):
        return json.dumps([{"id": "shared", "function": {"name": "control_dispatch", "arguments":
                                                         json.dumps({"tasks": [{} for _ in range(tasks)]})}}])

    with sqlite3.connect(path) as conn:
        for active, tasks in [(0, 9), (None, 2)]:
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                         ("nullfirst", "assistant", "", None, dispatch(tasks), active, 0))
    result = health._planner(0, 20, [])
    assert result["totals"]["dispatched_tasks"] == 2
