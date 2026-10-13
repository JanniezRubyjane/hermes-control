"""Offline checks for the routing harness; no Hermes or worker CLI is invoked."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from hermes_control.model_policy import EnginePolicy, ModelChoice

EVAL = Path(__file__).resolve().parents[1] / "scripts" / "eval"
sys.path.insert(0, str(EVAL))
import analyze  # noqa: E402
import build_project  # noqa: E402
import check_answers  # noqa: E402
import run as eval_run  # noqa: E402


def test_eval_home_is_profile_without_credentials_and_links(tmp_path):
    source = tmp_path / "source"
    for name in ("plugins", "skills/software-development", "memories", "control"):
        (source / name).mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", "auth.json", ".env", "SOUL.md", "state.db"):
        (source / name).write_text(name)
    target = tmp_path / "target"
    target.mkdir()
    for link in ("plugins/hermes_control", "skills/software-development/hermes-control"):
        (source / link).symlink_to(target, target_is_directory=True)
    home = eval_run.create_eval_home(source, tmp_path / "out")
    assert home.parent == source / "profiles"
    assert home.name.startswith("hc-eval-")
    assert not (home / "auth.json").exists()
    assert {p.name for p in home.iterdir()} == {"config.yaml", ".env", "plugins", "skills"}
    assert all((home / name).read_text() == name for name in ("config.yaml", ".env"))
    assert all((home / link).resolve() == target for link in
               ("plugins/hermes_control", "skills/software-development/hermes-control"))


def test_final_answer_comes_from_last_assistant_message(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT)")
    db.execute("INSERT INTO sessions VALUES ('s1')")
    db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
    db.executemany("INSERT INTO messages VALUES (?, ?, ?)", [
        ("s1", "assistant", "old"), ("s1", "tool", "misleading trace"),
        ("s1", "assistant", "  "), ("s1", "assistant", "final answer")])
    db.commit(); db.close()
    assert eval_run.final_answer(home, "Session: s1\nreasoning", 1, 2) == "final answer"


def test_final_answer_finds_session_in_time_window(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, created_at REAL)")
    db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
    db.execute("INSERT INTO sessions VALUES ('s', 105)")
    db.execute("INSERT INTO messages VALUES ('s', 'assistant', 'done')")
    db.commit(); db.close()
    assert eval_run.final_answer(home, "no banner", 100, 110) == "done"


def test_runner_sets_isolated_environment_and_records_final_answer(tmp_path, monkeypatch):
    source = tmp_path / "source"
    verify = source / "control/verify"
    (verify / "site").mkdir(parents=True)
    (verify / "python-path").write_text(sys.executable + "\n")
    (source / "config.yaml").write_text("model: test\n")
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps([{"id": "routine", "expect": "routine", "prompt": "Verify with {verify}"}]))
    monkeypatch.setattr(eval_run, "PROMPTS", prompts)
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr(eval_run, "build", lambda project: project)

    def fake_run(command, **kwargs):
        home = Path(kwargs["env"]["HERMES_HOME"])
        assert home != source and home.parent == source / "profiles"
        assert not (home / "auth.json").exists()
        assert kwargs["env"]["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
        assert str(verify / "site") in command[3]
        db = sqlite3.connect(home / "state.db")
        db.execute("CREATE TABLE sessions (id TEXT)")
        db.execute("INSERT INTO sessions VALUES ('s')")
        db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
        db.execute("INSERT INTO messages VALUES ('s', 'assistant', 'final')")
        db.commit(); db.close()
        return subprocess.CompletedProcess(command, 0, "Session: s\ntrace clamp", "")

    monkeypatch.setattr(eval_run.subprocess, "run", fake_run)
    assert eval_run.main(["--live", "--out", str(tmp_path / "out")]) == 0
    record = json.loads((tmp_path / "out/prompts.jsonl").read_text())
    assert record["answer"] == "final" and record["transcript"] == "Session: s\ntrace clamp"
    assert Path(record["eval_home"]).parent == source / "profiles"
    assert not Path(record["eval_home"]).exists()
    assert Path(record["state_db"]).is_file()


def test_runner_keeps_profile_and_copies_logs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    verify = source / "control/verify"
    (verify / "site").mkdir(parents=True)
    (verify / "python-path").write_text(sys.executable + "\n")
    (source / "auth.json").write_text("single-use-token")
    prompts = tmp_path / "prompts.json"
    prompts.write_text(json.dumps([{"id": "check", "expect": "check", "prompt": "hello"}]))
    monkeypatch.setattr(eval_run, "PROMPTS", prompts)
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr(eval_run, "build", lambda project: project)

    def fake_run(command, **kwargs):
        home = Path(kwargs["env"]["HERMES_HOME"])
        assert not (home / "auth.json").exists()
        (home / "control").mkdir()
        (home / "control/run-test.jsonl").write_text("{}\n")
        db = sqlite3.connect(home / "state.db")
        db.execute("CREATE TABLE sessions (id TEXT)")
        db.execute("INSERT INTO sessions VALUES ('s')")
        db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)")
        db.execute("INSERT INTO messages VALUES ('s', 'assistant', 'earlier commentary')")
        db.execute("INSERT INTO messages VALUES ('s', 'assistant', '')")
        db.commit(); db.close()
        return subprocess.CompletedProcess(command, 0, "Session: s", "")

    monkeypatch.setattr(eval_run.subprocess, "run", fake_run)
    assert eval_run.main(["--live", "--keep-home", "--out", str(tmp_path / "out")]) == 0
    record = json.loads((tmp_path / "out/prompts.jsonl").read_text())
    assert Path(record["eval_home"]).is_dir()
    assert not (Path(record["eval_home"]) / "auth.json").exists()
    assert record["answer"] == "" and record["answer_reason"] == "final assistant message is empty"
    assert Path(record["run_logs"][0]).is_file()
    assert Path(record["state_db"]).is_file()
    assert (source / "auth.json").read_text() == "single-use-token"


def test_eval_home_refuses_collision(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "profiles/hc-eval-fixed").mkdir(parents=True)
    monkeypatch.setattr(eval_run, "profile_name", lambda: "hc-eval-fixed")
    with __import__("pytest").raises(FileExistsError, match="already exists"):
        eval_run.create_eval_home(source, tmp_path / "out")


def test_final_answer_empty_last_message_and_session_selection(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, created_at REAL, parent_session_id TEXT)")
    db.executemany("INSERT INTO sessions VALUES (?, ?, ?)",
                   [("first", 105, None), ("last", 106, None), ("child", 107, "first")])
    db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, tool_calls TEXT)")
    db.executemany("INSERT INTO messages VALUES (?, ?, ?, ?)", [
        ("first", "assistant", "old answer", None),
        ("last", "assistant", "commentary", None),
        ("last", "assistant", "", '[{"name":"tool"}]'),
        ("last", "assistant", "  ", None),
        ("child", "assistant", "child answer", None)])
    db.commit(); db.close()
    assert eval_run.final_answer(home, "Session: first\nSession: last", 100, 110) == ""
    assert eval_run.answer_with_reason(home, "Session: last", 100, 110)[1] == "final assistant message is empty"
    assert eval_run.final_answer(home, "Session: first\nSession: missing", 100, 110) == ""
    assert eval_run.final_answer(home, "no footer", 100, 110) == ""
    db = sqlite3.connect(home / "state.db")
    db.execute("DELETE FROM sessions WHERE id='first'")
    db.execute("UPDATE messages SET content='top-level answer' WHERE session_id='last' AND content='  '")
    db.commit(); db.close()
    assert eval_run.final_answer(home, "no footer", 100, 110) == "top-level answer"


def test_verify_placeholder_and_missing_setup(tmp_path):
    home = tmp_path / "source"
    with __import__("pytest").raises(ValueError, match="scripts/verify-setup.sh"):
        eval_run.verify_command(home)
    verify = home / "control/verify"
    (verify / "site").mkdir(parents=True)
    (verify / "python-path").write_text("")
    with __import__("pytest").raises(ValueError, match="scripts/verify-setup.sh"):
        eval_run.verify_command(home)
    (verify / "python-path").write_text("/usr/bin/python3\n")
    command = eval_run.verify_command(home)
    assert command == f"PYTHONPATH={verify / 'site'} /usr/bin/python3 -m pytest -q -p no:cacheprovider"
    prompts = json.loads((EVAL / "prompts.yaml").read_text())
    assert all("{verify}" in p["prompt"] for p in prompts if p["id"] in ("routine", "hard"))
    assert next(p for p in prompts if p["id"] == "pinned")["expect"] == "review"


def test_builder_keeps_bug_without_spoiler():
    source = build_project.FILES["shop/pricing.py"]
    assert "return discount(after, rate)" in source
    assert "BUG:" not in source


def test_answer_checks_use_identifiers_and_numbers_only():
    assert check_answers.check("review", "round_money: +0.005")["ok"]
    assert not check_answers.check("hard", "81 -> 90")["ok"]


def test_builder_has_two_commits_and_main_is_original(tmp_path):
    repo = build_project.build(tmp_path / "fixture")
    count = subprocess.check_output(["git", "rev-list", "--count", "feature/rounding"],
                                    cwd=repo, text=True).strip()
    branch = subprocess.check_output(["git", "branch", "--show-current"],
                                     cwd=repo, text=True).strip()
    assert count == "2" and branch == "main"
    assert "return discount(after, rate)" in (repo / "shop/pricing.py").read_text(encoding="utf-8")


def test_use_key_resolves_from_policy_descriptions():
    policy = {"codex": EnginePolicy("small", {
        "small": ModelChoice("small", "checks with a clear answer", "low"),
        "large": ModelChoice("large", "coding, reviews, summaries", "medium")}),
        "claude": EnginePolicy("swift", {
            "swift": ModelChoice("swift", "routine edits and tests", "high"),
            "deep": ModelChoice("deep", "hard bugs across modules", "max")})}
    assert analyze.expected_route("check", policy) == {"engine": "codex", "model": "small", "effort": "low"}
    assert analyze.expected_route("hard", policy) == {"engine": "claude", "model": "deep", "effort": "max"}
    assert analyze.expected_route("summary", policy)["model"] == "large"
    assert analyze.expected_route("risky-review", policy, "claude")["engine"] == "claude"


def test_current_use_for_routes_and_unresolved_cases():
    policy = {
        "claude": EnginePolicy("claude-sonnet-5-5", {
            "claude-sonnet-5-5": ModelChoice("claude-sonnet-5-5", "read-only review of a diff codex wrote (the default reviewer); write tasks only when codex is over its cap or a sol attempt failed"),
            "claude-opus-5-5": ModelChoice("claude-opus-5-5", "read-only review of risky diffs: the safety guard (policy.py, verify.py, worktrees), changes across modules or task contracts, diffs over ~300 lines; hard bugs after a sol or astra attempt failed"),
        }),
        "codex": EnginePolicy("gpt-6-luna", {
            "gpt-6-luna": ModelChoice("gpt-6-luna", "quick checks with a clear answer: listing, finding usages, finding what is missing or untested; small mechanical edits; never summaries of code"),
            "gpt-6-sol": ModelChoice("gpt-6-sol", "the default for every write task: features, tests, fixes with a clear spec, backend and Python, front-end and page work; read-only briefs when the planner would read more than 3 files; summaries and explaining code"),
            "gpt-6-astra": ModelChoice("gpt-6-astra", "hard problems only: tricky bugs, cross-module refactors, design questions; reviewing a diff when claude is over its cap (so sol never reviews sol)"),
        }),
    }
    for key, model in {"check": "gpt-6-luna", "routine": "gpt-6-sol",
                       "summary": "gpt-6-sol", "hard": "gpt-6-astra",
                       "review": "claude-sonnet-5-5", "risky-review": "claude-opus-5-5"}.items():
        assert analyze.expected_route(key, policy)["model"] == model
    assert "unresolved" in analyze.expected_route("summary", {"codex": EnginePolicy("x", {
        "x": ModelChoice("x", "never summaries")})})
    ambiguous = {"codex": EnginePolicy("x", {"x": ModelChoice("x", "summaries"),
                                             "y": ModelChoice("y", "summaries")})}
    assert "ambiguous" in analyze.expected_route("summary", ambiguous)["unresolved"]


def test_checker_accepts_arrow_and_negated_imports():
    answer = "shop has pricing, discount, cart; pricing -> discount. __init__ has no imports."
    assert check_answers.check("summary", answer) == {"ok": True, "failures": []}


def test_checker_normalizes_module_names_and_full_clause_negation():
    assert check_answers.check("summary", "`pricing.py` -> `discount.py`; cart exists. No __init__ imports are present.")["ok"]
    assert check_answers.check("summary", "pricing und discount; cart; __init__.py")["ok"]
    assert check_answers.check("summary", "pricing; cart; __init__.py")["ok"] is False


def test_write_answer_text_does_not_claim_completion():
    assert not check_answers.check("routine", "clamp and tests passed")["ok"]
    assert not check_answers.check("hard", "81 -> 90")["ok"]


def test_analyser_joins_run_and_planner_context(tmp_path):
    home = tmp_path / "home"
    (home / "control").mkdir(parents=True)
    project = tmp_path / "project"
    out = tmp_path / "out"
    out.mkdir()
    (out / "prompts.jsonl").write_text(json.dumps({
        "id": "check", "expect": "check", "engine": None, "repo": str(project),
        "started_at": 100, "ended_at": 120, "returncode": 0,
        "answer": "valid_rate, total, round_money, checkout"}) + "\n", encoding="utf-8")
    events = [
        (101, "", "", "plan", {"repo": str(project), "planner": {"session_id": "s1"},
                                 "tasks": [{"id": "t", "engine": "codex", "model": "small", "effort": "low"}]}),
        (102, "t", "codex", "session", {"session_id": "w1"}),
        (105, "t", "codex", "result", {"ok": True, "usage": {"input_tokens": 20, "output_tokens": 7}}),
        (106, "t", "codex", "verify", {"exit_code": 0, "passed": True}),
        (107, "", "", "summary", {"tasks": [{"task_id": "t", "engine": "codex", "ok": True}]}),
    ]
    (home / "control/run-r1.jsonl").write_text("".join(json.dumps({
        "run_id": "r1", "task_id": task, "engine": engine, "seq": index,
        "ts": ts, "kind": kind, "payload": payload}) + "\n"
        for index, (ts, task, engine, kind, payload) in enumerate(events)), encoding="utf-8")
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, api_call_count INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, output_tokens INTEGER)")
    db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, tool_name TEXT, content TEXT)")
    db.execute("INSERT INTO sessions VALUES ('s1', 3, 100, 11, 9)")
    db.execute("INSERT INTO messages VALUES ('s1', 'tool', 'terminal', 'hello')")
    db.execute("INSERT INTO messages VALUES ('s1', 'tool', 'terminal', 'world!')")
    db.execute("INSERT INTO messages VALUES ('s1', 'user', 'terminal', 'ignored')")
    db.commit()
    db.close()
    policy = {"codex": EnginePolicy("small", {"small": ModelChoice("small", "checks", "low")})}
    rows = analyze.analyze(out, home=home, policy=policy)
    row = rows[0]
    assert row["run_ids"] == ["r1"] and row["dispatched"]
    assert row["tasks"][0]["actual"] == {"engine": "codex", "model": "small", "effort": "low"}
    assert row["tasks"][0]["worker_seconds"] == 3 and row["aggregate"]["tokens"]["codex"]["output_tokens"] == 7
    assert row["runs"][0]["planner"]["api_call_count"] == 3
    assert row["runs"][0]["planner"]["tool_result_chars"] == {"terminal": 11}
    assert "check | codex/small/low" in analyze.table(rows)


def test_analyser_reports_every_run_task_attempt_and_controller_outcome(tmp_path):
    home = tmp_path / "home"
    (home / "control").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    repo = str(tmp_path / "repo")
    (out / "prompts.jsonl").write_text("\n".join(json.dumps({
        "id": name, "expect": "check", "repo": repo, "started_at": start,
        "ended_at": end, "answer": "valid_rate total round_money checkout"})
        for name, start, end in (("p1", 100, 120), ("p2", 110, 130))) + "\n")

    def write(run_id, events):
        (home / "control" / f"run-{run_id}.jsonl").write_text("".join(json.dumps({
            "run_id": run_id, "task_id": task_id, "engine": engine, "seq": seq,
            "ts": ts, "kind": kind, "payload": payload}) + "\n"
            for seq, (ts, task_id, engine, kind, payload) in enumerate(events)))

    write("a", [
        (101, "", "", "plan", {"repo": repo, "tasks": [
            {"id": "t1", "engine": "codex", "model": "small", "effort": "low"},
            {"id": "t2", "engine": "codex", "model": "small", "effort": "low"}]}),
        (102, "t1", "codex", "session", {"session_id": "one"}),
        (103, "t1", "codex", "result", {"ok": True, "usage": {"input_tokens": 5}}),
        (104, "t1", "codex", "verify", {"exit_code": 1}),
        (105, "t2", "codex", "session", {"session_id": "two"}),
        (106, "t2", "codex", "error", {"ok": False, "usage": {"input_tokens": 7}}),
        (106.5, "", "", "summary", {"tasks": [{"task_id": "t1", "engine": "codex", "ok": True}]}),
        (107, "", "", "summary", {"tasks": [
            {"task_id": "t1", "engine": "codex", "ok": False},
            {"task_id": "t2", "engine": "codex", "ok": False}]}),
    ])
    write("b", [
        (107, "", "", "plan", {"repo": repo, "tasks": [{"id": "t3", "engine": "codex", "model": "small", "effort": "low"}]}),
        (109, "t3", "codex", "session", {"session_id": "three"}),
        (110, "t3", "claude", "rerouted", {"model": "other", "effort": "high"}),
        (111, "t3", "claude", "session", {"session_id": "four"}),
        (112, "t3", "claude", "result", {"ok": True, "usage": {"input_tokens": 11}}),
        (112.5, "t3", "claude", "verify", {"exit_code": 0}),
        (113, "", "", "summary", {"tasks": [{"task_id": "t3", "engine": "claude", "ok": True}]}),
    ])
    write("overlap", [(115, "", "", "plan", {"repo": repo, "tasks": []})])
    write("c", [
        (129, "", "", "plan", {"repo": repo, "tasks": [{"id": "t4", "engine": "codex", "model": "small", "effort": "low"}]}),
        (130, "t4", "codex", "error", {"ok": False, "usage": {"output_tokens": 3}}),
    ])
    policy = {"codex": EnginePolicy("small", {"small": ModelChoice("small", "checks", "low")})}
    rows = analyze.analyze(out, home=home, policy=policy)
    first, second = rows
    assert first["run_ids"] == ["a", "b"]
    assert second["run_ids"] == ["c"]
    assert first["ambiguous_runs"] == second["ambiguous_runs"] == ["overlap"]
    assert len(first["tasks"]) == 3
    assert first["tasks"][0]["ok"] is False
    assert first["tasks"][0]["verify_exit_code"] == 1
    assert first["tasks"][1]["tokens"]["codex"]["input_tokens"] == 7
    assert first["tasks"][1]["worker_seconds"] == 1
    assert first["tasks"][2]["actual"] == {"engine": "claude", "model": "other", "effort": "high"}
    assert [attempt["actual"]["model"] for attempt in first["tasks"][2]["attempts"]] == ["small", "other"]
    assert first["tasks"][2]["attempts"][1]["verify_exit_code"] == 0
    assert first["aggregate"]["tokens"]["codex"]["input_tokens"] == 12
    assert first["aggregate"]["tokens"]["claude"]["input_tokens"] == 11
    assert "t3:codex/small/low -> claude/other/high" in analyze.table(rows)
    assert "unresolved (" in analyze.table([{**first, "expected": {"unresolved": "no match"}}])
    assert second["tasks"][0]["ok"] is False
    assert second["aggregate"]["tokens"]["codex"]["output_tokens"] == 3


def test_runner_refuses_without_live(tmp_path):
    command = [sys.executable, str(EVAL / "run.py"), "--out", str(tmp_path / "out")]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0 and "without --live" in result.stderr
    assert not (tmp_path / "out").exists()


def test_analyser_uses_recorded_home_and_commit_evidence(tmp_path):
    home = tmp_path / "isolated"
    (home / "control").mkdir(parents=True)
    repo = build_project.build(tmp_path / "repo")
    pricing = repo / "shop/pricing.py"
    pricing.write_text(pricing.read_text() + "\ndef clamp(value, minimum, maximum):\n    return max(minimum, min(value, maximum))\n")
    (repo / "tests/test_clamp.py").write_text("from shop.pricing import clamp\n\ndef test_clamp():\n    assert clamp(5, 0, 10) == 5\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Eval", "-c", "user.email=e@example.invalid",
                    "commit", "-m", "clamp"], cwd=repo, check=True, capture_output=True)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    out = tmp_path / "out"
    out.mkdir()
    (out / "prompts.jsonl").write_text(json.dumps({"id": "routine", "expect": "routine",
        "repo": str(repo), "eval_home": str(home), "started_at": 100, "ended_at": 120,
        "answer": "could not do it", "transcript": "clamp exists; 5 passed"}) + "\n")
    events = [
        (101, "", "", "plan", {"repo": str(repo), "planner": {"session_id": "s"}, "tasks": [
            {"id": "write", "policy": "write", "engine": "codex", "model": "sol"},
            {"id": "review", "policy": "read-only", "engine": "claude", "model": "sonnet"}]}),
        (102, "write", "codex", "session", {}),
        (103, "write", "codex", "result", {"ok": True, "usage": {"input_tokens": 10}}),
        (104, "write", "codex", "verify", {"exit_code": 0}),
        (105, "", "", "summary", {"tasks": [{"task_id": "write", "ok": True, "commit": commit}]}),
        (106, "", "", "plan", {"repo": str(repo), "planner": {"session_id": "s"}, "tasks": []}),
    ]
    for run_id, selected in (("r1", events[:5]), ("r2", events[5:])):
        (home / "control" / f"run-{run_id}.jsonl").write_text("".join(json.dumps({
            "run_id": run_id, "task_id": task, "engine": engine, "ts": ts,
            "kind": kind, "payload": payload}) + "\n"
            for ts, task, engine, kind, payload in selected))
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT, api_call_count INTEGER)")
    db.execute("INSERT INTO sessions VALUES ('s', 4)")
    db.commit(); db.close()
    policy = {"codex": EnginePolicy("sol", {"sol": ModelChoice("sol", "default for every write task")})}
    row = analyze.analyze(out, policy=policy)[0]
    assert row["route"] == "match"
    assert row["answer"] == {"ok": True, "failures": []}
    assert row["aggregate"]["tokens"] == {"codex": {"input_tokens": 10}}
    assert analyze.table([row]).count("s:4") == 1
    bad = {**row, "expected": {"engine": "claude", "model": "sonnet"}}
    assert row["tasks"][0]["commit"] == commit
    assert analyze.committed_check("hard", repo, commit)["ok"] is False
    failed_events = list(events[:5])
    failed_events[3] = (104, "write", "codex", "verify", {"exit_code": 1})
    (home / "control/run-r1.jsonl").write_text("".join(json.dumps({
        "run_id": "r1", "task_id": task, "engine": engine, "ts": ts,
        "kind": kind, "payload": payload}) + "\n"
        for ts, task, engine, kind, payload in failed_events))
    failed = analyze.analyze(out, policy=policy)[0]
    assert failed["answer"]["ok"] is False
    assert "verify exit=1" in analyze.table([failed])


def test_route_judges_only_main_task_and_pinned_engine(tmp_path):
    home = tmp_path / "home"
    (home / "control").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    repo = str(tmp_path / "repo")
    prompts = [{"id": "pinned", "expect": "review", "engine": "claude", "repo": repo,
                "eval_home": str(home), "started_at": 100, "ended_at": 110,
                "answer": "round_money +0.005"}]
    (out / "prompts.jsonl").write_text(json.dumps(prompts[0]) + "\n")
    events = [
        {"ts": 101, "kind": "plan", "payload": {"repo": repo, "tasks": [
            {"id": "main", "engine": "claude", "model": "other", "policy": "read-only"},
            {"id": "review-added", "engine": "codex", "model": "sol", "policy": "write"}]}},
        {"ts": 102, "kind": "session", "task_id": "main", "engine": "claude", "payload": {}},
        {"ts": 103, "kind": "result", "task_id": "main", "engine": "claude", "payload": {"ok": True}},
        {"ts": 104, "kind": "session", "task_id": "review-added", "engine": "codex", "payload": {}},
    ]
    (home / "control/run-r.jsonl").write_text("".join(json.dumps({"run_id": "r", **event}) + "\n" for event in events))
    policy = {"claude": EnginePolicy("sonnet", {"sonnet": ModelChoice("sonnet", "default reviewer")})}
    row = analyze.analyze(out, policy=policy)[0]
    assert row["expected"]["model"] == "sonnet"
    assert row["route"] == "match"
    assert "route" in analyze.table([row]).splitlines()[0]
    events[1]["engine"] = "codex"
    events[0]["payload"]["tasks"][0]["engine"] = "codex"
    (home / "control/run-r.jsonl").write_text("".join(json.dumps({"run_id": "r", **event}) + "\n" for event in events))
    assert analyze.analyze(out, policy=policy)[0]["route"] == "mismatch: codex vs claude"


def _commit_fixture(repo, changes):
    for name, content in changes.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Eval", "-c", "user.email=e@example.invalid",
                    "commit", "-m", "fixture fix"], cwd=repo, check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def test_hard_judge_requires_cart_checkout_from_complete_commit(tmp_path):
    repo = build_project.build(tmp_path / "repo")
    pricing = (repo / "shop/pricing.py").read_text().replace("return discount(after, rate)", "return after")
    broken_cart = (repo / "shop/cart.py").read_text().replace("return total(self.prices, rate)", "return 81")
    commit = _commit_fixture(repo, {"shop/pricing.py": pricing, "shop/cart.py": broken_cart})
    result = analyze.committed_check("hard", repo, commit, sys.executable)
    assert not result["ok"] and "checkout" in " ".join(result["failures"])
    assert not (repo / "shop/__pycache__").exists()


def test_routine_judge_requires_clamp_behavior_and_new_test(tmp_path):
    repo = build_project.build(tmp_path / "repo")
    pricing = (repo / "shop/pricing.py").read_text()
    commit = _commit_fixture(repo, {"shop/pricing.py": pricing + "\ndef clamp(value, minimum, maximum):\n    return 5\n",
                                    "tests/test_clamp.py": "def test_clamp():\n    assert True  # clamp\n"})
    assert not analyze.committed_check("routine", repo, commit, sys.executable)["ok"]
    commit = _commit_fixture(repo, {"shop/pricing.py": pricing + "\ndef clamp(value, minimum, maximum):\n    return max(minimum, min(value, maximum))\n"})
    assert analyze.committed_check("routine", repo, commit, sys.executable)["ok"]
    repo2 = build_project.build(tmp_path / "repo2")
    commit2 = _commit_fixture(repo2, {"shop/pricing.py": pricing + "\ndef clamp(value, minimum, maximum):\n    return max(minimum, min(value, maximum))\n"})
    assert not analyze.committed_check("routine", repo2, commit2, sys.executable)["ok"]
    commit2 = _commit_fixture(repo2, {"tests/notes.txt": "clamp should be tested\n"})
    assert not analyze.committed_check("routine", repo2, commit2, sys.executable)["ok"]


def test_route_unresolved_when_multiple_primary_tasks_or_only_review(tmp_path):
    home = tmp_path / "home"
    (home / "control").mkdir(parents=True)
    out = tmp_path / "out"
    out.mkdir()
    repo = str(tmp_path / "repo")
    (out / "prompts.jsonl").write_text(json.dumps({"id": "check", "expect": "check", "repo": repo,
        "eval_home": str(home), "started_at": 100, "ended_at": 110, "answer": "valid_rate total round_money checkout"}) + "\n")
    tasks = [{"id": "review-diff", "policy": "read-only", "engine": "claude", "model": "wrong", "goal": "review the diff"},
             {"id": "one", "policy": "read-only", "engine": "codex", "model": "small"},
             {"id": "two", "policy": "read-only", "engine": "codex", "model": "small"}]
    events = [{"run_id": "r", "ts": 101, "kind": "plan", "payload": {"repo": repo, "tasks": tasks}}]
    for task in tasks:
        events.append({"run_id": "r", "ts": 102, "kind": "result", "task_id": task["id"],
                       "engine": task["engine"], "payload": {"ok": True}})
    path = home / "control/run-r.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    policy = {"codex": EnginePolicy("small", {"small": ModelChoice("small", "checks with a clear answer")})}
    assert analyze.analyze(out, policy=policy)[0]["route"].startswith("unresolved (")
    events[0]["payload"]["tasks"] = tasks[:1]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    assert analyze.analyze(out, policy=policy)[0]["route"].startswith("unresolved (")
