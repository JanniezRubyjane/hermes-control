"""Opt-in proof settings and advisory inspection of a committed task diff."""

from __future__ import annotations

import logging
import re
import shlex
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any

from .worktree import TaskWorktree, _run_git

logger = logging.getLogger(__name__)

DEFAULT_TEST_GLOBS = ("tests/**", "**/test_*.py", "**/*_test.py", "**/*.test.*", "**/*.spec.*")
DEFAULT_RED_EXIT_CODES = (1,)
_ASSERT = re.compile(r"^\s*assert\b|\bassert\w*\(")


def parse_settings(raw: Any) -> dict[str, Any]:
    settings = {"test_globs": list(DEFAULT_TEST_GLOBS),
                "red_exit_codes": list(DEFAULT_RED_EXIT_CODES)}
    if not isinstance(raw, Mapping):
        return settings
    for key, kind in (("test_globs", str), ("red_exit_codes", int)):
        value = raw.get(key)
        if isinstance(value, list) and all(type(item) is kind for item in value):
            settings[key] = list(value)
    return settings


def read_settings() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry

        entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
        settings = entry.get("settings") or {}
        return parse_settings(settings.get("proof"))
    except Exception:  # noqa: BLE001 - malformed config must never fail dispatch
        logger.warning("hermes-control: could not read proof settings; using defaults",
                       exc_info=True)
        return parse_settings(None)


def is_test_path(path: str, globs: list[str]) -> bool:
    # A leading **/ also matches zero directories (e.g. root test_calc.py).
    return any(fnmatchcase(path, glob) or
               (glob.startswith("**/") and fnmatchcase(path, glob[3:])) for glob in globs)


def _diff(worktree: TaskWorktree, commit: str, *options: str,
          paths: tuple[str, ...] = ()) -> str:
    proc = _run_git(["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--no-renames",
                     *options, worktree.base_commit, commit, "--",
                     *(f":(literal){path}" for path in paths)], cwd=str(worktree.repo_root))
    if proc.returncode:
        raise RuntimeError(proc.stderr.strip() or f"git diff exited {proc.returncode}")
    return proc.stdout


def changed_paths(worktree: TaskWorktree, commit: str) -> dict[str, str]:
    fields = _diff(worktree, commit, "--name-status", "-z").split("\0")
    return {fields[index + 1]: fields[index] for index in range(0, len(fields) - 1, 2)}


def advisory_warnings(task, worktree: TaskWorktree, commit: str,
                      changed: dict[str, str], globs: list[str]) -> list[str]:
    warnings = []
    non_tests = {path for path, status in changed.items()
                 if status in ("A", "M") and not is_test_path(path, globs)}
    named = set()
    for command in (task.verify, task.red):
        if not command:
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        for token in tokens:
            while token.startswith("./"):
                token = token[2:]
            if token in non_tests:
                named.add(token)
    warnings.extend(f"check_gaming: command names changed non-test file {path}"
                    for path in sorted(named))
    for path, status in changed.items():
        if not is_test_path(path, globs):
            continue
        if status == "D":
            warnings.append(f"weakened_tests: deleted test file {path}")
        elif status == "M":
            try:
                diff = _diff(worktree, commit, "--unified=0", paths=(path,))
            except Exception:  # noqa: BLE001 - retain warnings for the other paths
                logger.warning("hermes-control: could not inspect test diff for %s", path,
                               exc_info=True)
                continue
            added = removed = 0
            in_hunk = False
            for line in diff.splitlines():
                if line.startswith("@@"):
                    in_hunk = True
                elif in_hunk and line.startswith(("+", "-")) and _ASSERT.search(line[1:]):
                    added += line[0] == "+"
                    removed += line[0] == "-"
            if removed > added:
                warnings.append(f"weakened_tests: removed more assert lines than added in {path} "
                                f"({removed} removed, {added} added)")
    return warnings


def task_verdict(commit, outcome, verify_command, unproven_reason, proof=None) -> str:
    """Derive the proof level without changing the existing unproven contract."""
    if not commit:
        return "not-committed"
    if not verify_command:
        return "no-verify"
    if outcome is None:
        return "verify-blocked"
    if outcome.passed:
        if unproven_reason is not None:
            return "unproven"
        return proof["verdict"] if proof else "tests-passed"
    if outcome.exit_code is not None or (outcome.unproven_reason or "").startswith(
            "verification timed out"):
        return "verify-failed"
    return "verify-blocked"
