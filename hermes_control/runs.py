"""Run logs read back for the dashboard's Runs view: what the planner decided, what
each engine did, and where every task ended up.

The log is append-only JSONL (runlog.py). A run's tasks come from its ``plan``
event; each engine run inside a task starts with a ``session`` event, so those
split a task into attempts (a handover after a usage limit, or a resume, is a
second attempt). ``summary`` events, written when a dispatch or a resume ends,
settle tasks; a run with unsettled tasks and no event for a while has stopped.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable, Iterator

from .events import ControlEvent
from .runlog import RunLog, run_log_dir

# Longer than a task's default timeout (30 min): quiet this long means nobody is writing.
STALE_SECONDS = 35 * 60
_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_DETAIL_KEYS = ("file_path", "command", "pattern", "path", "query", "url", "description")
# Task worktrees live at <repo>/.worktrees/subagent-control-<run>-<task> (worktree.py).
_WORKTREE_IN_PATH = re.compile(r"(/[^\s'\"]*?)/\.worktrees/subagent-control-")
# The same prefix up to and including the worktree's own directory: every path a worker
# touches starts with it, so it is dropped from what the views show.
_IN_WORKTREE = re.compile(r"/[^\s'\"()\[\]]*?/\.worktrees/subagent-control-[A-Za-z0-9._-]+/")


def _short(text: str) -> str:
    return _IN_WORKTREE.sub("", text)


def _cut(text: Any, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _detail(tool_input: Any) -> str:
    """The one argument that says what a tool call did: a path, a command, a query."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _DETAIL_KEYS:
        if tool_input.get(key):
            return _cut(_short(str(tool_input[key])), 200)
    return ""


def _error_text(payload: dict[str, Any]) -> str:
    error = payload.get("error")
    return _cut(payload.get("message") or (error.get("message") if isinstance(error, dict) else "")
                or payload.get("subtype") or "engine error", 400)


def _new_task(task_id: str, planned: dict[str, Any] | None = None) -> dict[str, Any]:
    planned = planned or {}
    return {
        "id": task_id, "why": planned.get("why", ""), "policy": planned.get("policy"),
        "depends_on": planned.get("depends_on") or [], "verify_command": planned.get("verify"),
        "planned_engine": planned.get("engine"), "model": planned.get("model"),
        "effort": planned.get("effort"), "rerouted": planned.get("rerouted"),
        "attempts": [], "commit": None, "verify": None, "paused": None, "resumed": [],
        "limits": [], "branch": "", "worktree": "", "summary": None,
        "_tools": {}, "_next_how": None, "_latest_reroute": None,
    }


def _attempt_settings(planned_engine: str | None, planned_model: str | None,
                      planned_effort: str | None, reroute: dict[str, Any] | None,
                      engine: str, session: dict[str, Any] | None = None
                      ) -> tuple[str | None, str | None]:
    """Use only settings recorded before this attempt, never another engine's plan."""
    source = reroute if reroute is not None else (
        {"model": planned_model, "effort": planned_effort}
        if engine == planned_engine else {})
    session = session or {}
    return (session.get("model") or source.get("model"),
            session.get("effort") or source.get("effort"))


def _new_attempt(task: dict[str, Any], engine: str, ts: float,
                 session: dict[str, Any] | None = None) -> dict[str, Any]:
    model, effort = _attempt_settings(task["planned_engine"], task["model"],
                                      task["effort"], task["_latest_reroute"],
                                      engine, session)
    attempt = {
        "engine": engine, "model": model, "effort": effort,
        "session_id": (session or {}).get("session_id"), "started_at": ts, "ended_at": ts,
        "how": task["_next_how"] or ("planned" if not task["attempts"] else "continued"),
        "tools": 0, "tool_errors": 0, "steps": [], "last_text": "", "result": None,
        "error": None, "limit": None,
    }
    task["_next_how"] = None
    task["_tools"] = {}
    task["attempts"].append(attempt)
    return attempt


def _apply(task: dict[str, Any], kind: str, engine: str, ts: float,
           payload: dict[str, Any]) -> None:
    if kind == "session":
        _new_attempt(task, engine, ts, payload)
        return
    if kind == "limit":
        task["limits"].append({**payload, "engine": engine, "at": ts})
        if task["attempts"]:
            task["attempts"][-1]["limit"] = payload
        if payload.get("next") not in (None, "pause"):
            task["_next_how"] = "continued"
        return
    if kind == "rerouted":
        task["rerouted"] = payload
        task["_latest_reroute"] = payload
        return
    if kind == "paused":
        task["paused"] = {**payload, "at": ts}
        return
    if kind == "resumed":
        task["resumed"].append({**payload, "at": ts})
        if "model" in payload or "effort" in payload:
            task["_latest_reroute"] = payload
        task["paused"], task["summary"], task["_next_how"] = None, None, "resumed"
        return
    if kind == "commit":
        task["commit"] = payload if payload.get("sha") else None
        task["branch"] = payload.get("branch") or task["branch"]
        return
    if kind == "verify":
        task["verify"] = payload
        return
    # Controller notices (slot_wait, wake) belong only in the timeline; they
    # must not create an engine attempt or mark one as failed.
    if kind not in ("text", "thinking", "tool_start", "tool_end", "result", "error",
                    "permission_request", "usage"):
        return
    attempt = task["attempts"][-1] if task["attempts"] else _new_attempt(task, engine, ts)
    attempt["ended_at"] = ts
    if kind == "tool_start":
        step = {"type": "tool", "name": str(payload.get("name") or "tool"),
                "detail": _detail(payload.get("input")), "is_error": False}
        task["_tools"][payload.get("tool_id")] = step
        attempt["steps"].append(step)
        attempt["tools"] += 1
    elif kind == "tool_end":
        step = task["_tools"].get(payload.get("tool_id"))
        if step is not None and payload.get("is_error"):
            step["is_error"] = True
            attempt["tool_errors"] += 1
    elif kind in ("text", "thinking") and payload.get("text"):
        text = _cut(_short(payload["text"]), 800)
        attempt["steps"].append({"type": kind, "text": text})
        if kind == "text":
            attempt["last_text"] = text
    elif kind == "result":
        usage = payload.get("usage") or {}
        details = usage.get("output_tokens_details") or {}
        attempt["result"] = {"ok": bool(payload.get("ok", True)),
                             "input_tokens": usage.get("input_tokens"),
                             "cached_input_tokens": usage.get("cached_input_tokens") if "cached_input_tokens" in usage
                             else usage.get("cache_read_input_tokens"),
                             "cache_write_tokens": usage.get("cache_write_input_tokens") if "cache_write_input_tokens" in usage
                             else usage.get("cache_creation_input_tokens"),
                             "output_tokens": usage.get("output_tokens"),
                             "reasoning_tokens": usage.get("reasoning_output_tokens") if "reasoning_output_tokens" in usage
                             else details.get("thinking_tokens"),
                             "cost_usd": payload.get("total_cost_usd")}
    elif kind == "error":
        attempt["error"] = _error_text(payload)
        attempt["steps"].append({"type": "error", "text": attempt["error"]})


def _settled(task: dict[str, Any], stale: bool) -> bool:
    """Over, one way or another. Logs from before ``summary`` events existed settle a
    task by its check, or, once the run has gone quiet, by its engine's last word."""
    if task["summary"] is not None or task["paused"] or task["verify"] is not None:
        return True
    last = task["attempts"][-1] if task["attempts"] else None
    return bool(stale and last and (last["result"] or last["error"]))


def _status(task: dict[str, Any], *, settled: bool, stale: bool) -> tuple[str, str | None]:
    if task["paused"]:
        return "paused", task["paused"].get("reason")
    summary = task["summary"]
    verify = task["verify"]
    last = task["attempts"][-1] if task["attempts"] else None
    if task["commit"]:
        if verify and verify.get("passed"):
            return "done", None
        if verify or summary:
            return "unproven", ((verify or {}).get("unproven_reason")
                                or (summary or {}).get("unproven_reason") or "not verified")
        return ("stopped", "stopped while checking") if stale else ("running", "checking")
    if settled:
        ok = bool(last and last["result"] and last["result"]["ok"])
        if ok and task["policy"] == "read-only":
            return "done", None
        reason = ((summary or {}).get("unproven_reason") or (last or {}).get("error")
                  or ("nothing was committed" if ok else None))
        return ("unproven" if ok else "failed"), reason
    if stale:
        return "stopped", "no event for a long time; the run was stopped or crashed"
    return ("running", None) if task["attempts"] else ("waiting", None)


def run_view(run_id: str, *, now: Callable[[], float] = time.time) -> dict[str, Any] | None:
    """Everything the diagram draws for one run, or None when there is no such run."""
    if not _RUN_ID.fullmatch(run_id or ""):
        return None
    log = RunLog(run_id)
    if not log.path.exists():
        return None
    events = log.read_all()
    if not events:
        return None
    tasks: dict[str, dict[str, Any]] = {}
    planner, repo = None, None
    for event in events:
        payload = event.payload if isinstance(event.payload, dict) else {}
        if event.kind == "plan":
            planner, repo = payload.get("planner"), payload.get("repo")
            for planned in payload.get("tasks") or []:
                tasks.setdefault(planned["id"], _new_task(planned["id"], planned))
            continue
        if event.kind == "summary":
            for entry in payload.get("tasks") or []:
                task = tasks.setdefault(entry["task_id"], _new_task(entry["task_id"]))
                task["summary"] = entry
                if entry.get("paused_id") is None and entry.get("ok") is False:
                    task["paused"] = None
                task["branch"] = entry.get("branch") or task["branch"]
                task["worktree"] = entry.get("worktree") or task["worktree"]
                task["rerouted"] = entry.get("rerouted") or task["rerouted"]
            continue
        if not event.task_id:
            continue
        task = tasks.setdefault(event.task_id, _new_task(event.task_id))
        _apply(task, event.kind, event.engine, event.ts, payload)
        if repo is None and event.kind in ("tool_start", "tool_end"):
            # Logs from before the plan recorded its repo: a worker's paths name it,
            # in what it ran (claude) or in what that printed (codex).
            found = _WORKTREE_IN_PATH.search(str(payload.get("input") or payload.get("content") or ""))
            repo = found.group(1) if found else None

    updated = events[-1].ts
    stale = now() - updated > STALE_SECONDS
    settled_ids = {task_id for task_id, task in tasks.items() if _settled(task, stale)}
    settled = len(settled_ids) == len(tasks)
    views = []
    for task in tasks.values():
        status, reason = _status(task, settled=task["id"] in settled_ids, stale=stale)
        views.append({**{k: v for k, v in task.items() if not k.startswith("_")},
                      "status": status, "status_reason": reason})
    if repo is None:
        worktree = next((t["worktree"] for t in views if t["worktree"]), "")
        repo = worktree.split("/.worktrees/")[0] if "/.worktrees/" in worktree else None
    return {"run_id": run_id, "started_at": events[0].ts, "updated_at": updated,
            "state": ("paused" if any(task["paused"] for task in tasks.values()) else "finished")
            if settled else ("stopped" if stale else "running"),
            "repo": repo, "planner": planner, "tasks": views}
class _TraceBuilder:
    """Replay log events into task timelines, retaining pending calls for later ends."""

    def __init__(self) -> None:
        self.entries: dict[str, list[dict[str, Any]]] = {}
        self.planner: list[dict[str, Any]] = []
        self.pending: dict[tuple[str, Any], dict[str, Any]] = {}
        self.end_seq: dict[int, int] = {}
        self.attempts: dict[str, int] = {}
        self.next_how: dict[str, str] = {}
        self.planned: dict[str, dict[str, Any]] = {}
        self.reroutes: dict[str, dict[str, Any]] = {}

    def consume(self, event: ControlEvent) -> list[tuple[str, dict[str, Any]]]:
        payload = event.payload if isinstance(event.payload, dict) else {}
        kind, task_id = event.kind, event.task_id
        base = {"seq": event.seq, "ts": event.ts, "engine": event.engine}
        if kind == "plan":
            emitted = []
            for planned in payload.get("tasks") or []:
                if not isinstance(planned, dict) or not planned.get("id"):
                    continue
                entry = {**base, "type": "plan",
                         **{key: planned.get(key) for key in
                            ("why", "engine", "model", "effort", "policy")},
                         "goal": _cut(planned.get("goal"), 600)}
                self.entries.setdefault(planned["id"], []).append(entry)
                self.planned[planned["id"]] = planned
                emitted.append((planned["id"], entry))
            return emitted
        if kind == "planner" and not task_id:
            entry = {**base, "type": "planner", "phase": payload.get("phase")}
            entry.update({key: payload[key] for key in ("wait_seconds", "waited_seconds", "state")
                          if key in payload})
            self.planner.append(entry)
            return [("", entry)]
        if not task_id:
            return []
        entries = self.entries.setdefault(task_id, [])
        if kind == "rerouted":
            self.reroutes[task_id] = payload
        if kind == "resumed":
            self.next_how[task_id] = "resumed"
        elif kind == "limit" and payload.get("next") not in (None, "pause"):
            self.next_how[task_id] = "continued"
        if kind in ("thinking", "text", "tool_start", "tool_end", "result", "error",
                    "permission_request", "usage") and task_id not in self.attempts:
            self.attempts[task_id] = 1
        if kind == "session":
            count = self.attempts.get(task_id, 0)
            how = self.next_how.pop(task_id, None) or ("planned" if count == 0 else "continued")
            self.attempts[task_id] = count + 1
            self.pending = {key: value for key, value in self.pending.items() if key[0] != task_id}
            planned = self.planned.get(task_id, {})
            model, effort = _attempt_settings(planned.get("engine"), planned.get("model"),
                                               planned.get("effort"), self.reroutes.get(task_id),
                                               event.engine, payload)
            entry = {**base, "type": "session", "model": model, "effort": effort,
                     "session_id": payload.get("session_id"), "how": how}
        elif kind in ("thinking", "text"):
            if kind == "thinking" and not payload.get("text"):
                return []
            entry = {**base, "type": kind, "text": _cut(payload.get("text"), 4000)}
        elif kind == "tool_start":
            raw = json.dumps(payload.get("input"), ensure_ascii=False, default=str)
            entry = {**base, "type": "tool", "tool_id": payload.get("tool_id"),
                     "name": payload.get("name"), "input": _detail(payload.get("input")),
                     "input_raw": _cut(raw, 2000)}
            self.pending[(task_id, payload.get("tool_id"))] = entry
        elif kind == "tool_end":
            entry = self.pending.pop((task_id, payload.get("tool_id")), None)
            if entry is None:
                return []
            output = payload.get("content") or payload.get("stdout") or ""
            output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False,
                                                                      default=str)
            entry.update({"is_error": bool(payload.get("is_error")),
                          "output": _cut(output, 6000),
                          "stderr": _cut(payload.get("stderr"), 2000),
                          "duration_s": event.ts - entry["ts"]})
            if len(output) > 6000:
                entry["output_truncated"] = True
            if "exit_code" in payload:
                entry["exit_code"] = payload["exit_code"]
            self.end_seq[id(entry)] = event.seq
            return [(task_id, entry)]
        elif kind in ("limit", "rerouted", "paused", "resumed", "commit", "verify", "proof",
                      "error", "result", "slot_wait", "wake"):
            entry = {**base, "type": kind, "data": payload}
        else:
            return []
        entries.append(entry)
        return [(task_id, entry)]


def task_trace(run_id: str, task_id: str, *, since_seq: int | None = None) -> list[dict[str, Any]] | None:
    """Events for one task, including run-level planner phases; None for no run."""
    if not _RUN_ID.fullmatch(run_id or ""):
        return None
    log = RunLog(run_id)
    if not log.path.exists():
        return None
    events = log.read_all()
    if not events:
        return None
    builder = _TraceBuilder()
    for event in events:
        builder.consume(event)
    entries = builder.entries.get(task_id, []) + builder.planner
    entries.sort(key=lambda entry: (entry["ts"], entry["seq"]))
    if since_seq is not None:
        entries = [entry for entry in entries
                   if builder.end_seq.get(id(entry), entry["seq"]) > since_seq]
    return entries


def stream_run(run_id: str, *, poll_seconds: float = 0.5,
               heartbeat_seconds: float = 15, now: Callable[[], float] = time.time,
               sleep: Callable[[float], None] = time.sleep) -> Iterator[str]:
    """Send a full view after each log append, with idle heartbeats for SSE clients."""
    if not _RUN_ID.fullmatch(run_id or ""):
        return
    log = RunLog(run_id)
    try:
        offset = log.path.stat().st_size
    except FileNotFoundError:
        return
    view = run_view(run_id, now=now)
    if view is None:
        return
    trace = _TraceBuilder()
    for event in log.read_all():
        trace.consume(event)
    last_sent = now()
    yield f"event: view\ndata: {json.dumps(view)}\n\n"
    if view["state"] != "running":
        return

    while True:
        sleep(poll_seconds)
        try:
            events, offset = log.tail(offset)
        except FileNotFoundError:
            yield "event: gone\ndata: {}\n\n"
            return
        if events:
            new_entries = []
            for event in events:
                new_entries.extend((task_id, dict(entry)) for task_id, entry in trace.consume(event))
            view = run_view(run_id, now=now)
            if view is None:
                yield "event: gone\ndata: {}\n\n"
                return
            last_sent = now()
            yield f"event: view\ndata: {json.dumps(view)}\n\n"
            for task_id, entry in new_entries:
                yield f"event: trace\ndata: {json.dumps({'task_id': task_id, 'entry': entry})}\n\n"
            if view["state"] != "running":
                return
        elif now() - last_sent >= heartbeat_seconds:
            view = run_view(run_id, now=now)
            if view is None:
                yield "event: gone\ndata: {}\n\n"
                return
            last_sent = now()
            if view["state"] != "running":
                yield f"event: view\ndata: {json.dumps(view)}\n\n"
                return
            yield ": keep-alive\n\n"


def list_runs(limit: int = 30, *, now: Callable[[], float] = time.time) -> list[dict[str, Any]]:
    """The newest runs first, each with its tasks' status for the list."""
    files = sorted(run_log_dir().glob("run-*.jsonl"), key=lambda p: p.stat().st_mtime,
                   reverse=True)[:limit]
    listed = []
    for path in files:
        view = run_view(path.stem[len("run-"):], now=now)
        if view is None:
            continue
        listed.append({
            "run_id": view["run_id"], "started_at": view["started_at"],
            "updated_at": view["updated_at"], "state": view["state"], "repo": view["repo"],
            "planner": view["planner"],
            "tasks": [{"id": t["id"], "status": t["status"],
                       "engine": t["attempts"][-1]["engine"] if t["attempts"] else t["planned_engine"]}
                      for t in view["tasks"]]})
    return listed


def run_stats(limit: int = 30, *, run_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """One row per attempt in selected or recent runs, using the dashboard task view."""
    rows = []
    ids = run_ids if run_ids is not None else [listed["run_id"] for listed in list_runs(limit)]
    for run_id in dict.fromkeys(ids):
        view = run_view(run_id)
        if view is None:
            continue  # A live run may disappear between the list and the read.
        for task in view["tasks"]:
            for index, attempt in enumerate(task["attempts"]):
                result = attempt["result"] or {}
                verify = task["verify"]
                rows.append({
                    "run_id": view["run_id"], "task_id": task["id"],
                    "planned_engine": task["planned_engine"], "engine": attempt["engine"],
                    "planned_model": task["model"], "model": attempt["model"],
                    "model_recorded": attempt["model"] is not None,
                    "effort": attempt["effort"], "policy": task["policy"], "how": attempt["how"],
                    "tools": attempt["tools"], "tool_errors": attempt["tool_errors"],
                    "seconds": round(attempt["ended_at"] - attempt["started_at"]),
                    "input_tokens": result.get("input_tokens"),
                    "cached_input_tokens": result.get("cached_input_tokens"),
                    "output_tokens": result.get("output_tokens"),
                    "reasoning_tokens": result.get("reasoning_tokens"),
                    "cost_usd": result.get("cost_usd"), "status": task["status"],
                    "verify_passed": verify.get("passed") if verify is not None else None,
                    "final_attempt": index == len(task["attempts"]) - 1,
                    "moved": attempt["engine"] != task["planned_engine"],
                })
    return rows
