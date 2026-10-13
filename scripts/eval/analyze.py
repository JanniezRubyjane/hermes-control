"""Join evaluation prompts, control run logs, and read-only planner session metrics."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from hermes_control.model_policy import EnginePolicy

from check_answers import check


def committed_check(prompt_id: str, repo: Path, commit: str,
                    verify_python: str | None = None) -> dict[str, Any]:
    failures = []
    if not commit:
        return {"ok": False, "failures": ["controller summary has no commit"]}
    try:
        archive = subprocess.run(["git", "archive", commit], cwd=repo,
                                 capture_output=True, check=True, timeout=60).stdout
        with tempfile.TemporaryDirectory(prefix="routing-commit-") as directory:
            subprocess.run(["tar", "-x", "-C", directory], input=archive,
                           capture_output=True, check=True, timeout=60)
            if prompt_id == "routine":
                code = ("from shop.pricing import clamp\n"
                        "assert clamp(5,0,10)==5\nassert clamp(-1,0,10)==0\n"
                        "assert clamp(11,0,10)==10\n")
            else:
                code = ("from shop.cart import Cart\nfrom shop import pricing\n"
                        "cart=Cart()\ncart.add(100)\n"
                        "assert cart.checkout(0.1)==90, 'Cart.checkout(0.1) must be 90'\n"
                        "assert pricing.total([100],0.1)==90, 'pricing.total must be 90'\n")
            env = {**os.environ, "PYTHONPATH": directory, "PYTHONDONTWRITEBYTECODE": "1"}
            result = subprocess.run([verify_python or sys.executable, "-B", "-c", code],
                                    cwd=directory, env=env, text=True, capture_output=True,
                                    timeout=60, check=False)
            if result.returncode:
                failures.append(f"committed {prompt_id} behavior failed (exit {result.returncode}): "
                                f"{result.stderr[-2000:]}")
        if prompt_id == "routine":
            roots = subprocess.run(["git", "rev-list", "--max-parents=0", commit], cwd=repo,
                                   text=True, capture_output=True, check=True, timeout=60).stdout.splitlines()
            if len(roots) != 1:
                failures.append("fixture initial commit is ambiguous")
            else:
                diff = subprocess.run(["git", "diff", "--unified=0", roots[0], commit, "--", "tests/"],
                                      cwd=repo, text=True, capture_output=True, check=True,
                                      timeout=60).stdout
                test_file = False
                added_clamp_test = False
                for line in diff.splitlines():
                    if line.startswith("+++ b/"):
                        name = Path(line[6:])
                        test_file = (name.suffix == ".py" and name.name.startswith("test_"))
                    elif test_file and line.startswith("+") and not line.startswith("+++"):
                        if re.search(r"\bassert\b[^\n]*\bclamp\b", line):
                            added_clamp_test = True
                if not added_clamp_test:
                    failures.append("commit adds no test mentioning clamp under tests/")
    except subprocess.TimeoutExpired as exc:
        failures.append(f"committed {prompt_id} check timed out after 60 s: {str(exc.stderr or '')[-2000:]}")
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or ""
        failures.append(f"committed {prompt_id} check failed: {exc}; {str(detail)[-2000:]}")
    return {"ok": not failures, "failures": failures}

# Stronger phrases outrank broad ones. Scores describe the task, not model names
# or insertion order in the live settings.
USE_FOR = {
    "check": [(100, r"quick checks?\b"), (80, r"checks? with a clear answer"),
              (30, r"\bchecks?\b")],
    "routine": [(100, r"default for every write task"), (80, r"routine edits?"),
                (50, r"small mechanical edits?"), (20, r"\bedits?\b")],
    "hard": [(100, r"hard problems?"), (80, r"tricky bugs?"),
             (60, r"hard bugs?"), (30, r"complex")],
    "review": [(100, r"read-only review of a diff"), (80, r"default reviewer"),
               (40, r"\breviews?\b")],
    "risky-review": [(100, r"review of risky diffs?"),
                     (80, r"safety guard"), (30, r"hard bugs? across modules")],
    "summary": [(100, r"\bsummari(?:es|ze|zing)\b"),
                (60, r"explaining code"), (30, r"\bexplain code\b")],
}


def expected_route(key: str, policy: Mapping[str, EnginePolicy],
                   pinned_engine: str | None = None) -> dict[str, Any]:
    candidates = []
    for engine, rules in policy.items():
        if pinned_engine and engine != pinned_engine:
            continue
        for choice in rules.allowed.values():
            scores = []
            for clause in choice.use_for.split(";"):
                if re.search(r"\b(?:never|not|don't|doesn't|only when)\b", clause, re.I):
                    continue
                # A conditional use is not the normal route for this prompt.
                if re.search(r"\b(?:when|after)\b.*\b(?:cap|failed|limit)\b", clause, re.I):
                    continue
                scores.extend(score for score, pattern in USE_FOR[key]
                              if re.search(pattern, clause, re.I))
            if scores:
                candidates.append((max(scores), engine, choice))
    if not candidates:
        return {"unresolved": f"no affirmative use_for match for {key}"}
    best = max(score for score, _, _ in candidates)
    winners = [(engine, choice) for score, engine, choice in candidates if score == best]
    if len(winners) != 1:
        return {"unresolved": "ambiguous: " + ", ".join(
            f"{engine}/{choice.name}" for engine, choice in winners)}
    engine, choice = winners[0]
    return {"engine": engine, "model": choice.name, "effort": choice.effort}


def _events(home: Path) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
    runs = []
    for path in sorted((home / "control").glob("run-*.jsonl")):
        events = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        for event in events:
            if event.get("kind") == "plan":
                runs.append((event, events))
                break
    return runs


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _token(row: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def planner_metrics(home: Path, session_id: str | None) -> dict[str, Any]:
    result = {"api_call_count": None, "cache_read_tokens": None,
              "cache_write_tokens": None, "output_tokens": None, "tool_result_chars": {}}
    db_path = home / "state.db"
    if not session_id or not db_path.is_file():
        return result
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        sc = _columns(db, "sessions")
        if "id" not in sc:
            return result
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row:
            data = dict(row)
            result.update(api_call_count=_token(data, "api_call_count"),
                          cache_read_tokens=_token(data, "cache_read_tokens", "cache_read_input_tokens"),
                          cache_write_tokens=_token(data, "cache_write_tokens", "cache_creation_input_tokens"),
                          output_tokens=_token(data, "output_tokens"))
        mc = _columns(db, "messages")
        if {"session_id", "role", "tool_name"} <= mc:
            body = next((x for x in ("content", "text", "message") if x in mc), None)
            if body:
                totals: dict[str, int] = defaultdict(int)
                for tool, content in db.execute(
                    f"SELECT tool_name, {body} FROM messages WHERE session_id=? AND role='tool'",
                    (session_id,)):
                    totals[tool or "(unknown)"] += len(content or "")
                result["tool_result_chars"] = dict(sorted(totals.items()))
    finally:
        db.close()
    return result


TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens",
              "cache_read_input_tokens", "cache_write_input_tokens",
              "cache_creation_input_tokens", "reasoning_output_tokens")


def _tokens(events: list[dict[str, Any]]) -> dict[str, int]:
    terminals = [event for event in events if event.get("kind") in ("result", "error")
                 and event.get("payload", {}).get("usage")]
    sources = terminals or [event for event in events if event.get("kind") == "usage"]
    total: dict[str, int] = defaultdict(int)
    for event in sources:
        payload = event.get("payload", {})
        usage = payload.get("usage") or payload
        for key in TOKEN_KEYS:
            if type(usage.get(key)) is int:
                total[key] += usage[key]
    return dict(total)


def _seconds(events: list[dict[str, Any]]) -> float | None:
    starts = [e["ts"] for e in events if e.get("kind") == "session"]
    ends = [e["ts"] for e in events if e.get("kind") in ("result", "error")]
    return round(max(ends) - min(starts), 2) if starts and ends else None


def _task_report(task: dict[str, Any], events: list[dict[str, Any]],
                 summaries: list[dict[str, Any]]) -> dict[str, Any]:
    route = {key: task.get(key) for key in ("engine", "model", "effort")}
    attempts = []
    current = dict(route)
    portion: list[dict[str, Any]] = []

    def finish() -> None:
        if portion:
            terminal = next((e for e in reversed(portion)
                             if e.get("kind") in ("result", "error")), None)
            verifies = [e for e in portion if e.get("kind") == "verify"]
            attempts.append({"actual": dict(current), "tokens": _tokens(portion),
                             "worker_seconds": _seconds(portion),
                             "worker_ok": terminal.get("payload", {}).get("ok") if terminal else None,
                             "verify_exit_code": verifies[-1].get("payload", {}).get("exit_code")
                             if verifies else None})

    for event in events:
        if event.get("kind") == "rerouted":
            finish()
            portion = []
            note = event.get("payload", {})
            current = {"engine": event.get("engine"), "model": note.get("model"),
                       "effort": note.get("effort")}
        else:
            portion.append(event)
    finish()
    terminal = next((e for e in reversed(events) if e.get("kind") in ("result", "error")), None)
    summary = summaries[-1] if summaries else None
    ok = summary.get("ok") if summary is not None else (
        terminal.get("payload", {}).get("ok", terminal.get("kind") == "result")
        if terminal else None)
    verifies = [e for e in events if e.get("kind") == "verify"]
    completed_times = [attempt["worker_seconds"] for attempt in attempts
                       if attempt["worker_seconds"] is not None]
    token_total: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for attempt in attempts:
        engine = attempt["actual"].get("engine") or "unknown"
        for key, count in attempt["tokens"].items():
            token_total[engine][key] += count
    return {"task_id": task.get("id"), "planned": route, "actual": current,
            "attempts": attempts, "rerouted": bool(task.get("rerouted")) or
            any(e.get("kind") == "rerouted" for e in events), "ok": ok,
            "verify_exit_code": verifies[-1].get("payload", {}).get("exit_code")
            if verifies else None,
            "commit": summary.get("commit") if summary else None,
            "policy": task.get("policy"), "goal": task.get("goal"),
            "worker_seconds": round(sum(completed_times), 2) if completed_times else None,
            "tokens": {engine: dict(counts) for engine, counts in token_total.items()}}


def _run_report(plan: dict[str, Any], events: list[dict[str, Any]], home: Path) -> dict[str, Any]:
    payload = plan.get("payload", {})
    tasks = []
    for task in payload.get("tasks") or []:
        task_events = [e for e in events if e.get("task_id") == task.get("id")
                       and e.get("kind") != "plan"]
        summaries = [entry for e in events if e.get("kind") == "summary"
                     for entry in e.get("payload", {}).get("tasks", [])
                     if entry.get("task_id") == task.get("id")]
        tasks.append(_task_report(task, task_events, summaries))
    planner = payload.get("planner") or {}
    return {"run_id": plan.get("run_id"), "tasks": tasks,
            "planner_session_id": planner.get("session_id"),
            "planner": planner_metrics(home, planner.get("session_id"))}


def analyze(outdir: Path, *, home: Path | None = None,
            policy: Mapping[str, EnginePolicy] | None = None) -> list[dict[str, Any]]:
    prompts = [json.loads(line) for line in
               (outdir / "prompts.jsonl").read_text(encoding="utf-8").splitlines()]
    fallback_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    homes = [home or Path(prompt.get("evidence_home") or prompt.get("eval_home") or fallback_home)
             for prompt in prompts]
    if policy is None:
        # Import only for a real analysis; --help and offline fixture use no Hermes install.
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        import yaml
        from hermes_control.model_policy import parse_policy
        config = homes[0] / "config.yaml" if homes else fallback_home / "config.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8")) if config.is_file() else {}
        entry = ((((data or {}).get("plugins") or {}).get("entries") or {})
                 .get("hermes_control") or {})
        policy = parse_policy((entry.get("settings") or {}).get("models"))
    runs = [(plan, events, run_home) for run_home in dict.fromkeys(homes)
            for plan, events in _events(run_home)]
    assigned: dict[int, list[tuple[dict[str, Any], list[dict[str, Any]]]]] = defaultdict(list)
    ambiguous: dict[int, list[str]] = defaultdict(list)
    for plan, events, run_home in runs:
        windows = [index for index, prompt in enumerate(prompts)
                   if homes[index] == run_home
                   if plan.get("payload", {}).get("repo") == prompt["repo"]
                   and prompt["started_at"] <= plan.get("ts", -1) <= prompt["ended_at"]]
        if len(windows) == 1:
            assigned[windows[0]].append((plan, events))
        elif len(windows) > 1:
            for index in windows:
                ambiguous[index].append(plan.get("run_id"))
    rows = []
    for index, prompt in enumerate(prompts):
        expected = expected_route(prompt["expect"], policy, prompt.get("engine"))
        reports = [_run_report(plan, events, homes[index]) for plan, events in
                   sorted(assigned[index], key=lambda pair: pair[0]["ts"])]
        tasks = [task for report in reports for task in report["tasks"]]
        tokens: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for task in tasks:
            for engine, counts in task["tokens"].items():
                for key, count in counts.items():
                    tokens[engine][key] += count
        required_policy = "write" if prompt["expect"] in ("routine", "hard") else "read-only"
        is_review_prompt = prompt["expect"] in ("review", "risky-review")
        candidates = [task for task in tasks if task["policy"] == required_policy and
                      (is_review_prompt or not re.search(r"review", str(task["task_id"]) + " " +
                                                    str(task.get("goal") or ""), re.I))]
        main = candidates[0] if len(candidates) == 1 else None
        if len(candidates) != 1:
            route_verdict = f"unresolved ({len(candidates)} eligible {required_policy} tasks)"
        elif not main["attempts"]:
            route_verdict = "unresolved (selected task has no attempt)"
        elif "unresolved" in expected and prompt["id"] != "pinned":
            route_verdict = f"unresolved ({expected['unresolved']})"
        else:
            actual = main["actual"]
            wanted = (prompt.get("engine"),) if prompt["id"] == "pinned" else (expected.get("engine"), expected.get("model"))
            got = (actual.get("engine"),) if prompt["id"] == "pinned" else (actual.get("engine"), actual.get("model"))
            route_verdict = "match" if got == wanted else f"mismatch: {'/'.join(str(x) for x in got)} vs {'/'.join(str(x) for x in wanted)}"
        if prompt["id"] in ("routine", "hard"):
            failures = []
            if main is None:
                failures.append("no write task in matched runs")
            else:
                if main["ok"] is not True:
                    failures.append(f"task {main['task_id']} controller summary ok={main['ok']}")
                if main["verify_exit_code"] != 0:
                    failures.append(f"task {main['task_id']} verify exit={main['verify_exit_code']}")
                failures.extend(committed_check(prompt["id"], Path(prompt["repo"]), main["commit"],
                                                prompt.get("verify_python"))["failures"])
            answer = {"ok": not failures, "failures": failures}
        else:
            answer = check(prompt["id"], prompt.get("answer", ""))
        rows.append({"id": prompt["id"], "expect": prompt["expect"], "expected": expected,
                     "run_ids": [report["run_id"] for report in reports], "runs": reports,
                     "ambiguous_runs": ambiguous[index], "dispatched": bool(reports),
                     "tasks": tasks, "route": route_verdict,
                     "aggregate": {"run_count": len(reports), "task_count": len(tasks),
                                   "ok": all(t["ok"] is True for t in tasks) if tasks else None,
                                   "tokens": {engine: dict(counts) for engine, counts in tokens.items()},
                                   "worker_seconds": round(sum(t["worker_seconds"] or 0
                                                               for t in tasks), 2)},
                     "answer": answer,
                     "hermes_exit_code": prompt.get("returncode")})
    return rows


def table(rows: list[dict[str, Any]]) -> str:
    headers = ["prompt", "expected", "route", "runs", "ambiguous runs", "tasks",
               "planned per task", "actual attempts per task", "ok per task",
               "verify per task", "aggregate ok", "worker s", "tokens by engine",
               "planner calls by session", "planner cache read/write/out by session",
               "tool chars by session and name", "answer"]
    lines = [" | ".join(headers), " | ".join("---" for _ in headers)]
    seen_sessions: set[str] = set()
    for r in rows:
        def route(value: dict[str, Any]) -> str:
            return "/".join(str(value.get(key) or "-") for key in ("engine", "model", "effort"))

        tokens = r["aggregate"]["tokens"]
        expected = (f"unresolved ({r['expected']['unresolved']})"
                    if "unresolved" in r["expected"] else route(r["expected"]))
        sessions = {run.get("planner_session_id") or run["run_id"]: run["planner"]
                    for run in r["runs"] if (run.get("planner_session_id") or run["run_id"])
                    not in seen_sessions}
        seen_sessions.update(sessions)
        planner_calls = ", ".join(f"{session}:{planner['api_call_count']}"
                                   for session, planner in sessions.items()) or "-"
        planner_cache = ", ".join(
            f"{session}:" + "/".join(str(planner[key]) for key in
                                          ("cache_read_tokens", "cache_write_tokens",
                                           "output_tokens")) for session, planner in sessions.items()) or "-"
        tool_chars = ", ".join(
            f"{session}:{name}:{chars}" for session, planner in sessions.items()
            for name, chars in planner["tool_result_chars"].items()) or "-"
        values = [r["id"], expected, r["route"], ", ".join(r["run_ids"]) or "-",
                  ", ".join(r["ambiguous_runs"]) or "-",
                  str(r["aggregate"]["task_count"]),
                  ", ".join(f"{t['task_id']}:{route(t['planned'])}" for t in r["tasks"]) or "-",
                  ", ".join(f"{t['task_id']}:" + (" -> ".join(
                      route(attempt["actual"]) for attempt in t["attempts"]) or "-")
                      for t in r["tasks"]) or "-",
                  ", ".join(f"{t['task_id']}:{t['ok']}" for t in r["tasks"]) or "-",
                  ", ".join(f"{t['task_id']}:{t['verify_exit_code']}" for t in r["tasks"]) or "-",
                  str(r["aggregate"]["ok"]), str(r["aggregate"]["worker_seconds"]),
                  ", ".join(f"{engine}:{counts.get('input_tokens', 0)}/{counts.get('output_tokens', 0)}"
                            for engine, counts in sorted(tokens.items())) or "-",
                  planner_calls, planner_cache, tool_chars,
                  str(r["answer"]["ok"]) + (": " + "; ".join(r["answer"]["failures"])
                                              if r["answer"]["failures"] else "")]
        lines.append(" | ".join(values))
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("outdir", type=Path)
    parser.add_argument("--home", type=Path, help="override recorded evaluation home")
    args = parser.parse_args()
    rows = analyze(args.outdir, home=args.home)
    (args.outdir / "analysis.json").write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
    print(table(rows))


if __name__ == "__main__":
    main()
