"""The review digest preserves evidence while leaving the source repo alone."""

import argparse
import json
import subprocess

from hermes_control import watch
from hermes_control import review_pack
from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


def _parser():
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    return parser


def _run(capsys, *args):
    code = watch.run_cli(_parser().parse_args(["review-pack", *args]))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_review_pack_complete_and_read_only(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    main_branch = _git(repo, "branch", "--show-current")
    _git(repo, "branch", "review-branch")
    _git(repo, "switch", "-q", "review-branch")
    diff_text = "".join(f"line {n}\n" for n in range(100))
    (repo / "changed.txt").write_text(diff_text)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "change")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", main_branch)
    _git(repo, "merge", "-q", "--ff-only", "review-branch")
    before_head = _git(repo, "rev-parse", "HEAD")
    before_status = _git(repo, "status", "--porcelain")

    long_tail = "\n".join(f"failure detail {n}" for n in range(150))
    long_text = "worker final " + "x" * 1200
    plan = {"repo": str(repo), "parallel": {
        "max_tasks": 2, "per_engine": {"claude": 1, "codex": 2}, "error": None}, "tasks": [
        {"id": name, "engine": "claude", "model": "sonnet", "effort": "low",
         "why": f"why {name}", "verify": "pytest -q"}
        for name in ("pass", "fail", "moved", "paused", "errors")]}
    log = RunLog("digest")
    def add(task, engine, kind, payload):
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id="digest",
                         task_id=task, engine=engine,
                         start_seq=log.next_seq(task), now=1790000000))
    add("", "", "plan", plan)
    for name in ("pass", "fail", "moved", "errors"):
        engine = "claude"
        if name == "moved":
            add(name, "codex", "rerouted", {"from": "claude", "to": "codex",
                                                "reason": "capacity exhausted"})
            add(name, "codex", "session", {"model": "gpt-test", "effort": "high"})
            add(name, "codex", "result", {"ok": False})
            add(name, "codex", "limit", {"message": "quota", "resets_at": 1790001000,
                                             "next": "claude"})
            add(name, "claude", "rerouted", {"from": "codex", "to": "claude",
                                               "reason": "quota reset", "continued": True,
                                               "earlier": {"from": "claude", "to": "codex",
                                                           "reason": "capacity exhausted"}})
        add(name, engine, "session", {"model": "gpt-test" if name == "moved" else "sonnet",
                                       "effort": "high" if name == "moved" else "low"})
        add(name, engine, "text", {"text": long_text if name == "pass" else f"final {name}"})
        if name == "errors":
            for n in range(12):
                add(name, engine, "tool_start", {"tool_id": str(n), "name": f"tool-{n}"})
                add(name, engine, "tool_end", {"tool_id": str(n), "is_error": True,
                                                "content": f"error-{n} " + "z" * 350})
        add(name, engine, "result", {"ok": True, "usage": {
            "input_tokens": 12, "cached_input_tokens": 3, "output_tokens": 8,
            "reasoning_output_tokens": 2}, "total_cost_usd": 0.25 if name == "pass" else None})
        add(name, engine, "commit", {"sha": commit, "branch": "review-branch"})
        add(name, engine, "verify", {"command": "pytest -q", "passed": name != "fail",
                                      "exit_code": 1 if name == "fail" else 0,
                                      "output_tail": long_tail if name == "fail" else "a\nb\nc\nd",
                                      "unproven_reason": "full reason" if name == "fail" else None})
    add("paused", "claude", "paused", {"id": "pause-1", "reason": "quota reset",
                                       "resets_at": 1790001000})
    add("", "", "summary", {"tasks": [{"task_id": name, "worktree": str(repo / ".worktrees" / name),
                                            "branch": "review-branch" if name != "paused" else "",
                                            "base_commit": base if name != "paused" else "",
                                            "text": f"actual result {name}" if name == "moved" else
                                                    long_text if name == "pass" else f"final {name}",
                                            "unproven_reason": "full reason" if name == "fail" else None,
                                            "paused_id": "pause-1" if name == "paused" else None}
                                           for name in ("pass", "fail", "moved", "paused", "errors")]})

    code, output, err = _run(capsys, "digest", "--diff")
    assert code == 0 and not err
    assert f"repo: {repo}\nparallel: max_tasks=2 per_engine=claude:1,codex:2\n" in output
    assert [output.index(f"task {name}:") for name in ("pass", "fail", "moved", "paused", "errors")] == sorted(
        output.index(f"task {name}:") for name in ("pass", "fail", "moved", "paused", "errors"))
    assert long_tail in output and long_text in output and "full reason" in output
    assert "+line 99" in output and "+line 0" in output
    assert commit in output and base[:7] in output
    assert "capacity exhausted" in output and "pause-1" in output
    assert "(2 more in the run log)" in output and "tool-9" in output and "tool-10" not in output
    assert str(log.path) in output and "hermes control watch digest --once --no-usage" in output

    code, output, err = _run(capsys, "digest", "--json", "--diff")
    data = json.loads(output)
    assert code == 0 and not err
    assert list(data) == ["run_id", "state", "repo", "parallel", "run_log", "tasks"]
    assert data["parallel"] == plan["parallel"]
    assert [task["id"] for task in data["tasks"]] == ["pass", "fail", "moved", "paused", "errors"]
    assert data["tasks"][1]["verify"]["output_tail"] == long_tail
    assert data["tasks"][0]["worker_final_text"] == long_text
    assert data["tasks"][0]["worker_final_text_source"] == "result"
    assert data["tasks"][0]["base_sha"] == base
    assert data["tasks"][0]["base_source"] == "recorded"
    assert data["tasks"][0]["added_lines"] == 100
    assert data["tasks"][2]["worker_final_text"] == "actual result moved"
    assert data["tasks"][2]["moved"] is True
    assert data["tasks"][2]["actual_engine"] == "claude"
    assert data["tasks"][2]["rerouted"]["earlier"]["reason"] == "capacity exhausted"
    assert data["tasks"][2]["rerouted"]["continued"] is True
    assert data["tasks"][2]["limits"][0]["next"] == "claude"
    assert "continued" in output and "resets_at" in output
    assert data["tasks"][0]["diff"] == subprocess.run(
        ["git", "-C", str(repo), "diff", f"{base}..{commit}"], check=True,
        text=True, capture_output=True).stdout
    assert len(data["tasks"][4]["tool_errors"]) == 10
    assert data["tasks"][4]["tool_errors_omitted"] == 2
    assert data["tasks"][0]["attempts"][0]["cost_usd"] == 0.25
    assert data["tasks"][2]["attempts"][0]["cost_usd"] == "quota"
    assert _git(repo, "status", "--porcelain") == before_status
    assert _git(repo, "rev-parse", "HEAD") == before_head


def test_unknown_run_exits_one(capsys):
    assert _run(capsys, "absent") == (1, "", "no run absent\n")


def test_missing_repo_or_branch_is_reported_per_task(tmp_path, capsys):
    log = RunLog("missing")
    plan = {"repo": str(tmp_path / "gone"), "parallel": {
        "max_tasks": 1, "per_engine": {"claude": 1, "codex": 1},
        "error": "ValueError: parallel.max_tasks must be an integer between 1 and 8"}, "tasks": [
        {"id": "one", "engine": "claude", "why": "review"}]}
    log.append(stamp([RawEvent(kind="plan", payload=plan)], run_id="missing", task_id="",
                     engine="", start_seq=0, now=1790000000))
    code, output, _ = _run(capsys, "missing", "--json")
    assert code == 0
    assert "gone" in json.loads(output)["tasks"][0]["git_error"]
    assert json.loads(output)["parallel"] == plan["parallel"]
    code, output, _ = _run(capsys, "missing")
    assert code == 0
    assert ("parallel: max_tasks=1 per_engine=claude:1,codex:1 "
            "(config invalid, defaults used: ValueError: parallel.max_tasks must be an integer between 1 and 8)") in output


def test_old_log_uses_merge_base_and_last_text(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "file").write_text("base\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-qc", "worker")
    (repo / "file").write_text("changed\n")
    _git(repo, "commit", "-qam", "work")
    commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "-d", base)
    log = RunLog("older")
    def add(task, kind, payload):
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id="older",
                         task_id=task, engine="claude", start_seq=log.next_seq(task),
                         now=1790000000))
    add("", "plan", {"repo": str(repo), "tasks": [{"id": "one", "engine": "claude"}]})
    add("one", "text", {"text": "old final"})
    add("one", "commit", {"sha": commit, "branch": "worker"})
    add("", "summary", {"tasks": [{"task_id": "one", "branch": "worker"}]})
    code, output, _ = _run(capsys, "older", "--json")
    item = json.loads(output)["tasks"][0]
    assert code == 0
    assert item["base_sha"] == base and item["base_source"] == "merge-base"
    assert item["worker_final_text"] == "old final"
    assert item["worker_final_text_source"] == "last text event (older log)"
    assert json.loads(output)["parallel"] is None
    code, output, _ = _run(capsys, "older")
    assert code == 0 and "parallel: not recorded" in output


def test_git_oserror_is_reported(tmp_path, capsys, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    log = RunLog("git-oserror")
    log.append(stamp([RawEvent(kind="plan", payload={"repo": str(repo), "tasks": [
        {"id": "one", "engine": "claude"}]})], run_id="git-oserror", task_id="",
        engine="", start_seq=0, now=1790000000))
    log.append(stamp([RawEvent(kind="commit", payload={"sha": "a" * 40,
                                                        "branch": "worker"})],
                     run_id="git-oserror", task_id="one", engine="claude",
                     start_seq=0, now=1790000000))
    log.append(stamp([RawEvent(kind="summary", payload={"tasks": [
        {"task_id": "one", "branch": "worker", "commit": "a" * 40,
         "base_commit": "b" * 40}]})], run_id="git-oserror", task_id="",
        engine="", start_seq=1, now=1790000000))
    def unavailable(*args, **kwargs):
        raise OSError("git unavailable")
    monkeypatch.setattr(review_pack.subprocess, "run", unavailable)
    code, output, _ = _run(capsys, "git-oserror", "--json")
    assert code == 0
    assert "git unavailable" in json.loads(output)["tasks"][0]["git_error"]


def test_parallel_shape_and_arbitrary_engine_names():
    invalid = [[], "bad", {}, {"max_tasks": "2", "per_engine": {}},
               {"max_tasks": 2, "per_engine": []}]
    for index, parallel in enumerate(invalid):
        log = RunLog(f"invalid-parallel-{index}")
        log.append_next("", [RawEvent(kind="plan", payload={"tasks": [], "parallel": parallel})],
                        engine="", now=1790000000)
        pack = review_pack.build(log.run_id)
        assert pack["parallel"] is None
        assert "parallel: not recorded" in review_pack.format_text(pack)
    log = RunLog("other-engines")
    log.append_next("", [RawEvent(kind="plan", payload={"tasks": [], "parallel": {
        "max_tasks": 2, "per_engine": {"zebra": 1, "codex": 2, "alpha": 1}}})],
                    engine="", now=1790000000)
    assert "per_engine=alpha:1,codex:2,zebra:1" in review_pack.format_text(
        review_pack.build("other-engines"))
