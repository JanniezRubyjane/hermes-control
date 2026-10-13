"""What lets Hermes-Control work on this repo itself: task worktrees stay out of git,
Hermes finds its notes in .hermes.md, and the check a task names runs in the sandbox."""

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def test_task_worktrees_are_ignored_by_git():
    """Otherwise the first dispatch against this repo leaves an untracked
    .worktrees/ (and a rewritten .gitignore) in the checkout the plugin runs from."""
    assert ".worktrees/" in (REPO / ".gitignore").read_text(encoding="utf-8").split()


def test_the_notes_hermes_loads_name_a_check_that_exists():
    if not (REPO / ".hermes.md").exists():
        pytest.skip("the public export leaves out the maintainers' .hermes.md and docs/dev/")
    notes = (REPO / ".hermes.md").read_text(encoding="utf-8")
    command = re.search(r"`(/bin/sh (scripts/[\w./-]+\.sh))`", notes)

    assert command, "the notes must name the verify command"
    assert (REPO / command.group(2)).is_file()
    assert "docs/dev/STATUS.md" in notes and (REPO / "docs" / "dev" / "STATUS.md").is_file()


def test_the_sandbox_check_says_how_to_set_up_when_it_is_not(tmp_path):
    """Run before scripts/verify-setup.sh, it must say what to run, not fail obscurely."""
    proc = subprocess.run(["/bin/sh", str(REPO / "scripts" / "sandbox-check.sh")],
                          capture_output=True, text=True, cwd=REPO,
                          env={**os.environ, "HOME": str(tmp_path)})

    assert proc.returncode != 0
    assert "scripts/verify-setup.sh" in proc.stderr
