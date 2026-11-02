"""Engine-neutral permission request, and its mapping to each CLI's own flags.

The two engines do not have the same security model and this module is the
only place that difference lives: ``claude`` is prompt-gated (permission mode
plus tool allow/deny lists), ``codex`` is sandbox-contained (an OS-level
sandbox policy). ``assert_no_forbidden_flags`` is a belt-and-braces guard so
no future edit can quietly hand a worker an escape hatch.
"""

from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

# Flags and flag values that disable containment. Never emitted; asserted against.
# Every name below was read off the installed CLIs' own `--help` on 2026-09-23;
# the provenance comments are the record, because the first version of this set
# was written from the design doc and missed two of them.
FORBIDDEN_FLAGS: frozenset[str] = frozenset({
    # `agy --help` (agy 1.3.5): --remote-control exposes remote session control.
    "--remote-control",
    # `claude --help`: "--allow-dangerously-skip-permissions  Enable bypassing all
    # permission checks as an option, without it being enabled by default."
    "--allow-dangerously-skip-permissions",
    # `claude --help`: "--dangerously-skip-permissions  Bypass all permission
    # checks." A SEPARATE flag from the one above - claude ships both, and only
    # the longer name was guarded until this was checked against the real help.
    "--dangerously-skip-permissions",
    # `codex exec --help`: "--dangerously-bypass-approvals-and-sandbox  Skip all
    # confirmation prompts and execute commands without sandboxing."
    "--dangerously-bypass-approvals-and-sandbox",
    # `codex exec --help`: "--dangerously-bypass-hook-trust  Run enabled hooks
    # without requiring persisted hook trust for this invocation."
    "--dangerously-bypass-hook-trust",
    # `claude --help`: --permission-mode (choices: "acceptEdits", "auto",
    # "bypassPermissions", "manual", "dontAsk", "plan"). Both of these skip the
    # prompt gate that IS claude's containment; Hermes-Control emits only
    # "plan" (read-only) and "acceptEdits" (write).
    "bypassPermissions",
    "dontAsk",
    # `codex exec --help`: -s/--sandbox [possible values: read-only,
    # workspace-write, danger-full-access].
    "danger-full-access",
    # `codex exec --help` (codex-cli 0.155.1): "--approve-for-me  Route approval
    # requests through automatic review using the workspace-write sandbox". Like
    # claude's auto mode, a model then grants the escalations the sandbox exists
    # to stop, with no human in the loop of an unattended worker.
    "--approve-for-me",
})

# Values that disable containment only as the value of one particular flag. They
# cannot join FORBIDDEN_FLAGS as bare tokens, because the same word is an ordinary
# value of other flags and the guard would then reject harmless argv.
FORBIDDEN_FLAG_VALUES: dict[str, frozenset[str]] = {
    # `agy --help` (agy 1.3.5): --mode accepts accept-edits, auto, plan;
    # auto delegates approval to a classifier, with no human reviewing escapes.
    "--mode": frozenset({"auto"}),
    # `claude --help`: --permission-mode (choices: ..., "auto", ...), and
    # `claude auto-mode`: "Inspect or reset auto mode classifier" - auto hands the
    # approval decision to a classifier, so an unattended worker has nobody in the
    # loop. "auto" is also a value of `claude --autocompact <auto|tokens>` and of
    # `codex exec --color` (its default), hence matched against this flag only.
    "--permission-mode": frozenset({"auto"}),
    # `codex sandbox --help`: "-P, --permission-profile <NAME>". No --help lists the
    # built-in names; `codex sandbox -P :bogus` answers "unknown built-in profile",
    # and on codex-cli 0.155.1 `-P :danger-full-access` let the command write to
    # $HOME. The verify step emits `-P :workspace`; this is the value it must not.
    "-P": frozenset({":danger-full-access"}),
    "--permission-profile": frozenset({":danger-full-access"}),
}


def gemini_settings_problem(data: object) -> str | None:
    """Fail closed on machine-wide agy settings we have not reviewed."""
    if not isinstance(data, Mapping):
        return "agy settings must be a JSON object"
    # Unknown settings may widen access; only the probed sandbox posture is safe.
    for key in data:
        if key not in {"enableTerminalSandbox", "toolPermission", "trustedWorkspaces", "permissions"}:
            return f"agy settings contains unreviewed key: {key}"
    if data.get("enableTerminalSandbox") is not True:
        return "agy enableTerminalSandbox must be true"
    if "trustedWorkspaces" in data:
        trusted = data["trustedWorkspaces"]
        if not isinstance(trusted, list) or any(not isinstance(path, str) for path in trusted):
            return "agy trustedWorkspaces must be a list of strings"
    permission = data.get("toolPermission")
    if permission is not None and permission not in (
            "request-review", "strict", "proceed-in-sandbox"):
        return "agy toolPermission must be request-review, strict, or proceed-in-sandbox"
    if "toolPermission" in data and not isinstance(permission, str):
        return "agy toolPermission must be a string"
    permissions = data.get("permissions", {})
    if not isinstance(permissions, Mapping):
        return "agy permissions must be an object"
    for key in permissions:
        if key not in {"allow", "deny"}:
            return f"agy permissions contains unreviewed key: {key}"
        rules = permissions[key]
        if not isinstance(rules, list) or any(not isinstance(rule, str) for rule in rules):
            return f"agy permissions.{key} must be a list of strings"
    if permissions.get("allow"):
        return f"agy permissions.allow permits unsafe access: {permissions['allow'][0]}"
    return None


def gemini_permission_files_problem(home: Path, checked: dict[Path, str | None]) -> str | None:
    """Review external agy grants and retain digests of the exact bytes reviewed."""
    projects = home / "config" / "projects"
    try:
        try:
            mode = projects.lstat().st_mode
        except FileNotFoundError:
            checked[projects] = None
            entries = []
        else:
            if not stat.S_ISDIR(mode):
                return f"agy projects path {projects} must be a directory, not a symlink or file"
            checked[projects] = "directory"
            entries = sorted(projects.iterdir())
    except OSError as exc:
        return f"could not read agy projects directory {projects} ({exc})"
    for path in entries:
        try:
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                return f"agy project file {path} is a symlink"
            if not stat.S_ISREG(mode):
                return f"agy project entry {path} must be a regular JSON file"
            if path.name == ".DS_Store":
                continue
            if path.suffix != ".json":
                return f"agy project entry {path} is not a .json file"
            content = path.read_bytes()
        except OSError as exc:
            return f"could not read agy project file {path} ({exc})"
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError) as exc:
            return f"could not parse agy project file {path} ({exc})"
        if not isinstance(data, dict):
            return f"agy project file {path} must be a JSON object"
        for key in data:
            if key not in {"id", "name", "projectResources"}:
                return f"agy project file {path} contains unreviewed key: {key}"
        if "projectResources" in data and data["projectResources"] != {}:
            return f"agy project file {path} projectResources must be an empty object"
        checked[path] = hashlib.sha256(content).hexdigest()

    hooks = home / "antigravity-cli" / "hooks.json"
    try:
        content = hooks.read_bytes()
    except FileNotFoundError:
        if hooks.is_symlink():
            return f"could not read agy user hooks {hooks}: dangling symlink"
        checked[hooks] = None
    except OSError as exc:
        return f"could not read agy user hooks {hooks} ({exc})"
    else:
        try:
            if content.strip() and json.loads(content) != {}:
                return f"agy user hooks {hooks} must be empty or an empty JSON object"
        except (ValueError, UnicodeError) as exc:
            return f"could not parse agy user hooks {hooks} ({exc})"
        checked[hooks] = hashlib.sha256(content).hexdigest()
    return None


@dataclass(frozen=True)
class ExecPolicy:
    """What a task is allowed to do. ``write=False`` means look, don't touch."""

    write: bool
    allow_tools: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()


def claude_policy_flags(policy: ExecPolicy) -> list[str]:
    """Map to ``claude``'s prompt-gated model.

    ``plan`` mode is the read-only posture: Claude Code will not apply edits in
    it. ``acceptEdits`` auto-approves edits, which is safe here only because
    the worker's cwd is a throwaway worktree.
    """
    flags = ["--permission-mode", "acceptEdits" if policy.write else "plan"]
    if policy.allow_tools:
        flags += ["--allowedTools", *policy.allow_tools]
    if policy.deny_tools:
        flags += ["--disallowedTools", *policy.deny_tools]
    return flags


def codex_policy_flags(policy: ExecPolicy) -> list[str]:
    """Map to ``codex``'s sandbox model. Tool lists have no codex equivalent and
    are dropped rather than silently approximated."""
    return ["-s", "workspace-write" if policy.write else "read-only"]


def _tokens(argv: Sequence[str]) -> set[str]:
    """Every token a shell-level flag parser would see.

    Both CLIs accept ``--flag=value`` as well as ``--flag value``, so a plain
    membership test on argv elements lets ``--permission-mode=bypassPermissions``
    through untouched. Splitting on ``=`` is confined to flag-shaped elements:
    a task's ``goal`` is also an argv element, and splitting free prose would
    turn "reject --permission-mode=bypassPermissions" in a goal into a hard
    failure of the very task asking for that guard.
    """
    tokens: set[str] = set()
    for element in argv:
        token = str(element)
        tokens.add(token)
        if token.startswith("-"):
            tokens.update(token.split("="))
    return tokens


def _flag_values(argv: Sequence[str]) -> set[tuple[str, str]]:
    """``(flag, value)`` pairs in both the ``--flag value`` and ``--flag=value`` forms."""
    elements = [str(element) for element in argv]
    pairs: set[tuple[str, str]] = set()
    for index, element in enumerate(elements):
        if not element.startswith("-"):
            continue
        flag, separator, value = element.partition("=")
        if separator:
            pairs.add((flag, value))
        elif index + 1 < len(elements):
            pairs.add((element, elements[index + 1]))
    return pairs


def assert_no_forbidden_flags(argv: Sequence[str]) -> None:
    """Raise if *argv* contains any containment-disabling token or flag value."""
    found = {token for token in _tokens(argv) if token in FORBIDDEN_FLAGS}
    found |= {f"{flag} {value}" for flag, value in _flag_values(argv)
              if value in FORBIDDEN_FLAG_VALUES.get(flag, frozenset())}
    if found:
        raise ValueError(f"forbidden flag(s) in engine argv: {', '.join(sorted(found))}")
