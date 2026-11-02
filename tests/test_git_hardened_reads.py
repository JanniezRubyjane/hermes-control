"""Read-only controller Git calls use static hardening without extra probes."""

import subprocess

import pytest

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_git_env
from hermes_control import diagram, diagram_pipeline, health


@pytest.fixture
def git_calls(monkeypatch):
    # noninteractive_git_env() reads safe.directory with real git once per process and
    # caches it; fill that cache before subprocess.run is replaced, so the probe is
    # neither counted here nor cached with fake output for later tests.
    noninteractive_git_env()
    calls = []

    def capture(argv, **kwargs):
        calls.append((argv, kwargs))
        if "cat-file" in argv:
            output = b"first\nsecond\n"
        elif "--is-inside-work-tree" in argv:
            output = "true\n"
        else:
            output = "abc123\n"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", capture)
    return calls


def _assert_call(call, args, *, timeout=10, text=True):
    argv, kwargs = call
    assert argv == ["git", *harden_git_argv(args)]
    expected = {"capture_output": True, "timeout": timeout, "env": noninteractive_git_env()}
    if text:
        expected["text"] = True
    assert kwargs == expected


def test_diagram_blob_read(git_calls):
    calls = git_calls
    assert diagram._file_lines("/repo", "abc123", "app.py") == 2
    assert len(calls) == 1
    _assert_call(calls[0], ["-C", "/repo", "cat-file", "blob", "abc123:app.py"], text=False)


def test_diagram_staleness_read_is_cached(git_calls):
    calls = git_calls
    cache = {}
    record = {"repo": "/repo", "commit": "abc123"}
    assert diagram.is_stale(record, head_cache=cache) is False
    assert diagram.is_stale(record, head_cache=cache) is False
    assert len(calls) == 1
    _assert_call(calls[0], ["-C", "/repo", "rev-parse", "HEAD"])


def test_pipeline_worktree_and_head_reads(git_calls, tmp_path):
    calls = git_calls
    repo = str(tmp_path.resolve())
    assert diagram_pipeline.repo_commit(tmp_path) == (repo, "abc123")
    assert len(calls) == 2
    _assert_call(calls[0], ["-C", repo, "rev-parse", "--is-inside-work-tree"])
    _assert_call(calls[1], ["-C", repo, "rev-parse", "--verify", "HEAD^{commit}"])


@pytest.mark.parametrize("subcommand", ["log", "diff"])
@pytest.mark.parametrize("repo", [None, "/repo"])
def test_health_reads_disable_diff_drivers(git_calls, repo, subcommand):
    calls = git_calls
    assert health._git(repo, subcommand, "HEAD") == "abc123\n"
    assert len(calls) == 1
    globals_ = ["-c", "core.quotePath=false"]
    if repo is not None:
        globals_ += ["-C", repo]
    _assert_call(calls[0], [*globals_, subcommand, "HEAD"], timeout=60)
    assert calls[0][0] == ["git", *globals_, subcommand,
                           "--no-ext-diff", "--no-textconv", "HEAD"]
