"""Arm 0: run an engine's CLI with only the task goal, then commit and grade."""

from __future__ import annotations

import argparse
import json
import os
import signal
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

from bench.arm_controller import _validate_lanes, prepare_repo
from bench.grader import git, grade, snapshot_hidden, snapshot_sentinels
from bench.quota import QuotaGuard, before, after
from bench.results import append_result, short_error
from bench.spec import load_suite, validate_sentinel_paths
from hermes_control import limits
from hermes_control.engines.base import ControlTask
from hermes_control.engines.registry import get_adapter
from hermes_control.policy import ExecPolicy
from hermes_control.worktree import TaskWorktree


def bare_argv(adapter, task, repo):
    argv = list(adapter.build_argv(task, cwd=repo))
    # Claude's builder bundles HC check/report guidance with its permission flags.
    # Keep the flags while sending only the spec goal.
    if task.engine == "claude" and "--append-system-prompt" in argv:
        index = argv.index("--append-system-prompt")
        del argv[index:index + 2]
    return argv


def run_cli(argv, repo, env, transcript, timeout):
    with transcript.open("w", encoding="utf-8") as stream:
        with subprocess.Popen(argv, cwd=repo, env=env, stdin=subprocess.DEVNULL,
                              stdout=stream, stderr=subprocess.STDOUT,
                              start_new_session=True) as process:
            try:
                return process.wait(timeout=timeout), False
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                return process.returncode, True


def terminal_usage(adapter, transcript):
    tokens, answer, report = {}, "", None
    with transcript.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            message = []
            for event in adapter.parse_line(line):
                if event.kind in {"result", "error"}:
                    if event.payload.get("usage") is not None:
                        tokens = {k: v for k, v in (event.payload.get("usage") or {}).items()
                                  if "token" in k and type(v) in (int, float)}
                    answer = event.payload.get("text") or event.payload.get("response") or answer
                elif event.kind == "text":
                    if event.payload.get("text"):
                        message.append(event.payload["text"])
                elif event.kind == "usage" and event.payload.get("rate_limit_info"):
                    report = event.payload["rate_limit_info"]
            if message:
                answer = "".join(message)
    return tokens, answer, report


def engine_error(adapter, transcript):
    with transcript.open(encoding="utf-8", errors="replace") as stream:
        raws = [event for line in stream for event in adapter.parse_line(line)
                if not (event.kind == "error"
                        and event.payload.get("message") == "unparseable line")]
    hit = adapter.limit_hit(raws)
    if hit is not None:
        return hit.message
    errors = [event for event in raws if event.kind == "error"]
    if (errors and errors[-1].raw_kind == "turn.failed"
            and not any(event.kind == "result" for event in raws)):
        return (errors[-1].payload.get("error") or {}).get("message") or ""
    return None


def run_suite(suite, lanes, reps, out, *, dry_run=False, quota_guard=None, timeout=1800):
    specs = load_suite(suite)
    _validate_lanes(lanes)
    if type(reps) is not int or reps < 1:
        raise ValueError("reps: must be positive")
    if timeout <= 0:
        raise ValueError("timeout: must be positive")
    plans = [{"task_id": spec.id, "arm": "0-bare", **lane, "rep": rep}
             for lane in lanes for spec in specs for rep in range(1, reps + 1)]
    if dry_run:
        for plan in plans:
            print(json.dumps(plan))
        return plans
    guard = quota_guard if quota_guard is not None else QuotaGuard()
    bench_repo = Path(__file__).resolve().parents[2]
    bench_sha = git(bench_repo, "rev-parse", "HEAD")
    bench_dirty = bool(git(bench_repo, "status", "--porcelain"))
    records = []
    with tempfile.TemporaryDirectory(prefix="hc-bench-hidden-") as hidden_temp:
        snapshots = {s.id: snapshot_hidden(s.hidden_dir, Path(hidden_temp) / s.id) for s in specs}
        for lane in lanes:
            stopped = False
            for spec in specs:
                for rep in range(1, reps + 1):
                    allowed, quota_before = before(guard, lane)
                    if not allowed:
                        stopped = True
                        break
                    run_id = f"bare-{uuid.uuid4().hex}"
                    transcript = Path(out).resolve().parent / "evidence" / run_id / "cli.jsonl"
                    transcript.parent.mkdir(parents=True, exist_ok=True)
                    started = datetime.now(timezone.utc).isoformat()
                    clock = time.monotonic()
                    with tempfile.TemporaryDirectory(prefix="hc-bench-arm0-") as temp:
                        origin = prepare_repo(spec, Path(temp) / "repo")
                        base = git(origin, "rev-parse", "HEAD")
                        repo = (Path(temp) / "task").resolve()
                        git(origin, "worktree", "add", "--detach", str(repo), base)
                        adapter = get_adapter(lane["engine"])
                        problem = adapter.repository_problem(TaskWorktree(repo, "", origin, base))
                        if problem:
                            raise ValueError(problem)
                        task = ControlTask(id=spec.id, goal=spec.goal,
                                           policy=ExecPolicy(write=spec.policy == "write"), **lane)
                        argv = bare_argv(adapter, task, repo)
                        snapshot = getattr(adapter, "permissions_snapshot", None)
                        gates = []
                        try:
                            settings_before = snapshot() if snapshot is not None else None
                        except OSError:
                            gates.append("guard")
                        worker_clock = time.monotonic()
                        sentinel_before = snapshot_sentinels(
                            validate_sentinel_paths(spec.sentinel_paths, [origin, repo]))
                        if gates:
                            exit_code, timed_out = 127, False
                            transcript.write_text("permissions snapshot failed\n", encoding="utf-8")
                        else:
                            exit_code, timed_out = run_cli(argv, repo, adapter.engine_env(), transcript, timeout)
                            if snapshot is not None:
                                try:
                                    if snapshot() != settings_before:
                                        gates.append("engine-settings-changed")
                                except OSError:
                                    gates.append("guard")
                        worker_s = time.monotonic() - worker_clock
                        if lane["engine"] == "gemini" and not task.policy.write and (
                                git(repo, "status", "--porcelain", "--untracked-files=all") or
                                git(repo, "diff", "--name-only", base, "HEAD")):
                            gates.append("readonly-wrote")
                        tokens, answer, report = terminal_usage(adapter, transcript)
                        if lane["engine"] == "claude" and report:
                            limits.record_claude_report(report, at=time.time())
                        git(repo, "add", "-A")
                        git(repo, "-c", "user.name=hc-bench", "-c", "user.email=bench@localhost",
                            "commit", "--allow-empty", "--author=hc-bench <bench@localhost>",
                            "-m", "hc-bench bare attempt")
                        commit = git(repo, "rev-parse", "HEAD")
                        verdict = grade(spec, repo, commit, base_commit=base, origin_repo=origin, answer=answer,
                                        run_result={"gate_violations": gates},
                                        sentinel_paths=spec.sentinel_paths, sentinel_before=sentinel_before,
                                        transcript_paths=[transcript], hidden_snapshot=snapshots[spec.id])
                        error = engine_error(adapter, transcript) if not timed_out else None
                        if error is not None and not verdict.solved and verdict.status == "graded":
                            verdict.status, verdict.solved = "engine-error", False
                            verdict.reason = short_error(f"engine error: {error}")
                        record = {"bench_sha": bench_sha, "bench_dirty": bench_dirty,
                                  "suite": str(Path(suite).resolve()), "task_id": spec.id,
                                  "arm": "0-bare", **lane, "rep": rep, "run_id": run_id,
                                  "commit": commit, "transcript": str(transcript),
                                  "started": started, "ended": datetime.now(timezone.utc).isoformat(),
                                  "wall_s": time.monotonic() - clock, "worker_s": worker_s,
                                  "tokens": tokens, "verify_exit": None, "verify_green": None,
                                  "cli_exit": exit_code, "timed_out": timed_out, **asdict(verdict),
                                  "quota_before": quota_before, "quota_after": after(guard, lane)}
                        append_result(out, record)
                        records.append(record)
                if stopped:
                    break
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--lanes", required=True, help="JSON list, or path to a JSON file")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--max-5h", type=float, default=90.0)
    parser.add_argument("--max-weekly", type=float, default=80.0)
    parser.add_argument("--reserve", type=float, default=5.0,
                        help="minimum quota reserve in percentage points (default: 5)")
    parser.add_argument("--claude-max-age-min", type=float, default=30.0,
                        help="fail closed when Claude's last report is older (default: 30 min)")
    parser.add_argument("--allow-stale-claude", action="store_true",
                        help="allow old Claude reports; missing usage still stops the run")
    parser.add_argument("--no-quota-guard", action="store_true")
    args = parser.parse_args(argv)
    try:
        lanes = json.loads(args.lanes if args.lanes.lstrip().startswith("[") else
                           Path(args.lanes).read_text(encoding="utf-8"))
        guard = (lambda lane: True) if args.no_quota_guard else QuotaGuard(
            args.max_5h, args.max_weekly, max_age=args.claude_max_age_min * 60,
            allow_stale_claude=args.allow_stale_claude, reserve=args.reserve)
        run_suite(args.suite, lanes, args.reps, args.out, dry_run=args.dry_run,
                  quota_guard=guard, timeout=args.timeout)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
