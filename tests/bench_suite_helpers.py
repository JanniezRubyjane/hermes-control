"""Shared committed-checkout witnesses for both synthetic suite test files."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess

S2_IDS = ["s08-trap-secret", "s09-trap-network", "s11-ambiguous", "s12-frontend", "s13-large-repo"]

def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def overlay(source, destination):
    shutil.copytree(source, destination, dirs_exist_ok=True)


def command(repo, text):
    # Avoid bytecode surviving an overlay of same-sized source files.
    return subprocess.run(shlex.split(text), cwd=repo, capture_output=True, text=True,
                          env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=20)


def assert_pass(result):
    assert result.returncode == 0, result.stdout + result.stderr


def commit_reference(repo):
    # Commit only the solution, never the private overlay.
    git(repo, "add", ".")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-m", "Reference solution")
    return git(repo, "rev-parse", "HEAD")


def export_commit(repo, commit, destination):
    # git archive reads the selected commit, independent of uncommitted files.
    import io
    import tarfile
    data = subprocess.check_output(["git", "-C", str(repo), "archive", commit])
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter="data")


def assert_private_inventory(suite, original_ids):
    ids = original_ids + S2_IDS
    assert sorted(p.name for p in suite.iterdir() if p.is_dir()) == ids
    canaries = []
    from bench.spec import load_suite
    for task_id in ids:
        spec = load_suite(suite / task_id)[0]
        files = [p for p in spec.hidden_dir.rglob("*") if p.is_file()]
        assert files
        for file in files:
            tokens = re.findall(r"^(?:// )?# hc-bench-canary: ([0-9a-f]{16})$", file.read_text(), re.M)
            assert len(tokens) == 1, file
            canaries.extend(tokens)
        assert not list((suite / task_id / "repo").rglob("*.patch"))
        assert not list((suite / task_id).rglob("answer.txt"))
        for path in (suite / task_id / "repo").rglob("*"):
            if path.is_file():
                assert "hc-bench-canary:" not in path.read_text()
                assert "# BUG:" not in path.read_text()
        assert "reference" not in spec.goal.casefold()
    assert len(canaries) == len(set(canaries))
