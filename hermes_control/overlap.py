"""Find committed task branches that changed the same paths from one base."""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import Any, Sequence

from .worktree import _run_git

logger = logging.getLogger(__name__)


def find_overlaps(repo: str | Path | None, entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group tasks connected by shared changed paths at the same base commit.

    A diff that cannot be inspected contributes no paths, so warning generation
    cannot replace the task results.
    """
    if not any(entry.get("base_commit") and entry.get("commit") for entry in entries):
        return []
    if repo is None:
        logger.warning("hermes-control: cannot find overlaps without a repo")
        return []
    by_base: dict[str, list[tuple[str, set[str]]]] = defaultdict(list)
    for entry in entries:
        try:
            base, commit = entry.get("base_commit"), entry.get("commit")
            if not base or not commit:
                continue
            proc = _run_git(["diff", "--name-only", "-z", base, commit, "--"],
                            cwd=str(repo))
            if proc.returncode != 0:
                logger.warning("hermes-control: git diff failed for task %s: %s",
                               entry.get("task_id"), proc.stderr.strip())
                continue
            by_base[base].append((entry["task_id"], set(filter(None, proc.stdout.split("\0")))))
        except Exception as exc:  # noqa: BLE001 - overlap detection is advisory
            logger.warning("hermes-control: git diff failed for task %s: %s",
                           entry.get("task_id") if isinstance(entry, dict) else "?", exc)

    overlaps = []
    for base, tasks in by_base.items():
        paths: dict[str, list[int]] = defaultdict(list)
        for index, (_, changed) in enumerate(tasks):
            for path in changed:
                paths[path].append(index)
        neighbors: dict[int, set[int]] = defaultdict(set)
        for owners in paths.values():
            if len(owners) > 1:
                for owner in owners:
                    neighbors[owner].update(owners)
        seen: set[int] = set()
        for start in sorted(neighbors):
            if start in seen:
                continue
            component, pending = set(), [start]
            while pending:
                current = pending.pop()
                if current in component:
                    continue
                component.add(current)
                pending.extend(neighbors[current] - component)
            seen.update(component)
            shared = sorted(path for path, owners in paths.items()
                            if len(set(owners) & component) > 1)
            overlaps.append({"tasks": [tasks[i][0] for i in sorted(component)],
                             "base_commit": base, "files": shared})
    return overlaps
