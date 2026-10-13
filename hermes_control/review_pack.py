"""Read-only, complete evidence digest for one Hermes-Control run."""

from __future__ import annotations

import re
import subprocess
import shlex
from pathlib import Path
from typing import Any

from . import runs
from .events import ControlEvent
from .runlog import RunLog, _redact, run_log_path
from .worktree import _run_git
from .worker_report import parse as parse_worker_report, parse_findings


_COST_KEYS = ("tools", "tool_errors", "seconds", "input_tokens", "cached_input_tokens",
              "output_tokens", "reasoning_tokens")


def _git(repo: Path, *args: str) -> str:
    try:
        result = _run_git(args, cwd=str(repo))
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(str(exc)) from exc
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def _git_evidence(repo_name: str | None, task: dict[str, Any], *, include_diff: bool
                  ) -> dict[str, Any]:
    evidence: dict[str, Any] = {"branch": task["branch"],
                                "commit": (task["commit"] or {}).get("sha"),
                                "worktree": task["worktree"], "base": None,
                                "base_sha": None, "base_source": None,
                                "head_is_ancestor": None, "diff_stat": None,
                                "added_lines": None, "removed_lines": None}
    if include_diff:
        evidence["diff"] = None
    if not repo_name or not Path(repo_name).is_dir():
        evidence["git_error"] = f"repo gone: {repo_name or '(not recorded)'}"
        return evidence
    if not task["branch"]:
        evidence["git_error"] = "branch gone: (not recorded)"
        return evidence
    if not evidence["commit"]:
        evidence["git_error"] = "commit not recorded"
        return evidence
    repo = Path(repo_name)
    branch_ref = f"refs/heads/{task['branch']}"
    try:
        try:
            _git(repo, "rev-parse", "--verify", branch_ref)
        except ValueError:
            evidence["git_error"] = f"branch gone: {task['branch']}"
            return evidence
        _git(repo, "rev-parse", "--verify", f"{evidence['commit']}^{{commit}}")
        recorded_base = (task["summary"] or {}).get("base_commit")
        evidence["base_source"] = "recorded" if recorded_base else "merge-base"
        base = recorded_base or _git(repo, "merge-base", branch_ref, "HEAD").strip()
        evidence["base"] = base[:7]
        evidence["base_sha"] = base
        try:
            ancestor = _run_git(["merge-base", "--is-ancestor", "HEAD", branch_ref], cwd=str(repo))
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(str(exc)) from exc
        if ancestor.returncode not in (0, 1):
            raise ValueError(ancestor.stderr.strip() or "git merge-base --is-ancestor failed")
        evidence["head_is_ancestor"] = ancestor.returncode == 0
        span = f"{base}..{evidence['commit']}"
        evidence["diff_stat"] = _git(repo, "diff", "--no-ext-diff", "--no-textconv",
                                      span, "--stat").rstrip("\n")
        numstat = _git(repo, "diff", "--no-ext-diff", "--no-textconv", span, "--numstat")
        evidence["added_lines"] = sum(int(fields[0]) for line in numstat.splitlines()
                                       if (fields := line.split("\t", 2))[0].isdigit())
        evidence["removed_lines"] = sum(int(fields[1]) for line in numstat.splitlines()
                                         if (fields := line.split("\t", 2))[1].isdigit())
        if include_diff:
            evidence["diff"] = _git(repo, "diff", "--no-ext-diff", "--no-textconv", span)
    except (ValueError, RuntimeError) as exc:
        evidence["git_error"] = str(exc)
    return evidence


def _raw_evidence(events: list[ControlEvent]) -> tuple[dict[str, str], dict[str, list[dict[str, str]]],
                                         dict[str, int]]:
    final_text: dict[str, str] = {}
    errors: dict[str, list[dict[str, str]]] = {}
    omitted: dict[str, int] = {}
    names: dict[str, dict[Any, str]] = {}
    for event in events:
        if not event.task_id:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        task_id = event.task_id
        if event.kind == "session":
            final_text[task_id] = ""
        elif event.kind == "text" and payload.get("text"):
            final_text[task_id] = payload["text"]
        elif event.kind == "tool_start":
            names.setdefault(task_id, {})[payload.get("tool_id")] = str(payload.get("name") or "tool")
        elif event.kind == "tool_end" and payload.get("is_error"):
            content = payload.get("stderr") or payload.get("content") or payload.get("error") or ""
            if isinstance(content, list):
                content = "\n".join(str(item.get("text", item)) if isinstance(item, dict)
                                    else str(item) for item in content)
            entry = {"name": names.get(task_id, {}).get(payload.get("tool_id"), "tool"),
                     "error": str(content)[:300]}
            listed = errors.setdefault(task_id, [])
            if len(listed) < 10:
                listed.append(entry)
            else:
                omitted[task_id] = omitted.get(task_id, 0) + 1
    return final_text, errors, omitted


def _unbacked_claims(report: dict | None, events: list[ControlEvent], task_id: str
                     ) -> list[str]:
    if report is None:
        return []
    commands = []
    for event in events:
        if event.task_id != task_id or event.kind != "tool_start":
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        tool_input = payload.get("input")
        if isinstance(tool_input, dict) and isinstance(tool_input.get("command"), str):
            commands.append(tool_input["command"])
    def tokens(value: str) -> list[str]:
        normalized = " ".join(value.split())
        try:
            return shlex.split(normalized)
        except ValueError:
            return normalized.split()

    def matches(claim: str, command: str) -> bool:
        wanted = tokens(claim)
        if not wanted:
            return False
        wanted[-1] = re.sub(r"(?:;|&&|\|\|)+$", "", wanted[-1])
        if not wanted[-1]:
            wanted.pop()
        if not wanted:
            return False
        command_tokens = tokens(command)
        streams = [command_tokens]
        # Shell -c wraps the command being claimed in one quoted argument.
        streams.extend(tokens(command_tokens[index + 1])
                       for index, token in enumerate(command_tokens[:-1])
                       if token in {"-c", "-lc"})
        return any(stream[index:index + len(wanted)] == wanted
                   for stream in streams
                   for index in range(len(stream) - len(wanted) + 1))

    return [claim["command"] for claim in report["ran"]
            if not any(matches(claim["command"], command) for command in commands)]


def build(run_id: str, *, include_diff: bool = False) -> dict[str, Any] | None:
    view = runs.run_view(run_id)
    if view is None:
        return None
    events = RunLog(run_id).read_all()
    plans = {task["id"]: task for event in events
             if event.kind == "plan" and isinstance(event.payload, dict)
             for task in event.payload.get("tasks") or []
             if isinstance(task, dict) and task.get("id")}
    parallel = next((event.payload.get("parallel") for event in events
                     if event.kind == "plan" and isinstance(event.payload, dict)), None)
    if not (isinstance(parallel, dict) and isinstance(parallel.get("max_tasks"), int)
            and isinstance(parallel.get("per_engine"), dict)):
        parallel = None
    final_text, errors, omitted = _raw_evidence(events)
    stats = runs.run_stats(run_ids=[run_id])
    rows: dict[str, list[dict[str, Any]]] = {}
    for row in stats:
        rows.setdefault(row["task_id"], []).append({
            **{key: row[key] for key in _COST_KEYS},
            "cost_usd": row["cost_usd"] if row["cost_usd"] is not None
            else "quota" if row["engine"] == "codex" else "-"})
    tasks = []
    for task in view["tasks"]:
        last = task["attempts"][-1] if task["attempts"] else None
        verify = task["verify"]
        if verify is not None:
            output = _redact(verify.get("output_tail") or "")
            if verify.get("passed"):
                output = "".join(output.splitlines(keepends=True)[-3:])
            verify = {"command": verify.get("command"), "passed": verify.get("passed"),
                      "exit_code": verify.get("exit_code"), "output_tail": output}
        reroute = task["rerouted"] or {}
        summary = task["summary"] or {}
        proof = summary.get("proof") or next((event.payload for event in reversed(events)
                                             if event.task_id == task["id"] and event.kind == "proof"), None)
        proof = _redact(proof)
        item = {"id": task["id"], "status": task["status"],
                "status_reason": task["status_reason"], "why": task["why"],
                "planned_engine": task["planned_engine"], "planned_model": task["model"],
                "actual_engine": last["engine"] if last else None,
                "actual_model": last["model"] if last else None,
                "actual_effort": last["effort"] if last else None,
                "moved": bool(reroute or any(attempt["engine"] != task["planned_engine"]
                                              for attempt in task["attempts"])),
                "rerouted_reason": reroute.get("reason"),
                "rerouted": task["rerouted"], "limits": task["limits"],
                **_git_evidence(view["repo"], task, include_diff=include_diff),
                "verify": verify,
                "verdict": summary.get("verdict") or (proof or {}).get("verdict"),
                **({"proof": proof} if proof else {}),
                **({"warnings": summary["warnings"]} if summary.get("warnings") else {}),
                "worker_final_text": (summary["text"] if "text" in summary
                                      else final_text.get(task["id"], "")),
                "worker_final_text_source": ("result" if "text" in summary
                                             else "last text event (older log)"),
                "unproven_reason": ((task["verify"] or {}).get("unproven_reason")
                                    or (task["summary"] or {}).get("unproven_reason")),
                **({"failure_mode": summary["failure_mode"]} if summary.get("failure_mode") else {}),
                "attempts": rows.get(task["id"], []),
                "tool_errors": errors.get(task["id"], []),
                "tool_errors_omitted": omitted.get(task["id"], 0),
                "paused": ({key: (task["paused"] or {}).get(key)
                            for key in ("id", "reason", "resets_at")}
                           if task["paused"] else None)}
        if task.get("policy") == "read-only" and (
                task["id"].lower().startswith("review")
                or "review" in str(plans.get(task["id"], {}).get("goal", "")).lower()):
            item["review_findings"] = parse_findings(item["worker_final_text"])
        report = parse_worker_report(item["worker_final_text"])
        item["worker_report"] = report if report is not None else "no report"
        item["unbacked_claims"] = _unbacked_claims(report, events, task["id"])
        tasks.append(item)
    return {"run_id": run_id, "state": view["state"], "repo": view["repo"],
            "parallel": parallel,
            "run_log": str(run_log_path(run_id)), "tasks": tasks}


def format_text(pack: dict[str, Any]) -> str:
    lines = [f"run {pack['run_id']} · {pack['state']}", f"repo: {pack['repo']}"]
    parallel = pack.get("parallel")
    if parallel is None:
        lines.append("parallel: not recorded")
    else:
        engines = ",".join(f"{name}:{count}"
                           for name, count in sorted(parallel["per_engine"].items()))
        line = (f"parallel: max_tasks={parallel['max_tasks']} "
                f"per_engine={engines}")
        if parallel.get("error"):
            line += f" (config invalid, defaults used: {parallel['error']})"
        lines.append(line)
    for task in pack["tasks"]:
        lines.extend(["", f"task {task['id']}: {task['status']}",
                      f"status_reason: {task['status_reason']}", f"why: {task['why']}",
                      f"planned: {task['planned_engine']} / {task['planned_model']}",
                      f"actual: {task['actual_engine']} / {task['actual_model']} / {task['actual_effort']}",
                      f"moved: {'yes' if task['moved'] else 'no'} · reason: {task['rerouted_reason']}",
                      f"rerouted: {task['rerouted']}",
                      f"limits: {task['limits']}",
                      f"branch: {task['branch']}", f"commit: {task['commit']}",
                      f"worktree: {task['worktree']}",
                      f"base: {task['base']} · source: {task['base_source']} · sha: {task['base_sha']}",
                      f"HEAD ancestor of branch: {task['head_is_ancestor']}"])
        if task.get("failure_mode"):
            lines.append(f"failure_mode: {task['failure_mode']}")
        if task.get("git_error"):
            lines.append(f"git: {task['git_error']}")
        else:
            lines.extend(["diff stat:", task["diff_stat"] or "",
                          f"total: +{task['added_lines']}/-{task['removed_lines']}"])
            if "diff" in task:
                lines.extend(["diff:", task["diff"] or ""])
        verify = task["verify"]
        lines.append("verify: " + (f"{verify['command']} · passed={verify['passed']} · "
                                   f"exit_code={verify['exit_code']}" if verify else "not run"))
        if verify:
            lines.extend(["output_tail:", verify["output_tail"]])
        lines.append(f"verdict: {task.get('verdict') or 'not recorded'}")
        if "review_findings" in task:
            findings = task["review_findings"]
            lines.append("review_findings:")
            if findings is None or findings["unparsed"]:
                lines.append("  unparsed")
            else:
                lines.extend(f"  {key}: {findings[key]}"
                             for key in ("critical", "warning", "nit", "verdict"))
        proof = task.get("proof")
        if proof:
            lines.extend([f"proof: {proof['verdict']} · reason: {proof.get('reason')}",
                          f"test_paths: {', '.join(proof['test_paths'])}"])
            red = proof.get("red")
            if red:
                lines.extend([f"red exit_code={red['exit_code']} · command: {red['command']}",
                              "red output_tail:", red["output_tail"]])
        if task.get("warnings"):
            lines.extend(["warnings:", *task["warnings"]])
        lines.extend([f"worker final text ({task['worker_final_text_source']}):",
                      task["worker_final_text"],
                      "worker_report:"])
        report = task["worker_report"]
        if report == "no report":
            lines.append("no report")
        else:
            lines.append(f"  RESULT: {report['result']}")
            lines.extend((f"  RAN: {claim['command']} -> exit {claim['exit_code']}: "
                          f"{claim['output']}" if claim["exit_code"] is not None
                          else f"  RAN: {claim['command']}") for claim in report["ran"])
            if not report["ran"]:
                lines.append("  RAN: none")
            lines.extend([f"  DEVIATIONS: {report['deviations']}",
                          f"  FOLLOW-UPS: {report['follow_ups']}"])
        lines.extend(["unbacked_claims:",
                      *(f"  {command}" for command in task["unbacked_claims"]),
                      *(["  none"] if not task["unbacked_claims"] else []),
                      "unproven_reason:", str(task["unproven_reason"]), "cost:"])
        for index, row in enumerate(task["attempts"], 1):
            lines.append(f"  attempt {index}: " + " · ".join(f"{key}={value}" for key, value in row.items()))
        lines.append("tool errors:")
        for error in task["tool_errors"]:
            lines.append(f"  {error['name']}: {error['error']}")
        if task["tool_errors_omitted"]:
            lines.append(f"({task['tool_errors_omitted']} more in the run log)")
        if task["paused"]:
            paused = task["paused"]
            lines.append(f"paused: id={paused['id']} · reason={paused['reason']} · resets_at={paused['resets_at']}")
    lines.extend(["", f"run log: {pack['run_log']}",
                  f"full trace: hermes control watch {pack['run_id']} --once --no-usage"])
    return "\n".join(lines) + "\n"
