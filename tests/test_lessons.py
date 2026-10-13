"""Lessons are derived only from isolated, historical run logs."""

import argparse
import json
import time

import pytest

from hermes_control import lessons, runlog, watch


def event(kind, payload, *, run="one", task="task", engine="codex", ts=None, seq=0):
    return dict(run_id=run, task_id=task, engine=engine, seq=seq,
                ts=time.time() if ts is None else ts, kind=kind, payload=payload,
                raw_kind=None)


def write_log(run, *events):
    path = runlog.run_log_dir() / f"run-{run}.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in events) + "\n", encoding="utf-8")
    return path


def test_threshold_and_normalization():
    for run, path, number in [("one", "/tmp/abc/test.py", 12),
                              ("two", "/private/var/folders/xyz/T/tmp123/test.py", 99)]:
        write_log(run, event("verify", {"passed": False, "output_tail":
                  f"running\nAssertionError: {path}:{number} expected 3 got 4 abcdef123"}, run=run))
    rows = lessons.build()
    assert len(rows) == 1
    assert rows[0]["count"] == 2
    assert {item["run_id"] for item in rows[0]["evidence"]} == {"one", "two"}
    assert all(len(item["excerpt"]) <= 160 for item in rows[0]["evidence"])
    assert lessons.build(min_count=3) == []


def test_claude_refusal_and_codex_shell_command_heads():
    for run in ["one", "two"]:
        write_log(run,
                  event("tool_end", {"is_error": True, "tool_id": "c", "content":
                        "This Bash command contains multiple operations. The following part "
                        "requires approval: node --check different.js"}, run=run, engine="claude"),
                  event("tool_start", {"tool_id": "x", "input": {"command":
                        "/bin/zsh -lc 'git cat-file -t abcdef123'"}}, run=run),
                  event("tool_end", {"tool_id": "x", "is_error": True,
                        "exit_code": 1, "content": "missing"}, run=run))
    rows = {row["key"]: row for row in lessons.build()}
    assert rows["claude_permission:node"]["count"] == 2
    assert rows["codex_exit:git"]["count"] == 2


def test_newest_summary_wins_per_task_even_out_of_order():
    for run in ["one", "two"]:
        write_log(run,
                  event("summary", {"tasks": [{"task_id": "task", "failure_mode": "stall"}]},
                        run=run, ts=time.time(), seq=2),
                  event("summary", {"tasks": [{"task_id": "task", "failure_mode": "timeout"},
                                            {"task_id": "other", "failure_mode": "limit"}]},
                        run=run, ts=time.time() - 10, seq=1))
    rows = {row["key"]: row for row in lessons.build()}
    assert rows["failure_mode:stall"]["count"] == 2
    assert rows["failure_mode:limit"]["count"] == 2
    assert "failure_mode:timeout" not in rows


def test_corrupt_lines_and_files_skipped(monkeypatch):
    path = write_log("one", event("verify", {"passed": False, "output_tail": "FAILED foo"}))
    with path.open("ab") as handle:
        handle.write(b'{broken\nnull\n{"payload": []}\n\xff\n')
    unreadable = write_log("unreadable")
    original = type(path).open

    def open_file(self, *args, **kwargs):
        if self == unreadable:
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", open_file)
    assert lessons.build(min_count=1)[0]["count"] == 1


def test_since_uses_event_timestamp_not_file_age():
    now = time.time()
    write_log("one", event("verify", {"passed": False, "output_tail": "FAILED foo"},
                           ts=now - 31 * 86400),
              event("verify", {"passed": False, "output_tail": "FAILED foo"}, ts=now))
    assert lessons.build(now=now) == []
    assert lessons.build(since=32, now=now)[0]["count"] == 2


def test_summary_reasons_warnings_and_unbacked_claims():
    for run in ["one", "two"]:
        write_log(run, event("tool_start", {"input": {"command": "/bin/zsh -lc 'pytest -q'"}}, run=run),
                  event("summary", {"tasks": [{"task_id": "task",
                        "unproven_reason": f"verification failed (exit {1 if run == 'one' else 2})",
                        "warnings": ["check_gaming: changed script", "weakened_tests: removed assert",
                                     "red_passed: unexpected pass"],
                        "text": "RESULT: PASS\nRAN: pytest -q -> exit 0: passed\n"
                                "RAN: ruff check -> exit 0: clean\nDEVIATIONS: none\nFOLLOW-UPS: none"},
                        {"task_id": "empty", "unproven_reason": "empty diff: nothing changed"}]}, run=run))
    rows = {row["key"]: row for row in lessons.build()}
    assert rows["unproven_reason:verification failed"]["count"] == 2
    for prefix in ["check_gaming", "weakened_tests", "red_passed"]:
        assert rows[f"warning:{prefix}"]["count"] == 2
    assert rows["unbacked_claim:ruff"]["count"] == 2
    assert "unbacked_claim:pytest" not in rows
    assert not any("empty diff" in key for key in rows)


def test_cli_parser_json_limit_and_text(capsys):
    write_log("one", event("summary", {"tasks": [{"task_id": "task", "failure_mode": "stall",
              "unproven_reason": "not verified: no verify command was given"}]}))
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    defaults = parser.parse_args(["lessons"])
    assert defaults.since == 30 and defaults.min == 2
    args = parser.parse_args(["lessons", "--since", "2", "--min", "1", "--json", "--limit", "1"])
    assert watch.run_cli(args) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert len(lessons.build(min_count=1)) == 2
    assert {"key", "title", "count", "level", "latest_run", "evidence", "evidence_total", "reminder"} <= rows[0].keys()
    assert watch.run_cli(parser.parse_args(["lessons", "--min", "1"])) == 0
    text = capsys.readouterr().out
    assert all(word in text for word in ["class", "count", "level", "latest run", "proven"])
    assert "Failure mode: stall" in text and "1" in text
    assert "Unproven: not verified" in text


def test_report_preserves_log_bytes():
    path = write_log("one", event("summary", {"tasks": [{"task_id": "task", "failure_mode": "stall"}]}))
    before = {item.name: item.read_bytes() for item in path.parent.iterdir()}
    lessons.build(min_count=1)
    assert {item.name: item.read_bytes() for item in path.parent.iterdir()} == before


def test_bounded_newest_evidence_and_codex_last(capsys):
    now = time.time()
    for index in range(20):
        run = f"run{index}"
        write_log(run,
                  event("verify", {"passed": False, "output_tail": f"ValueError: {index}"},
                        run=run, ts=now - index),
                  event("tool_start", {"tool_id": "x", "input": {"command": "/bin/sh -c 'rg missing'"}},
                        run=run, ts=now - index),
                  event("tool_end", {"tool_id": "x", "is_error": True, "exit_code": 1},
                        run=run, ts=now - index))
    write_log("small", event("summary", {"tasks": [{"task_id": "task", "failure_mode": "stall"}]}))
    rows = lessons.build(min_count=1)
    assert [row["key"].split(":")[0] for row in rows] == ["verify_failure", "failure_mode", "codex_exit"]
    assert rows[0]["count"] == rows[0]["evidence_total"] == 20
    assert [item["run_id"] for item in rows[0]["evidence"]] == [f"run{i}" for i in range(4, -1, -1)]
    assert len(rows[-1]["evidence"]) == 3 and rows[-1]["evidence_total"] == 20
    lessons.report(min_count=1)
    output = capsys.readouterr().out
    assert "Codex non-zero exits (counts only)" in output
    assert "rg missing" not in output
    assert output.index("Failure mode: stall") < output.index("Codex non-zero exits (counts only)")


@pytest.mark.parametrize("output, expected", [
    ("Traceback (most recent call last):\nValueError: x", "ValueError: x"),
    ("TypeError: first\nE   package.ValueError: last", "E package.ValueError: last"),
    ("ValueError: first\nFAILED tests/test_x.py::test_x", "FAILED <path>::test_x"),
    ("@pytest.mark.parametrize\nassert x\nerror: lower\nTraceback (most recent call last):", None),
])
def test_verify_uses_last_error_line(output, expected):
    write_log("one", event("verify", {"passed": False, "output_tail": output}))
    rows = lessons.build(min_count=1)
    assert [row["title"] for row in rows] == ([] if expected is None else [f"Verify: {expected}"])


@pytest.mark.parametrize("command, expected", [
    ("/usr/bin/rg", "rg"), ("rg", "rg"), ("FOO=1 pytest", "pytest"),
    ("cd X && /usr/bin/rg x", "rg"), ("git commit -c abc", "git"),
    ("/bin/zsh -lc 'rg foo", "rg"), ("FOO=1 /bin/bash -c 'BAR=2 pytest'", "pytest"),
])
def test_command_head(command, expected):
    assert lessons._head(command) == expected


def test_absolute_and_bare_commands_share_class():
    for run, command in [("one", "/usr/bin/rg"), ("two", "rg")]:
        write_log(run, event("tool_start", {"tool_id": "x", "input": {"command": command}}, run=run),
                  event("tool_end", {"tool_id": "x", "is_error": True, "exit_code": 1}, run=run))
    assert [(row["key"], row["count"]) for row in lessons.build()] == [("codex_exit:rg", 2)]


def test_permission_refusal_colon_and_tool_error():
    write_log("one",
              event("tool_end", {"is_error": True, "content":
                    "Error: command requires approval: /opt/homebrew/bin/node x"}, engine="claude"),
              event("tool_end", {"is_error": True, "content": "cat: x: Permission denied"}, engine="claude"))
    assert [row["key"] for row in lessons.build(min_count=1)] == ["claude_permission:node"]


def test_normalized_bounded_summary_prefixes():
    for run, number in [("one", 123), ("two", 456)]:
        write_log(run, event("summary", {"tasks": [{"task_id": "task",
                  "unproven_reason": f"missing /tmp/file{number} {number} " + "x" * 100,
                  "warnings": [f"missing src/file{number}.py {number} " + "x" * 100 + ": detail"]}]}, run=run))
    rows = lessons.build()
    assert len(rows) == 2
    for row in rows:
        prefix = row["key"].split(":", 1)[1]
        assert len(prefix) == 80 and "<path>" in prefix and "<n>" in prefix
        assert row["count"] == 2


def test_normalization_preserves_words_and_requires_real_path():
    assert lessons._normalize("defaced and/or error/failure") == "defaced and/or <path>"
    assert lessons._normalize("abcdef1 src/test.py /tmp/x") == "<id> <path> <path>"


def test_summary_evidence_uses_newest_summary_timestamps():
    now = time.time()
    for index in range(8):
        run = str(index)
        write_log(run,
                  event("summary", {"tasks": [{"task_id": "task", "warnings": ["repeat"]}]},
                        run=run, ts=now - 100 + index),
                  event("summary", {"tasks": [{"task_id": "task", "warnings": ["repeat"]}]},
                        run=run, ts=now - index))
    row = lessons.build()[0]
    assert row["evidence_total"] == 8
    assert [item["run_id"] for item in row["evidence"]] == [str(i) for i in range(4, -1, -1)]


def test_approval_without_colon_and_own_permission_refusal():
    for content in ["This command requires approval", "I cannot proceed: permission denied"]:
        write_log("one", event("tool_start", {"tool_id": "x", "input": {"command": "node x"}}, engine="claude"),
                  event("tool_end", {"tool_id": "x", "is_error": True, "content": content}, engine="claude"))
        assert lessons.build(min_count=1)[0]["key"] == "claude_permission:node"
