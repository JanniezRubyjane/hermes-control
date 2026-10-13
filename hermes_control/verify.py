"""The controller's own check of a finished task: in an OS sandbox, or not at all.

Workers cannot prove their own work. Claude can run a controller-written check
script before finishing, but only the controller's later check counts as proof.
Codex's sandbox rarely has the project's tooling, so the first real run reported
a clean ``unproven_reason`` for work nothing had checked.
The planner now gives a task a ``verify`` command, and after the worker's changes
are committed the controller runs it in the task worktree under
``codex sandbox -P :workspace``: writes confined to the worktree and the temp
dir, no network. Without that sandbox the command does not run - it executes
code an LLM has just written, and "not verified" is an honest answer where
running it bare is not.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tools.ansi_strip import strip_ansi

from .engines.registry import get_adapter
from .policy import assert_no_forbidden_flags
from .proctree import (
    group_popen_kwargs,
    kill_tree,
    terminate_tree,
    wait_bounded,
)
from .runlog import run_log_dir

VERIFY_TIMEOUT_SECONDS = 600
_PROBE_TIMEOUT_SECONDS = 60
_TERMINATE_GRACE_SECONDS = 5
# The end of a test run is where its verdict is; the start is collection noise.
_OUTPUT_TAIL_LIMIT = 2000

# `codex sandbox --help`: "-P, --permission-profile <NAME>". `:workspace` is the
# built-in profile checked on codex-cli 0.155.1 (2026-09-23): writes inside -C
# and $TMPDIR succeed, writes to $HOME fail, outbound network fails.
_SANDBOX_PROFILE = ":workspace"
# printf assembles the marker at run time, so a binary that merely echoes the
# argv it was given (which holds the "%s" template) cannot pass for a sandbox.
_PROBE_COMMAND = "printf 'hermes-control-%s\\n' sandbox-ok"
_PROBE_MARKER = "hermes-control-sandbox-ok"


def _tail(output: str) -> str:
    """Last part of *output* without colour codes, which test runners emit when
    FORCE_COLOR is set and which would otherwise eat the tail budget."""
    return strip_ansi(output)[-_OUTPUT_TAIL_LIMIT:]


@dataclass(frozen=True)
class VerifyOutcome:
    """What running a task's ``verify`` command established.

    ``unproven_reason`` is None exactly when the command ran in the sandbox and
    exited 0; ``exit_code`` is None when it never ran or was stopped.
    """

    command: str
    passed: bool
    exit_code: int | None
    output_tail: str
    unproven_reason: str | None


def find_sandbox() -> str | None:
    """Path of the codex CLI, whose ``sandbox`` subcommand does the containing."""
    info = get_adapter("codex").probe()
    return info.path if info else None


def sandbox_argv(codex_path: str, worktree_path: Path, command: str) -> list[str]:
    argv = [codex_path, "sandbox", "-P", _SANDBOX_PROFILE, "-C", str(worktree_path),
            "--", "/bin/sh", "-c", command]
    assert_no_forbidden_flags(argv)
    return argv


def check_script_path(run_id: str, task_id: str) -> Path:
    """Keep the worker's executable check outside its editable worktree."""
    return run_log_dir().resolve() / "checks" / f"{run_id}-{task_id}.sh"


def check_script_text(codex_path: str, worktree_path: Path, command: str) -> str:
    """Give the worker the controller's sandbox command with no worker secrets.

    ``engine_env`` intentionally gives the worker provider credentials, but the
    check runs code an LLM just wrote. The controller's own verification uses
    ``hermes_subprocess_env(inherit_credentials=False)``, so this script must
    pass through no more than the small environment needed by the sandbox.
    """
    inherited = ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "USER", "LOGNAME",
                 "SHELL", "CODEX_HOME")
    lines = [
        "#!/bin/sh",
        "# Hermes-Control wrote this for the worker to run, not edit.",
        "set --",
    ]
    for name in inherited:
        lines.append(f'if [ "${{{name}+x}}" ]; then set -- "$@" "{name}=${name}"; fi')
    lines.append('set -- "$@" "TMPDIR=${TMPDIR:-/tmp}"')
    quoted_argv = " ".join(shlex.quote(part) for part in
                           sandbox_argv(codex_path, worktree_path, command))
    lines.append(f'exec /usr/bin/env -i "$@" {quoted_argv}')
    return "\n".join(lines) + "\n"


def write_check_script(codex_path: str, worktree_path: Path, command: str, *,
                       path: Path) -> Path:
    """Publish the check atomically, so a stale path cannot redirect the write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(check_script_text(codex_path, worktree_path, command))
            handle.flush()
            os.fchmod(handle.fileno(), 0o500)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _verify_env() -> dict[str, str]:
    """No provider credentials either: unlike the engines, the check needs none."""
    from tools.environments.local import hermes_subprocess_env

    return hermes_subprocess_env(inherit_credentials=False)


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int | None, str, bool]:
    """``(exit code, output tail, timed out)``; the exit code is None on timeout."""
    process = subprocess.Popen(
        argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=_verify_env(), **group_popen_kwargs())
    try:
        output, _ = process.communicate(timeout=timeout)
        return process.returncode, _tail(output), False
    except subprocess.TimeoutExpired:
        terminate_tree(process)
        if wait_bounded(process, _TERMINATE_GRACE_SECONDS) is None:
            kill_tree(process)
            wait_bounded(process, _TERMINATE_GRACE_SECONDS)
    try:
        output, _ = process.communicate(timeout=_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Something outside the group still holds the pipe; give up on its output
        # rather than wait on a process nothing here can reap.
        process.stdout.close()
        output = ""
    return None, _tail(output), True


def _not_run(command: str, reason: str, output: str = "") -> VerifyOutcome:
    return VerifyOutcome(command=command, passed=False, exit_code=None,
                         output_tail=output, unproven_reason=reason)


def run_verify(command: str, worktree_path: Path, *, codex_path: str | None,
               timeout: float = VERIFY_TIMEOUT_SECONDS) -> VerifyOutcome:
    """Run *command* in the supplied checkout with ``/bin/sh`` inside the sandbox."""
    if not codex_path:
        return _not_run(command, "not verified: no sandbox available "
                                 "(it comes from the codex CLI, which was not found)")
    # Prove the sandbox runs commands on this host before trusting its exit code:
    # a usage error from an older codex would otherwise read as a failing check.
    try:
        code, output, _ = _run(sandbox_argv(codex_path, worktree_path, _PROBE_COMMAND),
                               worktree_path, _PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as exc:
        code, output = None, str(exc)
    if code != 0 or _PROBE_MARKER not in output:
        detail = " ".join(output.split())[-300:] or f"exit {code}"
        return _not_run(command, f"not verified: the sandbox did not start ({detail})")

    try:
        code, output, timed_out = _run(sandbox_argv(codex_path, worktree_path, command),
                                       worktree_path, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return _not_run(command, f"not verified: the sandbox did not start ({exc})")
    if timed_out:
        reason: str | None = f"verification timed out after {timeout:g}s"
    elif code != 0:
        reason = f"verification failed (exit {code})"
    else:
        reason = None
    return VerifyOutcome(command=command, passed=reason is None, exit_code=code,
                         output_tail=output, unproven_reason=reason)
