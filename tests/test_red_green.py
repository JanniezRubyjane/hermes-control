"""Controller proof and advisory guards, using real disposable git checkouts."""

import dataclasses
import json
import shlex
import subprocess
import sys

import pytest

from hermes_control import dispatch, landing, pauses, proof, review_pack, runs, watch, worktree
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.verify import VerifyOutcome


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "calc.py").write_text("VALUE = 0\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests/test_old.py").write_text("assert True\nassert True\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    path = tmp_path / "task"
    git(repo, "worktree", "add", "-b", "task", str(path), base)
    wt = worktree.TaskWorktree(path, "task", repo, base)
    monkeypatch.setattr(landing, "run_verify", local_verify)
    yield wt
    assert not list(tmp_path.glob("*.verify-*"))
    assert ".verify-" not in git(repo, "worktree", "list", "--porcelain")


def local_verify(command, checkout, *, codex_path):
    if codex_path is None:
        return VerifyOutcome(command, False, None, "", "not verified: no sandbox available")
    proc = subprocess.run(["/bin/sh", "-c", command], cwd=checkout,
                          capture_output=True, text=True)
    reason = None if proc.returncode == 0 else f"verification failed (exit {proc.returncode})"
    return VerifyOutcome(command, reason is None, proc.returncode,
                         proc.stdout + proc.stderr, reason)


def task(**kwargs):
    return ControlTask(id="t", goal="fix", engine="claude", policy=ExecPolicy(write=True),
                       why="test proof", **kwargs)


def finish(tree, t, *, sandbox=lambda: "/fake/codex"):
    diff, reason = worktree.capture_worktree_diff(tree)
    # Include deletions staged by git add -AN as well as newly visible files.
    if reason is None:
        diff = git(tree.path, "diff", "HEAD")
    result = ControlResult(task_id=t.id, engine=t.engine, ok=True, text="done",
                           session_id=None, diff=diff, unproven_reason=reason)
    landed = landing.land(t, result, tree, run_id="proof", sandbox=sandbox)
    return landed, dispatch._done_entry(t, result, tree, landed, None)


def new_test(tree, body="from calc import VALUE\nassert VALUE == 1\n"):
    (tree.path / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tree.path / "tests/test_new.py").write_text(body, encoding="utf-8")


RED = "PYTHONPATH=. python3 tests/test_new.py"


def test_red_green_proven(tree):
    new_test(tree)
    landed, entry = finish(tree, task(verify=RED, red=RED))
    assert entry["verdict"] == "red-green-proven"
    assert entry["unproven_reason"] is None
    assert entry["proof"] == {"verdict": "red-green-proven", "reason": None,
                              "test_paths": ["tests/test_new.py"],
                              "red": {"command": RED, "exit_code": 1,
                                      "output_tail": entry["proof"]["red"]["output_tail"]}}
    assert "AssertionError" in entry["proof"]["red"]["output_tail"]
    assert [e.kind for e in landed.events] == ["commit", "verify", "proof"]


def test_red_passed_is_advisory(tree):
    new_test(tree, "print('passes on base')\nassert True\n")
    _, entry = finish(tree, task(verify=RED, red=RED))
    assert entry["verdict"] == "red-passed"
    assert entry["unproven_reason"] is None
    assert "red_passed: the new tests also pass without the change" in entry["warnings"]


@pytest.mark.parametrize("code", [4, 5])
def test_pytest_error_is_inconclusive(tree, code):
    new_test(tree)
    _, entry = finish(tree, task(verify="true", red=f"exit {code}"))
    assert entry["verdict"] == "red-inconclusive"
    assert entry["proof"]["red"]["exit_code"] == code
    assert entry["unproven_reason"] is None


def test_green_leg_must_pass(tree):
    new_test(tree)
    _, entry = finish(tree, task(verify="true", red="exit 1"))
    assert entry["verdict"] == "red-inconclusive"
    assert entry["proof"]["reason"] == "red command does not pass on the task commit"
    assert entry["unproven_reason"] is None


def test_no_changed_tests_skips_red(tree):
    (tree.path / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    landed, entry = finish(tree, task(verify="true", red="exit 1"))
    assert entry["verdict"] == "red-skipped"
    assert entry["proof"]["reason"] == "no test files changed"
    assert entry["proof"]["test_paths"] == []
    assert next(e for e in landed.events if e.kind == "proof").payload == entry["proof"]


@pytest.mark.parametrize("command", ["test -f data.txt", "test -f ./data.txt"])
def test_check_gaming_warning(tree, command):
    (tree.path / "data.txt").write_text("gaming\n", encoding="utf-8")
    _, entry = finish(tree, task(verify=command))
    assert entry["verdict"] == "tests-passed"
    assert entry["unproven_reason"] is None
    assert any(w.startswith("check_gaming:") and "data.txt" in w for w in entry["warnings"])
    assert "proof" not in entry


@pytest.mark.parametrize("body", [None, "assert True\n", "# removed both\n"])
def test_weakened_existing_tests(tree, body):
    path = tree.path / "tests/test_old.py"
    if body is None:
        path.unlink()
    else:
        path.write_text(body, encoding="utf-8")
    _, entry = finish(tree, task(verify="true"))
    assert any(w.startswith("weakened_tests:") and "test_old.py" in w for w in entry["warnings"])
    assert entry["unproven_reason"] is None


def test_replacing_asserts_does_not_warn(tree):
    (tree.path / "tests/test_old.py").write_text("assert 1\nassert 2\n", encoding="utf-8")
    _, entry = finish(tree, task(verify="true"))
    assert "warnings" not in entry
    assert "proof" not in entry


@pytest.mark.parametrize("extra", [{"policy": "read-only", "verify": "true", "red": "true"},
                                  {"policy": "write", "red": "true"},
                                  {"policy": "write", "verify": "true", "red": "-x"},
                                  {"policy": "write", "verify": "true", "red": 1}])
def test_invalid_red_names_field(extra):
    with pytest.raises(ValueError, match="red"):
        dispatch.parse_tasks([{"goal": "fix", "engine": "claude", **extra}])


def test_red_round_trip():
    parsed = dispatch.parse_tasks([{"goal": "fix", "engine": "claude", "policy": "write",
                                    "verify": "true", "red": " exit 1 "}])[0]
    assert parsed.red == "exit 1"
    assert pauses.task_from_dict(pauses.task_to_dict(parsed)).red == "exit 1"


@pytest.mark.parametrize("command, sandbox, expected", [(None, "/fake/codex", "no-verify"),
                                                      ("true", "/fake/codex", "tests-passed"),
                                                      ("exit 2", "/fake/codex", "verify-failed"),
                                                      ("true", None, "verify-blocked")])
def test_basic_verdicts(tree, command, sandbox, expected):
    new_test(tree)
    _, entry = finish(tree, task(verify=command), sandbox=lambda: sandbox)
    assert entry["verdict"] == expected
    assert "proof" not in entry


def test_uncommitted_verdict(tree):
    _, entry = finish(tree, task(verify="true"))
    assert entry["verdict"] == "not-committed"
    assert "proof" not in entry and "warnings" not in entry


@pytest.mark.parametrize("path", ["/tmp/test.py", "../test.py", "tests/../test.py"])
def test_red_checkout_refuses_unsafe_paths(tree, path):
    with pytest.raises(worktree.VerifyCheckoutError):
        with worktree.red_checkout(tree, tree.base_commit, tree.base_commit, [path]):
            pytest.fail("unsafe path accepted")


def test_red_checkout_uses_literal_paths(tree):
    for name in ["test_[x].py", "test_*.py", "-test.py", ":(glob)test.py"]:
        (tree.path / name).write_text("new test\n", encoding="utf-8")
    git(tree.path, "add", ".")
    git(tree.path, "commit", "-qm", "tests")
    sha = git(tree.path, "rev-parse", "HEAD")
    paths = ["test_[x].py", "test_*.py", "-test.py", ":(glob)test.py"]
    with worktree.red_checkout(tree, tree.base_commit, sha, paths) as checkout:
        assert git(checkout, "rev-parse", "HEAD") == tree.base_commit
        for path in paths:
            assert (checkout / path).read_text(encoding="utf-8") == "new test\n"
        assert (checkout / "calc.py").read_text(encoding="utf-8") == "VALUE = 0\n"


def test_post_verify_exception_keeps_result(tree, monkeypatch):
    new_test(tree)
    def broken(*args, **kwargs):
        raise OSError("red checkout failed")
    monkeypatch.setattr(worktree, "red_checkout", broken)
    monkeypatch.setattr(landing, "red_checkout", broken)
    _, entry = finish(tree, task(verify="true", red=RED))
    assert entry["commit"] == git(tree.path, "rev-parse", "HEAD")
    assert entry["verify"]["passed"] is True
    assert entry["verdict"] == "red-inconclusive"
    assert entry["unproven_reason"] is None


@pytest.mark.parametrize("resume", [False, True])
def test_dispatch_and_resume_carry_evidence(tree, monkeypatch, capsys, resume):
    import argparse

    new_test(tree, "print('passes on base')\nassert True\n")
    t = task(verify=RED, red=RED)
    def execute(ctx, original, run, rerouted, wt, **kwargs):
        diff, _ = worktree.capture_worktree_diff(wt)
        result = ControlResult(t.id, t.engine, True, "done", None, diff=diff)
        return dispatch._Outcome(run, result, rerouted)
    monkeypatch.setattr(dispatch, "_execute", execute)
    monkeypatch.setattr(dispatch, "find_sandbox", lambda: "/fake/codex")
    monkeypatch.setattr(dispatch, "engine_status", lambda *args: {"claude": None})
    monkeypatch.setattr(dispatch, "create_task_worktree_with_reason", lambda *args, **kwargs: (tree, ""))
    if resume:
        record = pauses.Pause("proof-t", "proof", pauses.task_to_dict(t), str(tree.repo_root),
                              pauses.worktree_to_dict(tree), "claude", None, "waiting", None, 1)
        pauses.save(record)
        entry = dispatch.resume_paused(record.id)
    else:
        entry = dispatch.dispatch([pauses.task_to_dict(t)], parent_cwd=str(tree.repo_root),
                                  run_id="proof")["tasks"][0]
    assert entry["verdict"] == "red-passed"
    assert entry["unproven_reason"] is None
    events = RunLog("proof").read_all()
    assert next(e for e in events if e.kind == "proof").payload == entry["proof"]
    summary = next(e for e in reversed(events) if e.kind == "summary").payload["tasks"][0]
    for key in ("proof", "warnings", "verdict"):
        assert summary[key] == entry[key]
    pack = review_pack.build("proof")
    evidence = pack["tasks"][0]
    for key in ("proof", "warnings", "verdict"):
        assert evidence[key] == entry[key]
    text = review_pack.format_text(pack)
    assert "verdict: red-passed" in text
    assert "red exit_code=0" in text and "passes on base" in text and "red_passed:" in text
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    assert watch.run_cli(parser.parse_args(["review-pack", "proof", "--json"])) == 0
    assert json.loads(capsys.readouterr().out)["tasks"][0]["proof"] == entry["proof"]


@pytest.mark.parametrize("raw", [None, [], "wrong", {"test_globs": "tests/**"},
                                 {"red_exit_codes": ["1"]}, {"red_exit_codes": [True]},
                                 {"test_globs": [1]}])
def test_invalid_settings_use_defaults(raw):
    from hermes_control import proof
    assert proof.parse_settings(raw) == proof.parse_settings(None)


def test_custom_proof_settings(tree, monkeypatch):
    from hermes_control import proof
    assert proof.parse_settings({"test_globs": [], "red_exit_codes": []}) == {
        "test_globs": [], "red_exit_codes": []}
    monkeypatch.setattr(proof, "read_settings", lambda: {
        "test_globs": ["checks/**"], "red_exit_codes": [2]})
    (tree.path / "checks").mkdir()
    (tree.path / "checks/check.py").write_text("# test\n", encoding="utf-8")
    command = "test -f tests/test_new.py && exit 0; exit 2"
    (tree.path / "tests/test_new.py").write_text("# not a test under configured globs\n", encoding="utf-8")
    _, entry = finish(tree, task(verify="true", red=command))
    assert entry["proof"]["test_paths"] == ["checks/check.py"]
    assert entry["verdict"] == "red-green-proven"
    assert any(w.startswith("check_gaming:") for w in entry["warnings"])


@pytest.mark.parametrize("failure", ["timeout", "no-sandbox", "exception"])
def test_red_failure_keeps_verify(tree, monkeypatch, failure):
    new_test(tree)
    def verify(command, checkout, *, codex_path):
        if command == "true":
            return local_verify(command, checkout, codex_path=codex_path)
        if failure == "exception":
            raise OSError("red check failed")
        reason = ("verification timed out after 600s" if failure == "timeout"
                  else "not verified: no sandbox available")
        return VerifyOutcome(command, False, None, "red output", reason)
    monkeypatch.setattr(landing, "run_verify", verify)
    landed, entry = finish(tree, task(verify="true", red=RED))
    assert entry["commit"]
    assert entry["verdict"] == "red-inconclusive"
    assert entry["unproven_reason"] is None
    assert entry["verify"]["passed"] is True
    assert landed.events[1].kind == "verify"
    assert landed.events[2].kind == "proof"


def test_verify_timeout_does_not_run_red(tree, monkeypatch):
    new_test(tree)
    calls = []
    def verify(command, checkout, **kwargs):
        calls.append(command)
        return VerifyOutcome(command, False, None, "timeout", "verification timed out after 600s")
    monkeypatch.setattr(landing, "run_verify", verify)
    _, entry = finish(tree, task(verify="true", red=RED))
    assert entry["verdict"] == "verify-failed"
    assert calls == ["true"]
    assert "proof" not in entry


def test_verify_cleanup_exception_keeps_result(tree, monkeypatch):
    from contextlib import contextmanager

    new_test(tree)
    original = worktree.verify_checkout
    @contextmanager
    def broken(wt, sha):
        with original(wt, sha) as path:
            yield path
        raise OSError("after verify")
    monkeypatch.setattr(landing, "verify_checkout", broken)
    landed, entry = finish(tree, task(verify="true"))
    assert entry["commit"] == git(tree.path, "rev-parse", "HEAD")
    assert entry["verify"]["passed"] is True
    assert entry["verdict"] == "unproven"
    assert "after verify" in entry["unproven_reason"]
    assert landed.events[1].payload["passed"] is True
    assert landed.events[1].payload["unproven_reason"] == entry["unproven_reason"]


def test_red_overlay_failure_cleans_up(tree):
    with pytest.raises(worktree.VerifyCheckoutError):
        with worktree.red_checkout(tree, tree.base_commit, tree.base_commit, ["tests/missing.py"]):
            pytest.fail("overlay should fail")


def test_assert_call_removal_warns(tree):
    path = tree.path / "tests/test_old.py"
    path.write_text("case.assertEqual(1, 1)\ncase.assertTrue(True)\n", encoding="utf-8")
    git(tree.path, "add", ".")
    git(tree.path, "commit", "-qm", "assertion baseline")
    tree = dataclasses.replace(tree, base_commit=git(tree.path, "rev-parse", "HEAD"))
    path.write_text("case.assertEqual(1, 1)\n", encoding="utf-8")
    _, entry = finish(tree, task(verify="true"))
    assert any(w.startswith("weakened_tests:") for w in entry["warnings"])


def test_guard_without_verify(tree):
    (tree.path / "tests/test_old.py").unlink()
    _, entry = finish(tree, task())
    assert entry["verdict"] == "no-verify"
    assert any(w.startswith("weakened_tests:") for w in entry["warnings"])


def test_shlex_failure_is_skipped(tree):
    (tree.path / "data.txt").write_text("gaming\n", encoding="utf-8")
    _, entry = finish(tree, task(verify="test -f 'data.txt"))
    assert "warnings" not in entry
    assert entry["verdict"] == "verify-failed"


def test_settings_read_plugin_namespace(monkeypatch):
    from hermes_control import proof

    configured = {"test_globs": ["checks/**"], "red_exit_codes": [3]}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {"proof": configured}}}}})
    assert proof.read_settings() == configured
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": "broken"}}}})
    assert proof.read_settings() == proof.parse_settings(None)


def test_passing_check_with_claude_warning_is_unproven(tree):
    new_test(tree)
    (tree.path / ".claude").mkdir()
    (tree.path / ".claude/settings.json").write_text("{}\n", encoding="utf-8")
    landed, entry = finish(tree, task(verify=RED, red=RED))
    assert entry["verify"]["passed"] is True
    assert entry["proof"]["verdict"] == "red-green-proven"
    assert entry["verdict"] == "unproven"
    assert entry["unproven_reason"] == (
        "warning: committed diff changes .claude/ (.claude/settings.json); "
        "review Claude settings before merging")
    assert landed.events[1].payload["unproven_reason"] == entry["unproven_reason"]


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("log_failure", [False, True])
def test_landing_summary_and_remote_status_keep_final_evidence(tree, monkeypatch, resume, log_failure):
    new_test(tree)
    t = task(verify=RED, red=RED)

    def execute(ctx, original, run, rerouted, wt, **kwargs):
        diff, _ = worktree.capture_worktree_diff(wt)
        result = ControlResult(t.id, t.engine, True, "done", None, diff=diff)
        return dispatch._Outcome(run, result, rerouted)

    monkeypatch.setattr(dispatch, "_execute", execute)
    monkeypatch.setattr(dispatch, "find_sandbox", lambda: "/fake/codex")
    monkeypatch.setattr(dispatch, "engine_status", lambda *args: {"claude": None})
    monkeypatch.setattr(dispatch, "create_task_worktree_with_reason", lambda *args, **kwargs: (tree, ""))
    original = RunLog.append_next

    def append(self, task_id, events, **kwargs):
        if log_failure and any(event.kind == "commit" for event in events):
            raise OSError("landing log full")
        return original(self, task_id, events, **kwargs)

    monkeypatch.setattr(RunLog, "append_next", append)
    if resume:
        record = pauses.Pause("proof-t", "proof", pauses.task_to_dict(t), str(tree.repo_root),
                              pauses.worktree_to_dict(tree), "claude", None, "waiting", None, 1)
        pauses.save(record)
        entry = dispatch.resume_paused(record.id)
    else:
        entry = dispatch.dispatch([pauses.task_to_dict(t)], parent_cwd=str(tree.repo_root),
                                  run_id="proof")["tasks"][0]
    assert entry["verdict"] == ("unproven" if log_failure else "red-green-proven")
    assert entry["unproven_reason"] == (
        "not verified: landing log failed: OSError: landing log full" if log_failure else None)
    assert entry["verify"]["passed"] is True
    summary = next(e for e in reversed(RunLog("proof").read_all()) if e.kind == "summary")
    assert summary.payload["tasks"][0]["verdict"] == entry["verdict"]
    remote = dispatch.control_status("proof")["tasks"][0]
    for key in ("verdict", "proof", "warnings", "unproven_reason"):
        assert (key in remote) == (key in entry)
        if key in entry:
            assert remote[key] == entry[key]


def test_remote_status_carries_warnings(tree, monkeypatch):
    (tree.path / "data.txt").write_text("check\n", encoding="utf-8")
    t = task(verify="test -f data.txt")
    landed, entry = finish(tree, t)
    log = RunLog("remote")
    log.append_next(t.id, landed.events, engine=t.engine, now=1)
    log.append_next("", [landing.RawEvent(kind="summary", payload={"tasks": [entry]})],
                    engine="", now=2)
    remote = dispatch.control_status("remote")["tasks"][0]
    assert remote["verdict"] == entry["verdict"]
    assert remote["warnings"] == entry["warnings"]
    assert "proof" not in remote


@pytest.mark.parametrize("red_command, exit_code, output, expected", [
    ("red", 2, "SyntaxError: invalid syntax", "red-inconclusive"),
    ("red", 3, "cannot import name", "red-inconclusive"),
    ("red", None, "not verified", "red-inconclusive"),
    ("verify", 1, "AssertionError", "red-green-proven"),
    ("verify", 0, "passes on base", "red-passed"),
])
def test_red_green_avoids_redundant_checks(tree, monkeypatch, red_command, exit_code, output, expected):
    new_test(tree)
    calls = []

    def verify(command, checkout, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return VerifyOutcome(command, True, 0, "green", None)
        return VerifyOutcome(command, exit_code == 0, exit_code, output,
                             None if exit_code == 0 else "red failed")

    monkeypatch.setattr(landing, "run_verify", verify)
    _, entry = finish(tree, task(verify="verify", red=red_command))
    assert entry["verdict"] == expected
    assert calls == ["verify", red_command]


def test_pytest_new_function_import_proves_red(tree):
    (tree.path / "calc.py").write_text("def added():\n    return 1\n", encoding="utf-8")
    (tree.path / "tests/test_new.py").write_text(
        "from calc import added\ndef test_added():\n    assert added() == 1\n", encoding="utf-8")
    command = (f"{shlex.quote(sys.executable)} -m pytest -q "
               "-p no:cacheprovider tests/test_new.py")
    _, entry = finish(tree, task(verify=command, red=command))
    assert entry["verify"]["passed"] is True, entry["verify"]["output_tail"]
    assert entry["proof"]["red"]["exit_code"] == 2
    assert "cannot import name" in entry["proof"]["red"]["output_tail"]
    assert entry["verdict"] == "red-green-proven"
    assert entry["proof"]["reason"] == "red: the new tests could not import what the change adds"


@pytest.mark.parametrize("body", ["def test_broken(:\n", "from calc import missing\n"])
def test_pytest_collection_failure_on_commit_is_inconclusive(tree, body):
    new_test(tree, body)
    command = (f"{shlex.quote(sys.executable)} -m pytest -q "
               "-p no:cacheprovider tests/test_new.py")
    _, entry = finish(tree, task(verify="true", red=command))
    assert entry["proof"]["red"]["exit_code"] == 2
    assert entry["verdict"] == "red-inconclusive"
    assert entry["unproven_reason"] is None


@pytest.mark.parametrize("output", ["ImportError while importing test module", "cannot import name",
                                  "ModuleNotFoundError: No module named", "has no attribute",
                                  "SyntaxError: invalid syntax"])
@pytest.mark.parametrize("green_passes", [False, True])
def test_exit_two_requires_import_evidence_and_green(tree, monkeypatch, output, green_passes):
    new_test(tree)
    calls = []

    def verify(command, checkout, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return VerifyOutcome(command, True, 0, "", None)
        if len(calls) == 2:
            return VerifyOutcome(command, False, 2, output, "verification failed (exit 2)")
        return VerifyOutcome(command, green_passes, 0 if green_passes else 1, "",
                             None if green_passes else "verification failed (exit 1)")

    monkeypatch.setattr(landing, "run_verify", verify)
    _, entry = finish(tree, task(verify="verify", red="red"))
    missing_symbol = not output.startswith("SyntaxError")
    assert entry["verdict"] == ("red-green-proven" if missing_symbol and green_passes
                                else "red-inconclusive")
    assert calls == (["verify", "red", "red"] if missing_symbol else ["verify", "red"])
    if missing_symbol and green_passes:
        assert entry["proof"]["reason"] == "red: the new tests could not import what the change adds"


def test_advisory_diff_failure_only_loses_one_file(tree, monkeypatch):
    (tree.path / "data.txt").write_text("check\n", encoding="utf-8")
    (tree.path / "tests/test_old.py").write_text("# assertions removed\n", encoding="utf-8")
    (tree.path / "tests/test_deleted.py").write_text("assert True\n", encoding="utf-8")
    git(tree.path, "add", ".")
    git(tree.path, "commit", "-qm", "baseline")
    tree = dataclasses.replace(tree, base_commit=git(tree.path, "rev-parse", "HEAD"))
    (tree.path / "data.txt").write_text("changed\n", encoding="utf-8")
    (tree.path / "tests/test_old.py").write_text("assert True\n", encoding="utf-8")
    (tree.path / "tests/test_deleted.py").unlink()
    original = proof._diff

    def diff(*args, **kwargs):
        if kwargs.get("paths") == ("tests/test_old.py",):
            raise OSError("per-file diff failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(proof, "_diff", diff)
    _, entry = finish(tree, task(verify="test -f data.txt"))
    assert entry["warnings"] == ["check_gaming: command names changed non-test file data.txt",
                                 "weakened_tests: deleted test file tests/test_deleted.py"]


@pytest.mark.parametrize("failure", ["skipped", "checkout", "sandbox"])
def test_proof_events_and_trace_include_early_results(tree, monkeypatch, failure):
    if failure == "skipped":
        (tree.path / "calc.py").write_text("VALUE = 1\n", encoding="utf-8")
    else:
        new_test(tree)

    def broken(*args, **kwargs):
        raise OSError("proof setup failed")

    sandbox_calls = []

    def sandbox():
        sandbox_calls.append(True)
        if failure == "sandbox" and len(sandbox_calls) > 1:
            broken()
        return "/fake/codex"

    if failure == "checkout":
        monkeypatch.setattr(landing, "red_checkout", broken)
    landed, entry = finish(tree, task(verify="true", red=RED), sandbox=sandbox)
    event = next(e for e in landed.events if e.kind == "proof")
    assert event.payload == entry["proof"]
    RunLog("trace").append_next("t", landed.events, engine="claude", now=1)
    step = next(e for e in runs.task_trace("trace", "t") if e["type"] == "proof")
    assert step["data"]["verdict"] == entry["verdict"]
    assert step["data"]["red"]["exit_code"] is None


def test_failed_entry_does_not_claim_commit_is_absent(tree):
    entry = dispatch._failed_entry(task(), "task failed; branch task may already hold a commit",
                                   worktree=tree)
    assert "verdict" not in entry
