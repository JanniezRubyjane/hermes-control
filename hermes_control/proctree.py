"""Process-group lifecycle for the children Hermes-Control spawns.

Engines and verify commands both start trees of processes (bash, node helpers,
MCP servers, test runners). Signalling only the direct child orphans the rest,
and they keep writing into a worktree the summary has just reported on - so
every spawn goes into its own group and every stop is a group stop.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from typing import Any


def group_popen_kwargs() -> dict[str, Any]:
    """Popen kwargs putting the child in its own process group.

    ``claude`` and ``codex`` spawn bash commands, node helpers and MCP servers.
    Signalling only the direct child orphans all of those, and they keep writing
    into a worktree the summary has just called "preserved for human review".
    Same pattern as ``tools/browser_use_cli.py`` and ``tools/process_registry.py``.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def signal_tree(process, sig: int, fallback) -> None:
    """Send *sig* to the whole process group, falling back to the direct child.

    The fallback matters on the paths where ``start_new_session`` did not apply
    (Windows) or the group is already gone: killing just the child is strictly
    better than killing nothing.
    """
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            fallback()
        return
    try:
        # The leader may already be reaped while its background children still
        # belong to this group. start_new_session makes its PID the group ID.
        os.killpg(process.pid, sig)  # windows-footgun: ok - the nt branch returned above
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            fallback()


def terminate_tree(process) -> None:
    """Polite stop for the engine and everything it spawned."""
    signal_tree(process, signal.SIGTERM, process.terminate)


def kill_tree(process) -> None:
    """Last resort. On Windows ``taskkill /T /F`` is the only tree-wide kill
    available, which is what ``tools/browser_use_cli.py`` uses for the same reason."""
    if os.name == "nt":
        from hermes_cli._subprocess_compat import windows_hide_flags
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=10, check=False,
                           creationflags=windows_hide_flags())
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.kill()
        return
    signal_tree(process, getattr(signal, "SIGKILL", signal.SIGTERM), process.kill)


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Some sandboxes deny signal 0 even to groups we may signal. It is
        # still evidence that the group has not been reported as gone.
        return True
    return True


def _wait_group_gone(process, timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        # A finished leader remains in its group as a zombie until reaped.
        process.poll()
        if not _group_exists(process.pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def stop_group(process, grace: float) -> None:
    """Stop the whole engine group, including children of an exited leader."""
    if os.name == "nt":
        terminate_tree(process)
        if wait_bounded(process, grace) is None:
            kill_tree(process)
        return

    # start_new_session makes the engine PID its group ID. Waiting on the
    # process alone cannot detect descendants after that leader has exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.terminate()
    if _wait_group_gone(process, grace):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            process.kill()
    _wait_group_gone(process, 0.5)


def wait_bounded(process, timeout: float) -> int | None:
    """Exit status, or None when *timeout* elapsed with the child still alive."""
    try:
        return process.wait(timeout=max(timeout, 0.1))
    except subprocess.TimeoutExpired:
        return None
