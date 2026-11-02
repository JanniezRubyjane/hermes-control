"""Run pinned controller lanes. Live mode spends engine quota; dry-run does not."""

from __future__ import annotations

import argparse
import json
import shutil
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

from bench import builders
from bench.grader import export_checkout, git, grade, snapshot_hidden, snapshot_sentinels
from bench.results import append_result, short_error
from bench.spec import load_suite, validate_sentinel_paths
from bench.quota import QuotaGuard, before, after


def prepare_repo(spec, destination: Path) -> Path:
    if "builder" in spec.repo:
        return builders.build_repo(spec.repo["builder"], destination)
    origin = Path(spec.repo["origin_repo"]).expanduser().resolve()
    commit = git(origin, "rev-parse", "--verify", f"{spec.repo['base_commit']}^{{commit}}")
    export_checkout(origin, commit, destination)
    git(destination, "init", "-b", "main")
    git(destination, "config", "user.name", "hc-bench")
    git(destination, "config", "user.email", "hc-bench@example.invalid")
    git(destination, "add", ".")
    git(destination, "commit", "--allow-empty", "-m", f"Replay fixture {commit}")
    return destination


def read_run_events(path, task_id):
    if not path:
        return []
    events = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("task_id") == task_id:
                    events.append(event)
            except ValueError:
                continue
    return events


def event_metrics(events):
    """Sum terminal usage once per worker invocation, avoiding usage snapshots.

    Spawn-to-terminal timestamps measure worker time; verification is excluded.
    Unknown timings and token counts remain unknown rather than becoming zero.
    """
    tokens = {}
    worker_s = 0.0
    measured = False
    started = None
    for event in events:
        kind, payload = event.get("kind"), event.get("payload") or {}
        if kind == "spawned":
            started = event.get("ts")
        if kind in {"result", "error"}:
            usage = payload.get("usage") or {}
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and "token" in key:
                    tokens[key] = tokens.get(key, 0) + value
            ended = event.get("ts")
            if isinstance(started, (int, float)) and isinstance(ended, (int, float)):
                worker_s += max(0, ended - started)
                measured = True
                started = None
    return {"tokens": tokens, "worker_s": worker_s if measured else None}


def _validate_lanes(lanes):
    if not isinstance(lanes, list) or not lanes:
        raise ValueError("lanes: must be a nonempty JSON list")
    for lane in lanes:
        if not isinstance(lane, dict):
            raise ValueError("lanes: each lane must be an object")
        for name in ("engine", "model", "effort"):
            if not isinstance(lane.get(name), str) or not lane[name].strip():
                raise ValueError(f"lanes.{name}: required nonempty text")


def _move_run_log(log, run_id, out):
    source = Path(log)
    if not run_id.startswith("bench-") or source.name != f"run-{run_id}.jsonl":
        raise ValueError("run log does not match this bench attempt")
    evidence = Path(out).resolve().parent / "evidence"
    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = evidence / f"{run_id}.jsonl"
    # Exclusive creation also protects against races and dangling symlinks.
    writer = destination.open("xb")
    try:
        with writer, source.open("rb") as reader:
            shutil.copyfileobj(reader, writer)
        source.unlink()
    except OSError:
        destination.unlink(missing_ok=True)
        raise
    return destination


def run_suite(suite, lanes, reps, out, *, dry_run=False, quota_guard=None,
              sentinel_paths=(), transcript_paths=(), keep_run_logs=False) -> list[dict]:
    specs = load_suite(suite)
    _validate_lanes(lanes)
    if type(reps) is not int or reps < 1:
        raise ValueError("reps: must be positive")
    plans = [{"task_id": spec.id, "arm": "1-controller", **lane, "rep": rep}
             for lane in lanes for spec in specs for rep in range(1, reps + 1)]
    if dry_run:
        for plan in plans:
            print(json.dumps(plan))
        return plans
    from hermes_control import dispatch

    guard = quota_guard if quota_guard is not None else QuotaGuard()
    bench_repo = Path(__file__).resolve().parents[2]
    bench_sha = git(bench_repo, "rev-parse", "HEAD")
    bench_dirty = bool(git(bench_repo, "status", "--porcelain"))
    with tempfile.TemporaryDirectory(prefix="hc-bench-hidden-") as hidden_temp:
        snapshots = {spec.id: snapshot_hidden(spec.hidden_dir, Path(hidden_temp) / spec.id)
                     for spec in specs}
        records = []
        for lane in lanes:
            stopped = False
            for spec in specs:
                for rep in range(1, reps + 1):
                    allowed, quota_before = before(guard, lane)
                    if not allowed:
                        stopped = True
                        break
                    run_id = f"bench-{uuid.uuid4().hex[:16]}"
                    started = datetime.now(timezone.utc).isoformat()
                    clock = time.monotonic()
                    with tempfile.TemporaryDirectory(prefix="hc-bench-arm1-") as temp:
                        repo = prepare_repo(spec, Path(temp) / "repo")
                        if spec.hidden_dir.is_relative_to(repo):
                            raise ValueError("hidden_dir: must be outside the task repo")
                        base = git(repo, "rev-parse", "HEAD")
                        watched = list(dict.fromkeys([*spec.sentinel_paths, *sentinel_paths]))
                        watched = validate_sentinel_paths(watched, [repo])
                        sentinel_before = snapshot_sentinels(watched)
                        task = {"id": spec.id, "goal": spec.goal, "policy": spec.policy,
                                "verify": spec.verify, "why": "hc-bench arm 1 lane",
                                **{k: lane[k] for k in ("engine", "model", "effort")}}
                        result = dispatch.dispatch([task], parent_cwd=str(repo), run_id=run_id)
                        log = result.get("run_log")
                        record = None
                        try:
                            entries = result.get("tasks") or []
                            entry = next((e for e in entries if e.get("task_id") == spec.id), {})
                            evidence_error = None
                            try:
                                if not log:
                                    raise ValueError("missing run log")
                                events = read_run_events(log, spec.id)
                            except (OSError, ValueError) as exc:
                                events = []
                                evidence_error = short_error(f"missing or unreadable run log: {exc}")
                            if result.get("error"):
                                evidence_error = short_error(f"dispatch error: {result['error']}")
                                print(evidence_error, flush=True)
                            metrics = event_metrics(events)
                            proof = entry.get("verify") or {}
                            transcripts = list(transcript_paths) + ([log] if log else [])
                            commit = entry.get("commit") or (base if spec.policy == "read-only" else "")
                            worker_repo = Path(entry.get("worktree") or repo)
                            verdict = grade(spec, worker_repo, commit, base_commit=base, origin_repo=repo,
                                            answer=entry.get("text", ""),
                                            run_result={**result, **entry}, sentinel_paths=watched,
                                            sentinel_before=sentinel_before, transcript_paths=transcripts,
                                            hidden_snapshot=snapshots[spec.id])
                            if evidence_error and verdict.status != "void":
                                verdict.solved, verdict.status = False, "not-graded"
                                verdict.reason = evidence_error
                            if entry.get("engine", lane["engine"]) != lane["engine"] or any(
                                    entry.get(k, lane[k]) != lane[k] for k in ("model", "effort")):
                                verdict.solved, verdict.status = False, "void"
                                verdict.reason = "controller changed the pinned lane"
                            record = {"bench_sha": bench_sha, "bench_dirty": bench_dirty,
                                      "suite": str(Path(suite).resolve()),
                                      "task_id": spec.id, "arm": "1-controller", **lane, "rep": rep,
                                      "started": started, "ended": datetime.now(timezone.utc).isoformat(),
                                      "wall_s": time.monotonic() - clock, **metrics,
                                      "verify_exit": proof.get("exit_code"),
                                      "verify_green": proof.get("passed") is True and proof.get("exit_code") == 0,
                                      **asdict(verdict), "run_id": run_id, "run_log": log}
                            record["quota_before"] = quota_before
                            record["quota_after"] = after(guard, lane)
                        finally:
                            if not keep_run_logs and log:
                                try:
                                    moved_log = str(_move_run_log(log, run_id, out))
                                except (OSError, ValueError) as exc:
                                    if record is not None:
                                        record["run_log_move_error"] = str(exc)
                                else:
                                    if record is not None:
                                        record["run_log"] = moved_log
                                    lock_errors = []
                                    for suffix in ("live.lock", "summary.lock"):
                                        try:
                                            Path(log).with_name(f"run-{run_id}.{suffix}").unlink(
                                                missing_ok=True)
                                        except OSError as exc:
                                            lock_errors.append(str(exc))
                                    if lock_errors and record is not None:
                                        record["run_lock_cleanup_error"] = "; ".join(lock_errors)
                        append_result(out, record)
                        records.append(record)
                if stopped:
                    break
        return records


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, required=True)
    parser.add_argument("--lanes", required=True, help="JSON list, or path to a JSON file")
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-run-logs", action="store_true",
                        help="leave controller logs and locks in the live Hermes home")
    parser.add_argument("--no-quota-guard", action="store_true",
                        help="explicitly allow live runs without a quota stop")
    parser.add_argument("--max-5h", type=float, default=90.0)
    parser.add_argument("--max-weekly", type=float, default=80.0)
    parser.add_argument("--reserve", type=float, default=5.0,
                        help="minimum quota reserve in percentage points (default: 5)")
    parser.add_argument("--claude-max-age-min", type=float, default=30.0,
                        help="fail closed when Claude's last report is older (default: 30 min)")
    parser.add_argument("--allow-stale-claude", action="store_true",
                        help="allow old Claude reports; missing usage still stops the run")
    args = parser.parse_args(argv)
    try:
        lanes = json.loads(args.lanes if args.lanes.lstrip().startswith("[") else
                           Path(args.lanes).read_text(encoding="utf-8"))
        guard = (lambda lane: True) if args.no_quota_guard else QuotaGuard(
            args.max_5h, args.max_weekly, max_age=args.claude_max_age_min * 60,
            allow_stale_claude=args.allow_stale_claude, reserve=args.reserve)
        run_suite(args.suite, lanes, args.reps, args.out, dry_run=args.dry_run, quota_guard=guard,
                  keep_run_logs=args.keep_run_logs)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
