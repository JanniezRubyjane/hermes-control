"""``hermes control watch``: the newest run, live, in a terminal.

Meant for a second pane beside a Hermes session: which task is running, on which
engine and model, what step it is on, and where every task ended up. It reads the
same run log as the dashboard's Runs view (runs.py), so the two never disagree.
See docs/runs-view.md.
"""

from __future__ import annotations

import argparse
import io
import json
from statistics import median
import sys
import time
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from . import limits, runs
from .engines.registry import ENGINE_NAMES

_MARK = {"done": ("✓", "green"), "unproven": ("!", "yellow"), "failed": ("✗", "red"),
         "paused": ("⏸", "yellow"), "running": ("●", "blue"), "waiting": ("○", "dim"),
         "stopped": ("■", "dim")}
_SPIN = "◐◓◑◒"
_TAIL_STEPS = 3  # the steps shown for the attempt that is working right now
_SAID_CHARS = 200  # a finished attempt's last message, cut to about two lines


def _lasted(start: float, end: float) -> str:
    seconds = max(0, round(end - start))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{round(seconds / 60)} min"
    return f"{seconds // 3600} h {round(seconds % 3600 / 60)} min"


def _line(text: str, style: str = "") -> Text:
    """One screen line: long ones end in … rather than wrapping the layout apart."""
    return Text(text, style=style, no_wrap=True, overflow="ellipsis")


def _para(text: str, indent: int, style: str = "") -> Padding:
    """An explanation: wraps under its own indent instead of losing its end."""
    return Padding(Text(text, style=style), (0, 0, 0, indent))


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _step(step: dict[str, Any]) -> str:
    if step["type"] == "tool":
        failed = "  (refused or failed)" if step.get("is_error") else ""
        return f"{step['name']:<18} {step.get('detail') or ''}{failed}".rstrip()
    return {"text": "said: ", "thinking": "thought: ", "error": "error: "}[step["type"]] + \
        _one_line(step.get("text"))


def _task(task: dict[str, Any], *, now: float) -> list[RenderableType]:
    mark, style = _MARK[task["status"]]
    head = Text(no_wrap=True, overflow="ellipsis")
    head.append(f"{mark} ", style=style)
    head.append(task["id"], style="bold")
    planned = " · ".join(p for p in (task["planned_engine"], task["model"], task["effort"]) if p)
    head.append(f"  {task['policy'] or 'task'} · planned {planned or '?'}", style="dim")
    if task["depends_on"]:
        head.append(" · after " + ", ".join(task["depends_on"]), style="dim")
    lines: list[RenderableType] = [head]
    if task["why"]:
        lines.append(_para(f"“{_one_line(task['why'])}”", 4, "italic dim"))
    moved = task["rerouted"]
    if moved:
        lines.append(_para(f"↪ moved from {moved.get('from')}: {moved.get('reason')}", 4, "yellow"))

    last = task["attempts"][-1] if task["attempts"] else None
    for attempt in task["attempts"]:
        working = task["status"] == "running" and attempt is last
        how = {"resumed": "resumed on ", "continued": "continued on "}.get(attempt["how"], "")
        refused = f" · {attempt['tool_errors']} refused or failed" if attempt["tool_errors"] else ""
        text = (f"    {_SPIN[int(now * 4) % 4] if working else '·'} {how}{attempt['engine']}"
                + (f" · {attempt['model']}" if attempt["model"] else "")
                + f" · {attempt['tools']} tools{refused} · {_lasted(attempt['started_at'], now if working else attempt['ended_at'])}"
                + (" · working…" if working else ""))
        lines.append(_line(text, "blue" if working else ""))
        if attempt["limit"]:
            nxt = attempt["limit"].get("next")
            lines.append(_line("      hit its usage limit" + (f"; {nxt} takes over" if nxt not in (None, "pause") else ""), "red"))
        elif attempt["error"]:
            lines.append(_para(_one_line(attempt["error"]), 6, "red"))
        if working:
            lines += [_line(f"      ⎿ {_step(step)}", "dim") for step in attempt["steps"][-_TAIL_STEPS:]]
        elif attempt["last_text"]:
            said = _one_line(attempt["last_text"])
            # A pane is small: two lines or so; the dashboard's Runs view has the rest.
            said = said if len(said) <= _SAID_CHARS else said[:_SAID_CHARS - 1].rstrip() + "…"
            lines.append(_para(f"said: {said}", 6, "dim"))

    status, reason = task["status"], task["status_reason"]
    if task["paused"]:
        frees = task["paused"].get("resets_at")
        lines.append(_para(f"⏸ paused: {task['paused'].get('reason')}"
                           + (f" · an engine frees up {limits.when(frees)}" if frees else ""), 4, "yellow"))
        lines.append(_line("      Resume it on the Engines tab of the dashboard (hermes dashboard).", "dim"))
    elif task["commit"]:
        verify = task["verify"]
        sha = task["commit"]["sha"][:7]
        if verify and verify.get("passed"):
            lines.append(_line(f"    ✓ committed {sha} · check passed (exit {verify.get('exit_code')})", "green"))
        elif verify:
            lines.append(_para(f"! committed {sha} · check failed (exit {verify.get('exit_code')})"
                               + (f": {verify.get('unproven_reason')}" if verify.get("unproven_reason") else ""), 4, "yellow"))
        else:
            lines.append(_line(f"    ! committed {sha} · " + ("checking…" if status == "running" else (reason or "not checked")),
                               "blue" if status == "running" else "yellow"))
        if task["branch"]:
            lines.append(_line(f"      merge: git merge --no-ff {task['branch']}", "dim"))
    elif status in ("failed", "unproven", "stopped"):
        lines.append(_para(f"{_MARK[status][0]} {status}: {reason or 'no reason recorded'}", 4, _MARK[status][1]))
        if task["worktree"]:
            lines.append(_line(f"      worktree kept: {task['worktree']}", "dim"))
    elif status == "waiting":
        lines.append(_line("    waiting for its turn", "dim"))
    return lines


def _usage_line() -> Text:
    try:
        caps = limits.read_caps()
    except ValueError:
        caps = limits.parse_caps(None)
    line = Text("usage  ", style="dim", no_wrap=True, overflow="ellipsis")
    for index, engine in enumerate(ENGINE_NAMES):
        if index:
            line.append("  │  ", style="dim")
        reading = limits.describe(engine, caps=caps)
        if reading["error"]:
            line.append(f"{engine} usage unknown", style="dim")
            continue
        windows = " · ".join(
            f"{w['label']} {w['used_percent']:.0f}%" if w["used_percent"] is not None
            else f"{w['label']} unknown" for w in reading["windows"])
        line.append(f"{engine} {windows} (cap {reading['cap']}%)",
                    style="red" if reading["held_back"] else "")
        if reading["held_back"]:
            line.append(" holding work back", style="red")
    return line


def frame(run_id: str | None = None, *, now: float | None = None, usage: bool = True,
          footer: str | None = None) -> Group:
    """One screen: the given run, or the newest one."""
    now = time.time() if now is None else now
    if run_id is None:
        newest = runs.list_runs(limit=1, now=lambda: now)
        view = runs.run_view(newest[0]["run_id"], now=lambda: now) if newest else None
        missing = "No runs yet. Waiting for Hermes to delegate work…"
    else:
        view = runs.run_view(run_id, now=lambda: now)
        missing = f"No run {run_id}. Run ids are in the Runs view and in the planner's summary."
    lines: list[RenderableType] = []
    if view is None:
        lines.append(_line(missing, "dim"))
    else:
        state_style = {"running": "blue", "stopped": "yellow", "paused": "yellow"}.get(view["state"], "green")
        head = Text(no_wrap=True, overflow="ellipsis")
        head.append("Hermes-Control · ", style="bold")
        head.append(f"run {view['run_id']}", style="bold")
        head.append(" · ")
        head.append(view["state"], style=state_style)
        repo = (view["repo"] or "").rstrip("/").rsplit("/", 1)[-1] or "unknown repo"
        head.append(f" · {repo} · {_lasted(view['started_at'], now if view['state'] == 'running' else view['updated_at'])}"
                    f" · started {limits.when(view['started_at'])}", style="dim")
        lines.append(head)
        planner = view["planner"] or {}
        if planner.get("model"):
            lines.append(_line(f"planner {planner['model']}"
                               + (f" ({planner['provider']})" if planner.get("provider") else ""), "dim"))
        if planner.get("session_id"):
            lines.append(_line(f"  its reasoning: hermes chat --resume {planner['session_id']}", "dim"))
        for task in view["tasks"]:
            lines.append(Text(""))
            lines += _task(task, now=now)
    if usage:
        lines += [Text(""), _usage_line()]
    if footer:
        lines += [Text(""), _line(footer, "dim")]
    return Group(*lines)


def frame_text(run_id: str | None = None, *, now: float | None = None, usage: bool = True,
               width: int = 140) -> str:
    """The frame as plain text (tests, and anything that is not a terminal)."""
    buffer = io.StringIO()
    Console(file=buffer, width=width, color_system=None, force_terminal=False).print(
        frame(run_id, now=now, usage=usage))
    return buffer.getvalue()


def watch(run_id: str | None = None, *, once: bool = False, interval: float = 1.0,
          usage: bool = True, console: Console | None = None) -> int:
    console = console or Console()
    if once or not console.is_terminal:
        console.print(frame(run_id, usage=usage))
        return 0
    footer = (f"watching run {run_id}" if run_id else "following the newest run") + " · Ctrl-C to stop"
    try:
        with Live(frame(run_id, usage=usage, footer=footer), console=console, screen=True,
                  auto_refresh=False) as live:
            while True:
                time.sleep(interval)
                live.update(frame(run_id, usage=usage, footer=footer), refresh=True)
    except KeyboardInterrupt:
        return 0


# --- `hermes control` --------------------------------------------------------------

def setup_cli(parser: argparse.ArgumentParser) -> None:
    """Build ``hermes control``'s arguments (Hermes hands over the subparser)."""
    commands = parser.add_subparsers(dest="control_command", metavar="<command>")
    follow = commands.add_parser(
        "watch", help="follow Hermes-Control runs live in this terminal",
        description="Show the newest Hermes-Control run and keep it up to date: each task, "
                    "the engine and model working on it, its latest steps, and where it "
                    "landed. Run it in a second pane beside a Hermes session.")
    follow.add_argument("run_id", nargs="?", help="one run to watch (default: always the newest)")
    follow.add_argument("--once", action="store_true", help="print it once and exit")
    follow.add_argument("--interval", type=float, default=1.0,
                        help="seconds between refreshes (default: 1)")
    follow.add_argument("--no-usage", action="store_true",
                        help="leave out the engines' usage line (it reads each account's usage)")
    stats = commands.add_parser("stats", help="show task attempt costs and routing over recent runs")
    stats.add_argument("--limit", type=int, default=30, metavar="N",
                       help="number of recent runs to include (default: 30)")
    stats.add_argument("--json", action="store_true", help="print attempt records as JSON")
    stats.add_argument("--run", action="append", metavar="RUN_ID",
                       help="include only this run (repeatable)")
    stats.add_argument("--by-model", action="store_true",
                       help="summarize attempts by engine, model, effort, and policy")
    review = commands.add_parser("review-pack", help="show a complete review digest for one run")
    review.add_argument("run_id", help="run to review")
    review.add_argument("--json", action="store_true", help="print the digest as JSON")
    review.add_argument("--diff", action="store_true", help="include full unified diffs")
    prune = commands.add_parser("prune", help="list or remove merged Hermes task worktrees")
    prune.add_argument("--repo", metavar="PATH", help="repository (default: current git top level)")
    prune.add_argument("--apply", action="store_true", help="remove safe worktrees and branches")
    prune.add_argument("--json", action="store_true", help="print entries as JSON")
    lessons = commands.add_parser("lessons", help="report recurring mistakes in run logs")
    lessons.add_argument("--since", type=float, default=30, metavar="DAYS",
                         help="look back this many days (default: 30)")
    lessons.add_argument("--min", type=int, default=2, metavar="N",
                         help="minimum occurrences per class (default: 2)")
    lessons.add_argument("--json", action="store_true", help="print classes and evidence as JSON")
    lessons.add_argument("--limit", type=int, metavar="N", help="maximum number of classes")
    lessons.add_argument("--recent", type=float, default=14, metavar="DAYS",
                         help="separate classes not seen this recently (default: 14)")
    health = commands.add_parser("health", help="report measured Hermes-Control health (L1)")
    health.add_argument("--since", type=float, default=30, metavar="DAYS",
                        help="look back this many days (default: 30)")
    health.add_argument("--recent", type=float, default=14, metavar="DAYS",
                        help="include lesson classes seen this recently (default: 14)")
    health.add_argument("--repo", metavar="PATH", help="repository (default: current git top level)")
    health.add_argument("--json", action="store_true", help="print the report as JSON")
    health.add_argument("--out", metavar="FILE", help="write the markdown report to this file")
    parser.set_defaults(_control_parser=parser)


def _tokens(value: int | None) -> str:
    return "-" if value is None else f"{value / 1000:.1f}k"


def stats(limit: int = 30, *, as_json: bool = False,
          run_ids: list[str] | None = None, by_model: bool = False) -> int:
    """Show attempt costs beside the planned route so planner changes can be measured."""
    rows = runs.run_stats(limit, run_ids=run_ids)
    missing = []
    if run_ids is not None:
        missing = [run_id for run_id in dict.fromkeys(run_ids)
                   if runs.run_view(run_id) is None]
        for run_id in missing:
            print(f"no run {run_id}", file=sys.stderr)
    if by_model:
        grouped: dict[tuple, list[dict[str, Any]]] = {}
        for row in rows:
            key = (row["engine"], row["model"] if row["model_recorded"]
                   else "(not recorded)", row["effort"], row["policy"])
            grouped.setdefault(key, []).append(row)
        summaries = []
        for (engine, model, effort, policy), group in grouped.items():
            tools = sum(row["tools"] for row in group)
            recorded_output = [row["output_tokens"] for row in group
                               if row["output_tokens"] is not None]
            recorded_reasoning = [row for row in group
                                  if row["reasoning_tokens"] is not None
                                  and row["output_tokens"] is not None]
            reasoning_output = sum(row["output_tokens"] for row in recorded_reasoning)
            summaries.append({
                "engine": engine, "model": model, "effort": effort, "policy": policy,
                "n": len(group), "proven": sum(row["final_attempt"] and
                                                  row["verify_passed"] is True for row in group),
                "check_failed": sum(row["final_attempt"] and
                                                        row["verify_passed"] is False
                                                        for row in group),
                "moved": sum(row["moved"] for row in group),
                "median_tools": median(row["tools"] for row in group),
                "tool_error_pct": 100 * sum(row["tool_errors"] for row in group) / tools
                if tools else 0.0,
                "median_seconds": median(row["seconds"] for row in group),
                "median_output_tokens": median(recorded_output) if recorded_output else None,
                "reasoning_pct": (100 * sum(row["reasoning_tokens"]
                                               for row in recorded_reasoning) / reasoning_output
                                  if reasoning_output else 0.0) if recorded_reasoning else None,
            })
        summaries.sort(key=lambda group: -group["n"])
        if as_json:
            print(json.dumps(summaries))
            return 1 if missing else 0
        table = Table(show_edge=False, pad_edge=False)
        columns = ("engine", "model", "effort", "policy", "n", "proven", "check failed",
                   "moved", "median tools", "tool error %", "median secs", "median output",
                   "reasoning %")
        for column in columns:
            table.add_column(column, justify="left" if column in columns[:4] else "right")
        for group in summaries:
            table.add_row(str(group["engine"]), str(group["model"]),
                          str(group["effort"] or "-"), str(group["policy"] or "-"),
                          str(group["n"]), str(group["proven"]),
                          str(group["check_failed"]), str(group["moved"]),
                          str(group["median_tools"]),
                          f"{group['tool_error_pct']:.1f}",
                          str(group["median_seconds"]),
                          str(group["median_output_tokens"])
                          if group["median_output_tokens"] is not None else "-",
                          f"{group['reasoning_pct']:.1f}"
                          if group["reasoning_pct"] is not None else "-")
        Console(width=max(180, Console().width)).print(table)
        return 1 if missing else 0
    if as_json:
        print(json.dumps(rows))
        return 1 if missing else 0

    table = Table(show_edge=False, pad_edge=False)
    for column in ("run", "task", "engine", "model", "effort", "tools", "errors",
                   "secs", "input", "cached", "output", "reasoning", "usd", "status", "check"):
        table.add_column(column, justify="right" if column in (
            "tools", "errors", "secs", "input", "cached", "output", "reasoning", "usd") else "left")
    for row in rows:
        engine = row["engine"]
        if row["moved"]:
            engine = f"{engine}←{row['planned_engine']}"
        check = {True: "pass", False: "fail", None: "-"}[row["verify_passed"]]
        model = row["model"] if row["model_recorded"] else "(not recorded)"
        table.add_row(row["run_id"], row["task_id"], engine, model,
                      row["effort"] or "-", str(row["tools"]), str(row["tool_errors"]),
                      str(row["seconds"]), _tokens(row["input_tokens"]),
                      _tokens(row["cached_input_tokens"]), _tokens(row["output_tokens"]),
                      _tokens(row["reasoning_tokens"]),
                      f"{row['cost_usd']:.3f}" if row["cost_usd"] is not None
                      else "quota" if row["engine"] == "codex" else "-",
                      row["status"], check)
    console = Console(width=max(160, Console().width))
    console.print(table)
    console.print(f"{len(rows)} attempts · {sum(row['moved'] for row in rows)} moved · "
                  f"{sum(row['tools'] for row in rows)} tool calls · "
                  f"{sum(row['tool_errors'] for row in rows)} errors · "
                  f"total input {sum(row['input_tokens'] or 0 for row in rows):,} · "
                  f"total cached {sum(row['cached_input_tokens'] or 0 for row in rows):,} · "
                  f"total output {sum(row['output_tokens'] or 0 for row in rows):,}")
    return 1 if missing else 0


def run_cli(args: argparse.Namespace) -> int:
    if getattr(args, "control_command", None) == "health":
        from . import health

        return health.report(args.since, repo=args.repo, as_json=args.json, out=args.out, recent=args.recent)
    if getattr(args, "control_command", None) == "lessons":
        from . import lessons

        return lessons.report(args.since, min_count=args.min, limit=args.limit, as_json=args.json,
                              recent=args.recent)
    if getattr(args, "control_command", None) == "prune":
        from . import prune

        return prune.prune(args.repo, apply=args.apply, as_json=args.json)
    if getattr(args, "control_command", None) == "review-pack":
        from . import review_pack

        pack = review_pack.build(args.run_id, include_diff=args.diff)
        if pack is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1
        print(json.dumps(pack, ensure_ascii=False) if args.json else review_pack.format_text(pack),
              end="" if not args.json else "\n")
        return 0
    if getattr(args, "control_command", None) == "watch":
        return watch(args.run_id, once=args.once, interval=max(0.2, args.interval),
                     usage=not args.no_usage)
    if getattr(args, "control_command", None) == "stats":
        return stats(args.limit, as_json=args.json, run_ids=args.run, by_model=args.by_model)
    parser = getattr(args, "_control_parser", None)
    if parser is not None:
        parser.print_help()
    return 1
