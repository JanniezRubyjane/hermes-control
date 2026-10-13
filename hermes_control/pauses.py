"""Tasks waiting for a usage limit to reset: one JSON file each under
``<hermes home>/control/paused/``. Written by a dispatch, read and resumed by the
dashboard, so the record carries everything a later run needs. See
docs/limits-and-resume.md §4.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .engines.base import ControlTask
from .policy import ExecPolicy
from .runlog import run_log_dir
from .worktree import TaskWorktree

_SAFE_ID = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class Pause:
    id: str
    run_id: str
    task: dict[str, Any]  # the task as planned, engine set to the one to resume on
    repo: str | None
    worktree: dict[str, str] | None  # None: paused before it started
    engine: str
    session_id: str | None
    reason: str
    resets_at: float | None  # the earliest time an engine may take it again
    paused_at: float
    rerouted: dict[str, Any] | None = None

    def control_task(self) -> ControlTask:
        return task_from_dict(self.task)

    def task_worktree(self) -> TaskWorktree | None:
        if self.worktree is None:
            return None
        return TaskWorktree(path=Path(self.worktree["path"]), branch=self.worktree["branch"],
                            repo_root=Path(self.worktree["repo_root"]),
                            base_commit=self.worktree.get("base_commit", ""))

    def worktree_path(self) -> Path | None:
        return Path(self.worktree["path"]) if self.worktree else None


def pause_id(run_id: str, task_id: str) -> str:
    return f"{run_id}-{re.sub(r'[^A-Za-z0-9._-]', '_', task_id)}"


def task_to_dict(task: ControlTask) -> dict[str, Any]:
    return {"id": task.id, "goal": task.goal, "engine": task.engine,
            "policy": "write" if task.policy.write else "read-only",
            "files_hint": list(task.files_hint), "depends_on": list(task.depends_on),
            "why": task.why, "model": task.model, "effort": task.effort, "verify": task.verify,
            "red": task.red}


def task_from_dict(data: dict[str, Any]) -> ControlTask:
    return ControlTask(id=data["id"], goal=data["goal"], engine=data["engine"],
                       policy=ExecPolicy(write=data.get("policy") == "write"),
                       files_hint=tuple(data.get("files_hint") or ()),
                       depends_on=tuple(data.get("depends_on") or ()),
                       why=data.get("why") or "", model=data.get("model"),
                       effort=data.get("effort"), verify=data.get("verify"), red=data.get("red"))


def worktree_to_dict(worktree: TaskWorktree) -> dict[str, str]:
    return {"path": str(worktree.path), "branch": worktree.branch,
            "repo_root": str(worktree.repo_root), "base_commit": worktree.base_commit}


def _directory() -> Path:
    directory = run_log_dir() / "paused"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save(record: Pause) -> None:
    path = _directory() / f"{record.id}.json"
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(dataclasses.asdict(record), ensure_ascii=False), encoding="utf-8")
    os.replace(staging, path)


def load(record_id: str) -> Pause | None:
    """The record, or None; an id that is not a plain file name never is one."""
    if not _SAFE_ID.fullmatch(record_id or ""):
        return None
    try:
        return Pause(**json.loads((_directory() / f"{record_id}.json").read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def all_paused() -> list[Pause]:
    records = (load(path.stem) for path in _directory().glob("*.json"))
    return sorted((r for r in records if r is not None), key=lambda r: r.paused_at)


def remove(record_id: str) -> bool:
    """Forget a paused task. Its worktree stays for the user to inspect or delete."""
    if not _SAFE_ID.fullmatch(record_id or ""):
        return False
    try:
        (_directory() / f"{record_id}.json").unlink()
        return True
    except OSError:
        return False
