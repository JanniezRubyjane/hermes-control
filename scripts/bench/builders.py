"""Small registry of deterministic synthetic repositories."""

import os
from pathlib import Path
import re
import shutil
import subprocess

from eval.build_project import build

BUILDERS = {"shop": build}


def build_tree(name: str, destination: Path) -> Path:
    """Copy an S seed; optional sibling branch.patch creates feature/x on main."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", name):
        raise ValueError(f"repo.builder: invalid tree name {name!r}")
    task = Path(__file__).resolve().parent / "suites" / "S" / name
    source = task / "repo"
    if not source.is_dir():
        raise ValueError(f"repo.builder: unknown tree {name!r}")
    destination = destination.expanduser().resolve()
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"destination is not empty: {destination}")
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="hc-bench", GIT_COMMITTER_NAME="hc-bench",
               GIT_AUTHOR_EMAIL="hc-bench@example.invalid",
               GIT_COMMITTER_EMAIL="hc-bench@example.invalid",
               GIT_AUTHOR_DATE="2026-09-24T00:00:00+00:00",
               GIT_COMMITTER_DATE="2026-09-24T00:00:00+00:00",
               GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c",
                        "commit.gpgsign=false", *args], cwd=destination, env=env,
                       check=True, capture_output=True, text=True)

    git("init", "-b", "main")
    git("config", "user.name", "hc-bench")
    git("config", "user.email", "hc-bench@example.invalid")
    git("add", ".")
    git("commit", "-m", f"Initial {name} fixture")
    patch = task / "branch.patch"
    if patch.is_file():
        git("switch", "-c", "feature/x")
        git("apply", str(patch))
        git("add", ".")
        git("commit", "-m", "Implement reservation updates")
        git("switch", "main")
    return destination


def build_repo(name: str, destination: Path) -> Path:
    if name.startswith("tree:"):
        return build_tree(name.removeprefix("tree:"), destination)
    if name not in BUILDERS:
        raise ValueError(f"repo.builder: unknown builder {name!r}")
    return BUILDERS[name](destination)
