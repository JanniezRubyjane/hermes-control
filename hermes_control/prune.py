"""Inspect and safely remove merged Hermes task worktrees and branches."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows has no flock.
    fcntl = None

from rich.console import Console
from rich.table import Table

from . import dispatch, pauses, runs, worktree
from .runlog import run_log_dir

_BRANCH_PREFIX = "hermes-subagent/subagent-control-"
_REF_PREFIX = "refs/heads/" + _BRANCH_PREFIX
_WORKTREE_LOCK_TIMEOUT = 60


def _git_dir(path: Path) -> Path:
    marker = path / ".git"
    if marker.is_dir():
        return marker.resolve()
    if marker.is_file():
        line = marker.read_text(encoding="utf-8").strip()
        if line.startswith("gitdir: "):
            return (path / line[len("gitdir: "):]).resolve()
    raise RuntimeError(f"{path} is not a git worktree")


def _main_worktree(path: Path) -> Path:
    """Locate the first checkout in Git's worktree listing."""
    for directory in (path, *path.parents):
        if (directory / ".git").exists():
            listing = _require_git(directory, "worktree", "list", "--porcelain")
            first = next((line[len("worktree "):] for line in listing.splitlines()
                          if line.startswith("worktree ")), None)
            if first:
                return Path(first).resolve()
            raise RuntimeError(f"{directory} has no Git worktree")
    raise RuntimeError(f"{path} is not a git worktree")


def _git(repo: Path, *args: str):
    return worktree._run_git(list(args), cwd=str(repo))


def _require_git(repo: Path, *args: str) -> str:
    result = _git(repo, *args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _worktrees(repo: Path) -> tuple[dict[str, tuple[Path, bool]], str | None]:
    """Read porcelain records; paths are verbatim, including their spaces."""
    listing = _require_git(repo, "worktree", "list", "--porcelain")
    found: dict[str, tuple[Path, bool]] = {}
    main_path: Path | None = None
    main_branch: str | None = None
    path: Path | None = None
    branch: str | None = None
    prunable = False
    for line in (*listing.splitlines(), ""):
        if not line:
            if path == main_path:
                main_branch = branch
            if path is not None and branch and branch.startswith(_BRANCH_PREFIX) and path != main_path:
                found[branch] = (path, prunable)
            path, branch, prunable = None, None, False
        elif line.startswith("worktree "):
            path = Path(line[len("worktree "):])
            if main_path is None:
                main_path = path
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None
        elif line == "prunable" or line.startswith("prunable "):
            prunable = True
    return found, main_branch


def _size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for directory, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if name != ".git"]
        for name in files:
            try:
                total += (Path(directory) / name).lstat().st_size
            except OSError:
                pass
    return total


def _state(repo: Path, path: Path | None, *, prunable: bool = False) -> str:
    if path is None:
        return "clean"
    if prunable or not path.is_dir():
        return "missing"
    git_dir = _git_dir(path)
    options = (f"--git-dir={git_dir}", f"--work-tree={path}")
    tracked = _require_git(repo, *options, "diff", "HEAD", "--name-only", "-z", "--")
    if tracked:
        return f"wip:{len([name for name in tracked.split(chr(0)) if name])}"
    untracked = _require_git(repo, *options, "ls-files", "--others", "--exclude-standard", "-z")
    scratch = [name for name in untracked.split("\0") if name]
    return f"scratch:{len(scratch)}" if scratch else "clean"


def _run_ids() -> list[str]:
    """Only existing run records can identify the run portion of a task branch."""
    return sorted((path.stem[len("run-"):] for path in run_log_dir().glob("run-*.jsonl")),
                  key=len, reverse=True)


def _hold(branch: str, path: Path | None, state: str, merged: bool,
          paused_paths: set[Path], paused_branches: set[str], current_paths: set[Path],
          run_ids: list[str], live_cache: dict[str, bool]) -> str | None:
    if path is not None and path.resolve() in current_paths:
        return "current"
    if branch in paused_branches or (path is not None and path.resolve() in paused_paths):
        return "paused"
    tail = branch[len(_BRANCH_PREFIX):]
    for run_id in run_ids:
        if tail.startswith(run_id + "-"):
            if run_id not in live_cache:
                view = runs.run_view(run_id)
                live_cache[run_id] = bool((view and view.get("state") == "running") or
                                          dispatch._run_is_live(run_id) is True)
            if live_cache[run_id]:
                return "live"
            break
    if state.startswith("wip:"):
        return "wip"
    if state.startswith("scratch:"):
        return "scratch"
    if not merged:
        return "not merged"
    return None


def _entries(repo: Path, current_paths: set[Path],
             sizes: dict[Path, int] | None = None) -> list[dict]:
    paths, main_branch = _worktrees(repo)
    refs = _require_git(repo, "for-each-ref", "--format=%(refname:short)",
                        _REF_PREFIX + "*").splitlines()
    paused_records = pauses.all_paused()
    paused_paths = {path.resolve() for record in paused_records
                    if (path := record.worktree_path()) is not None}
    paused_branches = {"hermes-subagent/subagent-" +
                       worktree.worktree_id(record.run_id, record.task["id"])
                       for record in paused_records}
    run_ids = _run_ids()
    live_cache: dict[str, bool] = {}
    rows = []
    for branch in sorted(set(refs) | set(paths)):
        if not branch.startswith(_BRANCH_PREFIX) or branch == main_branch:
            continue
        path, prunable = paths.get(branch, (None, False))
        tip = _require_git(repo, "show", "-s", "--format=%ct", branch).strip()
        merged = _git(repo, "merge-base", "--is-ancestor", branch,
                      "refs/heads/main").returncode == 0
        state = _state(repo, path, prunable=prunable)
        rows.append({"branch": branch, "path": str(path) if path else None,
                     "size": (sizes.get(path, 0) if sizes is not None else _size(path))
                     if path else 0,
                     "age_days": max(0, int((time.time() - int(tip)) // 86400)),
                     "merged": merged, "state": state,
                     "hold": _hold(branch, path, state, merged, paused_paths,
                                   paused_branches, current_paths,
                                   run_ids, live_cache)})
    return rows


def _apply(repo: Path, rows: list[dict]) -> str | None:
    result = _git(repo, "worktree", "prune")
    if result.returncode:
        message = result.stderr.strip() or "git worktree prune failed"
        for row in rows:
            row["action"] = "kept"
            row["message"] = message
        return message
    for row in rows:
        row["action"] = "kept"
        if row["hold"]:
            continue
        path = row["path"]
        if path and row["state"] != "missing":
            removed = _git(repo, "worktree", "remove", path)
            if removed.returncode:
                row["message"] = removed.stderr.strip() or "git worktree remove failed"
                continue
        deleted = _git(repo, "branch", "-d", row["branch"])
        if deleted.returncode:
            row["message"] = deleted.stderr.strip() or "git branch -d failed"
            continue
        row["action"] = "removed"
    return None


def _print_table(rows: list[dict], apply: bool) -> None:
    table = Table(show_edge=False, pad_edge=False)
    columns = ("branch", "path", "size", "age days", "merged", "state", "hold")
    if apply:
        columns += ("action", "message")
    for column in columns:
        table.add_column(column)
    for row in rows:
        values = [row["branch"], row["path"] or "-", str(row["size"]),
                  str(row["age_days"]), "yes" if row["merged"] else "no",
                  row["state"], row["hold"] or "-"]
        if apply:
            values += [row["action"], row.get("message", "")]
        table.add_row(*values)
    Console().print(table)


@contextmanager
def _prune_worktree_lock(repo: Path):
    """Wait briefly for task creation, then leave prune retryable."""
    if fcntl is None:
        yield
        return
    common = _require_git(repo, "rev-parse", "--git-common-dir").strip()
    lock_path = (repo / common).resolve() / "hermes-control-worktrees.lock"
    with lock_path.open("a+") as handle:
        deadline = time.monotonic() + _WORKTREE_LOCK_TIMEOUT
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("another Hermes process is creating a worktree; try again")
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def prune(repo: str | None = None, *, apply: bool = False, as_json: bool = False) -> int:
    """Report task refs, or remove only merged, clean, idle and unpaused ones."""
    try:
        invocation = Path(os.getcwd()).resolve()
        target = Path(repo).resolve() if repo else invocation
        root = _main_worktree(target)
        _require_git(root, "rev-parse", "--show-toplevel")
        current_paths = {invocation, *invocation.parents, target, *target.parents}
        if apply:
            paths, _ = _worktrees(root)
            sizes = {path: _size(path) for path, _ in paths.values()}
            with worktree._repository_lock(root), _prune_worktree_lock(root):
                rows = _entries(root, current_paths, sizes)
                prune_error = _apply(root, rows)
        else:
            rows = _entries(root, current_paths)
            prune_error = None
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"prune: {exc}", file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
    else:
        _print_table(rows, apply)
    if prune_error:
        print(f"prune: {prune_error}", file=sys.stderr)
    return 1 if prune_error else 0
