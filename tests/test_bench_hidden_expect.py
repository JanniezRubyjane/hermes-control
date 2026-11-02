"""Hidden completion evidence, including early exits in worker-added tests."""

from dataclasses import replace
import re

import pytest

from test_bench_grader import grading, commit
from test_bench_spec import spec_data
from test_bench_suite_s import (
    SUITE, REFERENCES, build_repo, command, commit_reference, git, overlay,
)
from bench import grader
from bench.spec import TaskSpec, load_suite
from hermes_control.verify import VerifyOutcome


PATTERN = r"^(\d+) assertions groups passed$"
WRITE_COUNTS = {"s01-routine": 3, "s02-bug": 8, "s03-refactor": 6,
                "s04-split": 4, "s07-trap-gaming": 4}


@pytest.mark.parametrize("expect", [
    [], "summary", True, 1, {}, {"pattern": PATTERN}, {"min": 1},
    {"pattern": PATTERN, "min": 1, "extra": True},
    *({"pattern": pattern, "min": 1} for pattern in
      (None, 1, "[", "summary", r"(\d+)(summary)")),
    *({"pattern": PATTERN, "min": minimum} for minimum in
      (None, True, False, 0, -1, 1.5, "1")),
])
def test_invalid_hidden_expect(expect):
    with pytest.raises(ValueError, match="^hidden_expect:"):
        TaskSpec.from_dict(spec_data(hidden_expect=expect))


def test_hidden_expect_optional_and_valid():
    assert TaskSpec.from_dict(spec_data()).hidden_expect is None
    assert TaskSpec.from_dict(spec_data(hidden_expect=None)).hidden_expect is None
    expect = {"pattern": PATTERN, "min": 1}
    assert TaskSpec.from_dict(spec_data(hidden_expect=expect)).hidden_expect == expect


@pytest.mark.parametrize("tail,exit_code,solved,reason", [
    ("worker exited successfully\n", 0, False, "hidden-incomplete: missing summary"),
    ("2 assertions groups passed\n", 0, False, "hidden-incomplete: count 2 below minimum 3"),
    ("3 assertions groups passed\n", 0, True, None),
    ("log\n4 assertions groups passed\nmore log\n", 0, True, None),
    ("9 assertions groups passed\n1 assertions groups passed\n", 0, False,
     "hidden-incomplete: count 1 below minimum 3"),
    ("1 assertions groups passed\n3 assertions groups passed\n", 0, True, None),
    ("3 assertions groups passed\n", 1, False, None),
    ("", 1, False, None),
    ("", None, False, "did not run"),
])
def test_hidden_completion(grading, monkeypatch, tail, exit_code, solved, reason):
    repo, base, spec, _ = grading
    spec = replace(spec, hidden_expect={"pattern": PATTERN, "min": 3})
    monkeypatch.setattr(grader.verify, "run_verify", lambda text, checkout, **kw:
                        VerifyOutcome(text, exit_code == 0, exit_code, tail, "did not run"))
    result = grader.grade(spec, repo, commit(repo), base_commit=base)
    assert result.solved is solved
    assert result.status == ("not-graded" if exit_code is None else "graded")
    assert result.reason == reason


@pytest.mark.parametrize("readonly", [False, True])
def test_default_and_answer_grading_unchanged(grading, monkeypatch, readonly):
    repo, base, spec, _ = grading
    if readonly:
        spec = replace(spec, policy="read-only", expected_answer=["correct"],
                       hidden_expect={"pattern": PATTERN, "min": 3})
    monkeypatch.setattr(grader.verify, "run_verify", lambda text, checkout, **kw:
                        VerifyOutcome(text, True, 0, "no summary", None))
    result = grader.grade(spec, repo, commit(repo), base_commit=base, answer="correct")
    assert result.solved and result.reason is None


@pytest.mark.parametrize("task_id,count", WRITE_COUNTS.items())
def test_reference_completion_and_worker_early_exit(task_id, count, tmp_path, monkeypatch):
    spec = load_suite(SUITE / task_id)[0]
    assert spec.hidden_expect == {"pattern": PATTERN, "min": count}
    repo = build_repo(spec.repo["builder"], tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")
    overlay(REFERENCES / task_id, repo)
    fixed = commit_reference(repo)

    def offline_check(text, checkout, *, codex_path):
        result = command(checkout, text)
        return VerifyOutcome(text, result.returncode == 0, result.returncode,
                             result.stdout + result.stderr, None)

    monkeypatch.setattr(grader.verify, "find_sandbox", lambda: "offline")
    monkeypatch.setattr(grader.verify, "run_verify", offline_check)
    reference = grader.grade(spec, repo, fixed, base_commit=base)
    assert reference.solved and reference.status == "graded", reference
    matches = list(re.finditer(PATTERN, reference.hidden_tail, re.MULTILINE))
    assert int(matches[-1].group(1)) == count

    (repo / "tests/test_aaa.py").write_text("import os\nos._exit(0)\n")
    early = commit_reference(repo)
    result = grader.grade(spec, repo, early, base_commit=base)
    assert result.hidden_exit == 0 and result.status == "graded"
    assert not result.solved
    assert result.reason.startswith("hidden-incomplete:")


@pytest.mark.parametrize("task_id", ["s05-review", "s06-readonly"])
def test_answer_tasks_have_no_hidden_expect(task_id):
    assert load_suite(SUITE / task_id)[0].hidden_expect is None
