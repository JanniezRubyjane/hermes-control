"""Replay extraction uses temporary histories; validation never needs a real sandbox."""

import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench import grader, replay
from bench.spec import load_spec
from hermes_control.verify import VerifyOutcome


def git(repo, *args, day=1):
    return subprocess.run(grader.git_argv(repo, *args), check=True, capture_output=True,
                          text=True, env={**grader.git_env(),
                          "GIT_AUTHOR_DATE": f"2024-01-{day:02d}T12:00:00+00:00",
                          "GIT_COMMITTER_DATE": f"2024-01-{day:02d}T12:00:00+00:00"}).stdout.rstrip("\n")


def write(repo, name, text):
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture
def history(tmp_path):
    repo = tmp_path / "origin"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Replay Test")
    git(repo, "config", "user.email", "replay@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    write(repo, "app.py", "VALUE = 0\n")
    write(repo, "tests/test_app.py", "from app import VALUE\nassert VALUE >= 0\n")
    write(repo, "tests/test_skill.py", "assert True\n")
    write(repo, "scripts/sandbox-check.sh", "#!/bin/sh\nset -eu\nfor test in \"$@\"; do\n"
          f"  PYTHONPATH=. {shlex.quote(sys.executable)} \"$test\"\ndone\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "initial")
    initial = git(repo, "rev-parse", "HEAD")
    records = []

    def merge(branch, goal, files, day, *, integration=False, merge_edit=False):
        base = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "-b", branch)
        for name, contents in files.items():
            write(repo, name, contents)
        git(repo, "add", ".")
        git(repo, "commit", "-m", goal, day=day)
        task_commit = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", "main")
        prefix = "integration branch" if integration else "task"
        subject = f"Merge hermes-control {prefix} {branch}"
        git(repo, "merge", "--no-ff", "--no-commit", branch)
        if merge_edit:
            with (repo / "tests/test_new.py").open("a") as handle:
                handle.write("# present only in merge tree\n")
            git(repo, "add", ".")
        git(repo, "commit", "-m", subject, day=day)
        sha = git(repo, "rev-parse", "HEAD")
        records.append(dict(sha=sha, base=base, goal=goal, task_commit=task_commit))

    merge("first", "Fix the app value\n\nKeep the public API stable.", {
        "app.py": "VALUE = 1\n",
        "tests/test_app.py": "from app import VALUE\nassert VALUE == 1\n",
        "tests/test_new.py": "#!/usr/bin/env python\n# coding: utf-8\n"
                             "from app import VALUE\nassert VALUE == 1\n",
        "tests/conftest.py": "# excluded from hidden tests\n",
        "tests/support.py": "# not a test file\n",
    }, 2, merge_edit=True)
    merge("second", "Add a useful capability", {
        "app.py": "VALUE = 2\n" + "# implementation detail\n" * 8,
        "tests/nested/test_added.py": "assert True\n",
    }, 4)
    merge("integration", "Integrate branches", {
        "integration.py": "INTEGRATED = True\n",
        "tests/test_integration.py": "assert True\n",
    }, 5, integration=True)
    merge("third", "REGRESSION in another module", {
        "other.py": "# large module\n" * 60,
        "tests/test_bad.py": "from app import VALUE\nassert VALUE == 99\n",
    }, 20)
    return repo, initial, [records[0], records[1], records[3]]


def specs(out, index):
    return [load_spec(out / entry["id"] / "spec.json") for entry in index]


def test_extract_goals_canaries_commands_and_unchanged_origin(history, tmp_path, monkeypatch):
    repo, _, records = history
    write(repo, "untracked", "leave me alone\n")
    write(repo, "app.py", "uncommitted content\n")
    marker = tmp_path / "hook-ran"
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(marker))}\n")
    hook.chmod(0o755)
    git(repo, "config", "core.fsmonitor", str(hook))
    before = (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "missing-git-dir"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(hook.parent))
    out = tmp_path / "private"
    index = replay.extract(repo, out)
    assert len(index) == 3
    assert [entry["merge_sha"] for entry in index] == [record["sha"] for record in records]
    tasks = specs(out, index)
    for entry, task, record in zip(index, tasks, records):
        assert task.id == "r-" + record["sha"][:12]
        assert task.goal == record["goal"] + "\n"
        assert task.repo == {"base_commit": record["base"], "origin_repo": str(repo)}
        assert task.source == "R" and task.policy == "write" and task.protected_paths == []
        assert entry["subject"].startswith("Merge hermes-control task ")
        raw = json.loads((out / task.id / "spec.json").read_text())
        assert raw["hidden_dir"] == "hidden"
        for path in task.hidden_dir.rglob("*.py"):
            data = path.read_bytes()
            assert len(re.findall(rb"# hc-bench-canary: [0-9a-f]{16}\n", data)) == 1
            original = git(repo, "show", f"{record['sha']}:{path.relative_to(task.hidden_dir)}")
            assert re.sub(rb"# hc-bench-canary: [0-9a-f]{16}\n", b"", data).decode().rstrip("\n") == original
    assert [task.kind for task in tasks] == ["bug", "routine", "bug"]
    first = tasks[0]
    assert first.verify == "/bin/sh scripts/sandbox-check.sh tests/test_app.py"
    assert first.hidden_cmd == "/bin/sh scripts/sandbox-check.sh tests/test_app.py tests/test_new.py"
    assert sorted(p.name for p in first.hidden_dir.rglob("*.py")) == ["test_app.py", "test_new.py"]
    new_test = (first.hidden_dir / "tests/test_new.py").read_text().splitlines()
    assert new_test[:2] == ["#!/usr/bin/env python", "# coding: utf-8"]
    assert new_test[2].startswith("# hc-bench-canary: ")
    assert new_test[-1] == "# present only in merge tree"
    assert tasks[1].verify == "/bin/sh scripts/sandbox-check.sh tests/test_app.py"
    assert tasks[1].hidden_cmd == "/bin/sh scripts/sandbox-check.sh tests/nested/test_added.py"
    assert tasks[2].verify == "/bin/sh scripts/sandbox-check.sh tests/test_skill.py"
    assert [entry["size"] for entry in index] == [2, 10, 60]
    assert [entry["stratum"] for entry in index] == ["small", "medium", "large"]
    assert [entry["rework"] for entry in index] == [True, False, False]
    assert (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref")) == before
    assert not marker.exists()
    assert (repo / "app.py").read_text() == "uncommitted content\n"


def test_stratified_sampling_fixed_seed_and_rework_priority(history, tmp_path):
    repo, _, records = history
    first = replay.extract(repo, tmp_path / "a", limit=2, seed=19, strata=True)
    second = replay.extract(repo, tmp_path / "b", limit=2, seed=19, strata=True)
    assert first == second and len(first) == 2
    assert first[0]["merge_sha"] == records[0]["sha"] and first[0]["rework"]
    assert len({entry["stratum"] for entry in first}) == 2
    assert replay.extract(repo, tmp_path / "zero", limit=0, strata=True) == []
    with pytest.raises(ValueError, match="nonnegative"):
        replay.extract(repo, tmp_path / "negative", limit=-1)
    # A larger pool exercises balance and seeded random choices within terciles.
    pool = [dict(id=str(i), stratum=replay.STRATA[i // 6], rework=False) for i in range(18)]
    sampled = replay._sample(pool, 9, 37, True)
    assert sampled == replay._sample(pool, 9, 37, True)
    assert sampled != replay._sample(pool, 9, 38, True)
    assert [sum(t["stratum"] == name for t in sampled) for name in replay.STRATA] == [3, 3, 3]
    for task in pool[:4]:
        task["rework"] = True
    assert all(task["rework"] for task in replay._sample(pool, 2, 37, True))
    assert replay._sample(pool, 5, 17, False) == replay._sample(pool, 5, 17, False)


@pytest.mark.parametrize("location", ["root", "child", "symlink", "subdir"])
def test_refuse_output_inside_origin(history, tmp_path, location):
    repo, _, _ = history
    source = repo
    out = repo if location == "root" else repo / "private-replays"
    if location == "symlink":
        out = tmp_path / "link"
        out.symlink_to(repo, target_is_directory=True)
    if location == "subdir":
        source = repo / "tests"
    with pytest.raises(ValueError, match="outside the repository"):
        replay.extract(source, out)
    assert not (repo / "index.json").exists()
    assert not (repo / "private-replays").exists()


def test_cli_and_default_output(history, tmp_path, monkeypatch, capsys):
    repo, _, _ = history
    monkeypatch.setenv("HOME", str(tmp_path))
    assert replay.main(["extract", "--repo", str(repo), "--limit", "1", "--seed", "3"]) == 0
    out = tmp_path / ".hermes/control/bench/R"
    assert len(json.loads((out / "index.json").read_text())) == 1
    assert "extracted: 1" in capsys.readouterr().out
    direct_out = tmp_path / "direct-cli"
    proc = subprocess.run([sys.executable, str(Path(replay.__file__).resolve()),
                           "extract", "--repo", str(repo), "--out", str(direct_out),
                           "--limit", "2", "--seed", "3", "--strata"],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "extracted: 2"
    assert len(json.loads((direct_out / "index.json").read_text())) == 2
    with pytest.raises(SystemExit) as exc:
        replay.main(["extract", "--repo", str(repo), "--out", str(repo)])
    assert exc.value.code == 2


@pytest.mark.parametrize("location", ["index", "task", "hidden"])
def test_output_symlinks_cannot_redirect_writes_to_origin(history, tmp_path, location):
    repo, _, records = history
    out = tmp_path / "suite"
    out.mkdir()
    task = out / ("r-" + records[0]["sha"][:12])
    if location == "index":
        (out / "index.json").symlink_to(repo / "app.py")
    elif location == "task":
        task.symlink_to(repo, target_is_directory=True)
    else:
        task.mkdir()
        (task / "hidden").symlink_to(repo, target_is_directory=True)
    before = (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref"))
    original = (repo / "app.py").read_bytes()
    with pytest.raises(ValueError, match="symlinks"):
        replay.extract(repo, out)
    assert (repo / "app.py").read_bytes() == original
    assert (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref")) == before


def test_validation_classifications_and_filter_free_exports(history, tmp_path, monkeypatch, capsys):
    repo, _, _ = history
    out = tmp_path / "suite"
    index = replay.extract(repo, out)
    original_specs = [(out / entry["id"] / "spec.json").read_bytes() for entry in index]
    marker = tmp_path / "filter-ran"
    git(repo, "config", "filter.replay.smudge", f"touch {shlex.quote(str(marker))}")
    # Worktree contents, attributes and refs must never enter a tree export.
    write(repo, ".gitattributes", "*.py filter=replay\n")
    write(repo, "app.py", "raise RuntimeError('dirty worktree')\n")
    before = (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref"))
    checkouts = []

    def unsandboxed(command, checkout, *, codex_path):
        assert codex_path == "fake-codex"
        assert checkout != repo and not (checkout / ".git").exists()
        assert not (checkout / ".gitattributes").exists()
        checkouts.append(checkout)
        proc = subprocess.run(command, shell=True, cwd=checkout, capture_output=True, text=True)
        return VerifyOutcome(command, proc.returncode == 0, proc.returncode,
                             (proc.stdout + proc.stderr)[-2000:], None)

    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: "fake-codex")
    monkeypatch.setattr(replay.verify, "run_verify", unsandboxed)
    assert replay.main(["validate", "--out", str(out)]) == 0
    assert "valid: 1 invalid-green-on-base: 1 invalid-red-on-merge: 1 not-validated: 0" in capsys.readouterr().out
    updated = json.loads((out / "index.json").read_text())
    assert [entry["status"] for entry in updated] == list(replay.STATUSES[:3])
    assert len(checkouts) == 6 and all(not checkout.exists() for checkout in checkouts)
    assert [(out / entry["id"] / "spec.json").read_bytes() for entry in index] == original_specs
    assert (git(repo, "status", "--porcelain=v1"), git(repo, "show-ref")) == before
    assert not marker.exists()


@pytest.mark.parametrize("failure", ["missing", "timeout", "launch"])
def test_validation_unavailable_is_not_validated(history, tmp_path, monkeypatch, failure):
    repo, _, _ = history
    out = tmp_path / "suite"
    index = replay.extract(repo, out)
    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: None if failure == "missing" else "fake")

    def unavailable(command, checkout, *, codex_path):
        if failure == "launch":
            raise OSError("cannot launch sandbox")
        return VerifyOutcome(command, False, None, "", "sandbox timed out")

    monkeypatch.setattr(replay.verify, "run_verify", unavailable)
    if failure == "missing":
        monkeypatch.setattr(replay.grader, "export_checkout", lambda *args: pytest.fail("no sandbox"))
    assert replay.validate(out) == dict(zip(replay.STATUSES, [0, 0, 0, 3, 0]))
    updated = json.loads((out / "index.json").read_text())
    assert all(entry["status"] == "not-validated" and entry["validation_reason"] for entry in updated)
    assert all((out / entry["id"] / "spec.json").exists() for entry in index)


@pytest.mark.parametrize("data,prefix", [
    (b"# coding: latin-1\n# caf\xe9\n", b"# coding: latin-1\n"),
    (b"# comment\n# coding: latin-1\n", b"# comment\n# coding: latin-1\n"),
    (b"\xef\xbb\xbfassert True\n", b"\xef\xbb\xbf"),
    (b"#!/usr/bin/python", b"#!/usr/bin/python\n"),
])
def test_canary_preserves_python_preamble(data, prefix):
    result = replay._canary(data)
    assert result.startswith(prefix + b"# hc-bench-canary: ")
    compile(result, "hidden.py", "exec")


def test_extract_replaces_invalid_goal_bytes_and_uses_main_branch(history, tmp_path, monkeypatch):
    repo, initial, records = history
    # A same-named tag must not change which history is extracted.
    git(repo, "tag", "main", initial)
    original_git = replay._git

    def odd_goal(source, *args):
        data = original_git(source, *args)
        if args == ("show", "-s", "--format=%B", records[0]["task_commit"]):
            return b"Fix an odd commit message: \xff\n\n"
        return data

    monkeypatch.setattr(replay, "_git", odd_goal)
    out = tmp_path / "odd-goals"
    index = replay.extract(repo, out)
    assert len(index) == 3
    assert specs(out, index)[0].goal == "Fix an odd commit message: \ufffd\n"


@pytest.mark.parametrize("files,expected", [
    (["tests/test_new.py", "tests/test_skill.py"], ["tests/test_new.py"]),
    (["tests/test_skill.py"], ["tests/test_skill.py"]),
    (["tests/unrelated.py"], []),
])
def test_visible_tests_only_select_paths_at_base(tmp_path, monkeypatch, files, expected):
    def tree(repo, *args):
        assert args == ("ls-tree", "-r", "--name-only", "-z", "base")
        return b"\0".join(name.encode() for name in files) + b"\0"

    monkeypatch.setattr(replay, "_git", tree)
    task = dict(base="base", hidden=["tests/test_new.py"], changes={"other.py": 1})
    assert replay._visible_tests(tmp_path, task) == expected


def test_extract_without_visible_tests_records_true_and_note(history, tmp_path, monkeypatch):
    repo, _, _ = history
    monkeypatch.setattr(replay, "_visible_tests", lambda repo, task: [])
    out = tmp_path / "no-visible"
    index = replay.extract(repo, out)
    for spec in specs(out, index):
        assert spec.verify == "true"
        assert "base commit" in spec.verify_note
        assert spec.hidden_cmd.startswith("/bin/sh scripts/sandbox-check.sh tests/")


@pytest.mark.parametrize("sandbox", [None, "offline"])
def test_validation_marks_malformed_index_entries_and_continues(history, tmp_path, monkeypatch, sandbox):
    repo, _, _ = history
    out = tmp_path / "malformed"
    index = replay.extract(repo, out)
    malformed = [{}, {"id": index[0]["id"]},
                 {"id": [], "merge_sha": index[0]["merge_sha"]},
                 {"id": index[0]["id"], "merge_sha": None}, None, "odd entry"]
    replay._write_json(out / "index.json", malformed + index[:1])
    monkeypatch.setattr(replay.verify, "find_sandbox", lambda: sandbox)
    outcomes = iter([1, 0])

    def offline_check(command, checkout, *, codex_path):
        code = next(outcomes)
        return VerifyOutcome(command, code == 0, code, "", None)

    monkeypatch.setattr(replay.verify, "run_verify", offline_check)
    counts = replay.validate(out)
    assert counts["invalid-index"] == len(malformed)
    assert counts["valid" if sandbox else "not-validated"] == 1
    updated = json.loads((out / "index.json").read_text())
    assert all(entry["status"] == "invalid-index" and entry["validation_reason"]
               for entry in updated[:-1])
    assert updated[-1]["id"] == index[0]["id"]
    assert updated[-1]["status"] == ("valid" if sandbox else "not-validated")
