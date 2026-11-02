"""Hermes is faked; profile files and controller evidence use a temporary home."""

import json
import os
import shlex
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import arm_full as arm
from bench.grader import Grade
from bench.results import read_results
from hermes_control import limits, verify


@pytest.fixture
def suite(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("from answer import VALUE\nassert VALUE == 42\n")
    data = {"id": "fix", "source": "S", "kind": "bug", "policy": "write",
            "goal": "Create answer.py with VALUE = 42", "verify": "true",
            "hidden_dir": str(hidden), "hidden_cmd": f"{shlex.quote(sys.executable)} hidden.py",
            "protected_paths": [], "repo": {"builder": "shop"}}
    (suite / "task.json").write_text(json.dumps(data))
    return suite


@pytest.fixture
def fake_hermes(monkeypatch):
    source = Path(os.environ["HERMES_HOME"])
    (source / "config.yaml").write_text("model: claude\n")
    (source / ".env").write_text("TEST=yes\n")
    (source / "auth.json").write_text("must never copy")
    (source / "control").mkdir()
    (source / "control/usage-claude.json").write_text(json.dumps({"at": time.time(), "info": {}}))
    calls, quotas, commits = [], [], []
    actual_run = subprocess.run
    def fake(command, repo, home, timeout):
        assert home.parent == source / "profiles"
        assert not (home / "auth.json").exists()
        assert (home / "config.yaml").read_text() == "model: claude\n"
        assert (home / ".env").read_text() == "TEST=yes\n"
        assert command == ["hermes", "chat", "-q", "Use Hermes-Control to: Create answer.py with VALUE = 42",
                           "--oneshot", "--in", str(repo)]
        calls.append((home, repo))
        assert quotas == ["claude", "codex"]
        # Two runs: the first write is wrong; only the LAST write solves the spec.
        (home / "control").mkdir()
        for index, value in enumerate((0, 42), 1):
            (repo / "answer.py").write_text(f"VALUE = {value}\n")
            arm.git(repo, "add", "-A")
            arm.git(repo, "commit", "-m", f"write {index}")
            sha = arm.git(repo, "rev-parse", "HEAD")
            commits.append(sha)
            run_id = f"r{index}"
            def event(kind, payload, task_id="write", ts=0):
                return {"run_id": run_id, "kind": kind, "task_id": task_id, "ts": ts, "payload": payload}
            events = [event("plan", {"tasks": [
                {"id": "read", "policy": "read-only", "engine": "claude"},
                {"id": "write", "policy": "write", "engine": "codex"}],
                "planner": {"session_id": "planner"}}, task_id="", ts=index),
                event("spawned", {}, ts=10),
                event("result", {"ok": True, "usage": {"input_tokens": 12}}, ts=12),
                event("summary", {"tasks": [{"task_id": "write", "commit": sha,
                    "verify": {"passed": True, "exit_code": 0}}]}, task_id="", ts=13)]
            (home / "control" / f"run-{run_id}.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n")
        with sqlite3.connect(home / "state.db") as db:
            db.execute("CREATE TABLE sessions (id TEXT, api_call_count INT, cache_read_tokens INT, cache_write_tokens INT, output_tokens INT)")
            db.execute("INSERT INTO sessions VALUES ('planner', 3, 500, 100, 20)")
            db.execute("CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT, tool_name TEXT)")
            db.execute("INSERT INTO messages VALUES ('planner', 'tool', 'abcdef', 'hermes_control')")
            db.execute("INSERT INTO messages VALUES ('planner', 'assistant', 'Done', NULL)")
        return 0, "Session: planner\n", "", False
    monkeypatch.setattr(arm, "run_hermes", fake)
    monkeypatch.setattr(arm, "ENGINE_NAMES", ("claude", "codex"))
    monkeypatch.setattr(verify, "find_sandbox", lambda: "fake")
    def hidden(command, checkout, *, codex_path):
        result = actual_run(command, shell=True, cwd=checkout, capture_output=True, text=True)
        return verify.VerifyOutcome(command, result.returncode == 0, result.returncode,
                                    result.stdout + result.stderr, None)
    monkeypatch.setattr(verify, "run_verify", hidden)
    def usage(engine, *, refresh=False):
        assert refresh is False
        quotas.append(engine)
        return limits.Usage(engine, windows=(limits.Window("weekly", 20, None),),
                            account="private@example.invalid")
    monkeypatch.setattr(limits, "usage", usage)
    return calls, quotas, commits


def test_full_grades_merged_writes_collects_runs_planner_and_removes_profile(suite, fake_hermes, tmp_path, capsys):
    calls, quotas, commits = fake_hermes
    out = tmp_path / "out.jsonl"
    row, = arm.run_suite(suite, 1, out)
    assert row["solved"] and row["hidden_checked"] and row["verify_green"]
    assert row["arm"] == "2-full" and row["commit"] not in commits
    assert row["write_tasks"] == 2 and row["conflicting_task_ids"] == []
    assert row["eval_home_deleted"] == str(calls[0][0]) and "eval_home" not in row
    assert row["run_ids"] == ["r1", "r2"] and row["tasks_dispatched"] == 4
    assert [t["commit"] for t in row["task_commits"]] == commits
    assert row["planner"] == {"api_call_count": 3, "cache_read_tokens": 500,
                              "cache_write_tokens": 100, "output_tokens": 20,
                              "tool_result_chars": {"hermes_control": 6}}
    assert row["tokens"] == {"input_tokens": 24} and row["worker_s"] == 4
    assert quotas == ["claude", "codex", "claude", "codex"]
    assert row["quota_before"] == row["quota_after"] == {"claude:weekly": 20, "codex:weekly": 20}
    assert "private@example.invalid" not in out.read_text()
    assert all(not home.exists() and not repo.exists() for home, repo in calls)
    assert str(calls[0][0]) in capsys.readouterr().out
    assert all(Path(p).is_file() for p in row["run_logs"])
    assert read_results(out) == [row]


def test_dry_run_no_profile_build_subprocess_or_quota(suite, monkeypatch, tmp_path, capsys):
    def forbidden(*a, **kw):
        pytest.fail("dry-run performed live work")
    for name in ("prepare_repo", "create_eval_home", "grade", "run_hermes"):
        monkeypatch.setattr(arm, name, forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(limits, "usage", forbidden)
    out = tmp_path / "out"
    assert arm.main(["--suite", str(suite), "--reps", "2", "--out", str(out), "--dry-run"]) == 0
    plans = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(plans) == 2 and all(p["arm"] == "2-full" for p in plans)
    assert not out.exists() and not (Path(os.environ["HERMES_HOME"]) / "profiles").exists()


def test_every_engine_checked_including_planner_before_build(suite, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(arm, "ENGINE_NAMES", ("codex", "gemini"))
    monkeypatch.setattr(arm, "prepare_repo", lambda *a: pytest.fail("must stop before build"))
    def guard(lane):
        calls.append(lane["engine"])
        return lane["engine"] != "claude"
    assert arm.run_suite(suite, 3, tmp_path / "out", quota_guard=guard) == []
    assert calls == ["claude", "codex", "gemini"]


def test_missing_logs_ungraded_and_profile_removed(suite, monkeypatch, tmp_path):
    profiles = []
    def fake(command, repo, home, timeout):
        profiles.append(home)
        return 1, "", "failed", False
    monkeypatch.setattr(arm, "run_hermes", fake)
    row, = arm.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert not row["solved"] and row["status"] == "not-graded"
    assert row["tasks_dispatched"] == 0 and row["run_ids"] == []
    assert profiles and not profiles[0].exists()


def test_profile_removed_if_grading_fails(suite, fake_hermes, tmp_path, monkeypatch):
    calls, _, _ = fake_hermes
    def fail(*a, **kw):
        raise ValueError("grading failed")
    monkeypatch.setattr(arm, "grade", fail)
    with pytest.raises(ValueError, match="grading failed"):
        arm.run_suite(suite, 1, tmp_path / "out")
    assert calls and not calls[0][0].exists()


def test_missing_write_commit_grades_available_commits(suite, fake_hermes, monkeypatch, tmp_path):
    original = arm.collect_runs
    def missing(home):
        tasks, *rest = original(home)
        tasks[-1]["commit"] = ""
        return tasks, *rest
    monkeypatch.setattr(arm, "collect_runs", missing)
    row, = arm.run_suite(suite, 1, tmp_path / "out")
    assert row["commit"] and not row["solved"] and row["status"] == "graded"
    assert row["hidden_checked"] and row["missing_commits"] == ["write"]


@pytest.mark.parametrize("age_delta", [-1, 0, 1])
def test_copy_back_only_newer_report_before_quota_after_and_delete(
        suite, fake_hermes, tmp_path, monkeypatch, age_delta):
    source = Path(os.environ["HERMES_HOME"])
    path = source / "control/usage-claude.json"
    original_report = {"at": time.time(), "info": {"utilization": 0.1}, "account": "test"}
    path.write_text(json.dumps(original_report))
    incoming = {**original_report, "at": original_report["at"] + age_delta,
                "info": {"utilization": 0.7}}
    original_run = arm.run_hermes
    homes = []
    def run(*args):
        result = original_run(*args)
        home = args[2]
        homes.append(home)
        (home / "control/usage-claude.json").write_text(json.dumps(incoming))
        return result
    monkeypatch.setattr(arm, "run_hermes", run)
    expected = incoming if age_delta > 0 else original_report
    original_usage = limits.usage
    def usage(engine, **kwargs):
        # fake_hermes asserts which reads happened before starting Hermes.
        if homes:
            assert json.loads(path.read_text()) == expected
            assert homes[0].exists()
        return original_usage(engine, **kwargs)
    monkeypatch.setattr(limits, "usage", usage)
    arm.run_suite(suite, 1, tmp_path / "out")
    assert json.loads(path.read_text()) == expected
    assert not homes[0].exists()


@pytest.mark.parametrize("existing", [None, "corrupt"])
def test_copy_back_when_real_report_missing_or_invalid(tmp_path, existing):
    source, home = tmp_path / "source", tmp_path / "eval"
    for directory in [source, home]:
        (directory / "control").mkdir(parents=True)
    target = source / "control/usage-claude.json"
    if existing:
        target.write_text(existing)
    report = {"at": 20, "info": {"utilization": 0.3}}
    (home / "control/usage-claude.json").write_text(json.dumps(report))
    assert arm.copy_claude_report(home, target)
    assert json.loads(target.read_text()) == report


def test_copy_back_survives_grading_failure(suite, fake_hermes, tmp_path, monkeypatch):
    source = Path(os.environ["HERMES_HOME"])
    original_run = arm.run_hermes
    report = {"at": time.time() + 1, "info": {"utilization": 0.5}}
    def run(*args):
        result = original_run(*args)
        (args[2] / "control/usage-claude.json").write_text(json.dumps(report))
        return result
    def fail(*args, **kwargs):
        raise ValueError("grading failed")
    monkeypatch.setattr(arm, "run_hermes", run)
    monkeypatch.setattr(arm, "grade", fail)
    with pytest.raises(ValueError, match="grading failed"):
        arm.run_suite(suite, 1, tmp_path / "out")
    assert json.loads((source / "control/usage-claude.json").read_text()) == report
    assert not fake_hermes[0][0][0].exists()


def test_timeout_kills_hermes_and_sleeping_child_group(suite, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    heartbeat = tmp_path / "heartbeat"
    child = ("import time\nfrom pathlib import Path\n"
             f"path = Path({str(heartbeat)!r})\n"
             "for index in range(2400):\n path.write_text(str(index))\n time.sleep(.05)\n")
    hermes = bin_dir / "hermes"
    hermes.write_text(f"#!{sys.executable}\nimport subprocess, sys, time\n"
                      f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
                      "print('worker started', flush=True)\ntime.sleep(120)\n")
    hermes.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    started = time.monotonic()
    # Allow interpreter startup under load; both processes would run for 120s.
    row, = arm.run_suite(suite, 1, tmp_path / "out", timeout=5,
                         quota_guard=lambda lane: True)
    assert row["timed_out"] and row["cli_exit"] != 0
    assert time.monotonic() - started < 30
    assert "worker started" in Path(row["transcript"]).read_text()
    last = heartbeat.read_text()
    time.sleep(.15)
    assert heartbeat.read_text() == last
    assert not Path(row["eval_home_deleted"]).exists()


def planner_writes(monkeypatch, changes):
    """Emit two independent write-task commits from the same base."""
    def run(command, repo, home, timeout):
        base = arm.git(repo, "rev-parse", "HEAD")
        tasks, summaries = [], []
        for index, (filename, text) in enumerate(changes, 1):
            arm.git(repo, "checkout", "--detach", base)
            (repo / filename).write_text(text)
            arm.git(repo, "add", "-A")
            arm.git(repo, "commit", "-m", f"task {index}")
            sha = arm.git(repo, "rev-parse", "HEAD")
            tasks.append({"id": f"w{index}", "policy": "write", "engine": "codex"})
            summaries.append({"task_id": f"w{index}", "commit": sha})
        control = home / "control"
        control.mkdir()
        events = [{"kind": "plan", "run_id": "r", "payload": {"tasks": tasks}},
                  {"kind": "summary", "run_id": "r", "payload": {"tasks": summaries}}]
        (control / "run-r.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        return 0, "planner says done", "", False
    monkeypatch.setattr(arm, "run_hermes", run)


def test_independent_write_tasks_graded_together_with_fixed_merge_author(suite, tmp_path, monkeypatch):
    planner_writes(monkeypatch, [("first.py", "FIRST = 1\n"), ("second.py", "SECOND = 2\n")])
    seen = []
    def grade(spec, repo, commit, **kwargs):
        assert arm.git(repo, "show", f"{commit}:first.py") == "FIRST = 1"
        assert arm.git(repo, "show", f"{commit}:second.py") == "SECOND = 2"
        assert arm.git(repo, "show", "-s", "--format=%an <%ae>|%cn <%ce>", commit) == (
            "hc-bench <bench@localhost>|hc-bench <bench@localhost>")
        assert len(arm.git(repo, "show", "-s", "--format=%P", commit).split()) == 2
        seen.append(commit)
        return Grade(solved=True)
    monkeypatch.setattr(arm, "grade", grade)
    row, = arm.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["solved"] and row["write_tasks"] == 2
    assert row["commit"] == seen[0]
    assert row["commit"] not in [t["commit"] for t in row["task_commits"]]


def test_conflicting_planner_tasks_are_not_graded_and_record_ids(suite, tmp_path, monkeypatch):
    planner_writes(monkeypatch, [("answer.py", "VALUE = 1\n"), ("answer.py", "VALUE = 2\n")])
    row, = arm.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["status"] == "not-graded" and not row["solved"]
    assert row["reason"] == "planner tasks conflict" and row["commit"] == ""
    assert row["conflicting_task_ids"] == ["w1", "w2"] and row["write_tasks"] == 2
    assert [t["task_id"] for t in row["task_commits"]] == ["w1", "w2"]


def test_readonly_passes_last_worker_full_text_instead_of_planner(suite, tmp_path, monkeypatch):
    path = suite / "task.json"
    spec = json.loads(path.read_text())
    spec.update(policy="read-only", kind="readonly", verify="", expected_answer=["shop"])
    path.write_text(json.dumps(spec))
    answer = "Worker's full explanation.\nANSWER: shop\nMore worker text."
    def run(command, repo, home, timeout):
        control = home / "control"
        control.mkdir()
        events = [{"kind": "plan", "run_id": "r", "payload": {"tasks": [
            {"id": "first", "policy": "read-only"}, {"id": "last", "policy": "read-only"}]}},
            {"kind": "summary", "run_id": "r", "payload": {"tasks": [
                {"task_id": "first", "text": "earlier answer"}, {"task_id": "last", "text": answer}]}}]
        (control / "run-r.jsonl").write_text("\n".join(json.dumps(e) for e in events))
        return 0, "planner's different final answer", "", False
    def grade(spec, repo, commit, **kwargs):
        assert kwargs["answer"] == answer
        assert commit == kwargs["base_commit"]
        assert kwargs["run_result"]["id"] == "last"
        return Grade(solved=True)
    monkeypatch.setattr(arm, "run_hermes", run)
    monkeypatch.setattr(arm, "grade", grade)
    row, = arm.run_suite(suite, 1, tmp_path / "out", quota_guard=lambda lane: True)
    assert row["solved"] and row["write_tasks"] == 0


def test_claude_freshness_cli_flags(suite, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(arm, "run_suite", lambda *a, **kw: calls.append(kw))
    assert arm.main(["--suite", str(suite), "--out", str(tmp_path / "out"),
                     "--claude-max-age-min", "12", "--allow-stale-claude"]) == 0
    guard = calls[0]["quota_guard"]
    assert guard.max_age == 720 and guard.allow_stale_claude


def test_timeout_stops_logged_worker_in_separate_session(tmp_path, monkeypatch):
    from hermes_control import proctree
    # Exercise the real group probe, supplying the start time without sandboxed ps.
    import importlib
    real_probe = importlib.reload(proctree).group_still_running
    monkeypatch.setattr(proctree, "group_still_running", real_probe)
    def start_time(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return None
        return "worker-start"
    monkeypatch.setattr(proctree, "_read_start_time", start_time)
    home = tmp_path / "eval"
    (home / "control").mkdir(parents=True)
    pidfile = tmp_path / "pid"
    heartbeat = tmp_path / "worker-heartbeat"
    child = ("import signal,time\nfrom pathlib import Path\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             f"path = Path({str(heartbeat)!r})\n"
             "while True:\n path.write_text(str(time.monotonic()))\n time.sleep(.02)\n")
    script = tmp_path / "hermes.py"
    script.write_text(
        "import json, os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child!r}], preexec_fn=os.setsid)\n"
        f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "event = {'kind':'spawned', 'payload':{'pid':child.pid, 'started':'worker-start'}}\n"
        "path = Path(os.environ['HERMES_HOME']) / 'control/run-worker.jsonl'\n"
        "path.write_text(json.dumps(event) + '\\n{partial')\n"
        "time.sleep(30)\n")
    try:
        started = time.monotonic()
        code, _, _, timed_out = arm.run_hermes([sys.executable, str(script)], tmp_path, home, 0.7)
        assert timed_out and code != 0 and time.monotonic() - started < 4
        last = heartbeat.read_text()
        time.sleep(.1)
        assert heartbeat.read_text() == last
    finally:
        if pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text()), 9)
            except ProcessLookupError:
                pass


def test_copy_back_uses_controller_log_directory(suite, tmp_path, monkeypatch):
    control = tmp_path / 'canonical-home/control'
    control.mkdir(parents=True)
    monkeypatch.setattr(arm, 'run_log_dir', lambda: control)
    report = {'at': time.time(), 'info': {'utilization': .4}}
    def run(command, repo, home, timeout):
        (home / 'control').mkdir()
        (home / 'control/usage-claude.json').write_text(json.dumps(report))
        return 0, '', '', False
    monkeypatch.setattr(arm, 'run_hermes', run)
    arm.run_suite(suite, 1, tmp_path / 'out', quota_guard=lambda lane: True)
    assert json.loads((control / 'usage-claude.json').read_text()) == report
    assert not (Path(os.environ['HERMES_HOME']) / 'control/usage-claude.json').exists()


def test_merge_failure_records_error_and_continues(suite, tmp_path, monkeypatch):
    planner_writes(monkeypatch, [('answer.py', 'VALUE = 42\n')])
    actual = subprocess.run
    def run(argv, **kwargs):
        if 'merge-tree' in argv:
            return subprocess.CompletedProcess(argv, 128, '', 'fatal: merge failed offline')
        return actual(argv, **kwargs)
    monkeypatch.setattr(subprocess, 'run', run)
    rows = arm.run_suite(suite, 2, tmp_path / 'out', quota_guard=lambda lane: True)
    assert len(rows) == 2
    assert all(r['status'] == 'not-graded' and not r['solved'] and
               'fatal: merge failed offline' in r['reason'] for r in rows)


def test_readonly_without_read_task_uses_last_planner_box(suite, tmp_path, monkeypatch):
    path = suite / 'task.json'
    spec = json.loads(path.read_text())
    spec.update(policy='read-only', kind='readonly', verify='', expected_answer=['shop'])
    path.write_text(json.dumps(spec))
    def run(command, repo, home, timeout):
        (home / 'control').mkdir()
        event = {'kind': 'plan', 'run_id': 'r', 'payload': {'tasks': []}}
        (home / 'control/run-r.jsonl').write_text(json.dumps(event))
        return 0, '╭─ ☤ Hermes ─╮\n│ wrong │\n╰──╯\n╭─ ☤ Hermes ─╮\n│ ANSWER: shop │\n╰──╯\nSession: r\n', '', False
    def grade(spec, repo, commit, **kwargs):
        assert kwargs['answer'] == 'ANSWER: shop'
        assert commit == kwargs['base_commit']
        return Grade(solved=True)
    monkeypatch.setattr(arm, 'run_hermes', run)
    monkeypatch.setattr(arm, 'grade', grade)
    row, = arm.run_suite(suite, 1, tmp_path / 'out', quota_guard=lambda lane: True)
    assert row['solved']
