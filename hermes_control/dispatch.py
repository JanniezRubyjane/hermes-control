"""Task orchestration: validate, order, run each task in its own worktree.

The summary returned to the parent agent deliberately excludes the workers'
intermediate tool calls and reasoning - the same contract as Hermes's own
``delegate_tool``. The full stream lives in the run log, for humans and for the
VSCode panel.

``dispatch`` owns worktree creation rather than ``run_task`` so that the branch
and path stay available for the summary even when a task fails.
"""

from __future__ import annotations

import dataclasses
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import functools
import logging
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

try:
    import fcntl
except ImportError:  # Windows has no flock, as with engine slots.
    fcntl = None

from . import limits, model_policy, parallel, pauses, proof, runs, stall
from .engines.base import ControlTask, engine_env
from .engines.registry import ENGINE_NAMES, get_adapter
from .events import EMPTY_DIFF_REASON, ControlResult, RawEvent
from .landing import land, verify_payload
from .overlap import find_overlaps
from .policy import ExecPolicy
from .proctree import (
    group_popen_kwargs,
    stop_group,
    wait_bounded,
)
from .routing import engine_status, move, route
from .runlog import RunLog, _redact, run_log_dir
from .verify import check_script_path, find_sandbox, write_check_script
from .worktree import (
    TaskWorktree,
    capture_worktree_diff,
    changed_claude_paths,
    changed_codex_paths,
    create_task_worktree_with_reason,
)

logger = logging.getLogger(__name__)

# Identity for the single run-level "plan" event dispatch() logs before any task
# runs (design doc D2a: the planner's engine choice and its `why` must be
# logged, not just returned in the summary). It describes the whole ordered
# plan, not one task or one engine, so both fields are deliberately empty
# rather than colliding with a real task id (parse_tasks never produces one)
# or naming one engine arbitrarily among the several a multi-task plan may use.
_PLAN_EVENT_TASK_ID = ""
_PLAN_EVENT_ENGINE = ""

_DEFAULT_TIMEOUT_SECONDS = 1800
_TERMINATE_GRACE_SECONDS = 10
# Last 8 KB of stderr, enough diagnostic context (e.g. claude -p's own stdin-wait
# warning) to explain a failure without letting a chatty or long-running engine
# grow this buffer without bound.
_STDERR_TAIL_LIMIT = 8192
# Both reader threads are joined here after the child has exited (or just been
# killed): by then each stream hits EOF almost immediately, so this is just
# headroom for that, not a normal-case wait.
_READER_JOIN_SECONDS = 5
# After SIGKILL (or taskkill /T /F) the child is gone almost immediately; this is
# only so a kill that did not take - no permission, a zombie parent - degrades to
# a reported failure instead of an unbounded wait, which is the bug I1 names.
_POST_KILL_WAIT_SECONDS = 5


def _drain_stderr(stream, tail: list[str]) -> None:
    """Read *stream* to EOF in bounded chunks, keeping only its trailing
    ``_STDERR_TAIL_LIMIT`` characters. Runs on a daemon thread started right after
    the engine spawns: engines write real diagnostics to stderr in normal
    operation, and a stdout-only reader deadlocks the moment a child fills the OS
    pipe buffer on stderr with nobody draining it - a deadlock ``run_task``'s own
    timeout can never catch, because that check only runs between complete stdout
    lines.
    """
    buffer = ""
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            buffer = (buffer + chunk)[-_STDERR_TAIL_LIMIT:]
    except (OSError, ValueError):
        pass
    finally:
        tail.append(buffer)


def _read_stdout(stream, line_queue: "queue.Queue[str | None]") -> None:
    """Read *stream* line by line onto *line_queue*, then push ``None`` once as
    an end-of-stream sentinel. Runs on a daemon thread so ``run_task``'s main
    loop can poll the queue with a timeout and keep re-checking its deadline on
    every wakeup - a plain ``for line in stream:`` blocks on ``readline()`` with
    no timeout of its own, so a child that hangs *silently* (emits nothing at
    all) would never let a deadline check inside that loop run in the first
    place.
    """
    try:
        if stream is not None:
            for raw_line in stream:
                line_queue.put(raw_line)
    except (OSError, ValueError):
        pass
    finally:
        line_queue.put(None)


def parse_tasks(raw_tasks: Sequence[dict[str, Any]], *,
                require_why: bool = False) -> list[ControlTask]:
    """Validate the planner's task list. Every rejection names the offending field
    so the model can repair its own call instead of guessing."""
    tasks: list[ControlTask] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks or []):
        if not isinstance(raw, dict):
            # `tasks: ["do the thing"]` is a plausible model mistake. Without this
            # the `raw.get(...)` below raises AttributeError, which dispatch()'s
            # `except ValueError` does not catch - so the model gets a traceback
            # instead of the repairable message every other bad field produces.
            raise ValueError(
                f"task #{index + 1} must be an object with id/goal/engine/why, "
                f"got {type(raw).__name__}")
        task_id = str(raw.get("id") or f"task-{index + 1}")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", task_id) is None:
            raise ValueError(
                f"task #{index + 1} id {task_id!r} must match "
                "[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
        if task_id in seen:
            raise ValueError(f"duplicate task id {task_id!r}")
        seen.add(task_id)
        goal = str(raw.get("goal") or "").strip()
        if not goal:
            raise ValueError(f"task {task_id!r} has no goal")
        if goal.startswith("-"):
            raise ValueError(f"task {task_id!r}: goal must start with a word, not '-'")
        engine = str(raw.get("engine") or "").strip()
        if engine not in ENGINE_NAMES:
            raise ValueError(
                f"task {task_id!r}: unknown engine {engine!r}; "
                f"choose one of {', '.join(ENGINE_NAMES)}")
        why = str(raw.get("why") or "").strip()
        if require_why and not why:
            raise ValueError(f"task {task_id!r}: why is required")
        # Both become argv values (`--model X`, `--effort X`): nothing flag-shaped.
        model = str(raw["model"]).strip() if raw.get("model") else None
        if model:
            try:
                model_policy.check_model_name(model)
            except ValueError as exc:
                raise ValueError(f"task {task_id!r}: {exc}") from None
        effort = str(raw.get("effort") or "").strip() or None
        if effort is not None and not effort.isalpha():
            raise ValueError(f"task {task_id!r}: effort {effort!r} must be one word, e.g. high")
        verify = str(raw.get("verify") or "").strip() or None
        red = raw.get("red")
        if red is not None:
            if not isinstance(red, str):
                raise ValueError(f"task {task_id!r}: red must be a string")
            if str(raw.get("policy") or "read-only") != "write" or not verify:
                raise ValueError(f"task {task_id!r}: red requires a write task with verify")
            red = red.strip() or None
            if red and red.startswith("-"):
                raise ValueError(f"task {task_id!r}: red must start with a word, not '-'")
        tasks.append(ControlTask(
            id=task_id, goal=goal, engine=engine,
            policy=ExecPolicy(write=str(raw.get("policy") or "read-only") == "write"),
            files_hint=tuple(str(f) for f in raw.get("files_hint") or ()),
            depends_on=tuple(str(d) for d in raw.get("depends_on") or ()),
            why=why,
            model=model or None,
            verify=verify, red=red,
            effort=effort))
    return tasks


def order_tasks(tasks: Sequence[ControlTask]) -> list[ControlTask]:
    """Dependency order, stable for independent tasks. Raises on a cycle or an
    unknown dependency - both are planner bugs worth failing loudly for."""
    by_id = {task.id: task for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            if dependency not in by_id:
                raise ValueError(f"task {task.id!r} depends on unknown task {dependency!r}")
    ordered: list[ControlTask] = []
    done: set[str] = set()
    remaining = list(tasks)
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.depends_on)]
        if not ready:
            stuck = ", ".join(sorted(t.id for t in remaining))
            raise ValueError(f"dependency cycle among tasks: {stuck}")
        for task in ready:
            ordered.append(task)
            done.add(task.id)
        remaining = [t for t in remaining if t.id not in done]
    return ordered


def with_files_hint(task: ControlTask) -> ControlTask:
    """Fold ``files_hint`` into the goal text the worker actually receives.

    The worker only ever sees ``goal`` - not the plan, not the other tasks - so a
    hint that is parsed, stored on the dataclass and never rendered is a promise
    the tool schema and the skill both make and nothing keeps. Phrased as a hint
    rather than a restriction: the planner's guess about where the work lives
    should not stop a worker that finds it somewhere else.
    """
    if not task.files_hint:
        return task
    listed = "\n".join(f"- {path}" for path in task.files_hint)
    return dataclasses.replace(
        task,
        goal=f"{task.goal}\n\nThe planner expects the relevant code to be here "
             f"(a starting point, not a restriction):\n{listed}")


def with_verify_note(task: ControlTask) -> ControlTask:
    """Tell the worker the controller's final check, even without a sandbox."""
    if not task.verify:
        return task
    return dataclasses.replace(
        task,
        goal=f"{task.goal}\n\nWhen you finish, Hermes-Control will commit your changes "
             f"and check them by running this command in this directory, in a sandbox "
             f"with no network:\n{task.verify}\nMake that command pass.")


def with_check_guidance(task: ControlTask) -> ControlTask:
    """Guide Codex write workers toward focused checks before their final check."""
    if task.engine != "codex" or not task.verify or not task.policy.write:
        return task
    return dataclasses.replace(
        task,
        goal=f"{task.goal}\n\nHow to work efficiently here: you may run the check "
             "command above yourself inside your sandbox at any time. While you "
             "work, run only the tests that cover the files you change (for example "
             "the matching test file), and run the full check command once when you "
             "think you are done; if it fails, fix and run it again. Read files in "
             "the line ranges you need rather than printing whole files, and read a "
             "whole file when you need its full context. Never skip, "
             "weaken or delete a test to make the check pass.")


def with_report_request(task: ControlTask) -> ControlTask:
    """Ask Codex write workers for a closing claim without changing proof."""
    from .worker_report import REPORT_REQUEST

    if task.engine != "codex" or not task.policy.write or REPORT_REQUEST in task.goal:
        return task
    return dataclasses.replace(task, goal=f"{task.goal}\n\n{REPORT_REQUEST}")


def with_own_check(task: ControlTask, script: Path) -> ControlTask:
    """Permit Claude to run only the controller-written sandbox entry point."""
    policy = dataclasses.replace(task.policy,
                                 allow_tools=(*task.policy.allow_tools, f"Bash({script})"))
    return dataclasses.replace(
        task, policy=policy,
        goal=f"{task.goal}\n\nYou can run that check yourself before you finish: run "
             f"exactly this command with your Bash tool, with nothing added before or "
             f"after it (it is pre-approved, and it runs the command above inside the "
             f"sandbox):\n{script}\nRun it, and fix what it reports until it passes.")


def run_task(task: ControlTask, *, run_id: str, log: RunLog, worktree: TaskWorktree,
             adapter=None, timeout: int | None = None,
             stall_settings: dict[str, float] | None = None,
             sandbox: Callable[[], str | None] | None = None) -> ControlResult:
    """Run one task to completion inside *worktree*, streaming events to *log*."""
    engine = adapter if adapter is not None else get_adapter(task.engine)
    # Older duck-typed adapters implement only the streaming contract. The built-in
    # adapters always provide this check for their repository-scoped configuration.
    check_repository = getattr(engine, "repository_problem", None)
    reason = check_repository(worktree) if check_repository is not None else None
    if reason:
        log.append_next(task.id, [RawEvent(kind="error", payload={"message": reason})],
                        engine=engine.name, now=time.time())
        return ControlResult(task_id=task.id, engine=engine.name, ok=False, text=reason,
                             session_id=None, unproven_reason=reason)
    task = with_report_request(with_check_guidance(with_verify_note(with_files_hint(task))))
    script = None
    if task.verify and engine.name == "claude" and sandbox is not None:
        codex_path = sandbox()
        if codex_path:
            script = check_script_path(run_id, task.id)
            task = with_own_check(task, script)
    try:
        if script is not None:
            write_check_script(codex_path, worktree.path, task.verify, path=script)
        return _run_task_engine(task, run_id=run_id, log=log, worktree=worktree,
                                engine=engine, timeout=timeout,
                                stall_settings=stall_settings)
    finally:
        if script is not None:
            try:
                script.unlink(missing_ok=True)
            except OSError:
                logger.warning("hermes-control: could not remove check script %s", script)


def _run_task_engine(task: ControlTask, *, run_id: str, log: RunLog,
                     worktree: TaskWorktree, engine, timeout: int | None,
                     stall_settings: dict[str, float] | None = None) -> ControlResult:
    """Stream the engine after run_task has prepared its temporary check."""
    settings = stall_settings if stall_settings is not None else stall.parse_settings(None)
    argv = engine.build_argv(task, cwd=worktree.path)
    try:
        # stdin must be closed: claude otherwise waits 3s for piped input.
        process = subprocess.Popen(
            argv, cwd=str(worktree.path), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", env=engine_env(),
            **group_popen_kwargs())
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("hermes-control: %s failed to start: %s", task.engine, exc)
        return ControlResult(task_id=task.id, engine=engine.name, ok=False, text=str(exc),
                             session_id=None, unproven_reason="engine failed to start",
                             failure_mode="start-failure")

    # Drain stderr on its own thread from the moment the engine spawns: reading
    # only stdout below would deadlock forever the instant a child fills the OS
    # pipe buffer on stderr, since nothing would ever read it.
    stderr_tail: list[str] = []
    stderr_thread = threading.Thread(target=_drain_stderr, args=(process.stderr, stderr_tail),
                                     daemon=True)
    stderr_thread.start()

    # Read stdout on its own thread too, handed off through a queue, for the
    # symmetric reason: a plain `for line in process.stdout:` blocks on
    # readline() with no timeout of its own, so a child that hangs *silently*
    # (never emits anything) would never let the deadline check below run at
    # all. Polling the queue with a timeout means every wakeup re-checks the
    # deadline, whether the child is chatty or dead silent.
    stdout_queue: queue.Queue[str | None] = queue.Queue()
    stdout_thread = threading.Thread(target=_read_stdout, args=(process.stdout, stdout_queue),
                                     daemon=True)
    stdout_thread.start()

    raws: list = []
    timed_out = False
    stalled_reason = None
    previous_wall, previous_mono = time.time(), time.monotonic()
    last_event = previous_mono
    wake_at = None
    wake_logged = False
    stdout_closed = False
    deadline = previous_mono + (timeout or _DEFAULT_TIMEOUT_SECONDS)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                if stdout_closed:
                    # EOF is not exit: keep watching a child that closed stdout.
                    if wait_bounded(process, min(remaining, 30)) is not None:
                        break
                    raw_line = None
                else:
                    raw_line = stdout_queue.get(timeout=min(remaining, 30))
            except queue.Empty:
                raw_line = ""
            wall, mono = time.time(), time.monotonic()
            slept = (wall - previous_wall) - (mono - previous_mono)
            previous_wall, previous_mono = wall, mono
            if slept > 60:
                wake_at = mono
                if not wake_logged:
                    log.append_next(task.id, [RawEvent(kind="wake", raw_kind="wake",
                        payload={"message": f"machine woke after {slept:.0f} s asleep",
                                 "slept_seconds": slept})], engine=engine.name, now=wall)
                    wake_logged = True
            if raw_line is None:
                stdout_closed = True
                if process.poll() is not None:
                    break
            line = raw_line.strip() if raw_line is not None else ""
            if line:
                last_event = mono
                wake_at = None
                try:
                    new = engine.parse_line(line)
                except Exception as exc:  # noqa: BLE001 - spec section 7: never fatal
                    # The adapter is the boundary with an external CLI whose output we do
                    # not control. One event it cannot handle must become an error event,
                    # not an exception that takes down every task in the run.
                    logger.warning("hermes-control: %s adapter could not parse a line: %s",
                                   engine.name, exc)
                    new = [RawEvent(kind="error", raw_kind="parse_failure", payload={
                        "message": "engine event could not be parsed",
                        "error": f"{type(exc).__name__}: {exc}", "line": line[:2000]})]
                if new:
                    log.append_next(task.id, new, engine=engine.name, now=wall)
                    raws.extend(new)
            if mono >= deadline:
                timed_out = True
                break
            # Count only awake silence. Wake grace can extend that deadline,
            # never shorten it; any non-empty stdout line restores normal tracking.
            silent = mono - last_event
            stalled = (silent >= settings["stall_seconds"]
                       and (wake_at is None or mono - wake_at >= settings["wake_grace_seconds"]))
            if stalled:
                stalled_reason = f"stalled: no engine output for {silent:.0f} s"
                if wake_at is not None:
                    stalled_reason += " after the machine woke"
                break
    finally:
        if not timed_out and stalled_reason is None:
            # Bound the final reap even if an exception interrupted the loop.
            if wait_bounded(process, deadline - time.monotonic()) is None:
                timed_out = True
        # A successful engine can leave background children in its group. Stop
        # them before measuring the worktree or starting another engine there.
        stop_group(process, _TERMINATE_GRACE_SECONDS)
        returncode = wait_bounded(process, _POST_KILL_WAIT_SECONDS)
        if returncode is None:
            # A kill that did not take. Report it as a failure rather than block
            # this thread on a child nothing can reap.
            logger.warning("hermes-control: %s (pid %s) survived SIGKILL; "
                           "abandoning the wait", task.engine, process.pid)
            returncode = -1
        # The child has exited (or just been killed above), so both reader
        # threads will hit EOF on their stream almost immediately; these joins
        # just wait for that to finish before we read their results.
        stdout_thread.join(timeout=_READER_JOIN_SECONDS)
        stderr_thread.join(timeout=_READER_JOIN_SECONDS)
        # Close each pipe only once its reader has actually finished.
        # `close()` takes the buffered reader's own lock, which a thread blocked
        # in read() still holds - so closing under it deadlocks, unbounded, with
        # no deadline left to save us. A reader stays blocked exactly when some
        # descendant we did not manage to kill still holds the write end (the
        # `signal_tree` fallback path). An abandoned daemon thread plus one
        # leaked fd is bounded; this close is not.
        if process.stdout is not None and not stdout_thread.is_alive():
            process.stdout.close()
        if process.stderr is not None and not stderr_thread.is_alive():
            process.stderr.close()

    diff, diff_failure = capture_worktree_diff(worktree)
    result = engine.extract_result(task, raws, diff)
    limit = engine.limit_hit(raws)
    if limit is not None:
        result = dataclasses.replace(result, limit=limit, failure_mode="limit")
    # Claude's rate_limit_event reports are its only usage source.
    report = next((r.payload["rate_limit_info"] for r in reversed(raws)
                   if r.kind == "usage" and r.payload.get("rate_limit_info")), None)
    if report:
        try:
            limits.record_claude_report(report, at=time.time())
        except Exception:  # noqa: BLE001 - reporting cannot fail a completed task
            logger.exception("hermes-control: could not record claude usage for task %s", task.id)
    if diff_failure and result.unproven_reason in (None, EMPTY_DIFF_REASON):
        # The adapter saw an empty diff and concluded "the worker changed
        # nothing". It was never measured; say so instead, and say where to look.
        result = dataclasses.replace(result, unproven_reason=diff_failure)
    stderr_text = (stderr_tail[0] if stderr_tail else "").strip()

    def _with_stderr(reason: str) -> str:
        # Surfaced only on failure paths - a passing task's summary should not
        # carry engine chatter.
        return f"{reason} | stderr tail: {stderr_text}" if stderr_text else reason

    if timed_out:
        return dataclasses.replace(
            result, ok=False, failure_mode="timeout",
            unproven_reason=_with_stderr("timed out; worktree preserved"))
    if stalled_reason is not None:
        return dataclasses.replace(result, ok=False, failure_mode="stall",
                                   unproven_reason=_with_stderr(stalled_reason))
    if returncode != 0:
        return dataclasses.replace(
            result, ok=False, failure_mode=result.failure_mode or "engine-exit",
            unproven_reason=_with_stderr(result.unproven_reason or f"engine exited {returncode}"))
    if not result.ok and result.failure_mode is None:
        result = dataclasses.replace(result, failure_mode="engine-exit")
    return result


def _diff_stat(diff: str) -> str:
    """``+added/-removed`` counting content lines only, not file headers."""
    added = sum(1 for line in diff.splitlines()
                if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff.splitlines()
                  if line.startswith("-") and not line.startswith("---"))
    return f"+{added}/-{removed}"


def _plan_payload(routed: Sequence[tuple[ControlTask, dict[str, Any] | None]],
                  ) -> dict[str, Any]:
    """The ordered plan, in the JSON-serializable shape the run log needs -
    ``policy`` as the ``read-only``/``write`` string, not the ``ExecPolicy``
    object. ``engine`` is where the task runs; ``rerouted`` says when that is
    not where the planner sent it."""
    return {"tasks": [
        {"id": task.id, "engine": task.engine, "why": task.why,
         "policy": "write" if task.policy.write else "read-only",
         "depends_on": list(task.depends_on), "verify": task.verify, "red": task.red,
         "model": task.model, "effort": task.effort, "rerouted": rerouted}
        for task, rerouted in routed
    ]}


class NotReady(Exception):
    """A paused task cannot resume yet; the message says why, and until when."""


@dataclasses.dataclass
class _Context:
    """What every task of one run shares while it is placed, run and paused."""

    run_id: str
    log: RunLog
    repo: str | None
    signed_in: list[str]
    caps: dict[str, int]
    policy: dict[str, Any]
    timeout: int | None
    parallel_settings: dict[str, Any] = dataclasses.field(
        default_factory=lambda: parallel.parse_parallel(None))
    stall_settings: dict[str, float] = dataclasses.field(default_factory=stall.read_settings)


@dataclasses.dataclass
class _Outcome:
    task: ControlTask  # as it last ran
    result: ControlResult
    rerouted: dict[str, Any] | None
    pause: pauses.Pause | None = None


def _caps() -> dict[str, int]:
    try:
        return limits.read_caps()
    except ValueError:
        logger.warning("hermes-control: limit settings are invalid; using the default caps",
                       exc_info=True)
        return limits.parse_caps(None)


def _parallel() -> tuple[dict[str, Any], str | None]:
    try:
        return parallel.read_parallel(), None
    except Exception as exc:  # noqa: BLE001 - settings must not strand an accepted run
        logger.warning("hermes-control: parallel settings are invalid; using the defaults",
                       exc_info=True)
        return parallel.parse_parallel(None), " ".join(f"{type(exc).__name__}: {exc}".split())[:300]


def _log(ctx: _Context, task_id: str, engine: str, kind: str, payload: dict[str, Any]) -> None:
    ctx.log.append_next(task_id, [RawEvent(kind=kind, payload=payload)],
                        engine=engine, now=time.time())


def _free_engine(ctx: _Context, *, besides: str | None = None) -> str | None:
    """A signed-in engine that is neither over its cap nor stopped by its limit."""
    for name in ctx.signed_in:
        if name != besides and limits.engine_hold(name, caps=ctx.caps) is None:
            return name
    return None


def _holds(signed_in: Sequence[str], caps: dict[str, int]) -> tuple[str, float | None]:
    """Why no engine can take work, and the earliest time one may again."""
    reasons, untils = [], []
    for name in ENGINE_NAMES:
        if name not in signed_in:
            reasons.append(f"{name} is not signed in")
            continue
        hold = limits.engine_hold(name, caps=caps)
        if hold is not None:
            reasons.append(hold[0])
            if hold[1]:
                untils.append(hold[1])
    return "; ".join(reasons) or "no engine can take work", min(untils, default=None)


def _move(ctx: _Context, task: ControlTask, engine: str, reason: str,
          earlier: dict[str, Any] | None) -> tuple[ControlTask, dict[str, Any]]:
    moved, note = move(task, engine, reason)
    if earlier:
        note["earlier"] = earlier
    moved = model_policy.apply_policy(moved, ctx.policy)
    note.update(model=moved.model, effort=moved.effort)
    return moved, note


def with_continuation(task: ControlTask, *, previous: str) -> ControlTask:
    """For a new session in a worktree where an earlier worker left unfinished edits."""
    return dataclasses.replace(task, resume_session=None, goal=(
        f"{task.goal}\n\nAn earlier worker ({previous}) started this task and stopped before "
        f"finishing, because it hit a usage limit. Its unfinished edits are already in this "
        f"directory: check `git status` and `git diff` first, keep what is right, and finish "
        f"the task."))


def with_resume(task: ControlTask, *, session_id: str) -> ControlTask:
    """For continuing the very session that stopped at a usage limit."""
    return dataclasses.replace(task, resume_session=session_id, goal=(
        f"You stopped before finishing this task because you hit a usage limit, which has "
        f"now reset. Continue from where you left off and finish it:\n\n{task.goal}"))


def _pause(ctx: _Context, original: ControlTask, *, worktree: TaskWorktree | None, engine: str,
           session_id: str | None, reason: str, until: float | None,
           rerouted: dict[str, Any] | None, last_run: ControlTask | None = None) -> pauses.Pause:
    ran = last_run or original
    record = pauses.Pause(
        id=pauses.pause_id(ctx.run_id, original.id), run_id=ctx.run_id,
        task=pauses.task_to_dict(dataclasses.replace(
            original, engine=engine, model=ran.model, effort=ran.effort)), repo=ctx.repo,
        worktree=pauses.worktree_to_dict(worktree) if worktree else None, engine=engine,
        session_id=session_id, reason=reason, resets_at=until, paused_at=time.time(),
        rerouted=rerouted)
    pauses.save(record)
    _log(ctx, original.id, engine, "paused",
         {"id": record.id, "reason": reason, "resets_at": until, "worktree": record.worktree})
    return record


def _pause_after_limit(ctx: _Context, original: ControlTask, worktree: TaskWorktree,
                       last_run: ControlTask, result: ControlResult,
                       rerouted: dict[str, Any] | None) -> pauses.Pause:
    others, others_until = _holds(ctx.signed_in, ctx.caps)
    own = result.limit.resets_at if result.limit else None
    if own is None:
        hold = limits.engine_hold(result.engine, caps=ctx.caps, refresh=True)
        own = hold[1] if hold else None
    until = min((t for t in (own, others_until) if t), default=None)
    return _pause(ctx, original, worktree=worktree, engine=result.engine,
                  session_id=result.session_id,
                  reason=f"{result.engine} hit its usage limit; {others}", until=until,
                  rerouted=rerouted, last_run=last_run)


def _run_guarded(ctx: _Context, task: ControlTask, worktree: TaskWorktree, *,
                 sandbox: Callable[[], str | None], continued: bool) -> ControlResult:
    from .slots import engine_slot

    if continued and task.engine == "claude":
        paths, failure = changed_claude_paths(worktree)
        if failure or paths:
            detail = (f"could not check .claude/ ({failure})" if failure else
                      f"the worktree changed .claude/ ({', '.join(paths)})")
            reason = f"refused to run claude: {detail}, which claude would load as settings"
            _log(ctx, task.id, task.engine, "error", {"message": reason})
            return ControlResult(task_id=task.id, engine=task.engine, ok=False, text=reason,
                                 session_id=None, unproven_reason=reason)
    if continued and task.engine == "codex":
        paths, failure = changed_codex_paths(worktree)
        if failure or paths:
            detail = (f"could not check .codex/ ({failure})" if failure else
                      f"the worktree changed .codex/ ({', '.join(paths)})")
            reason = f"refused to run codex: {detail}, which codex would load as settings"
            _log(ctx, task.id, task.engine, "error", {"message": reason})
            return ControlResult(task_id=task.id, engine=task.engine, ok=False, text=reason,
                                 session_id=None, unproven_reason=reason)
    with engine_slot(task.engine, ctx.parallel_settings["per_engine"][task.engine],
                     on_wait=lambda payload: _log(
                         ctx, task.id, task.engine, "slot_wait", payload)):
        return run_task(task, run_id=ctx.run_id, log=ctx.log, worktree=worktree,
                        timeout=ctx.timeout, sandbox=sandbox, stall_settings=ctx.stall_settings)


def _execute(ctx: _Context, original: ControlTask, run: ControlTask,
             rerouted: dict[str, Any] | None, worktree: TaskWorktree, *,
             sandbox: Callable[[], str | None], continued: bool = False) -> _Outcome:
    """Run *run* in *worktree*. On a usage limit, a free engine continues in the same
    worktree; with none free the task is paused (docs/limits-and-resume.md §4.2)."""
    result = _run_guarded(ctx, run, worktree, sandbox=sandbox, continued=continued)
    if result.limit is None:
        return _Outcome(run, result, rerouted)
    limits.forget(result.engine)
    other = _free_engine(ctx, besides=result.engine)
    _log(ctx, original.id, result.engine, "limit",
         {"message": result.limit.message, "resets_at": result.limit.resets_at,
          "next": other or "pause"})
    if other is None:
        return _Outcome(run, result, rerouted,
                        _pause_after_limit(ctx, original, worktree, run, result, rerouted))
    moved, note = _move(ctx, original, other, f"{result.engine} hit its usage limit "
                                              f"({result.limit.message})", rerouted)
    note["continued"] = True
    _log(ctx, original.id, other, "rerouted", note)
    moved = with_continuation(moved, previous=result.engine)
    second = _run_guarded(ctx, moved, worktree, sandbox=sandbox, continued=True)
    if second.limit is None:
        return _Outcome(moved, second, note)
    limits.forget(second.engine)
    _log(ctx, original.id, second.engine, "limit",
         {"message": second.limit.message, "resets_at": second.limit.resets_at, "next": "pause"})
    return _Outcome(moved, second, note,
                    _pause_after_limit(ctx, original, worktree, moved, second, note))


def _landed(original: ControlTask, run: ControlTask) -> ControlTask:
    """The task as committed: its own goal, the engine and model that finished it."""
    return dataclasses.replace(original, engine=run.engine, model=run.model, effort=run.effort)


def _done_entry(task: ControlTask, result: ControlResult, worktree: TaskWorktree,
                landing, rerouted: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "task_id": result.task_id, "engine": result.engine, "ok": result.ok,
        "text": _redact(result.text), "diff_stat": _diff_stat(result.diff),
        **({"failure_mode": result.failure_mode} if result.failure_mode else {}),
        "unproven_reason": _redact(landing.unproven_reason),
        "branch": worktree.branch, "base_commit": worktree.base_commit,
        "commit": landing.commit or "",
        "worktree": str(worktree.path), "why": task.why,
        "verify": verify_payload(landing.verify) if landing.verify else None,
        "verdict": proof.task_verdict(landing.commit, landing.verify, task.verify,
                                      landing.unproven_reason, landing.proof),
        **({"proof": _redact(landing.proof)} if landing.proof else {}),
        **({"warnings": list(landing.warnings)} if landing.warnings else {}),
        "rerouted": rerouted, "model": task.model, "effort": task.effort, "paused": None,
    }


def _paused_entry(task: ControlTask, record: pauses.Pause, *, result: ControlResult | None = None,
                  worktree: TaskWorktree | None = None,
                  rerouted: dict[str, Any] | None = None) -> dict[str, Any]:
    after = f" after {limits.when(record.resets_at)}" if record.resets_at else ""
    return {
        "task_id": task.id, "engine": record.engine, "ok": False,
        "text": result.text if result else "",
        "diff_stat": _diff_stat(result.diff) if result else "+0/-0",
        "verdict": "not-committed",
        "unproven_reason": (f"paused: {record.reason}. Nothing was committed; the user resumes "
                            f"it on the Engines tab of the Hermes dashboard{after}."),
        "branch": worktree.branch if worktree else "",
        "base_commit": worktree.base_commit if worktree else "", "commit": "",
        "worktree": str(worktree.path) if worktree else "", "why": task.why, "verify": None,
        "rerouted": rerouted, "model": task.model, "effort": task.effort,
        "paused": {"id": record.id, "reason": record.reason, "resets_at": record.resets_at},
    }


def _failed_entry(task: ControlTask, reason: str, *,
                  worktree: TaskWorktree | None = None,
                  rerouted: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "task_id": task.id, "engine": task.engine, "ok": False, "text": "",
        "diff_stat": "+0/-0", "unproven_reason": reason,
        "branch": worktree.branch if worktree else "",
        "base_commit": worktree.base_commit if worktree else "", "commit": "",
        "worktree": str(worktree.path) if worktree else "", "why": task.why,
        "verify": None, "rerouted": rerouted, "model": task.model,
        "effort": task.effort, "paused": None,
    }


def _planner(session_id: str | None) -> dict[str, Any]:
    """Who planned the run, for the Runs view: the configured model, and the Hermes
    session whose conversation holds the planner's reasoning."""
    try:
        from hermes_cli.config import load_config_readonly

        model = (load_config_readonly() or {}).get("model") or {}
    except Exception:  # noqa: BLE001 - a label must never fail a run
        model = {}
    if not isinstance(model, dict):
        model = {"default": str(model)}
    return {"provider": model.get("provider"), "model": model.get("default"),
            "session_id": session_id or None}


def _live_lock_path(run_id: str) -> Path:
    return run_log_dir() / f"run-{run_id}.live.lock"


@contextmanager
def _resume_record_lock(run_id: str, record_id: str) -> Iterator[None]:
    """Keep one paused record from running twice across processes."""
    if fcntl is None:
        yield
        return
    path = run_log_dir() / f"run-{run_id}.resume-{record_id}.lock"
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NotReady("this paused task is already resuming") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _live_run_lock(run_id: str) -> Iterator[None]:
    """Hold an exclusive OS lock for as long as this process is running *run_id*, so a
    Resume from another process (the dashboard) can tell a live run from a crashed one.
    The OS drops the lock when the process exits, however it exits."""
    if fcntl is None:
        yield
        return
    # Keep the file: removing it would let another process lock a new inode.
    handle = None
    try:
        handle = _live_lock_path(run_id).open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
        logger.warning("hermes-control: could not take run %s live lock; continuing unlocked",
                       run_id, exc_info=True)
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _run_is_live(run_id: str) -> bool | None:
    """Whether some process holds *run_id*'s live lock; None when that cannot be told."""
    if fcntl is None:
        return None
    try:
        # "a+": a shared flock on a write-only handle fails (EBADF) on NFS.
        with _live_lock_path(run_id).open("a+") as handle:
            try:
                # Shared: concurrent probes (two Resume clicks) must not see each
                # other as the live run, which holds the lock exclusively.
                fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
    except OSError:
        logger.warning("hermes-control: could not probe run %s live lock", run_id,
                       exc_info=True)
        return None


def _summary_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """A logged compact summary entry in dispatch()'s shape: ``paused_id`` -> ``paused``."""
    current = dict(entry)
    paused_id = current.pop("paused_id", None)
    current["paused"] = {"id": paused_id} if paused_id else None
    return current


def _log_summary(log: RunLog, run_id: str, entries: Sequence[dict[str, Any]],
                 repo: str | None) -> list[dict[str, Any]]:
    """Settle these tasks in the log, so the Runs view knows they are over."""
    with _resume_summary_lock(run_id):
        return _log_summary_unlocked(log, run_id, entries, repo)


def _log_summary_unlocked(log: RunLog, run_id: str, entries: Sequence[dict[str, Any]],
                          repo: str | None) -> list[dict[str, Any]]:
    """_log_summary for a caller already holding _resume_summary_lock(run_id)."""
    compact = [{"task_id": e["task_id"], "engine": e["engine"], "ok": e["ok"],
                "commit": e["commit"], "branch": e["branch"], "worktree": e["worktree"],
                "base_commit": e["base_commit"], "text": e["text"],
                "unproven_reason": e["unproven_reason"], "rerouted": e["rerouted"],
                # What the task ran and proved, so a status rebuilt from the log
                # (a resume in the dashboard process) does not report stale values.
                # Only keys the entry has: an explicit null would be merged over a
                # full entry another process still holds.
                **{key: e[key] for key in ("diff_stat", "verify", "model", "effort", "failure_mode",
                                          "verdict", "proof", "warnings")
                   if key in e},
                "paused_id": (e.get("paused") or {}).get("id", e.get("paused_id"))} for e in entries]
    overlaps = find_overlaps(repo, entries)
    log.summary_seq = None
    try:
        # Another process may have appended since this log last read the file, and
        # summary seqs must increase in file order.
        log.continue_sequences()
    except Exception:  # noqa: BLE001 - still attempt to log the summary
        logger.warning("hermes-control: could not continue run %s log sequences",
                       run_id, exc_info=True)
    try:
        log.append_next(_PLAN_EVENT_TASK_ID, [RawEvent(kind="summary", payload={
            "tasks": compact, "overlaps": overlaps})],
                        engine=_PLAN_EVENT_ENGINE, now=time.time())
    except Exception:  # noqa: BLE001 - keep the results and warnings even if logging fails
        logger.exception("hermes-control: could not log run %s summary", run_id)
    return overlaps


@contextmanager
def _resume_summary_lock(run_id: str) -> Iterator[None]:
    """Serialize summary merges across Hermes and dashboard processes."""
    if fcntl is None:
        yield
        return
    # Keep the file: removing it would let another process lock a new inode.
    handle = None
    try:
        handle = (run_log_dir() / f"run-{run_id}.summary.lock").open("a")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except OSError:
        if handle is not None:
            handle.close()
        logger.warning("hermes-control: could not lock run %s summary; continuing unlocked",
                       run_id, exc_info=True)
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _log_resume_summary(log: RunLog, run_id: str, entries: Sequence[dict[str, Any]],
                        repo: str | None) -> None:
    """Retain every task's latest summary entry when resuming part of a run."""
    with _resume_summary_lock(run_id):
        with _REGISTRY_LOCK:
            finished = _FINISHED.get(run_id)
        latest = {entry["task_id"]: entry for entry in finished["tasks"]} if finished else {}
        summary_seq = finished.get("_summary_seq") if finished else None
        try:
            events = log.read_all()
        except Exception:  # noqa: BLE001 - retain in-memory results if the log is unreadable
            logger.warning("hermes-control: could not read run %s summary log", run_id,
                           exc_info=True)
            events = []
        for event in events:
            if event.kind == "summary" and (summary_seq is None or event.seq > summary_seq):
                for entry in event.payload.get("tasks", []):
                    task_id = entry.get("task_id")
                    if not task_id:
                        continue
                    latest[task_id] = _summary_entry(entry)
                summary_seq = max(summary_seq if summary_seq is not None else -1, event.seq)
        for entry in entries:
            latest[entry["task_id"]] = entry
        tasks = list(latest.values())
        overlaps = []
        log.summary_seq = None
        try:
            overlaps = _log_summary_unlocked(log, run_id, tasks, repo)
        finally:
            # On append failure retain the watermark already reflected in tasks,
            # so an older on-disk result cannot undo this resume next time. Only a
            # process that already tracks the run keeps an entry: the dashboard
            # process must not accumulate them.
            with _REGISTRY_LOCK:
                if run_id in _FINISHED:
                    summary = {**_FINISHED[run_id], "run_id": run_id, "run_log": str(log.path),
                               "state": "finished", "tasks": tasks,
                               "_summary_seq": (log.summary_seq if log.summary_seq is not None
                                                else summary_seq)}
                    if overlaps:
                        summary["overlaps"] = overlaps
                    else:
                        summary.pop("overlaps", None)
                    _FINISHED[run_id] = summary


def _log_planner(log: RunLog, run_id: str, payload: dict[str, Any]) -> None:
    """Append run-level planner state without letting logging affect a run."""
    try:
        log.append_next(_PLAN_EVENT_TASK_ID, [RawEvent(kind="planner", payload=payload)],
                        engine=_PLAN_EVENT_ENGINE, now=time.time())
    except Exception:  # noqa: BLE001 - planner state is best-effort observability
        logger.debug("hermes-control: could not append planner event", exc_info=True)


def _prepare(raw_tasks: Sequence[dict[str, Any]], *, run_id: str, log: RunLog,
            parent_cwd: str | None, timeout: int | None,
            planner_session: str | None) -> tuple[list[tuple[ControlTask, dict[str, Any] | None]],
                                                  _Context]:
    """Validate and order the tasks, route them to a live engine, and log the plan
    event - the half of dispatch() that must complete before any tool call can
    safely return, in either dispatch() or start_dispatch(). Raises ValueError,
    naming the offending field, for the caller to turn into the `error` summary."""
    # require_why is the runtime half of design decision D2a / plan deviation
    # #5: the schema marks `why` required, but only this makes it true.
    tasks = order_tasks(parse_tasks(raw_tasks, require_why=True))
    # Checked once per run, before any worktree exists: a signed-out engine's
    # tasks move to one that can run them, and with none usable nothing starts.
    status = engine_status(get_adapter)
    routed = route(tasks, status)
    # After routing, so a moved task gets its new engine's default model.
    try:
        policy = model_policy.load_policy()
    except ValueError as exc:
        raise ValueError(
            f"Hermes-Control model settings are invalid ({exc}); fix them on the dashboard's "
            "Engines tab or under plugins.entries.hermes_control.settings.models in "
            "config.yaml") from None
    routed = [(model_policy.apply_policy(task, policy), rerouted)
              for task, rerouted in routed]

    parallel_settings, parallel_error = _parallel()
    ctx = _Context(run_id=run_id, log=log, repo=parent_cwd,
                   signed_in=[name for name in ENGINE_NAMES if status.get(name) is None],
                   caps=_caps(), policy=policy, timeout=timeout, parallel_settings=parallel_settings)

    # Log the plan itself, before any task runs, so the log reads
    # plan-then-execution and each engine's `why` is on record even after the
    # conversation that carried the summary is gone (design doc D2a).
    log.append_next(_PLAN_EVENT_TASK_ID, [RawEvent(kind="plan", payload={
        **_plan_payload(routed), "repo": parent_cwd, "planner": _planner(planner_session),
        "parallel": {**parallel_settings, "error": parallel_error},
        "stall": ctx.stall_settings})],
        engine=_PLAN_EVENT_ENGINE, now=time.time())

    return routed, ctx


def _run_routed(ctx: _Context,
                routed: Sequence[tuple[ControlTask, dict[str, Any] | None]]
                ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run ready tasks within the configured slots; return entries and overlaps."""
    # Probed once, and only if some task reaches its verify step. cache alone
    # allows duplicate concurrent first calls, so protect the lazy probe too.
    probe_lock = threading.Lock()
    cached_sandbox = functools.cache(find_sandbox)

    def sandbox() -> str | None:
        with probe_lock:
            return cached_sandbox()

    settings = ctx.parallel_settings
    state_lock = threading.Lock()
    results: list[dict[str, Any] | None] = [None] * len(routed)
    paused_ids: set[str] = set()
    failed_ids: set[str] = set()
    settled: set[str] = set()
    active = {engine: 0 for engine in ENGINE_NAMES}
    pending = dict(enumerate(routed))

    def run_one(index: int, item: tuple[ControlTask, dict[str, Any] | None],
                admitted: threading.Event | None = None) -> None:
        task, rerouted = item
        worktree = None
        entries: list[dict[str, Any]] = []
        failed_task = paused_task = False
        counted_engine = None
        try:
            with state_lock:
                failed = next((d for d in task.depends_on if d in failed_ids), None)
                waiting = next((d for d in task.depends_on if d in paused_ids), None)
            if failed is not None:
                reason = f"skipped: depends on {failed}, which failed"
                _log(ctx, task.id, task.engine, "error", {"message": reason})
                entries.append(_failed_entry(task, reason, rerouted=rerouted))
                failed_task = True
                return
            if waiting is not None:
                record = _pause(ctx, task, worktree=None, engine=task.engine, session_id=None,
                                reason=f"waits for paused task {waiting}", until=None,
                                rerouted=rerouted)
                entries.append(_paused_entry(task, record, rerouted=rerouted))
                paused_task = True
                return
            # Admission's handshake preserves planned order without holding the
            # bookkeeping lock across usage reads, which may start a process or use the network.
            hold = limits.engine_hold(task.engine, caps=ctx.caps)
            if hold is not None:
                other = _free_engine(ctx, besides=task.engine)
                if other is None:
                    reason, until = _holds(ctx.signed_in, ctx.caps)
                    record = _pause(ctx, task, worktree=None, engine=task.engine,
                                    session_id=None, reason=reason, until=until,
                                    rerouted=rerouted)
                    entries.append(_paused_entry(task, record, rerouted=rerouted))
                    paused_task = True
                    return
                task, rerouted = _move(ctx, task, other, hold[0], rerouted)
                _log(ctx, task.id, task.engine, "rerouted", rerouted)
            with state_lock:
                if admitted is not None:
                    # A cap move uses the destination's slot. Defer without occupying
                    # a worker or a total slot when that engine is already full.
                    if active[task.engine] >= settings["per_engine"][task.engine]:
                        pending[index] = (task, rerouted)
                        return
                    active[task.engine] += 1
                    counted_engine = task.engine
                    admitted.set()
            # The worktree id is scoped to the run: task ids repeat across runs by
            # design, and a repeated id means `git worktree add -b` hits an existing
            # branch and refuses, which used to be reported as "not a git repo".
            worktree, failure = create_task_worktree_with_reason(
                ctx.repo, task.id, run_id=ctx.run_id)
            if worktree is None:
                entries.append(_failed_entry(task, failure, rerouted=rerouted))
                failed_task = True
                return
            outcome = _execute(ctx, task, task, rerouted, worktree, sandbox=sandbox)
            if outcome.pause is not None:
                entries.append(_paused_entry(outcome.task, outcome.pause, result=outcome.result,
                                             worktree=worktree, rerouted=outcome.rerouted))
                paused_task = True
                return
            landing = land(_landed(task, outcome.task), outcome.result, worktree,
                           run_id=ctx.run_id, sandbox=sandbox)
            try:
                ctx.log.append_next(task.id, landing.events,
                                    engine=outcome.result.engine, now=time.time())
            except Exception as exc:  # noqa: BLE001 - keep the committed result
                logger.exception("hermes-control: could not log task %s landing", task.id)
                reason = f"not verified: landing log failed: {type(exc).__name__}: {exc}"
                landing = dataclasses.replace(landing, unproven_reason=reason)
            entry = _done_entry(outcome.task, outcome.result, worktree, landing,
                                outcome.rerouted)
            entries.append(entry)
            if (not entry["ok"] or (task.verify and entry["verify"] is None)
                    or (entry["verify"] is not None and not entry["verify"]["passed"])):
                failed_task = True
        except Exception as exc:  # noqa: BLE001 - one task must not abandon the run
            reason = f"{type(exc).__name__}: {exc}"
            if worktree is not None:
                reason += f"; branch {worktree.branch} may already hold a commit"
            logger.exception("hermes-control: task %s failed unexpectedly", task.id)
            try:
                pauses.remove(pauses.pause_id(ctx.run_id, task.id))
            except Exception:  # noqa: BLE001 - report the original failure
                logger.exception("hermes-control: could not remove pause for task %s", task.id)
            try:
                _log(ctx, task.id, task.engine, "error", {"message": reason})
            except Exception:  # noqa: BLE001 - still return and continue if logging failed
                logger.exception("hermes-control: could not log task %s failure", task.id)
            entries.append(_failed_entry(task, reason, worktree=worktree,
                                         rerouted=rerouted))
            failed_task = True
        finally:
            with state_lock:
                if entries:
                    results[index] = entries[0]
                    if failed_task:
                        failed_ids.add(task.id)
                    if paused_task:
                        paused_ids.add(task.id)
                    settled.add(task.id)
                if counted_engine is not None:
                    active[counted_engine] -= 1
                if admitted is not None:
                    admitted.set()

    if settings["max_tasks"] == 1:
        # Preserve the original sequential event/decision order without a pool.
        for index, item in enumerate(routed):
            run_one(index, item)
    else:
        with ThreadPoolExecutor(max_workers=settings["max_tasks"]) as pool:
            futures = {}
            while pending or futures:
                with state_lock:
                    ready = next((index for index in sorted(pending)
                                  if all(d in settled for d in pending[index][0].depends_on)
                                  and active[pending[index][0].engine]
                                  < settings["per_engine"][pending[index][0].engine]), None)
                    if sum(active.values()) >= settings["max_tasks"]:
                        ready = None
                    item = pending.pop(ready) if ready is not None else None
                if item is not None:
                    admitted = threading.Event()
                    futures[pool.submit(run_one, ready, item, admitted)] = (ready, item[0])
                    # Admission checks/moves must finish in planned order before
                    # choosing the next task. Engines and landing run outside it.
                    admitted.wait()
                else:
                    done, _ = wait(futures, return_when=FIRST_COMPLETED)
                    for future in done:
                        index, task = futures.pop(future)
                        try:
                            future.result()
                        except Exception as exc:  # noqa: BLE001 - even a failure handler can fail
                            logger.exception("hermes-control: task %s escaped its handler", task.id)
                            with state_lock:
                                if results[index] is None:
                                    results[index] = _failed_entry(task, f"{type(exc).__name__}: {exc}",
                                                                   rerouted=routed[index][1])
                                    settled.add(task.id)
                                    failed_ids.add(task.id)
    entries = [entry for entry in results if entry is not None]
    overlaps = []
    try:
        overlaps = _log_summary(ctx.log, ctx.run_id, entries, ctx.repo)
    except Exception:  # noqa: BLE001 - task results survive a summary log failure
        logger.exception("hermes-control: could not log run %s summary", ctx.run_id)
    return entries, overlaps


def dispatch(raw_tasks: Sequence[dict[str, Any]], *, parent_cwd: str | None,
             run_id: str | None = None, timeout: int | None = None,
             planner_session: str | None = None) -> dict[str, Any]:
    """Validate, order and run every task. Returns a compact summary."""
    run_id = run_id or uuid.uuid4().hex[:8]
    log = RunLog(run_id)
    try:
        routed, ctx = _prepare(raw_tasks, run_id=run_id, log=log, parent_cwd=parent_cwd,
                               timeout=timeout, planner_session=planner_session)
    except ValueError as exc:
        return {"run_id": run_id, "run_log": str(log.path), "error": str(exc), "tasks": []}
    with _live_run_lock(run_id):
        try:
            entries, overlaps = _run_routed(ctx, routed)
        finally:
            _log_planner(log, run_id, {"phase": "settled"})
    _log_planner(log, run_id, {"phase": "returned", "state": "finished"})
    # No "error" key at all on success: Hermes reads any string result with `"error"` in
    # its opening characters as a failed call (agent/display.py _detect_tool_failure) and
    # feeds that to the loop guardrail, so even `"error": null` marks a clean run as failed.
    result = {"run_id": run_id, "run_log": str(log.path), "tasks": entries,
              "state": "finished"}
    if overlaps:
        result["overlaps"] = overlaps
    return result


def resume_blocker(record: pauses.Pause, *, signed_in: Sequence[str] | None = None,
                   caps: dict[str, int] | None = None) -> tuple[str, str | None]:
    """``(reason, None)`` when *record* cannot resume now, else ``("", engine)`` with
    the engine it would run on. *signed_in* defaults to every engine: the dashboard
    asks often, and the resume itself checks sign-in for real."""
    blocking = next((p for p in pauses.all_paused()
                     if p.run_id == record.run_id and p.id != record.id
                     and p.task["id"] in record.task.get("depends_on", ())), None)
    if blocking is not None:
        first = blocking.task["id"]
        return f"this task depends on {first}, which is still paused: resume {first} first", None
    signed_in = list(ENGINE_NAMES) if signed_in is None else list(signed_in)
    caps = caps if caps is not None else _caps()
    order = [record.engine] + [name for name in signed_in if name != record.engine]
    pick = next((name for name in order if name in signed_in
                 and limits.engine_hold(name, caps=caps) is None), None)
    if pick is None:
        reason, until = _holds(signed_in, caps)
        return reason + (f"; try again after {limits.when(until)}" if until else ""), None
    return "", pick


def resume_paused(record_id: str, *, timeout: int | None = None) -> dict[str, Any]:
    """Start a paused task again (docs/limits-and-resume.md §4.5) and land it like any
    task. Raises LookupError for an unknown id and NotReady when it cannot start yet."""
    record = pauses.load(record_id)
    if record is None:
        raise LookupError(f"no paused task {record_id!r}")
    thread = _THREADS.get(record.run_id)
    if thread is not None and thread.is_alive():
        raise NotReady("its run is still in progress; resume it after the run finishes")
    log = RunLog(record.run_id)
    try:
        events = log.read_all()
    except Exception as exc:  # noqa: BLE001 - report log failures to the dashboard
        raise NotReady(f"could not read its run log: {exc}") from exc
    last_ts = next((event.ts for event in reversed(events) if event.kind != "planner"),
                   record.paused_at)
    live = _run_is_live(record.run_id)
    if live:
        raise NotReady("its run is still in progress; resume it after the run finishes")
    if live is None:
        # No OS lock to ask: fall back on the log's evidence and a staleness guess.
        settled = (not events or record.run_id in _FINISHED
                   or any(event.kind == "summary" or (event.kind == "planner"
                          and event.payload.get("phase") in ("settled", "returned"))
                          for event in events))
        if not settled and time.time() - last_ts <= runs.STALE_SECONDS:
            raise NotReady("its run is still in progress; resume it after the run finishes")
    with _resume_record_lock(record.run_id, record_id):
        current = pauses.load(record_id)
        if current is None:
            raise NotReady("this paused task has already been resumed")
        record = current
        original = record.control_task()
        try:
            policy = model_policy.load_policy()
        except ValueError as exc:
            raise NotReady(f"Hermes-Control model settings are invalid ({exc})") from None
        status = engine_status(get_adapter)
        log.continue_sequences()
        ctx = _Context(run_id=record.run_id, log=log, repo=record.repo,
                       signed_in=[name for name in ENGINE_NAMES if status.get(name) is None],
                       caps=_caps(), policy=policy, timeout=timeout, parallel_settings=_parallel()[0])
        for name in ENGINE_NAMES:
            limits.forget(name)  # a reset is exactly what the user is waiting on
        reason, pick = resume_blocker(record, signed_in=ctx.signed_in, caps=ctx.caps)
        if pick is None:
            raise NotReady(reason)

        worktree = record.task_worktree()
        started = worktree is not None
        if worktree is not None and not worktree.path.is_dir():
            raise NotReady(f"its worktree {worktree.path} is gone; discard this task and run it again")

        rerouted = record.rerouted
        if pick == record.engine:
            rules = ctx.policy.get(pick)
            stale_model = (original.model is not None and
                           ((record.rerouted or {}).get("dropped_model") == original.model or
                            (rules is not None and rules.allowed and
                             original.model not in rules.allowed)))
            stale_effort = (original.effort is not None and
                            ((record.rerouted or {}).get("dropped_effort") == original.effort or
                             original.effort not in get_adapter(pick).efforts))
            saved = dataclasses.replace(original, model=None if stale_model else original.model,
                                        effort=None if stale_model or stale_effort else original.effort)
            run = model_policy.apply_policy(saved, ctx.policy)
            if started and record.session_id and get_adapter(pick).resumes_sessions:
                run = with_resume(run, session_id=record.session_id)
            elif started:
                run = with_continuation(run, previous=record.engine)
        else:
            hold = limits.engine_hold(record.engine, caps=ctx.caps)
            why = hold[0] if hold else f"{record.engine} is not signed in"
            run, rerouted = _move(ctx, original, pick, why, record.rerouted)
            if started:
                rerouted["continued"] = True
                run = with_continuation(run, previous=record.engine)
        if worktree is None:
            worktree, failure = create_task_worktree_with_reason(record.repo, original.id,
                                                                 run_id=record.run_id)
            if worktree is None:
                raise NotReady(failure)
        if pick != record.engine:
            _log(ctx, original.id, pick, "rerouted", rerouted)
        _log(ctx, original.id, pick, "resumed",
             {"id": record.id, "engine": pick, "session": run.resume_session,
              "model": run.model, "effort": run.effort})

        sandbox = functools.cache(find_sandbox)
        outcome = _execute(ctx, original, run, rerouted, worktree, sandbox=sandbox,
                           continued=started)
        if outcome.pause is not None:
            entry = _paused_entry(outcome.task, outcome.pause, result=outcome.result,
                                  worktree=worktree, rerouted=outcome.rerouted)
            entry["skipped_dependents"] = []
            try:
                _log_resume_summary(log, record.run_id, [entry], record.repo)
            except Exception:  # noqa: BLE001 - task result survives a summary log failure
                logger.exception("hermes-control: could not log run %s summary", record.run_id)
            return entry
        landing = land(_landed(original, outcome.task), outcome.result, worktree,
                       run_id=record.run_id, sandbox=sandbox)
        try:
            log.append_next(original.id, landing.events,
                            engine=outcome.result.engine, now=time.time())
        except Exception as exc:  # noqa: BLE001 - keep the committed result
            logger.exception("hermes-control: could not log task %s landing", original.id)
            reason = f"not verified: landing log failed: {type(exc).__name__}: {exc}"
            landing = dataclasses.replace(landing, unproven_reason=reason)
        entry = _done_entry(outcome.task, outcome.result, worktree, landing, outcome.rerouted)
        skipped_entries = []
        skipped_ids = []
        try:
            # Settle the dependents while this task's own record still exists, so
            # resume_blocker refuses them meanwhile; remove it in `finally`, because
            # the task has landed and must never be resumable a second time.
            if (not entry["ok"] or (original.verify and entry["verify"] is None)
                    or (entry["verify"] is not None and not entry["verify"]["passed"])):
                pending = [p for p in pauses.all_paused()
                           if p.run_id == record.run_id and p.id != record.id]
                failed_ids = {original.id}
                while True:
                    newly_skipped = [p for p in pending
                                     if p.task["id"] not in failed_ids
                                     and any(dep in failed_ids for dep in p.task.get("depends_on", ()))]
                    if not newly_skipped:
                        break
                    failed_ids.update(p.task["id"] for p in newly_skipped)
                for dependent in pending:
                    if dependent.task["id"] not in failed_ids:
                        continue
                    task = dependent.control_task()
                    failed_dependency = next(dep for dep in task.depends_on if dep in failed_ids)
                    reason = f"skipped: depends on {failed_dependency}, which failed"
                    pauses.remove(dependent.id)
                    _log(ctx, task.id, task.engine, "error", {"message": reason})
                    skipped_ids.append(task.id)
                    skipped_entries.append(_failed_entry(task, reason,
                                                         worktree=dependent.task_worktree(),
                                                         rerouted=dependent.rerouted))
        finally:
            pauses.remove(record.id)
        entry["skipped_dependents"] = skipped_ids
        try:
            _log_resume_summary(log, record.run_id, [entry, *skipped_entries], record.repo)
        except Exception:  # noqa: BLE001 - task result survives a summary log failure
            logger.exception("hermes-control: could not log run %s summary", record.run_id)
        return entry


# --- surviving the planner's tool-call deadline ------------------------------
#
# Hermes gives one sequential tool call a fixed deadline
# (`timeouts.tools.sequential_call`, 420s by default) and calls it timed out
# from the planner's side even when the workers it started keep running and
# finish fine - the run is never lost (the log is still being written), but the
# planner has no way back to it short of digging the run id out of the run log
# by hand, and a real risk of dispatching the same tasks again beside the run
# still going. Hermes exempts its own `delegate_task` from that deadline
# (agent/tool_executor.py `_SEQUENTIAL_DEADLINE_EXEMPT_TOOLS`); a plugin cannot
# join that list, so `start_dispatch` copes on its own: it runs dispatch()'s
# task loop on a daemon thread and waits up to `wait` seconds for it, handing
# back an ordinary summary if that is enough time and a "running" placeholder
# with the run id otherwise. `control_status` is how the planner checks back.

_THREADS: dict[str, threading.Thread] = {}
_FINISHED: dict[str, dict[str, Any]] = {}
_REGISTRY_LOCK = threading.Lock()
MAX_STATUS_WAIT_SECONDS = 300


def _live_status_tasks(view: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The per-task ``id``/``engine``/``status`` view for a run that has not
    settled yet - all a run_view() gives while tasks are still moving."""
    if view is None:
        return []
    return [{"id": task["id"],
             "engine": (task["attempts"][-1]["engine"] if task["attempts"]
                       else task["planned_engine"]),
             "status": task["status"]} for task in view["tasks"]]


def _entry_from_view(task: dict[str, Any]) -> dict[str, Any]:
    """dispatch()'s entry shape, rebuilt from one of run_view()'s tasks - the
    closest reconstruction the run log allows for a run this process did not
    start (and so has no in-memory summary for). ``ok`` mirrors the engine's own
    last result, exactly as ``result.ok`` does in dispatch()'s own entry - not
    whether the task ended up proven, which is what ``status`` folds together.
    ``diff_stat`` has no source in the log at all. ``unproven_reason`` uses the
    summary when available, otherwise the status computed from the events."""
    last = task["attempts"][-1] if task["attempts"] else None
    engine = last["engine"] if last else task["planned_engine"]
    ok = bool(last and last.get("result") and last["result"].get("ok"))
    done = task["status"] == "done"
    commit = task["commit"] or {}
    paused = None
    if task["paused"]:
        paused = {"id": task["paused"].get("id"), "reason": task["paused"].get("reason"),
                  "resets_at": task["paused"].get("resets_at")}
    summary = task.get("summary") or {}
    return {
        "task_id": task["id"], "engine": engine, "ok": ok,
        "text": (last or {}).get("last_text", ""),
        "diff_stat": "", "unproven_reason": summary.get(
            "unproven_reason", None if done else task["status_reason"]),
        "branch": task["branch"], "commit": commit.get("sha") or "", "worktree": task["worktree"],
        "why": task["why"], "verify": task["verify"], "rerouted": task["rerouted"],
        "model": task["model"], "effort": task["effort"], "paused": paused,
        **{key: summary[key] for key in ("verdict", "proof", "warnings") if key in summary},
    }


def start_dispatch(raw_tasks: Sequence[dict[str, Any]], *, parent_cwd: str | None,
                   timeout: int | None = None, planner_session: str | None = None,
                   wait: float) -> dict[str, Any]:
    """Like dispatch(), but never lets the run outlast the planner's tool call
    unreported: validates and routes the tasks exactly as dispatch() does (so a
    validation error still comes back at once, with no thread started), then
    runs the rest of dispatch() on a daemon thread and waits up to *wait*
    seconds for it. A run that finishes in time comes back exactly as
    dispatch() would report it. One that does not comes back with
    ``state: "running"`` and the run id; the thread keeps going regardless, and
    `control_status` reports on it from here on - the planner must never
    dispatch the same tasks again."""
    run_id = uuid.uuid4().hex[:8]
    log = RunLog(run_id)
    try:
        routed, ctx = _prepare(raw_tasks, run_id=run_id, log=log, parent_cwd=parent_cwd,
                               timeout=timeout, planner_session=planner_session)
    except ValueError as exc:
        return {"run_id": run_id, "run_log": str(log.path), "error": str(exc), "tasks": []}

    finished = threading.Event()
    summary_box: list[dict[str, Any]] = []

    def _work() -> None:
        try:
            with _live_run_lock(run_id):
                _work_locked()
        finally:
            finished.set()  # only once the live lock is released

    def _work_locked() -> None:
        entries: list[dict[str, Any]] = []
        overlaps = []
        note = None
        try:
            entries, overlaps = _run_routed(ctx, routed)
        except Exception as exc:  # noqa: BLE001 - always settle a background run
            note = f"dispatch failed unexpectedly: {type(exc).__name__}: {exc}"
            logger.exception("hermes-control: background dispatch %s failed", run_id)
            entries = [_failed_entry(task, note, rerouted=rerouted)
                       for task, rerouted in routed]
            try:
                overlaps = _log_summary(log, run_id, entries, ctx.repo)
            except Exception:  # noqa: BLE001 - in-memory status still settles
                logger.exception("hermes-control: could not log failed run %s", run_id)
        finally:
            summary = {"run_id": run_id, "run_log": str(log.path), "tasks": entries,
                       "state": "finished"}
            if overlaps:
                summary["overlaps"] = overlaps
            if note:
                summary["note"] = note
            summary_box.append(summary)
            with _REGISTRY_LOCK:
                _FINISHED[run_id] = {**summary, "_summary_seq": log.summary_seq}
                _THREADS.pop(run_id, None)
            _log_planner(log, run_id, {"phase": "settled"})

    _log_planner(log, run_id, {"phase": "waiting", "wait_seconds": wait})
    wait_started = time.monotonic()
    thread = threading.Thread(target=_work, daemon=True)
    with _REGISTRY_LOCK:
        _THREADS[run_id] = thread
    thread.start()

    if finished.wait(wait):
        _log_planner(log, run_id, {"phase": "returned", "state": "finished"})
        return summary_box[0]
    _log_planner(log, run_id, {"phase": "returned_running",
                               "waited_seconds": time.monotonic() - wait_started})
    return {"run_id": run_id, "run_log": str(log.path), "state": "running",
           "tasks": _live_status_tasks(runs.run_view(run_id)),
           "note": f"still running after {wait}s; call control_status with this run_id - "
                   "do not dispatch these tasks again"}


def control_status(run_id: str, wait: float = 0.0) -> dict[str, Any]:
    """The current state and per-task summary of a run `start_dispatch` began -
    from this process for runs it started, and from the run log for other
    runs, so the planner never has to dig a run's outcome out of the log by hand."""
    if not runs._RUN_ID.fullmatch(run_id or ""):
        return {"error": f"no run {run_id}"}
    with _REGISTRY_LOCK:
        thread = _THREADS.get(run_id)
    waited_seconds = 0.0
    if thread is not None and thread.is_alive() and wait > 0:
        wait_started = time.monotonic()
        thread.join(timeout=min(wait, MAX_STATUS_WAIT_SECONDS))
        waited_seconds = time.monotonic() - wait_started
    if thread is not None and thread.is_alive():
        result = {"run_id": run_id, "state": "running",
                  "tasks": _live_status_tasks(runs.run_view(run_id))}
        _log_status_checked(run_id, result["state"], waited_seconds)
        return result
    with _REGISTRY_LOCK:
        finished = _FINISHED.get(run_id)
    try:
        summaries = [event for event in RunLog(run_id).read_all() if event.kind == "summary"]
    except Exception:  # noqa: BLE001 - a log outage must not hide a cached result
        if finished is None:
            raise
        summaries = []
        logger.warning("hermes-control: could not read run %s summary log", run_id,
                       exc_info=True)
    latest = summaries[-1] if summaries else None
    cached_seq = finished.get("_summary_seq") if finished else None
    refresh = (finished is not None and latest is not None
               and (cached_seq is None or latest.seq > cached_seq))
    if finished is not None and not refresh:
        result = {"run_id": run_id, "state": "finished", "tasks": finished["tasks"]}
        if finished.get("overlaps"):
            result["overlaps"] = finished["overlaps"]
        if finished.get("note"):
            result["note"] = finished["note"]
        _log_status_checked(run_id, result["state"], waited_seconds)
        return result
    if finished is not None:
        # A newer summary (a dashboard resume): its entries replace the cached ones
        # for the tasks it names; every other task keeps its full cached entry.
        changed = {entry["task_id"]: _summary_entry(entry)
                   for entry in latest.payload.get("tasks", []) if entry.get("task_id")}
        tasks = [{**entry, **changed.pop(entry["task_id"], {})} for entry in finished["tasks"]]
        tasks.extend(changed.values())
        result = {"run_id": run_id, "state": "finished", "tasks": tasks}
        overlaps = latest.payload.get("overlaps")
        if overlaps:
            result["overlaps"] = overlaps
        if finished.get("note"):
            result["note"] = finished["note"]
        with _REGISTRY_LOCK:
            _FINISHED[run_id] = {**result, "_summary_seq": latest.seq}
        _log_status_checked(run_id, result["state"], waited_seconds)
        return result
    view = runs.run_view(run_id)
    if view is None:
        return {"error": f"no run {run_id}"}
    if view["state"] in ("finished", "paused"):
        result = {"run_id": run_id, "state": "finished",
                  "tasks": [_entry_from_view(task) for task in view["tasks"]]}
        overlaps = latest.payload.get("overlaps") if latest else []
        if overlaps:
            result["overlaps"] = overlaps
    else:
        result = {"run_id": run_id, "state": "stopped", "tasks": _live_status_tasks(view)}
    _log_status_checked(run_id, result["state"], waited_seconds)
    return result


def _log_status_checked(run_id: str, state: str, waited_seconds: float) -> None:
    """Best-effort: an unreadable log must never hide a status result."""
    try:
        _log_planner(_continued_log(run_id), run_id,
                     {"phase": "status_checked", "state": state,
                      "waited_seconds": waited_seconds})
    except Exception:  # noqa: BLE001 - observability only
        logger.debug("hermes-control: could not log status check for run %s", run_id,
                     exc_info=True)


def _continued_log(run_id: str) -> RunLog:
    """A RunLog that appends after the sequences already on disk (a fresh RunLog
    would restart every counter at 0)."""
    log = RunLog(run_id)
    log.continue_sequences()
    return log
