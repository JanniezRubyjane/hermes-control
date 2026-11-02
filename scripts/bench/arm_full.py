"""Arm 2: grade the full Hermes planner and its controller tasks.

Only dispatched Claude workers report usage. The planner's own Claude use is
not reported by any reader, so even a fresh worker report can undercount it.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
# The eval scripts also support standalone execution and use sibling imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))

from eval.run import create_eval_home, final_answer
from eval.analyze import _events, planner_metrics
from bench.arm_controller import prepare_repo, event_metrics
from bench.grader import git, git_argv, git_env, grade, snapshot_hidden, snapshot_sentinels
from bench.quota import QuotaGuard, before, after
from bench.results import append_result, short_error
from bench.spec import load_suite, validate_sentinel_paths
from hermes_control import limits, proctree
from hermes_control.runlog import run_log_dir
from hermes_control.engines.registry import ENGINE_NAMES


def run_hermes(command, repo, home, timeout):
    with subprocess.Popen(
            command, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, text=True, start_new_session=True,
            env={**os.environ, "HERMES_HOME": str(home),
                 "HERMES_DISABLE_LAZY_INSTALLS": "1"}) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
            return process.returncode, stdout, stderr, False
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stop_spawned_workers(home)
            stdout, stderr = process.communicate()
            return process.returncode, stdout, stderr, True


def stop_spawned_workers(home):
    """Stop logged engine sessions while the eval profile still exists."""
    groups = set()
    for path in (home / "control").glob("run-*.jsonl"):
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue  # A killed writer may leave a partial final event.
                if not isinstance(event, dict) or event.get("kind") != "spawned":
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                pid, started = payload.get("pid"), payload.get("started")
                if type(pid) is int and pid > 1 and isinstance(started, (str, type(None))):
                    groups.add((pid, started))
    def signal_alive(sig):
        alive = False
        for pid, started in groups:
            if proctree.group_still_running(pid, started) is True:
                try:
                    os.killpg(pid, sig)
                    alive = True
                except ProcessLookupError:
                    pass
        return alive

    if signal_alive(signal.SIGTERM):
        time.sleep(0.3)
        signal_alive(signal.SIGKILL)


def copy_claude_report(home, target):
    """Publish a newer worker report before destroying the eval profile."""
    def read(path):
        try:
            data = path.read_bytes()
            report = json.loads(data)
            at = report["at"]
            if (type(at) not in (int, float) or not math.isfinite(at)
                    or not isinstance(report["info"], dict)):
                return None
            return at, data
        except (OSError, ValueError, KeyError, TypeError):
            return None

    incoming = read(home / "control" / "usage-claude.json")
    current = read(target)
    if incoming is None or (current is not None and incoming[0] <= current[0]):
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(incoming[1])
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    limits.forget("claude")
    return True


def grading_commit(repo, base, writes):
    """Merge available commits as objects, without checking out task files."""
    identity = {**git_env(), "GIT_AUTHOR_NAME": "hc-bench",
                "GIT_AUTHOR_EMAIL": "bench@localhost", "GIT_COMMITTER_NAME": "hc-bench",
                "GIT_COMMITTER_EMAIL": "bench@localhost"}
    merged, head = [], base
    for task in writes:
        if not task.get("commit"):
            continue
        commit = git(repo, "rev-parse", "--verify", f"{task['commit']}^{{commit}}")
        result = subprocess.run(
            git_argv(repo, "merge-tree", "--write-tree", head, commit),
            env=identity, capture_output=True, text=True)
        if result.returncode == 1:
            return "", [*merged, task["id"]]
        result.check_returncode()
        tree = result.stdout.splitlines()[0]
        head = subprocess.run(
            git_argv(repo, "commit-tree", tree, "-p", head, "-p", commit,
                     "-m", "hc-bench planner tasks"),
            env=identity, capture_output=True, text=True, check=True).stdout.strip()
        merged.append(task["id"])
    return (head if merged else ""), []


def collect_runs(home):
    """Keep planned order, including a final write task that failed to commit."""
    runs = sorted(_events(home), key=lambda pair: pair[0].get("ts", 0))
    tasks, run_ids, all_events, sessions = [], [], [], []
    for plan, events in runs:
        run_id = plan.get("run_id")
        run_ids.append(run_id)
        all_events.extend(events)
        payload = plan.get("payload") or {}
        session = (payload.get("planner") or {}).get("session_id")
        if session and session not in sessions:
            sessions.append(session)
        summaries = {entry["task_id"]: entry for event in events if event.get("kind") == "summary"
                     for entry in (event.get("payload") or {}).get("tasks", [])}
        for task in payload.get("tasks") or []:
            tasks.append({**task, **summaries.get(task["id"], {}), "run_id": run_id})
    return tasks, run_ids, all_events, sessions


def planner_dispatch_error(home, sessions):
    """Read refused dispatch results from the same session rows as planner metrics."""
    path = home / "state.db"
    if not path.is_file():
        return None
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        columns = {row[1] for row in db.execute("PRAGMA table_info(messages)")}
        if not {"session_id", "role", "tool_name"} <= columns:
            return None
        body = next((name for name in ("content", "text", "message") if name in columns), None)
        if body is None:
            return None
        query = f"SELECT {body} FROM messages WHERE role='tool' AND tool_name='control_dispatch'"
        params = ()
        if sessions:
            query += " AND session_id IN (" + ",".join("?" for _ in sessions) + ")"
            params = tuple(sessions)
        query += " ORDER BY rowid"
        error = None
        for (content,) in db.execute(query, params):
            try:
                result = json.loads(content)
            except (ValueError, TypeError):
                continue
            if isinstance(result, dict) and result.get("error"):
                error = result["error"]
        return error
    except sqlite3.Error:
        return None
    finally:
        if db is not None:
            db.close()


def planner_totals(home, sessions):
    readings = [planner_metrics(home, session) for session in sessions or [None]]
    result = {}
    for key in ("api_call_count", "cache_read_tokens", "cache_write_tokens", "output_tokens"):
        known = [r[key] for r in readings if r[key] is not None]
        result[key] = sum(known) if known else None
    chars = {}
    for reading in readings:
        for tool, count in reading["tool_result_chars"].items():
            chars[tool] = chars.get(tool, 0) + count
    return {**result, "tool_result_chars": chars}


def run_suite(suite, reps, out, *, dry_run=False, quota_guard=None, timeout=1800):
    specs = load_suite(suite)
    if type(reps) is not int or reps < 1:
        raise ValueError("reps: must be positive")
    if timeout <= 0:
        raise ValueError("timeout: must be positive")
    plans = [{"task_id": s.id, "arm": "2-full", "rep": rep, "request": s.request}
             for s in specs for rep in range(1, reps + 1)]
    if dry_run:
        for plan in plans:
            print(json.dumps(plan))
        return plans
    guard = quota_guard if quota_guard is not None else QuotaGuard()
    # Planner Claude use is unreported; these checks use worker reports only.
    lanes = [{"engine": e} for e in dict.fromkeys(("claude", *ENGINE_NAMES))]
    report_target = run_log_dir() / "usage-claude.json"
    source = report_target.parent.parent.resolve()
    bench_repo = Path(__file__).resolve().parents[2]
    bench_sha = git(bench_repo, "rev-parse", "HEAD")
    bench_dirty = bool(git(bench_repo, "status", "--porcelain"))
    records = []
    with tempfile.TemporaryDirectory(prefix="hc-bench-hidden-") as hidden_temp:
        snapshots = {s.id: snapshot_hidden(s.hidden_dir, Path(hidden_temp) / s.id) for s in specs}
        for spec in specs:
            for rep in range(1, reps + 1):
                quota_before, allowed = {}, True
                for lane in lanes:
                    ok, windows = before(guard, lane)
                    allowed = allowed and ok
                    quota_before.update({f"{lane['engine']}:{label}": used for label, used in windows.items()})
                if not allowed:
                    return records
                attempt_id = f"full-{uuid.uuid4().hex}"
                evidence = Path(out).resolve().parent / "evidence" / attempt_id
                evidence.mkdir(parents=True)
                started = datetime.now(timezone.utc).isoformat()
                clock, started_epoch = time.monotonic(), time.time()
                with tempfile.TemporaryDirectory(prefix="hc-bench-arm2-") as temp:
                    repo = prepare_repo(spec, Path(temp) / "repo")
                    base = git(repo, "rev-parse", "HEAD")
                    home = create_eval_home(source, evidence, plugin_root=bench_repo)
                    print(f"eval profile: {home}", flush=True)
                    sentinel_before = snapshot_sentinels(
                        validate_sentinel_paths(spec.sentinel_paths, [repo]))
                    try:
                        command = ["hermes", "chat", "-q", spec.request, "--oneshot", "--in", str(repo)]
                        try:
                            cli_exit, stdout, stderr, timed_out = run_hermes(command, repo, home, timeout)
                        except OSError:
                            cli_exit, stdout, stderr, timed_out = 127, "", "hermes could not start", False
                        finally:
                            copy_claude_report(home, report_target)
                        stdout = stdout.decode(errors="replace") if isinstance(stdout, bytes) else stdout
                        stderr = stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr
                        transcript = evidence / "hermes.txt"
                        transcript.write_text(stdout + "\n" + stderr, encoding="utf-8")
                        tasks, run_ids, events, sessions = collect_runs(home)
                        footers = re.findall(r"(?m)^Session:[ \t]*(\S+)[ \t]*$", stdout)
                        if footers and footers[-1] not in sessions:
                            sessions.append(footers[-1])
                        planner = planner_totals(home, sessions)
                        logs = []
                        for path in sorted((home / "control").glob("run-*.jsonl")):
                            target = evidence / path.name
                            shutil.copy2(path, target)
                            logs.append(target)
                        writes = [t for t in tasks if t.get("policy") == "write"]
                        last = writes[-1] if writes else {}
                        missing_commits = [t["id"] for t in writes if not t.get("commit")]
                        merge_error = None
                        try:
                            commit, conflicts = grading_commit(repo, base, writes)
                        except (OSError, subprocess.SubprocessError) as exc:
                            commit, conflicts = "", []
                            merge_error = str(getattr(exc, "stderr", None) or exc)
                            (evidence / "merge-error.txt").write_text(merge_error, encoding="utf-8")
                        if spec.policy == "read-only" and not writes:
                            commit = base
                        proof = last.get("verify") or {}
                        answer = final_answer(home, stdout, started_epoch, time.time())
                        if spec.policy == "read-only":
                            reads = [t for t in tasks if t.get("policy") == "read-only"]
                            last = reads[-1] if reads else {}
                            if last:
                                answer = last.get("text", "")
                            else:
                                # Oneshot output may have no session database.
                                clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", stdout)
                                boxes = re.findall(r"╭─ ☤ Hermes[^\n]*\n(.*?)^╰[^\n]*",
                                                   clean, flags=re.M | re.S)
                                if boxes:
                                    answer = "\n".join(line.removeprefix("│").removesuffix("│").strip()
                                                       for line in boxes[-1].splitlines()).strip()
                            if "text" not in last and last:
                                answer = next((e.get("payload", {}).get("text", "")
                                               for e in reversed(events) if e.get("kind") == "result"
                                               and e.get("run_id") == last["run_id"]
                                               and e.get("task_id") == last["id"]), "")
                        verdict = grade(spec, repo, commit, base_commit=base, answer=answer,
                                        run_result=last, transcript_paths=[transcript, *logs],
                                        sentinel_paths=spec.sentinel_paths, sentinel_before=sentinel_before,
                                        hidden_snapshot=snapshots[spec.id])
                        if conflicts and verdict.status != "void":
                            verdict.solved, verdict.status = False, "not-graded"
                            verdict.reason = "planner tasks conflict"
                        if merge_error and verdict.status != "void":
                            verdict.solved, verdict.status = False, "not-graded"
                            verdict.reason = short_error(f"planner tasks merge failed: {merge_error}")
                        dispatch_error = (planner_dispatch_error(home, sessions)
                                          if not run_ids or not writes else None)
                        if dispatch_error:
                            (evidence / "dispatch-error.json").write_text(
                                json.dumps({"error": dispatch_error}), encoding="utf-8")
                        if not run_ids and verdict.status != "void":
                            verdict.solved, verdict.status = False, "not-graded"
                            verdict.reason = "missing controller run logs"
                        if dispatch_error and not verdict.solved and verdict.status != "void":
                            verdict.solved, verdict.status = False, "not-graded"
                            verdict.reason = short_error(f"dispatch error: {dispatch_error}")
                        tokens, worker_times = {}, []
                        for task in tasks:
                            metrics = event_metrics([e for e in events if e.get("run_id") == task["run_id"]
                                                     and e.get("task_id") == task["id"]])
                            for key, value in metrics["tokens"].items():
                                tokens[key] = tokens.get(key, 0) + value
                            if metrics["worker_s"] is not None:
                                worker_times.append(metrics["worker_s"])
                        quota_after = {}
                        for lane in lanes:
                            quota_after.update({f"{lane['engine']}:{label}": used
                                                for label, used in after(guard, lane).items()})
                        record = {"bench_sha": bench_sha, "bench_dirty": bench_dirty,
                                  "suite": str(Path(suite).resolve()), "task_id": spec.id,
                                  "arm": "2-full", "engine": "claude", "model": None, "effort": None,
                                  "rep": rep, "run_id": run_ids[-1] if run_ids else None,
                                  "run_ids": run_ids, "tasks_dispatched": len(tasks),
                                  "write_tasks": len(writes), "conflicting_task_ids": conflicts,
                                  "missing_commits": missing_commits,
                                  "task_commits": [{"task_id": t["id"], "run_id": t["run_id"],
                                                    "commit": t.get("commit")} for t in writes],
                                  "commit": commit, "planner": planner, "planner_session_ids": sessions,
                                  "eval_home_deleted": str(home), "transcript": str(transcript),
                                  "run_logs": [str(p) for p in logs], "started": started,
                                  "ended": datetime.now(timezone.utc).isoformat(),
                                  "wall_s": time.monotonic() - clock,
                                  "worker_s": sum(worker_times) if worker_times else None,
                                  "tokens": tokens, "verify_exit": proof.get("exit_code"),
                                  "verify_green": proof.get("passed") is True and proof.get("exit_code") == 0,
                                  "cli_exit": cli_exit, "timed_out": timed_out, **asdict(verdict),
                                  "quota_before": quota_before, "quota_after": quota_after}
                        append_result(out, record)
                        records.append(record)
                    finally:
                        shutil.rmtree(home)
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--max-5h", type=float, default=90.0)
    parser.add_argument("--max-weekly", type=float, default=80.0)
    parser.add_argument("--reserve", type=float, default=5.0,
                        help="minimum quota reserve in percentage points (default: 5)")
    parser.add_argument("--claude-max-age-min", type=float, default=30.0,
                        help="fail closed when Claude's last worker report is older (default: 30 min); "
                             "the planner's own Claude use is not reported by any reader")
    parser.add_argument("--allow-stale-claude", action="store_true",
                        help="allow old Claude reports; missing usage still stops the run")
    parser.add_argument("--no-quota-guard", action="store_true")
    args = parser.parse_args(argv)
    try:
        guard = (lambda lane: True) if args.no_quota_guard else QuotaGuard(
            args.max_5h, args.max_weekly, max_age=args.claude_max_age_min * 60,
            allow_stale_claude=args.allow_stale_claude, reserve=args.reserve)
        run_suite(args.suite, args.reps, args.out, dry_run=args.dry_run,
                  quota_guard=guard, timeout=args.timeout)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
