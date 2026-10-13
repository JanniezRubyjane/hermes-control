import argparse
import json
import os
import sqlite3
import subprocess
import time

import pytest

from hermes_control import health, runlog, watch


REVIEW_LEVELS = {
    "critical": "serious", "blocker": "serious", "high": "serious", "bug": "serious",
    "warning": "moderate", "major": "moderate", "medium": "moderate", "missing": "moderate",
    "nit": "minor", "minor": "minor", "low": "minor",
}


@pytest.fixture
def logs():
    now = time.time()
    path = runlog.run_log_dir() / "run-health.jsonl"
    events = []

    def add(kind, payload, task="", ts=None, engine="codex"):
        events.append(dict(run_id="health", task_id=task, engine=engine,
                           seq=len(events), ts=now - 1000 if ts is None else ts,
                           kind=kind, payload=payload))

    add("plan", {"tasks": [
        {"id": "write", "engine": "codex", "policy": "write", "red": "pytest", "goal": "change"},
        {"id": "review2-write", "engine": "claude", "policy": "read-only", "goal": "inspect"},
        {"id": "inspect", "engine": "codex", "policy": "read-only", "goal": "REVIEW change"},
        {"id": "write-review", "engine": "codex", "policy": "write", "goal": "review"}]})
    for task, engine in [("write", "codex"), ("review2-write", "claude"), ("inspect", "codex")]:
        add("slot_wait", {"state": "waiting"}, task, now - 900, engine)
        add("session", {}, task, now - 890, engine)
        add("result", {"ok": True, "usage": {"input_tokens": 100, "output_tokens": 20}},
            task, now - 790, engine)
    add("commit", {"sha": "abc"}, "write", now - 780)
    add("verify", {"passed": False, "output_tail": "FAILED x"}, "write", now - 580)
    add("proof", {}, "write", now - 575)
    add("summary", {"tasks": [
        {"task_id": "review2-write", "text": "**Critical**: one\n[WARNING]: two\nseverity: bug\n"
         "- **missing**: three\n**nit**: four\nNOT READY\nThe warning was addressed; bugfix is prose."}]}, ts=now - 500)
    add("summary", {"tasks": [
        {"task_id": "write", "verdict": "unproven", "failure_mode": "stall", "warnings": ["red_passed"],
         "unproven_reason": "empty diff: nothing", "diff_stat": "+0/-0"},
        {"task_id": "review2-write", "text": "**Nit**: naming\nREADY TO MERGE"},
        {"task_id": "inspect", "text": "**critical**: bad\n[warning]: caution\nseverity: bug\n"
         "- **missing**: absent\n**nit**: style\nNOT READY\nThe warning is prose; bugfix isn't a label."},
        {"task_id": "write-review", "verdict": "not-committed", "diff_stat": "+0/-0"}]}, ts=now - 490)
    # Recurrence across distinct tasks, newest summaries only.
    add("summary", {"tasks": [{"task_id": f"failed{i}", "failure_mode": "stall"} for i in range(4)]},
        ts=now - 480)
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return now, path


@pytest.fixture
def state_db():
    path = health.state_db_path()
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE sessions(id TEXT, started_at REAL, api_call_count INTEGER,
                input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
                cache_write_tokens INTEGER);
            CREATE TABLE messages(session_id TEXT, role TEXT, content TEXT, tool_name TEXT,
                tool_calls TEXT, active INTEGER);
        """)
        for session, tasks, name, active, age in [
            ("direct", 2, "control_dispatch", None, 1), ("nested", 1, "tool_call", 1, 1),
            ("inactive", 10, "control_dispatch", 0, 1), ("old", 10, "control_dispatch", 1, 40)]:
            args = {"tasks": [{"id": str(i)} for i in range(tasks)]}
            if name == "tool_call":
                args = {"calls": [{"name": "control_dispatch", "arguments": json.dumps(args)},
                                   {"name": "other", "arguments": {}}]}
            calls = [{"function": {"name": name, "arguments": json.dumps(args)}}]
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                         (session, time.time() - age * 86400, 3, 1000, 100, 400, 50))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                         (session, "assistant", "", None, json.dumps(calls), active))
            conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                         (session, "tool", "123456", "control_dispatch", None, active))
        conn.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)",
                     ("direct", "assistant", "", None, "{bad", 1))
    return path


def test_all_log_metrics_and_candidates(logs, state_db, tmp_path):
    now, path = logs
    before = path.read_bytes(), state_db.read_bytes()
    data = health.build(repo=tmp_path, now=now)
    outcomes = data["outcomes"]
    assert outcomes["tasks_by_engine_policy"]["claude/read-only"] == 1
    assert outcomes["verify"] == {"pass": 0, "fail": 1}
    assert outcomes["verdicts"] == {"unproven": 1, "not-committed": 1}
    assert outcomes["warnings"] == {"red_passed": 1}
    assert outcomes["failure_modes"] == {"stall": 5}
    assert outcomes["empty_diff_write_tasks"] == 2
    reviews = data["reviews"]
    assert reviews["reviews"] == 2
    assert reviews["findings"] == {**dict.fromkeys(REVIEW_LEVELS, 0),
                                   "critical": 1, "warning": 1, "nit": 2, "bug": 1, "missing": 1}
    assert reviews["levels"] == {"serious": 2, "moderate": 2, "minor": 2}
    assert reviews["minor_only_or_empty_share"] == .5
    assert reviews["ready_share"] == .5
    assert reviews["runs"][0]["round"] == 2
    assert reviews["verdicts"] == {"READY TO MERGE": 1, "NOT READY": 1, "none": 0}
    planner = data["planner"]
    # 2 direct + 1 nested + 10 inactive tasks: inactive has no duplicate dispatch.
    assert planner["totals"]["dispatched_tasks"] == 13
    assert planner["totals"]["api_calls"] == 9
    assert planner["totals"]["cache_read_tokens"] == 1200
    assert planner["totals"]["cache_write_tokens"] == 150
    assert planner["totals"]["output_tokens"] == 300
    assert planner["totals"]["planner_processed_tokens_per_dispatched_task"] == pytest.approx(4650 / 13)
    assert planner["totals"]["tool_result_characters"] == {"control_dispatch": 12}
    assert planner["worker_processed_tokens"] == 360
    assert planner["worker_processed_tokens_per_task"] == 120
    assert len(planner["sessions"]) == 3
    timing = data["time"]["by_engine"]["codex"]
    assert timing["worker"]["median"] == 110
    assert timing["worker"]["p90"] == 110
    assert timing["landing"]["median"] == 210
    assert timing["proof"]["median"] == 5
    assert timing["slot_wait"]["median"] == 10
    assert data["red_green"]["planned"] == 4
    assert data["red_green"]["with_red"] == 1
    assert data["red_green"]["verdicts"] == {"unproven": 1}
    assert data["lessons"]["classes"][0]["count"] == 5
    # 4650 / 13 / 120 < 5, so only landing time and recurring stalls qualify.
    assert len(data["candidates"]["lines"]) == 2
    assert all("candidate" in line and "fix" not in line for line in data["candidates"]["lines"])
    assert (path.read_bytes(), state_db.read_bytes()) == before


def test_db_is_read_only(state_db):
    with health.open_state_db(state_db) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sessions")


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / "git"
    repo.mkdir()

    def git(*args, date=None, author_date=None):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        if date:
            env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
        if author_date:
            env["GIT_AUTHOR_DATE"] = author_date
        return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True,
                              capture_output=True, text=True).stdout

    git("init", "-b", "main")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    (repo / "base").write_text("base")
    git("add", ".")
    git("commit", "-m", "initial", date="2026-09-01T00:00:00Z")
    return repo, git


@pytest.fixture
def merge_repo(git_repo):
    repo, git = git_repo
    for index, (day, files) in enumerate([(1, ["src/é.py", "tests/a.py"]),
                                        (3, ["src/é.py"]), (11, ["src/é.py"]),
                                        (12, ["tests/a.py"])]):
        date = f"2026-09-{day:02d}T01:00:00Z"
        git("checkout", "-b", f"task{index}")
        for name in files:
            file = repo / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(f"value_for_task = {index}\n")
        git("add", ".")
        git("commit", "-m", "worker", date=date)
        git("checkout", "main")
        git("merge", "--no-ff", f"task{index}", "-m",
            f"Merge hermes-control task task{index}: change", date=date)
    return repo


def test_rework_line_churn_window_and_exclusions(merge_repo):
    now = 1789430400  # 2026-09-15 UTC
    rework = health.build(since=30, repo=merge_repo, now=now)["rework"]
    assert rework["merges"] == 4
    assert rework["reworked_merges"] == 1
    assert rework["rate"] == .25
    assert rework["added_lines"] == 3
    assert rework["churned_lines"] == 1
    assert rework["churn_rate"] == pytest.approx(1 / 3)
    assert rework["top_files"] == [{"file": "src/é.py", "churned_lines": 1}]
    assert rework["tasks"] == [f"task{i}" for i in range(4)]
    assert health.build(since=2, repo=merge_repo, now=now)["rework"]["merges"] == 0


def test_missing_sources_every_section_and_cli_out(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    home = health.state_db_path().parent
    assert list(home.iterdir()) == []
    data = health.build()
    assert list(home.iterdir()) == []
    for key in ("outcomes", "rework", "reviews", "planner", "time", "red_green", "lessons", "candidates"):
        assert data[key]["source"]
        assert data[key]["not_measured"], key
    markdown = health.markdown(data)
    assert markdown.count("not measured:") >= 8
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    defaults = parser.parse_args(["health"])
    assert defaults.since == 30 and defaults.repo is None
    assert watch.run_cli(defaults) == 0
    assert "L1" in capsys.readouterr().out
    target = tmp_path / "report" / "health.md"
    assert watch.run_cli(parser.parse_args(["health", "--json", "--out", str(target)])) == 0
    assert target.read_text().startswith("# Hermes-Control health")
    assert capsys.readouterr().out == ""
    assert watch.run_cli(parser.parse_args(["health", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["since_days"] == 30


def test_partial_sources_are_explicit(logs, tmp_path):
    now, _ = logs
    data = health.build(repo=tmp_path, now=now)
    assert any("verdict" in note for note in data["outcomes"]["not_measured"])
    assert any("claude" in note and "proof" in note for note in data["time"]["not_measured"])


def test_unavailable_db_schema_and_git_timeout(state_db, monkeypatch, tmp_path):
    with sqlite3.connect(state_db) as conn:
        conn.execute("DROP TABLE messages")
    data = health.build(repo=tmp_path)
    assert data["planner"]["not_measured"]

    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired("git", 60)

    monkeypatch.setattr(health.subprocess, "run", timeout)
    assert "60" in " ".join(health.build(repo=tmp_path)["rework"]["not_measured"])


def test_window_uses_event_ts_and_excludes_old_worker_tokens(logs, state_db, tmp_path):
    now, path = logs
    events = [json.loads(line) for line in path.read_text().splitlines()]
    for event in events:
        event["ts"] -= 40 * 86400
    # An in-window summary does not pull old worker tokens into the comparison.
    events[-1]["ts"] = now - 1
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    data = health.build(repo=tmp_path, now=now)
    assert data["planner"]["worker_processed_tokens"] is None
    assert data["time"]["by_engine"] == {}
    assert data["red_green"]["planned"] == 0
    assert data["reviews"]["reviews"] == 0


def test_review_empty_missing_text_and_labels(tmp_path):
    now = time.time()
    path = runlog.run_log_dir() / "run-review.jsonl"
    tasks = [{"id": name, "engine": "codex", "policy": "read-only"}
             for name in ("review1-empty", "review2-missing", "review3-labels")]
    events = [dict(run_id="review", task_id="", engine="", seq=0, ts=now - 10,
                   kind="plan", payload={"tasks": tasks}),
              dict(run_id="review", task_id="", engine="", seq=1, ts=now - 1,
                   kind="summary", payload={"tasks": [
                       {"task_id": "review1-empty", "text": ""},
                       {"task_id": "review2-missing"},
                       {"task_id": "review3-labels", "text": "**Critical**: one\n[Warning]: two\nseverity: nit\n"
                        "**Bug**: four\n[Missing]: five\nThe criticality warning and bugfix are prose."}]})]
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    reviews = health.build(repo=tmp_path, now=now)["reviews"]
    assert reviews["reviews"] == 3 and reviews["measured_texts"] == 2
    assert reviews["minor_only_or_empty_share"] == .5
    assert reviews["verdicts"]["none"] == 2
    assert reviews["findings"] == {**dict.fromkeys(REVIEW_LEVELS, 0),
                                   **dict.fromkeys(("critical", "warning", "nit", "bug", "missing"), 1)}
    assert "lacks summary text" in " ".join(reviews["not_measured"])


@pytest.mark.parametrize("text,severity", [
    ("**critical**", "critical"),
    ("**warning — caution**", "warning"),
    ("[critical]", "critical"),
    ("[Severity: warning]", "warning"),
    ("severity: nit", "nit"),
    ("### 1. [warning] Short title", "warning"),
    ("**W1 — warning — file**", "warning"),
    ("**N1 — nit — file**", "nit"),
    ("1. **critical** — file", "critical"),
    ("### 1. [critical] Short title", "critical"),
    ("- **nit** — file", "nit"),
])
def test_review_label_forms(text, severity):
    reviews = health._reviews([
        dict(run_id="labels", id="review", policy="read-only", summary={"text": text})
    ], {}, "test", {})
    expected = dict.fromkeys(REVIEW_LEVELS, 0)
    expected[severity] = 1
    assert reviews["findings"] == expected
    assert reviews["minor_only_or_empty_share"] == int(severity == "nit")


@pytest.mark.parametrize("severity,level", REVIEW_LEVELS.items())
@pytest.mark.parametrize("form", [
    "**{word}**: finding", "**W1 — {word} — file**",
    "[{word}]: finding", "[Severity: {word}]: finding", "severity: {word}",
    "### 1. [{word}] Short title",
    "| Title | Severity |\n| --- | --- |\n| Finding | {word} |",
])
def test_review_severity_levels_in_label_forms(severity, level, form):
    reviews = health._reviews([
        dict(run_id="labels", id="review", policy="read-only",
             summary={"text": form.format(word=severity.title())})
    ], {}, "test", {})
    expected_findings = dict.fromkeys(REVIEW_LEVELS, 0)
    expected_findings[severity] = 1
    expected_levels = dict.fromkeys(("serious", "moderate", "minor"), 0)
    expected_levels[level] = 1
    assert reviews["findings"] == reviews["runs"][0]["findings"] == expected_findings
    assert reviews["levels"] == reviews["runs"][0]["levels"] == expected_levels
    assert reviews["minor_only_or_empty_share"] == int(level == "minor")


def test_review_severity_table_column():
    text = ("| Title | Severity | Other |\n| --- | :---: | --- |\n"
            "| high | Low | medium |\n| low | HIGH | minor |\n"
            "| bug | Medium | critical |\n| low | no high-severity findings | nit |\n"
            "\n| Title | Status |\n| --- | --- |\n| finding | high |")
    reviews = health._reviews([
        dict(run_id="table", id="review", policy="read-only", summary={"text": text})
    ], {}, "test", {})
    assert reviews["findings"] == {**dict.fromkeys(REVIEW_LEVELS, 0), "low": 1, "high": 1, "medium": 1}
    assert reviews["levels"] == {"serious": 1, "moderate": 1, "minor": 1}


@pytest.mark.parametrize("texts,share", [
    (["**low**: one\n[nit]: two"], 1),
    (["**low**: one\n[nit]: two\nseverity: medium"], 0),
    (["**low**: one\n[nit]: two", "severity: medium", "No findings"], 2 / 3),
])
def test_review_minor_only_share_and_candidate(tmp_path, texts, share):
    reviews = health._reviews([
        dict(run_id="minor", id=f"review{index}", policy="read-only", summary={"text": text})
        for index, text in enumerate(texts)
    ], {}, "test", {})
    assert reviews["minor_only_or_empty_share"] == share
    assert "all_nit_or_empty_share" not in reviews
    data = health.build(repo=tmp_path)
    data["reviews"] = reviews
    candidates = health._candidates(data)["lines"]
    assert bool(candidates) == (share > .5)
    if candidates:
        assert f"minor-only or empty share {share:.1%}" in candidates[0]
    report = health.markdown(data)
    assert f"minor only or empty share: {share:.1%}" in report
    assert "all nit" not in report


@pytest.mark.parametrize("text", [
    "no critical", "zero warnings", "without bugs", "No findings",
    "Low", "Low alone in prose.", "no high-severity findings", "**no high-severity findings**",
    "[no high-severity findings]", "[high](url)", "[severity: medium](url)",
    "**no critical**", "**zero warnings**", "**without bugs**", "**No findings**",
    "[no critical]", "[missing docs](url)", "[critical](url)",
    "[Severity: warning](url)",
    "Missing documentation would help.", "Warning signs are absent.", "Bug reports were checked.",
    "Critical: one", "Warning: two", "Nit: three", "Bug: four", "Missing: five",
    "**There is a critical issue**", "[There is a warning]",
])
def test_review_prose_is_not_a_label(text):
    reviews = health._reviews([
        dict(run_id="labels", id="review", policy="read-only", summary={"text": text})
    ], {}, "test", {})
    assert not any(reviews["findings"].values())
    assert reviews["minor_only_or_empty_share"] == 1


@pytest.mark.parametrize("text,verdict", [
    ("Last line: READY TO MERGE or NOT READY\n**nit** — naming\nREADY TO MERGE\n\n", "READY TO MERGE"),
    ("NOT READY\nREADY TO MERGE\nAdditional explanation.\n", "READY TO MERGE"),
    ("READY TO MERGE\nNOT READY\n\n", "NOT READY"),
    ("No findings", "none"),
])
def test_review_uses_last_verdict_line(text, verdict):
    reviews = health._reviews([
        dict(run_id="verdict", id="review", policy="read-only", summary={"text": text})
    ], {}, "test", {})
    assert reviews["runs"][0]["verdict"] == verdict
    assert reviews["verdicts"][verdict] == 1
    assert reviews["ready_share"] == int(verdict == "READY TO MERGE")


def test_time_percentiles_use_task_intervals(tmp_path):
    now = time.time()
    events = []
    for index, duration in enumerate(range(10, 110, 10)):
        for kind, ts in [("session", now - 200), ("result", now - 200 + duration)]:
            events.append(dict(run_id="timing", task_id=f"task{index}", engine="codex", seq=len(events),
                               ts=ts, kind=kind, payload={}))
    path = runlog.run_log_dir() / "run-timing.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    metric = health.build(repo=tmp_path, now=now)["time"]["by_engine"]["codex"]["worker"]
    assert metric == {"count": 10, "median": 55, "p90": 90}


def _write_events(run_id, records, now):
    events = [dict(run_id=run_id, task_id=task, engine=engine, seq=index,
                   ts=now - age, kind=kind, payload=payload)
              for index, (kind, task, engine, age, payload) in enumerate(records)]
    path = runlog.run_log_dir() / f"run-{run_id}.jsonl"
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_processed_usage_matches_real_engine_cache_semantics(state_db, tmp_path):
    now = time.time()
    with sqlite3.connect(state_db) as conn:
        conn.execute("UPDATE messages SET active = 0 WHERE session_id = 'nested'")
        conn.execute("UPDATE sessions SET input_tokens = 1000000, output_tokens = 100000, "
                     "cache_read_tokens = 180700000, cache_write_tokens = 27400000 "
                     "WHERE id = 'direct'")
    _write_events("usage", [
        ("result", "codex-worker", "codex", 20, {"ok": True, "usage": {
            "input_tokens": 50000000, "cached_input_tokens": 40000000,
            "output_tokens": 2600000, "reasoning_output_tokens": 500000}}),
        ("result", "claude-worker", "claude", 10, {"ok": True, "usage": {
            "input_tokens": 2000000, "cache_creation_input_tokens": 2000000,
            "cache_read_input_tokens": 15000000, "output_tokens": 1000000}}),
    ], now)
    data = health.build(repo=tmp_path, now=now)
    planner = data["planner"]
    # Direct uses 209200000 tokens; nested and inactive add 1550 each, with no duplicates.
    assert planner["totals"]["planner_processed_tokens"] == 209203100
    # 2 direct + 1 nested + 10 inactive tasks still count, even with active=0.
    assert planner["totals"]["planner_processed_tokens_per_dispatched_task"] == pytest.approx(209203100 / 13)
    assert planner["worker_processed_tokens"] == 72600000
    assert planner["worker_processed_tokens_per_task"] == 36300000
    assert planner["worker_cache_read_tokens"] == 55000000
    assert planner["worker_output_tokens"] == 3600000
    assert planner["planner_to_worker_ratio"] == pytest.approx((209203100 / 13) / 36300000)
    report = health.markdown(data)
    assert f"- planner session total / dispatched tasks: {health._inline(209203100 / 13)}" in report
    assert "| planner session total / dispatched tasks |" in report
    assert "worker processed tokens per task: 36300000" in report


def test_worker_processed_usage_sums_attempts_per_task(tmp_path):
    now = time.time()
    _write_events("retry", [
        ("session", "worker", "codex", 50, {}),
        ("result", "worker", "codex", 40, {"usage": {
            "input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 20}}),
        ("session", "worker", "claude", 30, {}),
        ("result", "worker", "claude", 20, {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 30, "output_tokens": 10}}),
    ], now)
    planner = health.build(repo=tmp_path, now=now)["planner"]
    assert planner["worker_tasks"] == 1
    assert planner["worker_processed_tokens_per_task"] == 220


def test_follow_on_additions_are_not_rework_but_removals_are(git_repo):
    repo, git = git_repo
    contents = ["original_function = True\n",
                "original_function = True\nfollow_on_feature = True\n",
                "follow_on_feature = True\nreplacement_function = True\n"]
    for index, text in enumerate(contents):
        date = f"2026-09-{index + 2:02d}T00:00:00Z"
        git("checkout", "-b", f"task{index}")
        (repo / "code.py").write_text(text)
        git("add", ".")
        git("commit", "-m", "worker", date=date)
        git("checkout", "main")
        git("merge", "--no-ff", f"task{index}", "-m",
            f"Merge hermes-control task task{index}: change", date=date)
        rework = health.build(repo=repo, now=1789430400)["rework"]
        assert rework["reworked_merges"] == (1 if index == 2 else 0)
    assert rework["merges"] == 3
    assert rework["added_lines"] == 3
    assert rework["churned_lines"] == 1
    assert rework["churn_rate"] == pytest.approx(1 / 3)
    assert rework["top_files"] == [{"file": "code.py", "churned_lines": 1}]


@pytest.mark.parametrize("later_day", [3, 10])
def test_rework_removal_consumes_only_one_prior_addition(git_repo, later_day):
    repo, git = git_repo
    contents = ["duplicate_line = True\n",
                "duplicate_line = True\nduplicate_line = True\n",
                "duplicate_line = True\n"]
    for index, content in enumerate(contents):
        day = [2, later_day, later_day + 1][index]
        date = f"2026-09-{day:02d}T00:00:00Z"
        git("checkout", "-b", f"task{index}")
        (repo / "code.py").write_text(content)
        git("add", ".")
        git("commit", "-m", "worker", date=date)
        git("checkout", "main")
        git("merge", "--no-ff", f"task{index}", "-m",
            f"Merge hermes-control task task{index}: change", date=date)
    rework = health._rework(repo, 1788220800, 1789430400)
    assert rework["merges"] == 3
    assert rework["added_lines"] == 2
    assert rework["reworked_merges"] == 1
    assert rework["churned_lines"] == 1
    assert rework["churn_rate"] == .5
    assert rework["top_files"] == [{"file": "code.py", "churned_lines": 1}]


def test_task_merge_reachable_through_integration_is_counted(git_repo):
    repo, git = git_repo
    git("checkout", "-b", "integration")
    git("checkout", "-b", "worker")
    (repo / "code.py").write_text("real_feature = True\nshort\n\n")
    git("add", ".")
    git("commit", "-m", "worker", date="2026-08-01T00:00:00Z")
    git("checkout", "integration")
    git("merge", "--no-ff", "worker", "-m", "Merge hermes-control task integrated: feature",
        date="2026-09-02T00:00:00Z", author_date="2026-08-01T00:00:00Z")
    git("checkout", "main")
    git("merge", "--no-ff", "integration", "-m", "Merge hermes-control/topic: integration",
        date="2026-09-14T00:00:00Z")
    rework = health.build(repo=repo, now=1789430400)["rework"]
    assert rework["merges"] == 1
    assert rework["tasks"] == ["integrated"]
    assert rework["added_lines"] == 1
    assert rework["churned_lines"] == 0
    assert health.build(since=7, repo=repo, now=1789430400)["rework"]["merges"] == 0


@pytest.mark.parametrize("fallback", ["text", "result"])
def test_old_summary_uses_full_worker_final_text(tmp_path, fallback):
    now = time.time()
    text = "context\n" * 200 + "**Critical**: broken\nNOT READY"
    _write_events("old-review", [
        ("plan", "", "", 50, {"tasks": [
            {"id": "review-old", "policy": "read-only", "engine": "codex"}]}),
        ("session", "review-old", "codex", 40, {}),
        (fallback, "review-old", "codex", 20,
         {"text": text} if fallback == "text" else {"result": text, "ok": True}),
        ("summary", "", "", 10, {"tasks": [{"task_id": "review-old", "verdict": "read-only"}]}),
    ], now)
    reviews = health.build(repo=tmp_path, now=now)["reviews"]
    assert reviews["measured_texts"] == 1
    assert reviews["findings"]["critical"] == 1
    assert reviews["verdicts"]["NOT READY"] == 1
    assert reviews["not_measured"] == []


def test_landing_starts_at_result_and_proof_at_verify(tmp_path):
    now = time.time()
    _write_events("landing", [
        ("session", "verified", "codex", 100, {}),
        ("result", "verified", "codex", 80, {"ok": True}),
        ("commit", "verified", "codex", 30, {"sha": "abc"}),
        ("verify", "verified", "codex", 30, {"passed": True}),
        ("proof", "verified", "codex", 25, {}),
        ("session", "committed", "claude", 100, {}),
        ("result", "committed", "claude", 60, {"ok": True}),
        ("commit", "committed", "claude", 30, {"sha": "def"}),
    ], now)
    timings = health.build(repo=tmp_path, now=now)["time"]["by_engine"]
    assert timings["codex"]["landing"] == {"count": 1, "median": 50, "p90": 50}
    assert timings["codex"]["proof"] == {"count": 1, "median": 5, "p90": 5}
    assert timings["claude"]["landing"]["median"] == 30
    assert timings["claude"]["proof"]["count"] == 0


def test_candidates_exclude_count_only_and_stale_classes_and_format_dates(tmp_path):
    now = 1791504000  # 2026-10-09 UTC
    records = []
    for index in range(10):
        records.extend([
            ("tool_start", f"probe{index}", "codex", 100, {
                "tool_id": str(index), "input": {"command": "rg missing"}}),
            ("tool_end", f"probe{index}", "codex", 90, {
                "tool_id": str(index), "is_error": True, "exit_code": 1}),
        ])
    records.extend([
        ("summary", "", "", 20 * 86400, {"tasks": [
            {"task_id": f"old{index}", "failure_mode": "old failure"} for index in range(20)]}),
        ("summary", "", "", 86400, {"tasks": [
            {"task_id": f"new{index}", "failure_mode": "active failure"} for index in range(5)]}),
    ])
    _write_events("classes", records, now)
    data = health.build(repo=tmp_path, now=now)
    candidates = "\n".join(data["candidates"]["lines"])
    assert "count only" not in candidates
    assert "old failure" not in candidates
    assert "active failure: 5 occurrences" in candidates
    assert "last_seen 2026-10-08" in health.markdown(data)
    assert f"last_seen {now - 86400}" not in health.markdown(data)
    assert health.build(repo=tmp_path, now=now, recent=.5)["candidates"]["lines"] == []
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    args = parser.parse_args(["health", "--recent", "0.5"])
    assert args.recent == .5
    assert watch.run_cli(args) == 0


@pytest.mark.parametrize("gap", [0, 7 * 86400, 7 * 86400 + 1])
def test_rework_seven_day_boundaries_and_duplicate_lines(git_repo, gap):
    from datetime import datetime, timezone

    repo, git = git_repo
    start = 1788307200  # 2026-09-02 UTC
    contents = ["duplicate_line = True\nduplicate_line = True\n",
                "duplicate_line = True\n"]
    for index, content in enumerate(contents):
        date = datetime.fromtimestamp(start + index * gap, timezone.utc).isoformat()
        git("checkout", "-b", f"task{index}")
        (repo / "code.py").write_text(content)
        git("add", ".")
        git("commit", "-m", "worker", date=date)
        git("checkout", "main")
        git("merge", "--no-ff", f"task{index}", "-m",
            f"Merge hermes-control task task{index}: change", date=date)
    rework = health.build(repo=repo, now=1789430400)["rework"]
    expected = int(gap <= 7 * 86400)
    assert rework["merges"] == 2
    assert rework["added_lines"] == 2
    assert rework["reworked_merges"] == expected
    assert rework["churned_lines"] == expected
    assert rework["churn_rate"] == expected / 2


@pytest.mark.parametrize("reason_source", ["summary", "verify"])
def test_empty_diff_reason_counts_without_diff_stat(tmp_path, reason_source):
    now = time.time()
    records = [
        ("plan", "", "", 20, {"tasks": [
            {"id": "empty", "policy": "write", "engine": "codex"},
            {"id": "unknown", "policy": "write", "engine": "codex"}]}),
        ("summary", "", "", 10, {"tasks": [
            {"task_id": "empty", **({"unproven_reason": "empty diff"} if reason_source == "summary" else {})},
            {"task_id": "unknown"}]}),
    ]
    if reason_source == "verify":
        records.append(("verify", "empty", "codex", 15, {"passed": False, "unproven_reason": "empty diff"}))
    _write_events("empty-diff", records, now)
    outcomes = health.build(repo=tmp_path, now=now)["outcomes"]
    assert outcomes["empty_diff_write_tasks"] == 1
    assert "write task diff missing for 1 tasks" in outcomes["not_measured"]
