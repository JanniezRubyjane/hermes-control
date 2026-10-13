"""A worker's closing report is a claim, not verification evidence."""

import pytest

from hermes_control import dispatch, review_pack
from hermes_control.engines.base import ControlTask
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.events import ControlEvent, RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worker_report import REPORT_REQUEST, parse


REPORT = ("RESULT: PASS\n"
          "RAN: pytest -q -> exit 0: 3 passed\n"
          "RAN: python -m compileall app -> exit 0: Listing app\n"
          "DEVIATIONS: none\n"
          "FOLLOW-UPS: none")


@pytest.mark.parametrize("text", [
    REPORT,
    "**RESULT:** PASS\n**RAN:** `pytest -q -> exit 0: 3 passed`\n"
    "**RAN:** `python -m compileall app -> exit 0: Listing app`\n"
    "**DEVIATIONS:** none\n**FOLLOW-UPS:** none",
    "Done.\n```text\n" + REPORT + "\n```",
    "- RESULT: PASS\n- RAN: pytest -q -> exit 0: 3 passed\n"
    "- RAN: python -m compileall app -> exit 0: Listing app\n"
    "- DEVIATIONS: none\n- FOLLOW-UPS: none",
    "**RESULT: PASS**\n**RAN: pytest -q -> exit 0: 3 passed**\n"
    "**RAN: python -m compileall app -> exit 0: Listing app**\n"
    "**DEVIATIONS: none**\n**FOLLOW-UPS: none**",
])
def test_parse_report_wrappers(text):
    assert parse(text) == {
        "result": "PASS",
        "ran": [
            {"command": "pytest -q", "exit_code": 0, "output": "3 passed"},
            {"command": "python -m compileall app", "exit_code": 0,
             "output": "Listing app"},
        ],
        "deviations": "none", "follow_ups": "none",
    }


def test_parse_missing_block():
    assert parse("Tests passed, with no changes needed.") is None


def _tool_event(command):
    return ControlEvent(run_id="r", task_id="t", engine="codex", seq=0, ts=0,
                        kind="tool_start", payload={"input": {"command": command}})


def test_unparseable_ran_line_is_preserved_and_checked():
    report = parse("RESULT: PASS\nRAN: pytest passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert report["ran"] == [{"command": "pytest passed", "exit_code": None,
                              "output": ""}]
    assert review_pack._unbacked_claims(report, [_tool_event("git status")], "t") == [
        "pytest passed"]


def test_claim_match_normalizes_whitespace_on_both_sides():
    report = parse("RESULT: PASS\nRAN: pytest -q -> exit 0: passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert review_pack._unbacked_claims(report, [_tool_event("pytest  \n  -q")], "t") == []


def test_claim_match_requires_contiguous_whole_tokens():
    report = parse("RESULT: PASS\nRAN: git -> exit 0: done\n"
                   "RAN: make -> exit 0: done\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    events = [_tool_event("cmake"), _tool_event("cat pytest.ini")]
    assert review_pack._unbacked_claims(report, events, "t") == ["git", "make"]


def test_markdown_bold_stripping_preserves_glob():
    report = parse("**RESULT:** PASS\n**RAN:** tests/**/x.py -> exit 0: passed\n"
                   "**DEVIATIONS:** none\n**FOLLOW-UPS:** none")
    assert report["ran"] == [{"command": "tests/**/x.py", "exit_code": 0,
                              "output": "passed"}]


@pytest.mark.parametrize("result_line", ["**RESULT**: PASS", "**RESULT:** **PASS**"])
def test_parse_bold_result_with_colon_outside_or_bold_value(result_line):
    report = parse(f"{result_line}\nRAN: none\n"
                   "DEVIATIONS: **none**\nFOLLOW-UPS: `none`")
    assert report == {"result": "PASS", "ran": [],
                      "deviations": "none", "follow_ups": "none"}


def test_ran_bullets_continue_empty_ran_field():
    report = parse("RESULT: PASS\nRAN:\n"
                   "- pytest -q -> exit 0: passed\n"
                   "* tests/**/x.py -> exit 0: passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert report["ran"] == [
        {"command": "pytest -q", "exit_code": 0, "output": "passed"},
        {"command": "tests/**/x.py", "exit_code": 0, "output": "passed"},
    ]


@pytest.mark.parametrize("value", ["", "none", "none.", "n/a", "none (read-only)"])
def test_ran_empty_or_none_like_has_no_claims(value):
    report = parse(f"RESULT: PASS\nRAN: {value}\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert report["ran"] == []
    assert review_pack._unbacked_claims(report, [], "t") == []


def test_backticked_command_has_no_trailing_backtick():
    report = parse("RESULT: PASS\nRAN: `pytest -q` -> exit 0: 3 passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert report["ran"] == [{"command": "pytest -q", "exit_code": 0,
                              "output": "3 passed"}]
    assert review_pack._unbacked_claims(report, [_tool_event("pytest -q")], "t") == []


@pytest.mark.parametrize("separator", [";", "&&", "||"])
def test_claim_match_strips_glued_shell_separator(separator):
    report = parse(f"RESULT: PASS\nRAN: pytest -q{separator} -> exit 0: passed\n"
                   "DEVIATIONS: none\nFOLLOW-UPS: none")
    assert review_pack._unbacked_claims(report, [_tool_event("pytest -q")], "t") == []


def test_parse_keeps_last_full_report_block():
    text = ("RESULT: PASS\nRAN: pytest -q -> exit 0: passed\n"
            "DEVIATIONS: none\nFOLLOW-UPS: none\n"
            "RESULT: ISSUES\nRAN: make -> exit 1: failed")
    assert parse(text) == parse("RESULT: PASS\nRAN: pytest -q -> exit 0: passed\n"
                                "DEVIATIONS: none\nFOLLOW-UPS: none")


def test_parse_none_returns_no_report():
    assert parse(None) is None


def test_claude_contract_requests_report_for_writes_only(tmp_path):
    adapter = ClaudeAdapter()
    for write in (True, False):
        task = ControlTask(id="t", goal="Do work", engine="claude",
                           policy=ExecPolicy(write=write))
        argv = adapter.build_argv(task, cwd=tmp_path)
        contract = argv[argv.index("--append-system-prompt") + 1]
        assert (REPORT_REQUEST in contract) is write
        assert contract.count(REPORT_REQUEST) == int(write)


def test_codex_goal_requests_report_once_for_writes_only(monkeypatch, tmp_path):
    seen = []

    def capture(task, **kwargs):
        seen.append(task.goal)
        return None

    monkeypatch.setattr(dispatch, "_run_task_engine", capture)
    for write in (True, False):
        task = ControlTask(id="t", goal="Do work", engine="codex",
                           policy=ExecPolicy(write=write))
        dispatch.run_task(task, run_id="report", log=None,
                          worktree=type("Worktree", (), {"path": tmp_path})(),
                          adapter=type("Adapter", (), {"name": "codex"})())
    assert seen[0].count(REPORT_REQUEST) == 1
    assert REPORT_REQUEST not in seen[1]


def test_review_pack_shows_report_and_unbacked_claims(tmp_path, monkeypatch):
    log = RunLog("worker-report", directory=tmp_path)
    log.append_next("", [RawEvent("plan", {"repo": str(tmp_path / "gone"), "tasks": [
        {"id": "one", "engine": "codex"}, {"id": "two", "engine": "codex"}]})],
        engine="", now=1)
    log.append_next("one", [RawEvent("tool_start", {
        "tool_id": "1", "name": "command_execution",
        "input": {"command": "/bin/sh -c 'pytest -q'"}})], engine="codex", now=2)
    log.append_next("one", [RawEvent("text", {"text": REPORT})], engine="codex", now=3)
    log.append_next("two", [RawEvent("tool_start", {
        "tool_id": "2", "name": "command_execution",
        "input": {"command": "python -m compileall app"}})], engine="codex", now=4)
    monkeypatch.setattr(review_pack, "RunLog", lambda run_id: log)
    monkeypatch.setattr(review_pack, "run_log_path", lambda run_id: log.path)
    monkeypatch.setattr(review_pack.runs, "run_view", lambda run_id: {
        "repo": str(tmp_path / "gone"), "state": "done", "tasks": [
            {"id": task_id, "status": "done", "status_reason": None,
             "why": "", "planned_engine": "codex", "model": None,
             "attempts": [], "verify": None, "rerouted": None,
             "summary": {"text": REPORT} if task_id == "one" else {},
             "branch": "", "commit": None, "worktree": "", "limits": [],
             "paused": None}
            for task_id in ("one", "two")]})
    monkeypatch.setattr(review_pack.runs, "run_stats", lambda **kwargs: [])
    pack = review_pack.build("worker-report")
    one, two = pack["tasks"]
    assert one["worker_report"]["result"] == "PASS"
    assert one["unbacked_claims"] == ["python -m compileall app"]
    assert two["worker_report"] == "no report"
    assert two["unbacked_claims"] == []
    formatted = review_pack.format_text(pack)
    assert "worker_report:" in formatted and "PASS" in formatted
    assert "unbacked_claims:" in formatted and "python -m compileall app" in formatted
