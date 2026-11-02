"""Private, versioned line comments for committed task reviews."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from . import review_diff, runs
from .runlog import run_log_dir

MAX_BODY = 256 * 1024
INVALID = "invalid review"


class VersionConflict(Exception):
    """The review changed since the caller read it."""


def _task(run_id: str, task_id: str) -> dict[str, Any]:
    # Validate before even resolving the storage root or looking up the run.
    if not (isinstance(run_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id)
            and isinstance(task_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", task_id)):
        raise ValueError(INVALID)
    view = runs.run_view(run_id)
    task = next((task for task in view["tasks"] if task["id"] == task_id), None) if view else None
    if task is None:
        raise LookupError("no such run or task")
    return task


def _read(path: Path, run_id: str, task_id: str, task: dict) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        # Reject non-finite numbers and lone surrogates before HTTP serialization.
        json.dumps(record, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if (not isinstance(record, dict)
                or not {"version", "run_id", "task_id", "commit", "comments"} <= record.keys()
                or record["run_id"] != run_id or record["task_id"] != task_id
                or not (record["commit"] is None or isinstance(record["commit"], str))
                or not isinstance(record["comments"], list)):
            raise ValueError(INVALID)
        items = []
        for comment in record["comments"]:
            if (not isinstance(comment, dict)
                    or set(comment) != {"id", "path", "side", "line", "text", "created_at"}
                    or type(comment["created_at"]) not in (int, float)
                    or not isinstance(comment["path"], str)):
                raise ValueError(INVALID)
            items.append({key: value for key, value in comment.items() if key != "created_at"})
        _comments({"version": record["version"], "comments": items}, {"comments": []},
                  {item["path"] for item in items})
        return record
    except FileNotFoundError:
        return dict(version=0, run_id=run_id, task_id=task_id,
                    commit=(task.get("commit") or {}).get("sha"), comments=[])
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError(INVALID) from None


def _response(record: dict, task: dict) -> dict:
    note = ""
    if record["comments"]:
        from agent.redact import redact_sensitive_text

        sha = record["commit"]
        lines = [f"Review comments on hermes-control task {record['task_id']} (run {record['run_id']})",
                 f"branch {task.get('branch') or 'unknown'} at commit {sha[:12] if sha else 'unknown'} (not merged into main):"]
        for comment in sorted(record["comments"], key=lambda c: (c["path"], c["line"], c["side"])):
            text = re.sub(r"[\r\n\u2028\u2029\u0085]+", " ", comment["text"])
            lines.append(f"- {comment['path']}:{comment['line']} ({comment['side']}) — {text}")
        note = redact_sensitive_text("\n".join(lines), force=True)
    return {key: record[key] for key in ("version", "commit", "comments")} | {"note": note}


def get(run_id: str, task_id: str) -> dict:
    task = _task(run_id, task_id)
    path = run_log_dir() / "reviews" / run_id / f"{task_id}.json"
    return _response(_read(path, run_id, task_id, task), task)


def _comments(body: Any, record: dict, allowed: set[str]) -> list[dict]:
    if (not isinstance(body, dict) or set(body) != {"version", "comments"}
            or type(body["version"]) is not int or body["version"] < 0
            or not isinstance(body["comments"], list) or len(body["comments"]) > 200):
        raise ValueError(INVALID)
    existing = {c["id"]: c for c in record["comments"]}
    comments, ids = [], set()
    for item in body["comments"]:
        if (not isinstance(item, dict) or not {"path", "side", "line", "text"} <= set(item)
                or set(item) - {"id", "path", "side", "line", "text"}
                or not isinstance(item["path"], str) or item["path"] not in allowed
                or not isinstance(item["side"], str) or item["side"] not in {"old", "new"}
                or type(item["line"]) is not int or not 1 <= item["line"] <= 10_000_000
                or not isinstance(item["text"], str) or not 1 <= len(item["text"].strip()) <= 2000):
            raise ValueError(INVALID)
        identifier = item.get("id", secrets.token_hex(6))
        if (not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{12}", identifier)
                or identifier in ids):
            raise ValueError(INVALID)
        ids.add(identifier)
        comments.append(dict(id=identifier, path=item["path"], side=item["side"],
                             line=item["line"], text=item["text"].strip(),
                             created_at=existing[identifier]["created_at"] if identifier in existing else time.time()))
    return comments


def put(run_id: str, task_id: str, body: Any) -> dict:
    task = _task(run_id, task_id)
    try:
        if len(json.dumps(body, ensure_ascii=False).encode("utf-8")) > MAX_BODY:
            raise ValueError(INVALID)
    except (TypeError, UnicodeError, RecursionError):
        raise ValueError(INVALID) from None
    root = run_log_dir() / "reviews"
    directory = root / run_id
    root.mkdir(mode=0o700, exist_ok=True)
    directory.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    directory.chmod(0o700)
    # Lock the directory, not the replaced inode: readers always see a whole file,
    # and concurrent writers (including other processes) compare inside the lock.
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        path = directory / f"{task_id}.json"
        record = _read(path, run_id, task_id, task)
        diff = review_diff.build(run_id, task_id) or {}
        allowed = {f["path"] for f in diff.get("files", [])} | {c["path"] for c in record["comments"]}
        comments = _comments(body, record, allowed)
        if body["version"] != record["version"]:
            raise VersionConflict()
        record.update(version=record["version"] + 1, comments=comments,
                      commit=(task.get("commit") or {}).get("sha"), updated_at=time.time())
        response = _response(record, task)
        staging = directory / f".{task_id}-{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False)
            os.replace(staging, path)
        finally:
            staging.unlink(missing_ok=True)
        return response
    finally:
        os.close(descriptor)
