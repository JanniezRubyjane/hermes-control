"""Suite S witnesses: real committed seeds, private overlays and reference fixes."""

import ast
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from bench.builders import build_repo
from bench import grader
from bench.spec import load_suite
from hermes_control.verify import VerifyOutcome
from bench_suite_helpers import (git, overlay, command, assert_pass, commit_reference,
                                 export_commit, assert_private_inventory)

SUITE = ROOT / "scripts/bench/suites/S"
REFERENCES = ROOT / "tests/fixtures/bench_s_ref"
IDS = ["s01-routine", "s02-bug", "s03-refactor", "s04-split", "s05-review",
       "s06-readonly", "s07-trap-gaming"]


@pytest.mark.parametrize("task_id", IDS)
def test_seed_and_reference(task_id, tmp_path):
    specs = load_suite(SUITE / task_id)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.id == task_id
    assert spec.source == "S"
    assert spec.hidden_dir == SUITE / task_id / "hidden"
    assert "hidden" not in spec.goal.casefold()
    repo = build_repo(spec.repo["builder"], tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")
    assert git(repo, "branch", "--show-current") == "main"
    assert git(repo, "rev-list", "--count", "main") == "1"
    assert_pass(command(repo, spec.verify))
    if spec.policy == "read-only":
        expected = (REFERENCES / task_id / "answer.txt").read_text().splitlines()
        assert set(spec.expected_answer) == set(expected)
        assert len(expected) == len(set(expected))
        overlay(spec.hidden_dir, repo)
        assert_pass(command(repo, spec.hidden_cmd))
        assert git(repo, "rev-parse", "HEAD") == base
        return

    # Export the seed's committed blobs, exactly as grading will do.
    seed_checkout = tmp_path / "seed-checkout"
    export_commit(repo, base, seed_checkout)
    overlay(spec.hidden_dir, seed_checkout)
    failed = command(seed_checkout, spec.hidden_cmd)
    assert failed.returncode != 0, f"{task_id}: seed unexpectedly solved"
    expected_errors = ("AssertionError", "ImportError")
    if task_id == "s07-trap-gaming":
        expected_errors = ("ValueError: quantity must be positive",)
    assert any(error in failed.stderr for error in expected_errors), failed.stderr
    assert str(seed_checkout / "tests") in failed.stderr

    protected = {path: (repo / path).read_bytes() for path in spec.protected_paths}
    overlay(REFERENCES / task_id, repo)
    for path, content in protected.items():
        assert (repo / path).read_bytes() == content
    assert_pass(command(repo, spec.verify))
    fixed = commit_reference(repo)
    assert fixed != base
    fixed_checkout = tmp_path / "fixed-checkout"
    export_commit(repo, fixed, fixed_checkout)
    overlay(spec.hidden_dir, fixed_checkout)
    assert_pass(command(fixed_checkout, spec.hidden_cmd))
    assert not any("test_" in path for path in git(repo, "diff", "--name-only", base, fixed).splitlines())


def test_suite_inventory_and_private_material():
    assert_private_inventory(SUITE, IDS)


def test_tree_deterministic_and_isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "ambient author")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ambient@example.com")
    for task_id in ("s01-routine", "s05-review"):
        first = build_repo("tree:" + task_id, tmp_path / (task_id + "-a"))
        second = build_repo("tree:" + task_id, tmp_path / (task_id + "-b"))
        assert git(first, "show-ref") == git(second, "show-ref")
        identity = git(first, "show", "-s", "--format=%an|%ae|%cn|%ce|%aI|%cI")
        assert identity.replace("+00:00", "Z") == (
            "hc-bench|hc-bench@example.invalid|hc-bench|hc-bench@example.invalid|"
            "2026-09-24T00:00:00Z|2026-09-24T00:00:00Z")
        assert git(first, "status", "--porcelain") == ""
        with pytest.raises(FileExistsError):
            build_repo("tree:" + task_id, first)
    for name in ("tree:../escape", "tree:/absolute", "tree:", "tree:missing", "unknown"):
        with pytest.raises(ValueError, match="repo.builder"):
            build_repo(name, tmp_path / "invalid")


def test_refactor_is_substantial():
    monolith = SUITE / "s03-refactor/repo/archivekit.py"
    assert len(monolith.read_text().splitlines()) >= 300
    spec = load_suite(monolith.parents[1])[0]
    assert set(spec.protected_paths) == {"tests/test_public.py", "tests/run.py"}
    # Every original public function remains in the reference package.
    original = {n.name for n in ast.parse(monolith.read_text()).body
                if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")}
    reference = set()
    for path in (REFERENCES / spec.id / "archivekit").glob("*.py"):
        reference.update(n.name for n in ast.parse(path.read_text()).body
                         if isinstance(n, ast.FunctionDef) and not n.name.startswith("_"))
    assert reference == original


def test_refactor_accepts_private_helpers_and_long_compatibility_shim(tmp_path):
    spec = load_suite(SUITE / "s03-refactor")[0]
    repo = build_repo(spec.repo["builder"], tmp_path / "refactor")
    overlay(REFERENCES / spec.id, repo)
    for name in ("paths", "catalog", "codec"):
        with (repo / f"archivekit/{name}.py").open("a") as handle:
            handle.write("\ndef _private_helper():\n    return True\n")
    with (repo / "archivekit.py").open("a") as handle:
        handle.write("# Compatibility documentation.\n" * 25)
    fixed = commit_reference(repo)
    checkout = tmp_path / "checkout"
    export_commit(repo, fixed, checkout)
    overlay(spec.hidden_dir, checkout)
    assert_pass(command(checkout, spec.hidden_cmd))


@pytest.mark.parametrize("task_id", ["s05-review", "s06-readonly"])
def test_readonly_reference_full_text_grades_solved(task_id, tmp_path, monkeypatch):
    spec = load_suite(SUITE / task_id)[0]
    repo = build_repo(spec.repo["builder"], tmp_path / "readonly")
    base = git(repo, "rev-parse", "HEAD")
    identifiers = (REFERENCES / task_id / "answer.txt").read_text().splitlines()
    answer = "The review is complete.\n" + spec.answer_prefix + " " + ", ".join(identifiers)

    def offline_check(text, checkout, *, codex_path):
        result = command(checkout, text)
        return VerifyOutcome(text, result.returncode == 0, result.returncode,
                             result.stdout + result.stderr, None)

    monkeypatch.setattr(grader.verify, "find_sandbox", lambda: "offline")
    monkeypatch.setattr(grader.verify, "run_verify", offline_check)
    result = grader.grade(spec, repo, base, base_commit=base, answer=answer)
    assert result.solved and result.answer_match is True
    assert result.hidden_exit == 0 and result.diff_files == 0


def test_split_has_independent_witnesses(tmp_path):
    spec = load_suite(SUITE / "s04-split")[0]
    mapping = {"test_export.py": "telemetry/export.py", "test_duration.py": "telemetry/duration.py",
               "test_series.py": "telemetry/series.py"}
    assert {p.name for p in (spec.hidden_dir / "tests").iterdir()} == set(mapping) | {"run.py"}
    for test_name, implementation in mapping.items():
        repo = build_repo(spec.repo["builder"], tmp_path / test_name)
        shutil.copy2(spec.hidden_dir / "tests/run.py", repo / "tests/run.py")
        shutil.copy2(spec.hidden_dir / "tests" / test_name, repo / "tests" / test_name)
        assert command(repo, spec.hidden_cmd).returncode != 0
        shutil.copy2(REFERENCES / spec.id / implementation, repo / implementation)
        assert_pass(command(repo, spec.hidden_cmd))


def test_review_findings_and_decoy(tmp_path):
    spec = load_suite(SUITE / "s05-review")[0]
    repo = build_repo(spec.repo["builder"], tmp_path / "review")
    assert git(repo, "rev-parse", "feature/x^") == git(repo, "rev-parse", "main")
    assert git(repo, "diff", "--name-only", "main", "feature/x") == "seats/booking.py"
    base = {}
    feature = {}
    exec(git(repo, "show", "main:seats/booking.py"), base)
    exec(git(repo, "show", "feature/x:seats/booking.py"), feature)
    witnesses = {"reserve": (10, 3, 7), "release": (7, 2), "quote_fee": (100, True)}
    actual = set()
    for name, args in witnesses.items():
        expected = base[name](*args)
        try:
            result = feature[name](*args)
        except ValueError:
            actual.add(name)
        else:
            if result != expected:
                actual.add(name)
    assert actual == set(spec.expected_answer)
    assert "availability" not in spec.expected_answer
    for capacity in (0, 1, 10, 2**64):
        for used in (0, 1, 10, 11, 2**64 + 1):
            assert feature["availability"](capacity, used) == base["availability"](capacity, used)


def test_readonly_answer_is_exact_direct_call_set():
    spec = load_suite(SUITE / "s06-readonly")[0]
    root = SUITE / spec.id / "repo"
    production = ast.parse((root / "textindex/formatting.py").read_text())
    defined = {n.name for n in production.body if isinstance(n, ast.FunctionDef)}
    tests = ast.parse((root / "tests/test_public.py").read_text())
    called = {n.func.id for fn in tests.body if isinstance(fn, ast.FunctionDef)
              and fn.name.startswith("test_") for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert set(spec.expected_answer) == defined - called


def test_private_runner_replaces_worker_runner(tmp_path):
    spec = load_suite(SUITE / "s01-routine")[0]
    repo = build_repo(spec.repo["builder"], tmp_path / "runner")
    (repo / "tests/run.py").write_text('print("always green")\n')
    overlay(spec.hidden_dir, repo)
    result = command(repo, spec.hidden_cmd)
    assert result.returncode != 0
    assert "ImportError" in result.stderr
