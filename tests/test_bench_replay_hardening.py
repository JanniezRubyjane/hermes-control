"""Malformed replay entries and unusual histories cannot derail a suite."""

import json

import pytest

from test_bench_replay import git, history, write
from test_bench_replay_goals import PATTERN, validation_suite
from bench import replay
from bench.spec import load_spec


@pytest.mark.parametrize("redirect", ["../escape", "symlink"])
def test_validate_refuses_redirected_task_directory(history, tmp_path, monkeypatch, redirect):
    _, out, path, entry = validation_suite(history, tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data["hidden_expect"] = {"pattern": PATTERN, "min": 12}
    replay._write_json(path, data)
    original = path.read_bytes()
    external = tmp_path / "escape"
    path.parent.rename(external)
    if redirect == "symlink":
        path.parent.symlink_to(external, target_is_directory=True)
    else:
        entry["id"] = redirect
    replay._write_json(out / "index.json", [entry])

    def no_verify(*args, **kwargs):
        pytest.fail("redirected spec must never be verified")

    original_read = type(path).read_text

    def guarded_read(target, *args, **kwargs):
        if target.name == "spec.json":
            pytest.fail("redirected spec must never be read")
        return original_read(target, *args, **kwargs)

    monkeypatch.setattr(replay.verify, "run_verify", no_verify)
    monkeypatch.setattr(type(path), "read_text", guarded_read)
    assert replay.validate(out)["invalid-index"] == 1
    assert (external / "spec.json").read_bytes() == original
    entry = json.loads((out / "index.json").read_text())[0]
    assert entry["status"] == "invalid-index"
    assert ("symlink" if redirect == "symlink" else "component") in entry["validation_reason"]


@pytest.mark.parametrize("task_id", ["", ".", "..", "nested/task", "nested\\task"])
def test_validate_refuses_noncomponent_ids(tmp_path, monkeypatch, task_id):
    out = tmp_path / "suite"
    out.mkdir()
    replay._write_json(out / "index.json", [{"id": task_id, "merge_sha": "unused"}])
    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: None)
    assert replay.validate(out)["invalid-index"] == 1
    entry = json.loads((out / "index.json").read_text())[0]
    assert "component" in entry["validation_reason"]


def test_extract_skips_merge_with_empty_branch_range(history, tmp_path):
    repo, initial, records = history
    first_parent = git(repo, "rev-parse", "main")
    write(repo, "tests/test_merge_only.py", "assert True\n")
    git(repo, "add", ".")
    tree = git(repo, "write-tree")
    merge = git(repo, "commit-tree", tree, "-p", first_parent, "-p", initial,
                "-m", "Merge hermes-control task empty-range")
    git(repo, "update-ref", "refs/heads/main", merge)
    assert git(repo, "rev-list", "--reverse", "--topo-order", "--no-merges",
               f"{merge}^1..{merge}^2") == ""
    out = tmp_path / "extracted"
    index = replay.extract(repo, out)
    assert [entry["merge_sha"] for entry in index] == [record["sha"] for record in records]
    assert all((out / entry["id"] / "spec.json").is_file() for entry in index)


def test_extract_empty_commit_message_is_routine(history, tmp_path):
    repo, _, _ = history
    git(repo, "checkout", "-b", "empty-message")
    write(repo, "tests/test_empty_message.py", "assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "--allow-empty-message", "-m", "")
    # Keep the assembled goal nonempty, as required by TaskSpec, while its
    # first part (which determines kind) is empty.
    git(repo, "commit", "--allow-empty", "-m", "Fix later in the branch")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "empty-message", "-m", "Merge hermes-control task empty-message")
    task_id = "r-" + git(repo, "rev-parse", "HEAD")[:12]
    out = tmp_path / "extracted"
    index = replay.extract(repo, out)
    assert index[-1]["id"] == task_id
    assert load_spec(out / task_id / "spec.json").kind == "routine"


@pytest.mark.parametrize("write_fails", [False, True])
def test_validate_invalid_spec_clears_stale_floor(history, tmp_path, monkeypatch, write_fails):
    _, out, path, _ = validation_suite(history, tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data.pop("goal")
    data["hidden_expect"] = {"pattern": PATTERN, "min": 12}
    replay._write_json(path, data)
    writes = []
    original_write = replay._write_json

    def save(target, value):
        if target == path:
            writes.append(dict(value))
            if write_fails:
                raise OSError("cleanup write failed")
        original_write(target, value)

    monkeypatch.setattr(replay, "_write_json", save)
    assert replay.validate(out)["not-validated"] == 1
    assert len(writes) == 1
    assert "hidden_expect" not in writes[0]
    if not write_fails:
        assert "hidden_expect" not in json.loads(path.read_text())
    entry = json.loads((out / "index.json").read_text())[0]
    assert "goal: required" in entry["validation_reason"]
    if write_fails:
        assert "cleanup write failed" in entry["validation_reason"]
