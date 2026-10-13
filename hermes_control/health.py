"""L1 self-assessment from existing local evidence; no LLM or mutation of inputs.

Candidate rules (at most three, ranked by metric / threshold, then section):
rework rate > .20; minor-only-or-empty review share > .50; planner processed tokens
per dispatched task > 5 * worker processed tokens per task; an engine's median
landing time > its median worker time; a recent, non-count-only lesson class
count >= 5. These are
candidates for investigation, not instructions to change anything.

Windows use event timestamps, merge commit timestamps and session started_at,
not file mtimes. Session counters are lifetime totals for sessions started in
the window, not token-level timestamps. Planner processed tokens include input,
cache read, cache write and output. Codex input already includes cache reads;
Claude input excludes cache reads and writes, so those are added. Output already
includes reasoning tokens. Worker tokens per task include all measured attempts.
Timing is elapsed wall time per task (including slot wait in worker time), with
nearest-rank p90. Landing is result to verify (or commit when verify is absent);
proof is verify to proof.
Recent merges have only the observed portion of their seven-day rework window.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import subprocess
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Iterator

from . import lessons, runs

LONG_SESSION_CALLS = 100
_API_CALL = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ INFO \[([^\]]+)\] "
    r"agent\.conversation_loop: API call #\d+: model=\S+ provider=\S+ "
    r"in=\d+ out=\d+ total=\d+ latency=\S+s cache=(\d+)/\d+ "
    r"\(\d+%\) write=(\d+) id=\S+\s*$")
_LOG_FIELDS = ("log_api_calls", "log_cache_read_tokens", "log_cache_write_tokens",
               "log_big_writes", "log_big_write_tokens")

SECTIONS = (
    ("outcomes", "Outcomes"), ("rework", "Rework after merge"),
    ("reviews", "Review effectiveness"), ("planner", "Planner cost"),
    ("time", "Time breakdown per task"), ("red_green", "Red-green use"),
    ("lessons", "Recurring mistakes"), ("candidates", "Candidates"),
)
_SEVERITY_LEVELS = {
    "critical": "serious", "warning": "moderate", "nit": "minor",
    "bug": "serious", "missing": "moderate", "blocker": "serious",
    "high": "serious", "major": "moderate", "medium": "moderate",
    "minor": "minor", "low": "minor",
}
_SEVERITIES = "|".join(_SEVERITY_LEVELS)
_LABEL = re.compile(
    rf"\*\*\s*(?:[A-Z]\d+\s*[-—–]\s*)?({_SEVERITIES})\b[^*\n]*\*\*"
    rf"|\[\s*(?:severity\s*:\s*)?({_SEVERITIES})\b[^\]\n]*\](?!\()"
    rf"|\bseverity\s*:\s*({_SEVERITIES})\b(?![^\[\]\n]*\]\()", re.I)


def _section(source: str, **values: Any) -> dict[str, Any]:
    return dict(source=source, not_measured=[], **values)


def state_db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state.db"


@contextmanager
def open_state_db(path: Path) -> Iterator[sqlite3.Connection]:
    """URI mode=ro enforces read-only even if the file is writable by the user."""
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None
    return value


def _dispatched(tool_calls: Any) -> list[tuple[str | None, int]]:
    """Expose each dispatch's call id and task count for both tool schemas."""
    dispatched = []
    calls = _json(tool_calls)
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        args = _json(function.get("arguments"))
        if function.get("name") == "control_dispatch":
            planned = args.get("tasks") if isinstance(args, dict) else None
            dispatched.append((call.get("id"), len(planned) if isinstance(planned, list) else 0))
        elif function.get("name") == "tool_call" and isinstance(args, dict):
            nested = args.get("calls")
            for index, inner in enumerate(nested if isinstance(nested, list) else []):
                if isinstance(inner, dict) and inner.get("name") == "control_dispatch":
                    arguments = _json(inner.get("arguments"))
                    planned = arguments.get("tasks") if isinstance(arguments, dict) else None
                    outer_id = call.get("id")
                    call_id = f"{outer_id}#{index}" if outer_id is not None else None
                    dispatched.append((call_id, len(planned) if isinstance(planned, list) else 0))
    return dispatched


def _planner_logs(log_dir: Path, sessions: list[dict], notes: list[str]) -> None:
    """Cross-check lifetime session counters against read-only API call logs."""
    measured: dict[str, Counter] = {}
    ids = {row["id"] for row in sessions}
    seen: set[str] = set()
    unmatched = 0
    try:
        paths = sorted(path for path in log_dir.glob("agent.log*")
                       if path.name == "agent.log" or path.name.startswith("agent.log."))
        for path in paths:
            file_lines: set[str] = set()
            try:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        match = _API_CALL.fullmatch(line)
                        if match is None:
                            if (line not in seen
                                    and "agent.conversation_loop: API call #" in line
                                    and any(session in ids for session in re.findall(r"\[([^\]]+)\]", line))):
                                unmatched += 1
                                file_lines.add(line)
                            continue
                        if match[1] not in ids:
                            continue
                        if line in seen:
                            continue
                        file_lines.add(line)
                        counts = measured.setdefault(match[1], Counter())
                        read, write = int(match[2]), int(match[3])
                        counts.update(log_api_calls=1, log_cache_read_tokens=read,
                                      log_cache_write_tokens=write,
                                      log_big_writes=int(write > 30000),
                                      log_big_write_tokens=write if write > 30000 else 0)
            except OSError as exc:
                notes.append(f"agent log unavailable: {path}: {exc}")
            seen.update(file_lines)
    except OSError as exc:
        notes.append(f"agent logs unavailable: {exc}")
    if unmatched:
        notes.append(f"{unmatched} agent.log API call lines did not match the expected format")
    for row in sessions:
        counts = measured.get(row["id"])
        row.update({key: counts[key] if counts is not None else None for key in _LOG_FIELDS})
    missing = len(ids - measured.keys())
    if missing:
        notes.append(f"{missing} sessions lacked agent.log API call log lines")


def _planner(cutoff: float, now: float, stats: list[dict]) -> dict:
    path = state_db_path()
    result = _section(f"{path}: sessions and all messages for dispatched tasks; "
                      "tool_result_characters from active messages only (read-only); "
                      f"{path.parent / 'logs'}/agent.log and agent.log.* API call cache cross-check "
                      "(read-only, identical lines across files deduplicated); "
                      "worker run-log result usage via runs.run_view", sessions=[])
    worker_rows = [row for row in stats
                   if row.get("input_tokens") is not None and row.get("output_tokens") is not None]
    worker_tasks = {(row["run_id"], row["task_id"]) for row in worker_rows}
    worker_tokens = sum(row["input_tokens"] + row["output_tokens"] + (
        (row.get("cached_input_tokens") or 0) + (row.get("cache_write_tokens") or 0)
        if row["engine"] == "claude" else 0) for row in worker_rows)
    result.update(worker_processed_tokens=worker_tokens if worker_rows else None,
                  worker_tasks=len(worker_tasks),
                  worker_processed_tokens_per_task=worker_tokens / len(worker_tasks) if worker_tasks else None,
                  worker_cache_read_tokens=sum(row.get("cached_input_tokens") or 0 for row in worker_rows)
                  if worker_rows else None,
                  worker_cache_write_tokens=sum(row.get("cache_write_tokens") or 0 for row in worker_rows)
                  if worker_rows else None,
                  worker_output_tokens=sum(row["output_tokens"] for row in worker_rows) if worker_rows else None)
    if not worker_rows or len(worker_rows) < len(stats):
        result["not_measured"].append("worker input/output tokens missing for some or all attempts")
    fields = {"api_calls": "api_call_count", "input_tokens": "input_tokens",
              "output_tokens": "output_tokens", "cache_read_tokens": "cache_read_tokens",
              "cache_write_tokens": "cache_write_tokens"}
    totals = {key: 0 for key in fields}
    totals.update(dispatched_tasks=0, planner_processed_tokens=0,
                  planner_processed_tokens_per_dispatched_task=None,
                  tool_result_characters={}, planner_to_worker_total_ratio=None,
                  long_sessions=0, long_session_calls=0, long_session_token_share=None)
    result["totals"] = totals
    try:
        with open_state_db(path) as conn:
            sessions = conn.execute(
                "SELECT id, started_at, api_call_count, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens FROM sessions "
                "WHERE started_at >= ? AND started_at <= ? ORDER BY started_at, id",
                (cutoff, now)).fetchall()
            for session in sessions:
                messages = conn.execute(
                    "SELECT role, content, tool_name, tool_calls, active FROM messages "
                    "WHERE session_id = ? ORDER BY COALESCE(active, 1) DESC",
                    (session["id"],)).fetchall()
                counts = [call for message in messages for call in _dispatched(message["tool_calls"])]
                if not counts:
                    continue
                seen_dispatches: set[tuple[str, str]] = set()
                tasks = 0
                for call_id, count in counts:
                    if call_id is not None:
                        key = (session["id"], call_id)
                        if key in seen_dispatches:
                            continue
                        seen_dispatches.add(key)
                    tasks += count
                row = {"id": session["id"], "started_at": session["started_at"],
                       "dispatched_tasks": tasks}
                for key, column in fields.items():
                    row[key] = session[column] or 0
                    if session[column] is None:
                        result["not_measured"].append(f"session {session['id']} lacks {column}")
                    totals[key] += row[key]
                row["planner_processed_tokens"] = sum(row[key] for key in (
                    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens"))
                row["planner_processed_tokens_per_dispatched_task"] = (
                    row["planner_processed_tokens"] / row["dispatched_tasks"] if row["dispatched_tasks"] else None)
                characters: Counter = Counter()
                for message in messages:
                    if message["role"] == "tool" and message["active"] in (None, 1):
                        characters[message["tool_name"] or "unknown"] += len(message["content"] or "")
                row["tool_result_characters"] = dict(characters)
                row["tool_result_characters_total"] = sum(characters.values())
                for field, total in (("processed_per_call", row["planner_processed_tokens"]),
                                     ("cache_read_per_call", row["cache_read_tokens"]),
                                     ("tool_result_characters_per_call", row["tool_result_characters_total"])):
                    row[field] = total / row["api_calls"] if row["api_calls"] else None
                result["sessions"].append(row)
                totals["dispatched_tasks"] += row["dispatched_tasks"]
                totals["planner_processed_tokens"] += row["planner_processed_tokens"]
                for tool, count in characters.items():
                    totals["tool_result_characters"][tool] = totals["tool_result_characters"].get(tool, 0) + count
    except (OSError, sqlite3.Error) as exc:
        result["not_measured"].append(f"state.db unavailable or missing required schema: {exc}")
        return result
    if not result["sessions"]:
        result["not_measured"].append("no active control_dispatch sessions in the window")
    _planner_logs(path.parent / "logs", result["sessions"], result["not_measured"])
    long_sessions = [row for row in result["sessions"] if row["api_calls"] >= LONG_SESSION_CALLS]
    totals.update(
        planner_to_worker_total_ratio=totals["planner_processed_tokens"] / worker_tokens
        if worker_rows and worker_tokens else None,
        long_sessions=len(long_sessions),
        long_session_calls=sum(row["api_calls"] for row in long_sessions),
        long_session_token_share=sum(row["planner_processed_tokens"] for row in long_sessions)
        / totals["planner_processed_tokens"] if totals["planner_processed_tokens"] else None)
    if totals["dispatched_tasks"]:
        totals["planner_processed_tokens_per_dispatched_task"] = (
            totals["planner_processed_tokens"] / totals["dispatched_tasks"])
    else:
        result["not_measured"].append("no dispatched task arguments to divide planner tokens by")
    worker_per_task = result["worker_processed_tokens_per_task"]
    result["planner_to_worker_ratio"] = (totals["planner_processed_tokens_per_dispatched_task"] / worker_per_task
                                           if totals["planner_processed_tokens_per_dispatched_task"] is not None
                                           and worker_per_task else None)
    return result


def _git(repo: str | Path | None, *args: str) -> str:
    command = ["git", "-c", "core.quotePath=false"]
    if repo is not None:
        command += ["-C", str(repo)]
    proc = subprocess.run([*command, *args], capture_output=True, text=True, timeout=60)
    if proc.returncode:
        raise ValueError(proc.stderr.strip() or "git failed")
    return proc.stdout


def _changed_lines(diff: str) -> tuple[Counter, Counter]:
    """Count eligible (path, stripped text) lines in a first-parent merge diff."""
    added, removed = Counter(), Counter()
    old_path = new_path = None
    in_hunk = False

    def path(header: str) -> str | None:
        name = json.loads(header) if header.startswith('"') else header.removesuffix("\t")
        return None if name == "/dev/null" else name[2:]

    for line in diff.splitlines():
        if line.startswith("diff --git "):
            old_path = new_path = None
            in_hunk = False
        elif not in_hunk and line.startswith("--- "):
            old_path = path(line[4:])
        elif not in_hunk and line.startswith("+++ "):
            new_path = path(line[4:])
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            name = new_path if line[0] == "+" else old_path
            text = line[1:].strip()
            if name and not name.startswith(("tests/", "docs/")) and not name.endswith(".md") and len(text) >= 8:
                (added if line[0] == "+" else removed)[name, text] += 1
    return added, removed


def _rework(repo: str | Path | None, cutoff: float, now: float) -> dict:
    result = _section("local git: all task merges reachable from main; git diff <merge>^1 <merge>; "
                      "exact stripped lines removed within seven days (excluding tests/docs and short lines)",
                      merges=0, reworked_merges=0, rate=None, added_lines=0, churned_lines=0,
                      churn_rate=None, top_files=[], tasks=[])
    try:
        if repo is None:
            repo = _git(None, "rev-parse", "--show-toplevel").strip()
        result["repo"] = str(repo)
        output = _git(repo, "log", "--reverse", "--topo-order", "--merges", "--format=%H%x00%ct%x00%s", "main")
        merges = []
        for record in output.splitlines():
            sha, timestamp, subject = record.split("\0", 2)
            if not subject.startswith("Merge hermes-control task "):
                continue
            ts = float(timestamp)
            if not cutoff <= ts <= now:
                continue
            diff = _git(repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                        "--src-prefix=a/", "--dst-prefix=b/", "--unified=0", sha + "^1", sha, "--")
            added, removed = _changed_lines(diff)
            merges.append(dict(ts=ts, added=added, removed=removed,
                               task=subject[len("Merge hermes-control task "):].partition(":")[0]))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        result["not_measured"].append(f"git metrics skipped: no local main history or git unavailable: {exc}")
        return result
    # Git traversal order need not match committer date across integration branches.
    merges.sort(key=lambda merge: merge["ts"])
    reworked, churned, files = set(), 0, Counter()
    remaining = [merge["added"].copy() for merge in merges]
    for index, merge in enumerate(merges):
        # Each removal consumes prior additions once, newest merge first.
        removed = merge["removed"].copy()
        for earlier in range(index - 1, -1, -1):
            overlap = remaining[earlier] & removed
            remaining[earlier].subtract(overlap)
            removed.subtract(overlap)
            if overlap and merge["ts"] - merges[earlier]["ts"] <= 7 * 86400:
                reworked.add(earlier)
                churned += sum(overlap.values())
                for (name, _), count in overlap.items():
                    files[name] += count
    added = sum(sum(merge["added"].values()) for merge in merges)
    result.update(merges=len(merges), reworked_merges=len(reworked),
                  rate=len(reworked) / len(merges) if merges else None,
                  added_lines=added, churned_lines=churned, churn_rate=churned / added if added else None,
                  top_files=[dict(file=name, churned_lines=count) for name, count in files.most_common(5)],
                  tasks=[merge["task"] for merge in merges],
                  incomplete_windows=sum(merge["ts"] > now - 7 * 86400 for merge in merges))
    if not merges:
        result["not_measured"].append("no matching main-branch task merges in the window")
    return result


def _outcomes(tasks: list[dict], source: str) -> dict:
    grouped, verify, verdicts, warnings, failures = Counter(), Counter(), Counter(), Counter(), Counter()
    missing = Counter()
    empty = 0
    for task in tasks:
        engine = task["attempts"][-1]["engine"] if task["attempts"] else task["planned_engine"]
        grouped[f"{engine or 'unknown'}/{task['policy'] or 'unknown'}"] += 1
        if engine is None or task["policy"] is None:
            missing["engine/policy"] += 1
        summary = task["summary"] or {}
        check = task["verify"] or summary.get("verify")
        if check and isinstance(check.get("passed"), bool):
            verify["pass" if check["passed"] else "fail"] += 1
        else:
            missing["verify pass/fail"] += 1
        if summary.get("verdict"):
            verdicts[str(summary["verdict"])] += 1
        else:
            missing["verdict"] += 1
        warnings.update(str(warning) for warning in summary.get("warnings") or [])
        if summary.get("failure_mode"):
            failures[str(summary["failure_mode"])] += 1
        if not task["summary"]:
            missing["summary warnings/failure_mode"] += 1
        if task["policy"] and task["policy"] != "read-only":
            reason = summary.get("unproven_reason") or (check or {}).get("unproven_reason") or ""
            # The controller stores a line-count stat, not a unified diff.
            # A committed binary/mode-only change can also have +0/-0.
            empty_stat = summary.get("diff_stat") in ("", "+0/-0")
            if str(reason).lower().startswith("empty diff") or (
                    empty_stat and not summary.get("commit") and not task["commit"]):
                empty += 1
            elif "diff_stat" not in summary:
                missing["write task diff"] += 1
    result = _section(source + "; runs.run_view task summaries", tasks=len(tasks),
                      tasks_by_engine_policy=dict(grouped), verify={"pass": verify["pass"], "fail": verify["fail"]},
                      verdicts=dict(verdicts), warnings=dict(warnings), failure_modes=dict(failures),
                      empty_diff_write_tasks=empty)
    result["not_measured"] = [f"{field} missing for {count} tasks" for field, count in missing.items()]
    if not tasks:
        result["not_measured"].append("no task events in the window")
    return result


def _reviews(tasks: list[dict], plans: dict, source: str, task_events: dict) -> dict:
    result = _section(source + "; plan goals; newest summary text or worker final text/result", reviews=0, measured_texts=0,
                      findings=dict.fromkeys(_SEVERITIES.split("|"), 0),
                      levels=dict.fromkeys(("serious", "moderate", "minor"), 0),
                      verdicts={"READY TO MERGE": 0, "NOT READY": 0, "none": 0}, runs=[],
                      minor_only_or_empty_share=None, ready_share=None)
    minor_only, ready = 0, 0
    for task in tasks:
        identity = (task["run_id"], task["id"])
        plan = plans.get(identity, {})
        if task["policy"] != "read-only" or not (
                task["id"].lower().startswith("review") or "review" in str(plan.get("goal", "")).lower()):
            continue
        result["reviews"] += 1
        text = (task["summary"] or {}).get("text")
        if not isinstance(text, str):
            # Keep full raw text: the dashboard's attempt last_text is truncated.
            for event in task_events.get(identity, []):
                if event.kind == "session":
                    text = None
                elif event.kind == "text" and event.payload.get("text"):
                    text = event.payload["text"]
                elif event.kind == "result" and not isinstance(text, str):
                    text = event.payload.get("text", event.payload.get("result"))
        match = re.match(r"review(\d+)-", task["id"], re.I)
        row = dict(run_id=task["run_id"], task_id=task["id"], round=int(match[1]) if match else None,
                   findings=dict.fromkeys(_SEVERITIES.split("|"), 0),
                   levels=dict.fromkeys(("serious", "moderate", "minor"), 0), verdict="none")
        result["runs"].append(row)
        if not isinstance(text, str):
            result["not_measured"].append(f"review {task['run_id']}/{task['id']} lacks summary text and worker final text/result")
            continue
        result["measured_texts"] += 1
        severity_columns = []
        for line in text.splitlines():
            # One finding per severity per line even if a label is repeated.
            labels = {next(group for group in label.groups() if group).lower()
                      for label in _LABEL.finditer(line)}
            if line.strip().startswith("|") and line.strip().endswith("|"):
                cells = [cell.strip().lower() for cell in line.strip()[1:-1].split("|")]
                if "severity" in cells:
                    severity_columns = [index for index, cell in enumerate(cells) if cell == "severity"]
                else:
                    labels.update(cells[index] for index in severity_columns
                                  if index < len(cells) and cells[index] in _SEVERITY_LEVELS)
            else:
                severity_columns = []
            for label in labels:
                row["findings"][label] += 1
                result["findings"][label] += 1
                level = _SEVERITY_LEVELS[label]
                row["levels"][level] += 1
                result["levels"][level] += 1
        for line in reversed(text.splitlines()):
            if re.search(r"\bNOT READY\b", line, re.I):
                row["verdict"] = "NOT READY"
                break
            if re.search(r"\bREADY TO MERGE\b", line, re.I):
                row["verdict"] = "READY TO MERGE"
                ready += 1
                break
        result["verdicts"][row["verdict"]] += 1
        if not row["levels"]["serious"] and not row["levels"]["moderate"]:
            minor_only += 1
    measured = result["measured_texts"]
    if measured:
        result["minor_only_or_empty_share"] = minor_only / measured
        result["ready_share"] = ready / measured
    else:
        result["not_measured"].append("no review final text in the window")
    return result


def _timing(task_events: dict, cutoff: float, now: float, source: str) -> dict:
    samples: dict = defaultdict(lambda: defaultdict(list))
    metrics = ("worker", "landing", "proof", "slot_wait")
    for events in task_events.values():
        first = events[0]
        results = [event for event in events if event.kind == "result"]
        non_wait = next((event for event in events if event.kind != "slot_wait"), None)
        engine = results[-1].engine if results else non_wait.engine if non_wait else first.engine
        engine = engine or "unknown"
        samples[engine]  # Retain engines even when every interval is incomplete.

        def add(metric: str, start, end):
            if start and end and cutoff <= end.ts <= now and end.ts >= start.ts:
                samples[engine][metric].append(end.ts - start.ts)

        add("worker", first, results[-1] if results else None)
        commits = [event for event in events if event.kind == "commit"]
        commit = commits[-1] if commits else None
        verifies = [event for event in events if event.kind == "verify"]
        verify = verifies[-1] if verifies else None
        proof = next((event for event in events if event.kind == "proof" and verify and event.ts >= verify.ts), None)
        add("landing", results[-1] if results else None, verify or commit)
        add("proof", verify, proof)
        waiting = next((event for event in events if event.kind == "slot_wait"
                        and event.payload.get("state") == "waiting"), None)
        add("slot_wait", waiting, non_wait)
    result = _section(source + "; task event ts (seconds, nearest-rank p90)", by_engine={})
    for engine, values in sorted(samples.items()):
        result["by_engine"][engine] = {}
        for metric in metrics:
            ordered = sorted(values[metric])
            result["by_engine"][engine][metric] = dict(
                count=len(ordered), median=median(ordered) if ordered else None,
                p90=ordered[math.ceil(.9 * len(ordered)) - 1] if ordered else None)
            if not ordered:
                result["not_measured"].append(f"{engine} {metric}: missing paired task timestamps")
    if not samples:
        result["not_measured"].append("no task timestamps in the window")
    return result


def _candidates(data: dict) -> dict:
    scored = []

    def add(section: str, value: float | None, threshold: float, description: str, inclusive=False):
        if value is not None and (value >= threshold if inclusive else value > threshold):
            scored.append((value / threshold, section, f"candidate: {section} — {description}"))

    rate = data["rework"]["rate"]
    add("Rework after merge", rate, .2, f"rework rate {(rate or 0):.1%}")
    share = data["reviews"]["minor_only_or_empty_share"]
    add("Review effectiveness", share, .5, f"minor-only or empty share {(share or 0):.1%}")
    ratio = data["planner"].get("planner_to_worker_ratio")
    add("Planner cost", ratio, 5, f"planner processed tokens per dispatched task {(ratio or 0):.2f}x worker per task")
    for engine, values in data["time"]["by_engine"].items():
        worker, landing = values["worker"]["median"], values["landing"]["median"]
        if worker is not None and landing is not None and landing > worker:
            scored.append((landing / worker if worker else math.inf, "Time breakdown per task",
                           f"candidate: Time breakdown per task — {engine} median landing {landing:g}s "
                           f"> worker {worker:g}s"))
    active = [row for row in data["lessons"]["classes"]
              if not row["key"].startswith("codex_exit:") and not row["stale"]]
    if active:
        row = active[0]
        add("Recurring mistakes", row["count"], 5, f"{row['title']}: {row['count']} occurrences", True)
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    # Several engines can trip timing; one candidate per section keeps it useful.
    selected = {}
    for _, section, line in scored:
        selected.setdefault(section, line)
    result = _section("fixed threshold rules in health.py docstring", lines=list(selected.values())[:3])
    if not any((data["rework"]["rate"] is not None, data["reviews"]["measured_texts"],
                data["planner"]["sessions"], data["time"]["by_engine"], data["lessons"]["classes"])):
        result["not_measured"].append("no measurements available for candidate selection")
    return result


def build(since: float = 30, *, repo: str | Path | None = None, now: float | None = None,
          recent: float = 14) -> dict:
    """Collect evidence without executing tools other than local git reads."""
    now = time.time() if now is None else now
    cutoff = now - since * 86400
    events = lessons._events()
    plans, task_events, summaries = {}, defaultdict(list), {}
    selected = set()
    for event in events:
        if event.ts > now:
            continue
        if event.kind == "plan":
            for task in event.payload.get("tasks") or []:
                if isinstance(task, dict) and task.get("id"):
                    identity = (event.run_id, task["id"])
                    plans[identity] = task
                    if event.ts >= cutoff:
                        selected.add(identity)
        elif event.kind == "summary":
            for task in event.payload.get("tasks") or []:
                if isinstance(task, dict) and task.get("task_id"):
                    identity = (event.run_id, task["task_id"])
                    summaries[identity] = task
                    if event.ts >= cutoff:
                        selected.add(identity)
        elif event.task_id:
            identity = (event.run_id, event.task_id)
            task_events[identity].append(event)
            if event.ts >= cutoff:
                selected.add(identity)
    run_ids = sorted({run_id for run_id, _ in selected})
    tasks, stats = [], []
    for run_id in run_ids:
        view = runs.run_view(run_id)
        if view is None:
            continue
        for task in view["tasks"]:
            identity = (run_id, task["id"])
            if identity in selected:
                tasks.append({**task, "run_id": run_id, "summary": summaries.get(identity)})
            # run_view preserves all result usage counters; run_stats drops
            # Claude cache-write tokens. Sum attempts, then divide by tasks.
            for attempt in task["attempts"]:
                if cutoff <= attempt["ended_at"] <= now:
                    stats.append(dict(run_id=run_id, task_id=task["id"], engine=attempt["engine"],
                                      **(attempt["result"] or {})))
    from hermes_constants import get_hermes_home

    source = f"{Path(get_hermes_home()) / 'control'}/run-*.jsonl"
    data = dict(level="L1", since_days=since, generated_at=now,
                outcomes=_outcomes(tasks, source), rework=_rework(repo, cutoff, now),
                reviews=_reviews(tasks, plans, source, task_events), planner=_planner(cutoff, now, stats),
                time=_timing({key: values for key, values in task_events.items() if key in selected}, cutoff, now, source))
    planned = {key: task for key, task in plans.items() if key in selected}
    reds = {key: task for key, task in planned.items() if task.get("red")}
    verdicts = Counter(str((summaries.get(key) or {}).get("verdict") or "none") for key in reds)
    data["red_green"] = _section(source + "; plan task.red and newest summary verdict", planned=len(planned),
                                 with_red=len(reds), verdicts=dict(verdicts))
    if not planned:
        data["red_green"]["not_measured"].append("no planned tasks in the window")
    if any("red" not in task for task in planned.values()):
        data["red_green"]["not_measured"].append("some plan tasks predate the red field")
    if verdicts["none"]:
        data["red_green"]["not_measured"].append(f"{verdicts['none']} red tasks lack a verdict")
    active = [row for row in lessons.build(since, now=now, recent=recent) if not row["stale"]]
    active.sort(key=lambda row: (row["key"].startswith("codex_exit:"), -row["count"], -row["last_seen"], row["key"]))
    data["lessons"] = _section(source + f"; lessons.build, count >= 2 and last_seen within {recent:g} days",
                               classes=active[:5])
    if not events:
        data["lessons"]["not_measured"].append("no run log events")
    elif not any(cutoff <= event.ts <= now for event in events):
        data["lessons"]["not_measured"].append("no run log events in the window")
    data["candidates"] = _candidates(data)
    return data


def _inline(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def markdown(data: dict) -> str:
    lines = ["# Hermes-Control health (L1, report-only)", "",
             f"Window: last {data['since_days']:g} days. Processed tokens include input, cache read/write and output; "
             "Codex input already includes cache reads. Reasoning tokens are included in output.",
             "Session counters cover sessions started in the window. Recent merges have incomplete seven-day follow-up.", ""]
    for key, title in SECTIONS:
        section = data[key]
        lines += [f"## {title}", "", f"Data source: {section['source']}", ""]
        for field, value in section.items():
            if field in ("source", "not_measured"):
                continue
            if field == "lines":
                lines.extend(f"- {line}" for line in value)
                if not value:
                    lines.append("- candidates: 0 threshold crossings")
            elif field == "classes":
                lines.append(f"- active recurring classes shown: {len(value)}")
                lines.extend(f"- {row['title']}: {row['count']} occurrences; last_seen "
                             f"{datetime.fromtimestamp(row['last_seen'], timezone.utc):%Y-%m-%d}" for row in value)
            elif field == "by_engine":
                lines += _table(["Engine", "Phase", "Tasks", "Median (s)", "p90 (s)"], [
                    [engine, phase, metric["count"], metric["median"], metric["p90"]]
                    for engine, phases in value.items() for phase, metric in phases.items()])
            elif field == "sessions":
                lines.append(f"- planner sessions: {len(value)}")
                columns = ["id", "api_calls", "cache_read_tokens", "cache_write_tokens", "output_tokens",
                           "dispatched_tasks", "planner_processed_tokens_per_dispatched_task",
                           "processed_per_call", "cache_read_per_call", "tool_result_characters_total",
                           "tool_result_characters_per_call", *_LOG_FIELDS]
                lines += _table(["planner session total / dispatched tasks"
                                 if column == "planner_processed_tokens_per_dispatched_task"
                                 else column.replace("_", " ") for column in columns],
                                [[row[column] for column in columns] for row in value])
                lines.extend(f"- session {row['id']} tool-result characters by tool name: "
                             f"{_inline(row['tool_result_characters'])}" for row in value)
            elif field == "runs":
                severities = _SEVERITIES.split("|")
                lines += _table(["Run", "Task", "Round", *severities, "Verdict"], [
                    [row["run_id"], row["task_id"], row["round"],
                     *(row["findings"][severity] for severity in severities), row["verdict"]]
                    for row in value])
            elif field == "totals":
                for metric, number in value.items():
                    label = ("planner session total / dispatched tasks"
                             if metric == "planner_processed_tokens_per_dispatched_task"
                             else "total " + metric.replace("_", " "))
                    lines.append(f"- {label}: {_inline(number)}")
            elif field == "planner_to_worker_ratio":
                lines.append(f"- {field.replace('_', ' ')}: {_inline(value)}")
                lines.append("- per-task ratio (denominators differ: planner counts tasks dispatched "
                             f"in sessions, worker counts run-log tasks): {_inline(value)}")
            elif field == "rate" or field.endswith(("_rate", "_share")):
                lines.append(f"- {field.replace('_', ' ')}: {value:.1%}" if value is not None
                             else f"- {field.replace('_', ' ')}: unavailable")
            else:
                lines.append(f"- {field.replace('_', ' ')}: {_inline(value)}")
        lines.append("")
        lines.extend(f"not measured: {reason}" for reason in section["not_measured"])
        lines.append("")
    return "\n".join(lines)


def _table(columns: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return []

    def cells(values):
        return "| " + " | ".join(_inline(value).replace("|", "\\|").replace("\n", " ")
                                  for value in values) + " |"

    return ["", cells(columns), cells(["---"] * len(columns)), *(cells(row) for row in rows), ""]


def report(since: float = 30, *, repo: str | Path | None = None, as_json: bool = False,
           out: str | Path | None = None, recent: float = 14) -> int:
    data = build(since, repo=repo, recent=recent)
    if out is not None:
        path = Path(out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(data), encoding="utf-8")
    elif as_json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(markdown(data), end="")
    return 0
