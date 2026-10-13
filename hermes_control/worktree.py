"""One persistent git worktree per task, plus a disposable verify checkout.

Reusing ``tools.subagent_worktree`` buys the gitignore handling, the unborn-HEAD
degradation and the naming convention for free. The cost is a doubled prefix in
the branch name (``hermes-subagent/subagent-control-<run_id>-<task_id>``); tests
pin it so consumers merge the right ref.

Task worktrees survive the run for review. After merging into local ``main``,
``hermes control prune`` can remove clean, idle task worktrees and branches.
The detached checkout used for post-commit verification is removed after the check.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None

from hermes_cli._subprocess_compat import harden_git_argv, noninteractive_repo_git_env

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 60

# The logger ``tools.subagent_worktree`` warns on. It returns a bare ``None`` for
# every failure and puts git's own message only in that warning, so capturing it
# is the only way the reason can reach the run summary.
_UPSTREAM_LOGGER = "tools.subagent_worktree"

NOT_A_GIT_REPO = "no worktree (not a git repo)"


# Task worktree creation, controller commits and verify checkout maintenance
# mutate shared git metadata. Never hold these locks while an engine/check runs.
_repository_locks: dict[Path, threading.RLock] = {}
_repository_locks_guard = threading.Lock()


def _repository_lock(repo: str | Path | None):
    key = Path(repo or os.getcwd()).resolve()
    try:
        common = _run_git(["rev-parse", "--git-common-dir"], cwd=str(key))
        if common.returncode == 0 and common.stdout.strip():
            key = (key / common.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        pass  # Git unavailable: still serialize operations on this resolved path.
    with _repository_locks_guard:
        return _repository_locks.setdefault(key, threading.RLock())


@contextmanager
def _worktree_file_lock(repo: str | Path | None):
    """Serialize task worktree creation and prune across Hermes processes."""
    if fcntl is None:
        yield
        return
    try:
        root = Path(repo or os.getcwd()).resolve()
        common = _run_git(["rev-parse", "--git-common-dir"], cwd=str(root))
        if not common.returncode:
            lock_path = (root / common.stdout.strip()).resolve() / "hermes-control-worktrees.lock"
            handle = lock_path.open("a+")
    except (OSError, subprocess.SubprocessError):
        logger.warning("hermes-control: could not open worktree lock; continuing unlocked",
                       exc_info=True)
        yield
        return
    if common.returncode:
        yield  # Outside a repository, task creation reports NOT_A_GIT_REPO.
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _run_git(args, cwd: str, timeout: int = _GIT_TIMEOUT):
    """Run git capturing output; never raises on non-zero exit.

    Hardened against malicious .git/config (GHSA-7x36-8jrh-v4pw): this runs unattended
    against a worktree the worker controls, so .gitconfig must not execute.
    """
    env = noninteractive_repo_git_env(cwd)
    if env is None:
        raise OSError("refused git operation: could not harden repository filter configuration")
    return subprocess.run(["git", *harden_git_argv(args)], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=timeout,
                          stdin=subprocess.DEVNULL, env=env)


# Commits the controller makes for a worker. A fixed identity: the hardened git
# environment hides the operator's global user.name on purpose, and work an engine
# wrote should not be attributed to the person who ran Hermes. Signing is switched
# off because an unattended commit cannot answer a gpg passphrase prompt.
# What counts as the worker's work: everything in the worktree except Python
# bytecode. A worker that runs the tests itself leaves __pycache__ behind, and a
# repo that does not ignore it would otherwise get .pyc files on the task branch
# (found by the first real run). pytest/mypy/ruff caches ignore themselves.
# The diff and the commit share this, so diff_stat describes what is committed.
_WORK_PATHSPEC = (".", ":(exclude,glob)**/__pycache__/**", ":(exclude,glob)**/*.py[cod]")

_COMMIT_CONFIG = ("-c", "user.name=Hermes-Control", "-c", "user.email=hermes-control@localhost",
                  "-c", "commit.gpgsign=false")


@dataclass(frozen=True)
class TaskWorktree:
    path: Path
    branch: str
    repo_root: Path
    base_commit: str


class VerifyCheckoutError(RuntimeError):
    """The committed tree could not be checked out for verification."""


def _remove_verify_checkout(worktree: TaskWorktree, path: Path) -> None:
    """Best-effort cleanup; a cleanup error must not erase a verify result."""
    try:
        removed = _run_git(["worktree", "remove", "--force", str(path)],
                           cwd=str(worktree.repo_root))
        if removed.returncode == 0:
            return
        logger.warning("hermes-control: verify checkout removal failed: %s",
                       removed.stderr.strip())
    except Exception as exc:  # noqa: BLE001 - removal must never hide a verdict
        logger.warning("hermes-control: verify checkout removal failed: %s", exc)
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        _run_git(["worktree", "prune"], cwd=str(worktree.repo_root))
    except Exception:  # noqa: BLE001 - cleanup cannot replace the verification verdict
        logger.warning("hermes-control: verify checkout cleanup failed for %s", path,
                       exc_info=True)


def _sweep_stale_verify_checkouts(worktree: TaskWorktree) -> None:
    """Remove abandoned checkouts after their maximum verify lifetime has passed."""
    from .verify import VERIFY_TIMEOUT_SECONDS

    pattern = re.compile(re.escape(worktree.path.name) + r"\.verify-[0-9a-f]{32}")
    cutoff = time.time() - VERIFY_TIMEOUT_SECONDS - 300
    try:
        siblings = list(worktree.path.parent.iterdir())
    except OSError:
        return
    for path in siblings:
        try:
            if (pattern.fullmatch(path.name) and path.is_dir() and not path.is_symlink()
                    and path.stat().st_mtime < cutoff):
                _remove_verify_checkout(worktree, path)
        except OSError:  # noqa: PERF203 - a stale checkout must not stop verification
            logger.warning("hermes-control: could not inspect verify checkout %s", path,
                           exc_info=True)


@contextmanager
def verify_checkout(worktree: TaskWorktree, commit: str):
    """Yield a detached checkout containing only *commit*, then remove it."""
    with _repository_lock(worktree.repo_root):
        _sweep_stale_verify_checkouts(worktree)
        path = worktree.path.with_name(f"{worktree.path.name}.verify-{uuid.uuid4().hex}")
        # _run_git disables hooks and named filter drivers, including those selected
        # by a committed .gitattributes. Git
        # >= 2.30.2 is required for the symlink checkout fix (CVE-2021-21300).
        try:
            added = _run_git(["worktree", "add", "--detach", str(path), commit],
                             cwd=str(worktree.repo_root))
        except (OSError, subprocess.SubprocessError) as exc:
            _remove_verify_checkout(worktree, path)
            raise VerifyCheckoutError(str(exc) or type(exc).__name__) from exc
        if added.returncode != 0:
            _remove_verify_checkout(worktree, path)
            raise VerifyCheckoutError(added.stderr.strip() or
                                      f"git worktree add exited {added.returncode}")
    try:
        yield path
    finally:
        with _repository_lock(worktree.repo_root):
            _remove_verify_checkout(worktree, path)


@contextmanager
def red_checkout(worktree: TaskWorktree, base_commit: str, commit: str, paths):
    """Base code with the task's tests overlaid, using verify's isolation and cleanup."""
    paths = tuple(paths)
    if any(not path or Path(path).is_absolute() or ".." in Path(path).parts for path in paths):
        raise VerifyCheckoutError("red checkout paths must be relative and contain no '..'")
    with verify_checkout(worktree, base_commit) as checkout:
        if paths:
            with _repository_lock(worktree.repo_root):
                try:
                    restored = _run_git(["checkout", commit, "--",
                                         *(f":(literal){path}" for path in paths)],
                                        cwd=str(checkout))
                except (OSError, subprocess.SubprocessError) as exc:
                    raise VerifyCheckoutError(str(exc) or type(exc).__name__) from exc
                if restored.returncode:
                    raise VerifyCheckoutError(restored.stderr.strip() or
                                              f"git checkout exited {restored.returncode}")
        yield checkout


def uncommitted_worktree_paths(worktree: TaskWorktree, limit: int = 5) -> list[str]:
    """Ignored or untracked task files absent from a committed verify checkout."""
    try:
        status = _run_git(["status", "--porcelain=v1", "-z", "--ignored",
                           "--untracked-files=all"], cwd=str(worktree.path))
        if status.returncode != 0:
            return []
        return [record[3:] for record in status.stdout.split("\0")
                if record[:2] in ("??", "!!")][:limit]
    except Exception:  # noqa: BLE001 - diagnosis must never hide the verify verdict
        return []


def worktree_id(run_id: str, task_id: str) -> str:
    """Worktree/branch id for one task of one run.

    Scoped to the run on purpose. Task ids are short and stable by design
    (``task-1``, ``task-2``, and the tool schema asks the planner for stable
    names), so a run-independent id makes the *second* dispatch against the same
    repo collide with the first run's branch - ``git worktree add -b`` refuses,
    and the task silently gets no worktree at all.
    """
    return f"control-{run_id}-{task_id}" if run_id else f"control-{task_id}"


class _WarningCapture(logging.Handler):
    """Collects ``tools.subagent_worktree``'s own warnings for the duration of one call."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # noqa: BLE001 - a logging handler must never raise into the caller
            pass


def create_task_worktree_with_reason(
        parent_cwd: str | None, task_id: str, *,
        run_id: str) -> tuple[TaskWorktree | None, str]:
    """Isolated worktree for *task_id* in run *run_id*, plus why it failed.

    The reason is load-bearing: "not a git repo" and "the repo refused the
    worktree" are different facts, and reporting the second as the first states
    something false about a repo Hermes is standing in.
    """
    from tools.subagent_worktree import create_subagent_worktree, resolve_repo_root

    with _repository_lock(resolve_repo_root(parent_cwd) or parent_cwd), \
            _worktree_file_lock(resolve_repo_root(parent_cwd) or parent_cwd):
        capture = _WarningCapture()
        upstream = logging.getLogger(_UPSTREAM_LOGGER)
        upstream.addHandler(capture)
        try:
            info = create_subagent_worktree(parent_cwd,
                                            subagent_id=worktree_id(run_id, task_id))
        finally:
            upstream.removeHandler(capture)
        if info:
            return TaskWorktree(path=Path(info["path"]), branch=info["branch"],
                                repo_root=Path(info["repo_root"]),
                                base_commit=info.get("base_commit", "")), ""
        if not resolve_repo_root(parent_cwd):
            return None, NOT_A_GIT_REPO
        detail = "; ".join(capture.messages) or "git worktree add failed"
        return None, f"no worktree (worktree creation failed: {detail})"


def create_task_worktree(parent_cwd: str | None, task_id: str, *,
                         run_id: str) -> TaskWorktree | None:
    """Isolated worktree for *task_id*, or None outside a git repo / on failure.

    ``run_id`` is keyword-only with no default on purpose: a caller that leaves
    it out is exactly the cross-run collision this scoping exists to prevent.
    """
    return create_task_worktree_with_reason(parent_cwd, task_id, run_id=run_id)[0]


def _capture_failed(worktree: TaskWorktree, detail: str) -> str:
    """The wording ``tools.subagent_worktree`` uses for the same situation: an
    unmeasured worktree is not an empty one, and the human has to be told where
    to look before concluding the worker did nothing."""
    return (f"diff capture failed ({detail}) - inspect {worktree.path} "
            f"(branch {worktree.branch}) before assuming no work")


def capture_worktree_diff(worktree: TaskWorktree) -> tuple[str, str | None]:
    """``(diff, failure reason)``. The second value is the whole point: returning
    ``""`` for both "the worker changed nothing" and "git would not tell us" is
    what turns a measurement failure into an affirmative claim downstream."""
    try:
        staged = _run_git(["add", "-AN", "--", *_WORK_PATHSPEC], cwd=str(worktree.path))
        proc = _run_git(["diff", "--no-color", "--", *_WORK_PATHSPEC], cwd=str(worktree.path))
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("hermes-control: diff failed for %s", worktree.path, exc_info=True)
        return "", _capture_failed(worktree, str(exc) or type(exc).__name__)
    if staged.returncode != 0:
        # `add -AN` is what makes untracked files visible to `git diff`; if it
        # failed, a clean diff below would be measuring the wrong thing.
        logger.warning("hermes-control: git add -AN failed for %s", worktree.path)
        return "", _capture_failed(
            worktree, f"git add -AN exited {staged.returncode}: "
                      f"{(staged.stderr or '').strip()}")
    if proc.returncode != 0:
        logger.warning("hermes-control: git diff failed for %s", worktree.path)
        return "", _capture_failed(
            worktree, f"git diff exited {proc.returncode}: {(proc.stderr or '').strip()}")
    return proc.stdout, None


def worktree_diff(worktree: TaskWorktree) -> str:
    """Everything the worker changed, tracked or not. Empty string on any failure -
    callers that must tell "nothing changed" from "could not measure" apart use
    :func:`capture_worktree_diff` instead."""
    return capture_worktree_diff(worktree)[0]


def changed_claude_paths(worktree: TaskWorktree) -> tuple[list[str], str | None]:
    """Changes under .claude, including untracked files and changes since the base."""
    return _changed_settings_paths(worktree, ".claude")


def changed_codex_paths(worktree: TaskWorktree) -> tuple[list[str], str | None]:
    """Changes under .codex, with the same checks as the Claude continuation guard."""
    return _changed_settings_paths(worktree, ".codex")


def _changed_settings_paths(worktree: TaskWorktree, directory: str
                             ) -> tuple[list[str], str | None]:
    try:
        paths = {entry.name for entry in worktree.path.iterdir()
                 if entry.name.casefold() == directory and entry.name != directory}
        settings = worktree.path / directory
        if settings.is_symlink():
            paths.add(f"{directory} is a symlink")
        elif settings.is_dir():
            def raise_walk_error(error: OSError) -> None:
                raise error

            for root, dirs, files in os.walk(settings, followlinks=False,
                                              onerror=raise_walk_error):
                for name in dirs + files:
                    path = Path(root) / name
                    if path.is_symlink():
                        paths.add(f"{path.relative_to(worktree.path)} is a symlink")
        status = _run_git(["status", "--porcelain=v1", "-z", "--untracked-files=all",
                           "--ignored=matching",
                           "--", f":(icase){directory}"], cwd=str(worktree.path))
        base = _run_git(["diff", "--name-only", "-z", worktree.base_commit,
                         "--", f":(icase){directory}"], cwd=str(worktree.path))
    except (OSError, subprocess.SubprocessError) as exc:
        return [], str(exc)
    for proc in (status, base):
        if proc.returncode != 0:
            return [], proc.stderr.strip() or f"git exited {proc.returncode}"
    paths.update(entry[3:] for entry in status.stdout.split("\0") if entry)
    paths.update(path for path in base.stdout.split("\0") if path)
    return sorted(paths), None


def base_settings_paths(worktree: TaskWorktree, engine: str, *, restricted: bool = False
                         ) -> tuple[list[str], str | None]:
    """Inspect base settings; restricted Claude still refuses settings symlinks."""
    directory = f".{engine}"
    try:
        # Preserve modes: a settings directory itself may be a symlink to a
        # tracked configuration directory, which ls-tree does not recurse into.
        proc = _run_git(["ls-tree", "-r", "-z", "--full-tree", worktree.base_commit],
                        cwd=str(worktree.path))
    except (OSError, subprocess.SubprocessError) as exc:
        return [], str(exc)
    if proc.returncode:
        return [], proc.stderr.strip() or f"git exited {proc.returncode}"
    paths = []
    for entry in proc.stdout.split("\0"):
        if not entry:
            continue
        metadata, separator, path = entry.partition("\t")
        fields = metadata.split()
        if not separator or len(fields) != 3:
            return [], "could not parse base tree entry"
        components = [component.casefold() for component in path.split("/")]
        if fields[0] == "120000" and components[0] in {".codex", ".claude"}:
            paths.append(path)
            continue
        if components[0] != directory:
            continue
        if ((len(components) > 1 and engine == "codex") or
                (not restricted and engine == "claude" and
                 fnmatch.fnmatchcase("/".join(components), ".claude/settings*.json"))):
            paths.append(path)
    return sorted(paths), None


def committed_claude_paths(worktree: TaskWorktree, commit: str) -> list[str]:
    """Paths under .claude in the task's committed diff from its base."""
    try:
        proc = _run_git(["diff", "--name-only", "-z", worktree.base_commit, commit,
                         "--", ":(icase).claude"], cwd=str(worktree.path))
    except (OSError, subprocess.SubprocessError):
        return [".claude/ (could not inspect committed diff)"]
    if proc.returncode != 0:
        return [".claude/ (could not inspect committed diff)"]
    return [path for path in proc.stdout.split("\0") if path]


def commit_worktree(worktree: TaskWorktree, *, message: str) -> tuple[str | None, str | None]:
    """Commit everything the worker changed onto the task branch.

    Returns ``(sha, None)`` on success, ``(None, None)`` when there is nothing to
    commit, and ``(None, reason)`` when git fails. Workers are told never to commit,
    so without this step merging a task branch brings back nothing at all.
    """
    with _repository_lock(worktree.repo_root):
        cwd = str(worktree.path)
        try:
            added = _run_git(["add", "-A", "--", *_WORK_PATHSPEC], cwd=cwd)
            if added.returncode != 0:
                return None, f"git add failed: {added.stderr.strip()[:300]}"
            if _run_git(["diff", "--cached", "--quiet"], cwd=cwd).returncode == 0:
                return None, None
            committed = _run_git([*_COMMIT_CONFIG, "commit", "-q", "-m", message], cwd=cwd)
            if committed.returncode != 0:
                return None, f"git commit failed: {committed.stderr.strip()[:300]}"
            head = _run_git(["rev-parse", "HEAD"], cwd=cwd)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("hermes-control: commit failed in %s", worktree.path, exc_info=True)
            return None, f"commit failed: {exc}"
        return head.stdout.strip() or None, None
