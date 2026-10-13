"""Review findings have stable counts without changing dispatch policy."""

import pytest

from hermes_control import health, review_pack, worker_report
from hermes_control.events import RawEvent
from hermes_control.runlog import RunLog
from test_review_pack import _run


def expected(critical=0, warning=0, nit=0, verdict=None, unparsed=False):
    return dict(critical=critical, warning=warning, nit=nit,
                verdict=verdict, unparsed=unparsed)


def test_review_format_and_new_shape():
    assert "[critical|warning|nit] path:line - finding - evidence" in worker_report.REVIEW_FORMAT
    assert "VERDICT: READY" in worker_report.REVIEW_FORMAT
    assert "VERDICT: NOT READY" in worker_report.REVIEW_FORMAT
    assert "no findings" in worker_report.REVIEW_FORMAT
    text = ("[critical] app.py:12 - crash - traceback\n"
            "[warning] app.py:20 - missing check - input accepted\n"
            "[nit] app.py:30 - typo - misspelled name\nVERDICT: NOT READY")
    assert worker_report.parse_findings(text) == expected(1, 1, 1, "NOT READY")


@pytest.mark.parametrize("label,level", [
    ("critical", "critical"), ("bug", "critical"), ("blocker", "critical"),
    ("high", "critical"), ("warning", "warning"), ("missing", "warning"),
    ("major", "warning"), ("medium", "warning"), ("nit", "nit"),
    ("minor", "nit"), ("low", "nit"),
])
@pytest.mark.parametrize("form", ["**{label} finding**", "**P1 — {label} finding**",
                                  "[{label}] finding", "[severity: {label} finding]",
                                  "severity: {label}"])
def test_legacy_labels(label, level, form):
    assert worker_report.parse_findings(form.format(label=label.upper())) == expected(**{level: 1})


def test_count_once_per_severity_per_line_like_health():
    assert worker_report.parse_findings(
        "[bug] **bug** severity: bug [high] [warning]\n[bug]"
    ) == expected(critical=3, warning=1)


def test_table_severity_column():
    text = ("| Finding | Severity | Evidence |\n| --- | --- | --- |\n"
            "| crash | high | traceback |\n| check | missing | input |\n"
            "| typo | low | spelling |\n\n| unrelated | critical | prose |")
    assert worker_report.parse_findings(text) == expected(1, 1, 1)


@pytest.mark.parametrize("text,verdict", [
    ("VERDICT: READY", "READY"), ("READY TO MERGE", "READY"),
    ("VERDICT: READY TO MERGE", "READY"), ("NOT READY", "NOT READY"),
    ("VERDICT: NOT READY", "NOT READY"),
    ("NOT READY TO MERGE", "NOT READY"),
    ("READY TO MERGE\nVERDICT: NOT READY", "NOT READY"),
    ("VERDICT: READY\nNote: the loader is not ready for prod.", "READY"),
    ("VERDICT: NOT READY\nReady to merge after fixes.", "NOT READY"),
    ("VERDICT: READY (was NOT READY before the fix)", "READY"),
    ("VERDICT: NOT READY\nVERDICT: READY", "READY"),
    ("**verdict: not ready**\n- VERDICT: READY", "READY"),
    ("READY TO MERGE\nThe loader is NOT READY.", "NOT READY"),
    ("NOT READY\nReady to merge after fixes.", "READY"),
])
def test_verdict(text, verdict):
    assert worker_report.parse_findings(text) == expected(verdict=verdict)


@pytest.mark.parametrize("text,verdict", [
    ("VERDICT: **READY**", "READY"), ("VERDICT: `NOT READY`", "NOT READY"),
    ("**VERDICT:** READY", "READY"), ("**VERDICT: NOT READY**", "NOT READY"),
    ("VERDICT: _READY_\nThe loader is not ready.", "READY"),
])
def test_verdict_with_markdown_marks(text, verdict):
    assert worker_report.parse_findings(text) == expected(verdict=verdict)


@pytest.mark.parametrize("payload", [
    # A task without an id still stops runs.run_view (runs.py:225) before review_pack runs.
    "not a dict", {"tasks": None},
])
def test_review_pack_survives_malformed_plan_event(payload):
    log = RunLog("malformed-plan")
    log.append_next("", [RawEvent(kind="plan", payload=payload)], engine="", now=1790000000)
    log.append_next("", [RawEvent(kind="plan", payload={"tasks": [
        {"id": "review1-task", "engine": "claude", "policy": "read-only"},
    ]})], engine="", now=1790000000)
    log.append_next("review1-task", [RawEvent(kind="text", payload={"text": "no findings"})],
                    engine="claude", now=1790000000)
    pack = review_pack.build(log.run_id)
    assert pack["tasks"][0]["review_findings"] == expected()


@pytest.mark.parametrize("text", ["no findings", "No findings.", "- no findings",
                                  "**No findings**", "No findings\nVERDICT: READY"])
def test_no_findings(text):
    assert worker_report.parse_findings(text) == expected(verdict="READY" if "VERDICT" in text else None)


@pytest.mark.parametrize("text", ["", "I read the code and it looks good.",
                                  "I found no findings file; the diff path was wrong.",
                                  "no findings.txt", "No issues found", "LGTM",
                                  "[critical|warning|nit] path:line - finding - evidence",
                                  "[severity: critical|warning|nit]",
                                  "[options | severity: warning]",
                                  "[critical](docs.md) is a link"])
def test_unparsed(text):
    assert worker_report.parse_findings(text) == expected(unparsed=True)


def test_none():
    assert worker_report.parse_findings(None) is None


def test_placeholder_does_not_hide_real_findings():
    assert worker_report.parse_findings(
        "[critical|warning|nit] template\n[warning] app.py:2 - check - evidence"
    ) == expected(warning=1)


@pytest.mark.parametrize("text", [
    "**P1 — bug finding**\n**missing finding**\n**low finding**",
    "[blocker] finding\n[major] finding\n[minor] finding",
    "severity: high\nseverity: medium\nseverity: nit",
    "[severity: critical finding]\n[severity: warning finding]\n[severity: nit finding]",
    "| Finding | Severity |\n| --- | --- |\n| crash | bug |\n"
    "| check | missing |\n| typo | low |",
    "| Severity | Finding |\n| --- | --- |\n| high | crash |\n"
    "Prose ends the table.\n| warning | unrelated |\n"
    "| Finding | Severity |\n| --- | --- |\n| typo | minor |",
    "[bug] **bug** severity: bug [high] [warning]\n[bug]",
])
def test_legacy_counts_match_health_rows(text):
    task = {"run_id": "parity", "id": "review1-task", "policy": "read-only",
            "summary": {"text": text}}
    row = health._reviews([task], {}, "test", {})["runs"][0]
    findings = worker_report.parse_findings(text)
    for canonical, legacy in (("critical", "serious"), ("warning", "moderate"),
                              ("nit", "minor")):
        assert findings[canonical] == row["levels"][legacy]


@pytest.mark.parametrize("task_id,goal,policy,is_review", [
    ("inspect", "Inspect the logs", "read-only", False),
    ("ReViEw1-task", "Inspect the logs", "read-only", True),
    ("inspect", "Inspect the logs and ReViEw the diff", "read-only", True),
    ("inspect", "x" * 700 + " review the diff", "read-only", True),
    ("review1-task", "Review the diff", "write", False),
])
def test_review_pack_review_task_filter(task_id, goal, policy, is_review):
    log = RunLog("review-filter")
    log.append_next("", [RawEvent(kind="plan", payload={"tasks": [
        {"id": task_id, "engine": "claude", "policy": policy, "goal": goal},
    ]})], engine="", now=1790000000)
    log.append_next(task_id, [RawEvent(kind="text", payload={"text": "no findings"})],
                    engine="claude", now=1790000000)
    pack = review_pack.build(log.run_id)
    assert ("review_findings" in pack["tasks"][0]) == is_review
    assert ("review_findings:" in review_pack.format_text(pack)) == is_review


@pytest.mark.parametrize("summary", [False, True])
@pytest.mark.parametrize("text,counts", [
    ("[warning] app.py:2 - missing check - input\nVERDICT: NOT READY",
     expected(warning=1, verdict="NOT READY")),
    ("no findings\nVERDICT: READY", expected(verdict="READY")),
    ("Review was unreadable", expected(unparsed=True)),
])
def test_review_pack_read_only(capsys, summary, text, counts):
    log = RunLog("findings")
    log.append_next("", [RawEvent(kind="plan", payload={"tasks": [
        {"id": "review1-task", "engine": "claude", "policy": "read-only"},
        {"id": "write", "engine": "codex", "policy": "write"},
    ]})], engine="", now=1790000000)
    log.append_next("review1-task", [RawEvent(kind="text", payload={
        "text": "stale text" if summary else text})], engine="claude", now=1790000000)
    if summary:
        log.append_next("", [RawEvent(kind="summary", payload={"tasks": [
            {"task_id": "review1-task", "text": text}]})], engine="", now=1790000000)
    pack = review_pack.build(log.run_id)
    assert pack["tasks"][0]["review_findings"] == counts
    assert "review_findings" not in pack["tasks"][1]
    output = review_pack.format_text(pack)
    assert output.count("review_findings:") == 1
    if counts["unparsed"]:
        assert "review_findings:\n  unparsed" in output
    else:
        for key, value in counts.items():
            if key != "unparsed":
                assert f"  {key}: {value}" in output
    assert _run(capsys, log.run_id)[0] == 0
