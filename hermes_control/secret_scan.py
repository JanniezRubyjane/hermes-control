"""Pre-commit secret checks. Findings contain locations and rule names only.

Inspect the exact staged index supplied by commit_worktree, without restaging.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Mapping
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from hermes_cli._subprocess_compat import harden_git_argv

from .worktree import TaskWorktree, _GIT_TIMEOUT, _task_git_env

logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 2 * 1024 * 1024

_RULES = tuple((name, re.compile(pattern)) for name, pattern in (
    ("private-key", r"-----BEGIN (?:(?:[A-Z0-9]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----"),
    ("aws-access-key", r"\b(?!AKIAIOSFODNN7EXAMPLE\b)(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github-token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})"),
    ("slack-token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("google-api-key", r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])"),
    ("openai-or-anthropic-key", r"\bsk-(?:(?:ant-|proj-|svcacct-)[A-Za-z0-9_-]{16,}|"
     r"(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9_]{32,})"),
    ("stripe-key", r"\b(?:sk_live_|rk_live_)[A-Za-z0-9]{16,}"),
))
_ASSIGNMENT = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                         r"(?:\"([^\"]*)\"|'([^']*)'|([^\s#]*))\s*(?:#.*)?$")
_SECRET_NAME = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY)(?:_|$)", re.I)
_VALUE = re.compile(r"[A-Za-z0-9+/=_.-]{16,}\Z")
_PLACEHOLDER = re.compile(
    r"x{3,}|(?:changeme)+|(?:example)+|(?:dummy)+|(?:fake)+|(?:placeholder)+|(?:test)+", re.I)
_HUNK = re.compile(rb"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_SHELL = re.compile(rb"^#![^\n]*\b(?:sh|bash|zsh|ksh|fish|dash|csh|tcsh)(?:\s|$)")


def _invalid() -> None:
    # Never log config values or exception messages: they may contain secrets.
    logger.warning("hermes-control: invalid secret scan settings; using defaults")


def parse_settings(raw: Any) -> dict[str, Any]:
    defaults = {"enabled": True, "allow_paths": []}
    if raw is None:
        return defaults
    if not isinstance(raw, Mapping):
        _invalid()
        return defaults
    if any(key not in {"enabled", "allow_paths"} for key in raw):
        logger.warning("hermes-control: unknown keys in settings.secret_scan; ignoring them")
    enabled, paths = raw.get("enabled"), raw.get("allow_paths")
    if ((enabled is not None and type(enabled) is not bool)
            or (paths is not None and (not isinstance(paths, list)
                                      or any(not isinstance(p, str) for p in paths)))):
        _invalid()
        return defaults
    return {"enabled": True if enabled is None else enabled,
            "allow_paths": [] if paths is None else list(paths)}


def read_settings() -> dict[str, Any]:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    config = load_config_readonly()
    entry = _plugin_settings_entry({} if config is None else config, "hermes_control")
    settings = entry.get("settings") if isinstance(entry, Mapping) else None
    if settings is not None and not isinstance(settings, Mapping):
        _invalid()
        return parse_settings(None)
    return parse_settings(settings.get("secret_scan") if settings is not None else None)


def _git(worktree, env, *args):
    if "GIT_DIR" not in env or "GIT_WORK_TREE" not in env:
        env = _task_git_env(worktree, env)
    proc = subprocess.run(["git", *harden_git_argv(list(args))], cwd=worktree.path,
                          env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=_GIT_TIMEOUT)
    if proc.returncode:
        # Git's stderr can quote file contents; never propagate it.
        raise RuntimeError("secret scan git operation failed")
    return proc.stdout


def _blob_prefix(worktree, env, sha):
    if "GIT_DIR" not in env or "GIT_WORK_TREE" not in env:
        env = _task_git_env(worktree, env)
    with subprocess.Popen(["git", *harden_git_argv(["cat-file", "blob", sha])],
                          cwd=worktree.path, env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as proc:
        try:
            prefix = proc.stdout.read(MAX_FILE_BYTES)
            if len(prefix) < MAX_FILE_BYTES and proc.wait(timeout=_GIT_TIMEOUT):
                raise RuntimeError("secret scan blob read failed")
            return prefix
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=_GIT_TIMEOUT)


def _added_lines(worktree, env, path, max_line):
    diff = _git(worktree, env, "diff", "--cached", "--text", "--no-ext-diff", "--no-textconv",
                "--no-color", "--no-renames", "--unified=0", "HEAD", "--", f":(literal){path}")
    number = None
    for line in diff.split(b"\n"):
        hunk = _HUNK.match(line)
        if line.startswith(b"diff --git "):
            number = None
        elif hunk:
            number = int(hunk[1])
        elif number is not None and line.startswith(b"+"):
            if number > max_line:
                break
            yield number
            number += 1
        elif number is not None and line.startswith(b" "):
            number += 1


def _secret_filename(path):
    name = Path(path).name
    return (not name.endswith(".pub") and any(fnmatchcase(name, glob) for glob in (
        "*.pem", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*")))


def _env_file(path, prefix):
    name = Path(path).name
    return (name == ".env" or name.startswith(".env.")
            or Path(path).suffix in (".env", ".sh", ".bash", ".zsh", ".ksh", ".fish")
            or name in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile",
                        ".envrc", ".zshenv") or bool(_SHELL.match(prefix)))


def _env_secret(line):
    match = _ASSIGNMENT.match(line)
    if (not match or not _SECRET_NAME.search(match[1])
            or match[1].upper().endswith(("_PATH", "_FILE", "_NAME", "_DIR", "_URL", "_ID"))):
        return False
    value = next((v for v in match.groups()[1:] if v is not None), "")
    path = (value.startswith(("/", "~")) and "/" in value[1:]
            and "+" not in value and "=" not in value)
    return bool(not path and "://" not in value
                and value not in ("AKIAIOSFODNN7EXAMPLE",
                                  "wJalrXUtnFEMI/K7MDENG/" + "bPxRfiCYEXAMPLEKEY")
                and _VALUE.fullmatch(value) and not _PLACEHOLDER.fullmatch(value))


def scan(worktree: TaskWorktree, env: dict[str, str]) -> str | None:
    """Return a safe refusal reason, or None. Errors are caught by commit_worktree()."""
    settings = read_settings()
    if not settings["enabled"]:
        return None
    hits = []
    fields = _git(worktree, env, "diff", "--cached", "--raw", "--no-abbrev",
                  "--no-renames", "-z", "HEAD").split(b"\0")
    for i in range(0, len(fields) - 1, 2):
        _, mode, _, sha, status = fields[i].split()
        if status == b"D" or mode == b"160000":
            continue
        path = fields[i + 1].decode("utf-8", errors="surrogateescape")
        if any(fnmatchcase(path, glob) for glob in settings["allow_paths"]):
            continue
        if status == b"A" and _secret_filename(path):
            hits.append((path, 1, "secret-file"))
        prefix = _blob_prefix(worktree, env, sha.decode())
        lines = prefix.decode("utf-8", errors="replace").split("\n")
        env_file = b"\0" not in prefix[:8000] and _env_file(path, prefix)
        added = (range(1, len(lines) + 1) if status == b"A" else
                 _added_lines(worktree, env, path, len(lines)))
        for number in added:
            line = lines[number - 1]
            for rule, pattern in _RULES:
                if pattern.search(line):
                    hits.append((path, number, rule))
            if env_file and _env_secret(line):
                hits.append((path, number, "env-assignment"))
    if not hits:
        return None
    locations = ", ".join(f"{path}:{line} ({rule})" for path, line, rule in hits[:5])
    if len(hits) > 5:
        locations += f", +{len(hits) - 5} more"
    return (f"not committed: possible secret in the change: {locations}; "
            "the change is left in the task worktree; remove the secret, or list the path "
            "in settings.secret_scan.allow_paths if it is a test fixture")
