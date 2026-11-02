"""Validated JSON task specifications. Hidden material never enters worker goals."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

KINDS = frozenset("routine bug refactor split review readonly trap-gaming trap-secret "
                  "trap-network fallback ambiguous frontend large-repo fix-round".split())


def validate_sentinel_paths(paths, repos=()) -> list[str]:
    """Expand external paths, checking task/control repos rather than ancestors."""
    if not isinstance(paths, list):
        raise ValueError("sentinel_paths: must be a list of absolute or ~/ paths")
    roots = [Path(__file__).resolve().parents[2], *(Path(repo).expanduser().resolve() for repo in repos)]
    expanded = []
    for value in paths:
        if not isinstance(value, str) or not (value.startswith("/") or value.startswith("~/")):
            raise ValueError("sentinel_paths: must contain absolute or ~/ paths")
        try:
            path = Path(value).expanduser().absolute()
            resolved = path.resolve()
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"sentinel_paths: invalid path ({exc})") from exc
        if any(location.is_relative_to(root) for location in (path, resolved) for root in roots):
            raise ValueError("sentinel_paths: must be outside the task and hermes-control repositories")
        expanded.append(str(path))
    return expanded


@dataclass(frozen=True)
class TaskSpec:
    id: str
    source: str
    kind: str
    policy: str
    goal: str
    verify: str
    hidden_dir: Path
    hidden_cmd: str
    protected_paths: list[str]
    repo: dict[str, str]
    expected_answer: list[str] | None = None
    secret_marker: str | None = None
    allowed_config: list[str] = field(default_factory=list)
    request: str | None = None
    answer_prefix: str | None = None
    answer_forbidden: list[str] = field(default_factory=list)
    answer_casefold: bool = False
    verify_note: str | None = None
    hidden_expect: dict[str, str | int] | None = None
    forbidden_diff_patterns: list[str] = field(default_factory=list)
    sentinel_paths: list[str] = field(default_factory=list)
    answer_patterns: list[str] = field(default_factory=list)

    def __post_init__(self):
        for name in ("id", "source", "kind", "policy", "goal", "verify", "hidden_cmd"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name}: must be text")
        if self.request is None:
            object.__setattr__(self, "request", "Use Hermes-Control to: " + self.goal)
        if not isinstance(self.request, str) or not self.request.strip():
            raise ValueError("request: must be nonempty text")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", self.id):
            raise ValueError("id: invalid task id")
        for name, choices in (("source", {"S", "R"}), ("kind", KINDS),
                              ("policy", {"write", "read-only"})):
            if getattr(self, name) not in choices:
                raise ValueError(f"{name}: must be one of {sorted(choices)}")
        for name in ("goal", "hidden_cmd"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name}: must not be empty")
        if self.policy == "write" and not self.verify.strip():
            raise ValueError("verify: required for write tasks")
        if not isinstance(self.hidden_dir, (str, Path)) or not str(self.hidden_dir).strip():
            raise ValueError("hidden_dir: must be a path")
        try:
            object.__setattr__(self, "hidden_dir", Path(self.hidden_dir).expanduser().resolve())
        except (OSError, ValueError, RuntimeError) as exc:
            raise ValueError(f"hidden_dir: invalid path ({exc})") from exc
        if not isinstance(self.protected_paths, list):
            raise ValueError("protected_paths: must be a list of relative paths")
        for item in self.protected_paths:
            if (not isinstance(item, str) or not item or "\\" in item
                    or PurePosixPath(item).is_absolute()
                    or any(p in {"..", ".git"} for p in PurePosixPath(item).parts)
                    or item == "."):
                raise ValueError("protected_paths: must contain repo-relative paths")
        if not isinstance(self.allowed_config, list):
            raise ValueError("allowed_config: must be a list of relative paths")
        for item in self.allowed_config:
            if (not isinstance(item, str) or not item or "\\" in item
                    or PurePosixPath(item).is_absolute()
                    or any(p in {"..", ".git"} for p in PurePosixPath(item).parts)
                    or item == "." or str(PurePosixPath(item)) != item):
                raise ValueError("allowed_config: must contain normalized repo-relative paths")
        if not isinstance(self.repo, dict):
            raise ValueError("repo: must be an object")
        if set(self.repo) not in ({"builder"}, {"base_commit", "origin_repo"}):
            raise ValueError("repo: use builder or base_commit and origin_repo")
        for name, value in self.repo.items():
            if not isinstance(value, str) or not value.strip() or value.startswith("-"):
                raise ValueError(f"repo.{name}: must be nonempty text, not an option")
        if self.source == "S" and "builder" not in self.repo:
            raise ValueError("repo: synthetic source requires builder")
        if self.source == "R" and "origin_repo" not in self.repo:
            raise ValueError("repo: replay source requires origin_repo")
        object.__setattr__(self, "sentinel_paths", validate_sentinel_paths(
            self.sentinel_paths, [self.repo["origin_repo"]] if "origin_repo" in self.repo else []))
        if self.expected_answer is not None and (
                not isinstance(self.expected_answer, list)
                or not all(isinstance(x, str) for x in self.expected_answer)):
            raise ValueError("expected_answer: must be a list of strings")
        if self.secret_marker is not None and (
                not isinstance(self.secret_marker, str) or not self.secret_marker):
            raise ValueError("secret_marker: must be nonempty text")
        if not isinstance(self.forbidden_diff_patterns, list):
            raise ValueError("forbidden_diff_patterns: must be a list of regex strings")
        for pattern in self.forbidden_diff_patterns:
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("forbidden_diff_patterns: must contain nonempty regex strings")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"forbidden_diff_patterns: invalid regex ({exc})") from exc
            if compiled.search("") is not None:
                raise ValueError("forbidden_diff_patterns: must not match the empty string")
        if self.answer_prefix is not None and (
                not isinstance(self.answer_prefix, str) or not self.answer_prefix.strip()
                or not self.answer_prefix.endswith(":")):
            raise ValueError("answer_prefix: must be nonempty text ending in ':'")
        if (not isinstance(self.answer_forbidden, list)
                or not all(isinstance(x, str) for x in self.answer_forbidden)):
            raise ValueError("answer_forbidden: must be a list of strings")
        if not isinstance(self.answer_casefold, bool):
            raise ValueError("answer_casefold: must be a boolean")
        if not isinstance(self.answer_patterns, list):
            raise ValueError("answer_patterns: must be a list of regex strings")
        for pattern in self.answer_patterns:
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("answer_patterns: must contain nonempty regex strings")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"answer_patterns: invalid regex ({exc})") from exc
        if self.verify_note is not None and (
                not isinstance(self.verify_note, str) or not self.verify_note.strip()):
            raise ValueError("verify_note: must be nonempty text")
        if self.hidden_expect is not None:
            if (not isinstance(self.hidden_expect, dict)
                    or set(self.hidden_expect) != {"pattern", "min"}):
                raise ValueError("hidden_expect: must be an object with pattern and min keys")
            pattern = self.hidden_expect["pattern"]
            if not isinstance(pattern, str):
                raise ValueError("hidden_expect: pattern must be text")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"hidden_expect: invalid pattern ({exc})") from exc
            if compiled.groups != 1:
                raise ValueError("hidden_expect: pattern must have exactly one capture group")
            minimum = self.hidden_expect["min"]
            if type(minimum) is not int or minimum < 1:
                raise ValueError("hidden_expect: min must be a positive integer")

    @classmethod
    def from_dict(cls, data: dict, *, directory: Path | None = None) -> TaskSpec:
        if not isinstance(data, dict):
            raise ValueError("spec: must be an object")
        required = {"id", "source", "kind", "policy", "goal", "verify", "hidden_dir",
                    "hidden_cmd", "protected_paths", "repo"}
        for name in sorted(required - data.keys()):
            raise ValueError(f"{name}: required")
        unknown = data.keys() - required - {
            "expected_answer", "secret_marker", "allowed_config", "request",
            "answer_prefix", "answer_forbidden", "answer_casefold", "verify_note",
            "hidden_expect", "forbidden_diff_patterns", "sentinel_paths", "answer_patterns"}
        if unknown:
            raise ValueError(f"{sorted(unknown)[0]}: unknown field")
        data = dict(data)
        if directory is not None:
            if isinstance(data["hidden_dir"], str) and data["hidden_dir"].strip():
                data["hidden_dir"] = directory / Path(data["hidden_dir"]).expanduser()
            repo = data.get("repo")
            if (isinstance(repo, dict) and isinstance(repo.get("origin_repo"), str)
                    and repo["origin_repo"].strip()):
                data["repo"] = {**repo, "origin_repo": str(
                    (directory / Path(repo["origin_repo"]).expanduser()).resolve())}
        return cls(**data)

    @classmethod
    def load(cls, path: str | Path) -> TaskSpec:
        return load_spec(path)


def load_spec(path: str | Path) -> TaskSpec:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"spec: invalid JSON in {path}: {exc}") from exc
    return TaskSpec.from_dict(data, directory=path.resolve().parent)


def load_suite(directory: str | Path) -> list[TaskSpec]:
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("suite: must be a directory")
    # A suite is either flat (<id>.json) or one directory per task (<id>/spec.json, as suites S and R are);
    # index.json is the replay extractor's listing, not a task.
    paths = sorted(p for p in directory.glob("*.json") if p.name != "index.json")
    paths += sorted(directory.glob("*/spec.json"))
    specs = [load_spec(path) for path in paths]
    if not specs:
        raise ValueError(f"suite: no task specs in {directory}")
    ids = set()
    for spec in specs:
        if spec.id in ids:
            raise ValueError(f"id: duplicate {spec.id!r}")
        ids.add(spec.id)
    return sorted(specs, key=lambda spec: spec.id)
