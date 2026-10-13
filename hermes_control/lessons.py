"""Read-only recurring mistakes from run logs; two occurrences make a class.

Fixed fix-level hints (whether a fix belongs in tests, docs or architecture)
by kind: verify_failure -> test,
claude_permission -> architecture, codex_exit -> docs, failure_mode ->
architecture, unproven_reason -> test, warning -> test, unbacked_claim -> docs.
These are starting points, not generated rules. Codex exits are counts only:
non-zero exits often represent normal probes. Any rule made from a class must
be proven against one listed past case.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from . import worker_report
from .events import ControlEvent
from .review_pack import _unbacked_claims

LEVELS = {
    "verify_failure": "test", "claude_permission": "architecture",
    "codex_exit": "docs", "failure_mode": "architecture",
    "unproven_reason": "test", "warning": "test", "unbacked_claim": "docs",
}
REMINDER = "Any rule made from this class must be proven against one listed past case."


def _events() -> list[ControlEvent]:
    """Read lines independently so bad bytes and partial writes lose only a line."""
    from hermes_constants import get_hermes_home

    events = []
    try:
        paths = sorted((Path(get_hermes_home()) / "control").glob("run-*.jsonl"))
    except OSError:
        return events
    for path in paths:
        try:
            with path.open("rb") as handle:
                for line in handle:
                    try:
                        events.append(ControlEvent.from_json_line(line.decode("utf-8")))
                    except (UnicodeError, ValueError, KeyError, TypeError):
                        continue
        except OSError:
            continue
    return sorted(events, key=lambda event: (event.ts, event.seq))


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item.get("text")) if isinstance(item, dict)
                         else _text(item) for item in value)
    return ""


def _normalize(value: str) -> str:
    # Replace the whole path before its digits/hashes, including macOS temp dirs.
    value = re.sub(r"(?<![\w/])(?:[A-Za-z]:[\\/]|/|\.{1,2}/)?[\w.-]+(?:/[\w.-]+)+",
                   lambda match: match.group() if any(
                       part.lower() in {"and", "or"} for part in match.group().split("/"))
                   else "<path>", value)
    value = re.sub(r"\b(?=[a-fA-F\d]*\d)[a-fA-F\d]{7,}\b", "<id>", value)
    value = re.sub(r"\b\d+(?:\.\d+)*\b", "<n>", value)
    return " ".join(value.split())


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _head(command: str) -> str:
    tokens = _tokens(command)
    while tokens and re.match(r"^[A-Za-z_]\w*=", tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] == "cd" and "&&" in tokens:
        return _head(shlex.join(tokens[tokens.index("&&") + 1:]))
    if tokens and os.path.basename(tokens[0]) in {"sh", "bash", "zsh", "dash"}:
        if len(tokens) > 2 and tokens[1] in {"-c", "-lc"}:
            try:
                shlex.split(command)
            except ValueError:
                inner = re.sub(r"^\s*(?:[A-Za-z_]\w*=\S+\s+)*\S+\s+-l?c\s+", "", command)
                return _head(inner.strip("'\""))
            return _head(tokens[2])
    return os.path.basename(tokens[0].strip("'\"")) if tokens else "unknown"


def build(since: float = 30, *, min_count: int = 2, limit: int | None = None,
          now: float | None = None, recent: float = 14) -> list[dict[str, Any]]:
    """Active classes first, by frequency, with Codex exits last in each group.

    Each matching event/summary entry counts once. Summary classes use only the
    newest summary per (run, task), even when that summary clears an old failure.
    Starts outside the window still supply command context for an in-window end.
    Evidence retains the newest five rows per class (three for Codex exits).
    """
    now = time.time() if now is None else now
    cutoff = now - since * 86400
    events = _events()
    classes: dict[str, dict[str, Any]] = {}
    latest: dict[str, float] = {}
    starts: dict[tuple[str, str, str], str] = {}
    command_events: dict[str, list[ControlEvent]] = defaultdict(list)
    summaries: dict[tuple[str, str], tuple[ControlEvent, dict]] = {}

    def add(kind: str, signature: str, title: str, event: ControlEvent,
            excerpt: str, task_id: str | None = None) -> None:
        key = f"{kind}:{signature}"
        if key not in classes:
            classes[key] = dict(key=key, title=title, count=0, level=LEVELS[kind],
                                first_seen=event.ts, last_seen=event.ts,
                                latest_run=event.run_id, evidence=[], evidence_total=0, reminder=REMINDER)
            latest[key] = event.ts
        row = classes[key]
        row["first_seen"] = min(row["first_seen"], event.ts)
        row["last_seen"] = max(row["last_seen"], event.ts)
        row["count"] += 1
        row["evidence_total"] += 1
        row["evidence"].append(dict(run_id=event.run_id,
                                    task_id=event.task_id if task_id is None else task_id,
                                    excerpt=" ".join(excerpt.split())[:160]))
        del row["evidence"][:-(3 if kind == "codex_exit" else 5)]
        if event.ts >= latest[key]:
            row["latest_run"] = event.run_id
            latest[key] = event.ts

    for event in events:
        payload = event.payload
        identity = (event.run_id, event.task_id)
        if event.kind == "tool_start":
            tool_input = payload.get("input")
            command = tool_input.get("command") if isinstance(tool_input, dict) else None
            if isinstance(command, str):
                command_events[event.run_id].append(event)
                starts[(*identity, str(payload.get("tool_id")))] = command
        elif event.kind == "summary":
            tasks = payload.get("tasks")
            for task in tasks if isinstance(tasks, list) else []:
                if isinstance(task, dict) and isinstance(task.get("task_id"), str):
                    summaries[(event.run_id, task["task_id"])] = (event, task)
        if event.ts < cutoff:
            continue
        if event.kind == "verify" and payload.get("passed") is False:
            output = _text(payload.get("output_tail"))
            signature = next((line.strip() for line in reversed(output.splitlines())
                              if re.match(r"^(E\s+)?[\w.]*(Error|Exception)\b|^FAILED ", line.strip())), None)
            if signature:
                normalized = _normalize(signature)
                add("verify_failure", normalized, f"Verify: {normalized}", event, signature)
        elif event.kind == "tool_end" and payload.get("is_error"):
            content = _text(payload.get("content"))
            command = starts.get((*identity, str(payload.get("tool_id"))), "")
            approval = re.search(r"requires approval\b", content, re.I)
            refusal = re.search(r"^(?:Error:\s*)?(?:I\b.*|This\b.*|)permission (?:denied|request)\b",
                                content, re.I)
            if event.engine == "claude" and (approval or refusal):
                # The approval message's colon identifies the refused portion of
                # a multi-operation command, not necessarily the original head.
                refused = (content[approval.end():].partition(":")[2].strip()
                           if approval else "") or command
                head = _head(refused)
                add("claude_permission", head, f"Claude permission: {head}", event, content)
            elif event.engine == "codex" and type(payload.get("exit_code")) is int \
                    and payload["exit_code"] != 0:
                head = _head(command)
                add("codex_exit", head, f"Codex non-zero exit: {head} (count only)",
                    event, command or content)

    for (run_id, task_id), (event, task) in sorted(
            summaries.items(), key=lambda item: (item[1][0].ts, item[1][0].seq)):
        if event.ts < cutoff:
            continue
        mode = task.get("failure_mode")
        if isinstance(mode, str) and mode:
            add("failure_mode", mode, f"Failure mode: {mode}", event, mode, task_id)
        reason = task.get("unproven_reason")
        if isinstance(reason, str) and reason.strip() and not reason.strip().lower().startswith("empty diff"):
            prefix = reason.partition(":")[0].strip().lower()
            if prefix.startswith("verification failed"):
                prefix = "verification failed"
            elif reason.lower().startswith("not verified:"):
                # Keep the fixed explanation (e.g. no verify command) while
                # dropping additional colon-delimited exception detail.
                prefix = ":".join(reason.split(":")[:2]).strip().lower()
            prefix = _normalize(prefix)[:80]
            add("unproven_reason", prefix, f"Unproven: {prefix}", event, reason, task_id)
        warnings = task.get("warnings")
        for warning in warnings if isinstance(warnings, list) else []:
            if isinstance(warning, str) and warning.strip():
                prefix = _normalize(warning.partition(":")[0].strip())[:80]
                add("warning", prefix, f"Warning: {prefix}", event, warning, task_id)
        report = worker_report.parse(task.get("text"))
        for command in _unbacked_claims(report, command_events[run_id], task_id):
            head = _head(command)
            add("unbacked_claim", head, f"Unbacked claim: {head}", event, command, task_id)
    rows = [row for row in classes.values() if row["count"] >= min_count]
    for row in rows:
        row["stale"] = row["last_seen"] < now - recent * 86400
    rows.sort(key=lambda row: (row["stale"], row["key"].startswith("codex_exit:"),
                               -row["count"], -latest[row["key"]], row["key"]))
    return rows if limit is None else rows[:max(0, limit)]


def report(since: float = 30, *, min_count: int = 2, limit: int | None = None,
           as_json: bool = False, recent: float = 14) -> int:
    rows = build(since, min_count=min_count, limit=limit, recent=recent)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False))
        return 0
    console = Console()

    def date(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    for stale in (False, True):
        group = [row for row in rows if row["stale"] == stale]
        if stale:
            if not group:
                continue
            console.print(f"Not seen in the last {recent:g} days")
        table = Table(show_edge=False, pad_edge=False)
        for column in ("class", "count", "level", "latest run", "first", "last"):
            table.add_column(column)
        for row in group:
            if not row["key"].startswith("codex_exit:"):
                table.add_row(row["title"], str(row["count"]), row["level"], row["latest_run"],
                              date(row["first_seen"]), date(row["last_seen"]))
        console.print(table)
        for row in group:
            if row["key"].startswith("codex_exit:"):
                continue
            console.print(row["title"], markup=False)
            for item in row["evidence"]:
                console.print(f"  {item['run_id']} / {item['task_id']}: {item['excerpt']}",
                              markup=False, soft_wrap=True)
        exits = [row for row in group if row["key"].startswith("codex_exit:")]
        if exits:
            console.print("Codex non-zero exits (counts only)")
            for row in exits:
                console.print(f"  {row['title']}: {row['count']} · first {date(row['first_seen'])} "
                              f"· last {date(row['last_seen'])}", markup=False)
    console.print(REMINDER)
    return 0
