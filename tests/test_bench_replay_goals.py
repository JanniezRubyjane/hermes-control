"""Replay goals cover branch history and validation records completion evidence."""

import json

import pytest

from test_bench_replay import history, git, write
from bench import grader, replay
from bench.spec import load_spec
from hermes_control.verify import VerifyOutcome


PATTERN = r"^(?:=+ )?(\d+) passed\b"


def add_merge(repo, messages):
    git(repo, "checkout", "-b", "goals")
    for number, message in enumerate(messages):
        write(repo, f"tests/test_goal_{number}.py", "assert True\n")
        git(repo, "add", ".")
        git(repo, "commit", "-m", message)
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "goals", "-m", "Merge hermes-control task goals")
    return "r-" + git(repo, "rev-parse", "HEAD")[:12]


@pytest.mark.parametrize("message,goal,kind", [
    ("hermes-control: a6-reconcile (codex)\n\n\nFix recovery\n\nKeep state.",
     "Fix recovery\n\nKeep state.\n", "bug"),
    ("hermes-control: empty (hermes)", "hermes-control: empty (hermes)\n", "routine"),
    ("docs: usage\n\nExplain recovery.", "docs: usage\n\nExplain recovery.\n", "routine"),
])
def test_single_commit_goal(history, tmp_path, message, goal, kind):
    repo, _, _ = history
    task_id = add_merge(repo, [message])
    out = tmp_path / "goals"
    index = replay.extract(repo, out)
    spec = load_spec(out / task_id / "spec.json")
    assert spec.goal == goal
    assert spec.kind == kind
    assert next(entry for entry in index if entry["id"] == task_id)["commits"] == 1


def test_branch_parts_include_inner_merge_commits_in_order(history, tmp_path):
    repo, _, _ = history
    git(repo, "checkout", "-b", "goals")
    write(repo, "tests/test_goal.py", "assert True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "fix: recovery tests")
    write(repo, "recovery.py", "STATE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "hermes-control: reconcile (codex)\n\nAdd recovery.")
    git(repo, "checkout", "-b", "inner")
    write(repo, "gate.py", "GATE = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "hermes-control: gate (hermes)\n\nAdd a gate.")
    git(repo, "checkout", "goals")
    git(repo, "merge", "--no-ff", "inner", "-m", "inner merge must be excluded")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "goals", "-m", "Merge hermes-control task goals")
    task_id = "r-" + git(repo, "rev-parse", "HEAD")[:12]
    out = tmp_path / "parts"
    index = replay.extract(repo, out)
    spec = load_spec(out / task_id / "spec.json")
    assert spec.goal == ("Implement every part below in this one change; none of them is in the repository yet.\n\n"
                         "Part 1 of 3:\nfix: recovery tests\n\n\n"
                         "Part 2 of 3:\nAdd recovery.\n\n\n"
                         "Part 3 of 3:\nAdd a gate.\n")
    assert spec.kind == "bug"
    assert index[-1]["commits"] == 3


def test_cli_ids_filter_before_sampling_and_unknown_id(history, tmp_path, capsys):
    repo, _, records = history
    ids = ["r-" + record["sha"][:12] for record in records]
    out = tmp_path / "selected"
    args = ["extract", "--repo", str(repo), "--out", str(out)]
    assert replay.main([*args, "--id", ids[0], "--id", ids[2]]) == 0
    assert [entry["id"] for entry in json.loads((out / "index.json").read_text())] == [ids[0], ids[2]]
    assert replay.main([*args, "--id", ids[2], "--limit", "1"]) == 0
    assert [entry["id"] for entry in json.loads((out / "index.json").read_text())] == [ids[2]]
    with pytest.raises(ValueError, match="r-unknown"):
        replay.extract(repo, out, ids=["r-unknown"], limit=0)
    with pytest.raises(SystemExit) as exc:
        replay.main([*args, "--id", "r-unknown"])
    assert exc.value.code == 2
    assert "r-unknown" in capsys.readouterr().err


def validation_suite(history, tmp_path, monkeypatch, base_code=1, merge_code=0, tail=""):
    repo, _, _ = history
    out = tmp_path / "validation"
    replay.extract(repo, out, limit=1, strata=True)
    entry = json.loads((out / "index.json").read_text())[0]
    path = out / entry["id"] / "spec.json"
    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: "fake-codex")

    def check(command, checkout, *, codex_path):
        assert codex_path == "fake-codex"
        code = base_code if checkout.name == "base" else merge_code
        return VerifyOutcome(command, code == 0, code, tail, "timeout" if code is None else None)

    monkeypatch.setattr(replay.verify, "run_verify", check)
    return repo, out, path, entry


@pytest.mark.parametrize("tail", ["12 passed, 2 skipped in 0.8s", "99 passed\n=== 12 passed in 0.8s ==="])
def test_validate_pass_floor_loads_and_grader_enforces(history, tmp_path, monkeypatch, tail):
    repo, out, path, entry = validation_suite(history, tmp_path, monkeypatch, tail=tail)
    assert replay.validate(out)["valid"] == 1
    spec = load_spec(path)
    assert spec.hidden_expect == {"pattern": PATTERN, "min": 12}
    assert json.loads((out / "index.json").read_text())[0]["hidden_passed"] == 12
    # A worker fix should not include the fixture merge's added conftest.py.
    git(repo, "checkout", "-b", "worker", spec.repo["base_commit"])
    write(repo, "app.py", "VALUE = 1\n")
    git(repo, "add", "app.py")
    git(repo, "commit", "-m", "Fix app")
    worker = git(repo, "rev-parse", "HEAD")
    for output, solved in [("12 passed in 0.8s", True), ("11 passed", False), ("all skipped", False)]:
        monkeypatch.setattr(grader.verify, "run_verify", lambda command, checkout, **kw:
                            VerifyOutcome(command, True, 0, output, None))
        result = grader.grade(spec, repo, worker, base_commit=spec.repo["base_commit"])
        assert result.solved is solved
        assert not result.gate_violations
        if not solved:
            assert result.reason.startswith("hidden-incomplete:")


def test_validate_no_summary_removes_old_floor_and_records_reason(history, tmp_path, monkeypatch):
    _, out, path, _ = validation_suite(history, tmp_path, monkeypatch, tail="1 failed, 30 passed in 15.51s")
    data = json.loads(path.read_text())
    data["hidden_expect"] = {"pattern": PATTERN, "min": 12}
    replay._write_json(path, data)
    assert replay.validate(out)["valid"] == 1
    assert "hidden_expect" not in json.loads(path.read_text())
    entry = json.loads((out / "index.json").read_text())[0]
    assert "hidden_passed" not in entry
    assert entry["validation_reason"] == "no pytest summary in merge output"


@pytest.mark.parametrize("base_code,merge_code,sandbox,status", [
    (0, 0, "fake-codex", "invalid-green-on-base"),
    (1, 1, "fake-codex", "invalid-red-on-merge"),
    (1, None, "fake-codex", "not-validated"),
    (1, 0, None, "not-validated"),
    (1, 0, "fake-codex", "invalid-index"),
])
def test_revalidate_nonvalid_removes_stale_floor(history, tmp_path, monkeypatch,
                                                base_code, merge_code, sandbox, status):
    _, out, path, entry = validation_suite(history, tmp_path, monkeypatch, base_code, merge_code)
    data = json.loads(path.read_text())
    data["hidden_expect"] = {"pattern": PATTERN, "min": 12}
    replay._write_json(path, data)
    entry["hidden_passed"] = 12
    if status == "invalid-index":
        entry.pop("merge_sha")
    replay._write_json(out / "index.json", [entry])
    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: sandbox)
    assert replay.validate(out)[status] == 1
    assert "hidden_expect" not in json.loads(path.read_text())
    assert "hidden_passed" not in json.loads((out / "index.json").read_text())[0]


def test_validate_refuses_symlinked_spec(history, tmp_path, monkeypatch):
    _, out, path, _ = validation_suite(history, tmp_path, monkeypatch, tail="12 passed")
    original = path.read_bytes()
    target = path.with_name("external.json")
    target.write_bytes(original)
    path.unlink()
    path.symlink_to(target)
    assert replay.validate(out)["not-validated"] == 1
    assert target.read_bytes() == original
    assert "symlink" in json.loads((out / "index.json").read_text())[0]["validation_reason"]
