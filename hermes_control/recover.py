"""Tasks of a run whose Hermes process died, turned into paused, resumable tasks.

The gateway that runs a dispatch ends with ``os._exit``, so an idle exit, a restart,
a kill or a power loss leaves the run's unsettled tasks with no commit and no pause
record. The OS drops the run's live lock whenever the process dies, however it dies:
a reader that finds the lock free and no ``summary`` event in the log knows the run
is over. ``reconcile`` runs on read (control_status, the Runs view) and settles such
a run from its log alone - never from the Runs view's 35-minute staleness guess, so
a run that died hours ago is recovered just the same.
"""

from __future__ import annotations

from contextlib import contextmanager
import logging
from pathlib import Path
import time
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # Windows has no flock; nothing can be told there.
    fcntl = None

from . import dispatch, pauses, proof, runs
from .events import ControlEvent
from .landing import NO_VERIFY_REASON, verify_payload
from .runlog import RunLog, run_log_dir
from .verify import VerifyOutcome
from .worktree import TaskWorktree, _run_git, worktree_id

logger = logging.getLogger(__name__)

INTERRUPTED_REASON = "the Hermes process running this run ended before the task finished"
UNPROVEN_REASON = "the Hermes process running this run ended before the check finished"
# A run that wrote an event this recently may still be between two writes.
GRACE_SECONDS = 60.0


def reconcile(run_id: str) -> bool:
    """Pause the unsettled tasks of *run_id* if its process is gone. True only when
    something was written. Never raises: a status read or a page must not break."""
    try:
        return _reconcile(run_id)
    except Exception:  # noqa: BLE001 - recovery is best-effort on every read path
        logger.warning("hermes-control: could not reconcile run %s", run_id, exc_info=True)
        return False


def _over(events: list[ControlEvent]) -> bool:
    """No summary, and nothing but planner phases for the last GRACE_SECONDS."""
    if not events or any(event.kind == "summary" for event in events):
        return False
    newest = next((event for event in reversed(events) if event.kind != "planner"), None)
    return newest is not None and time.time() - newest.ts > GRACE_SECONDS


def _resume_lock_held(run_id: str) -> bool:
    for path in run_log_dir().glob(f"run-{run_id}.resume-*.lock"):
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    return False


@contextmanager
def _live_lock(run_id: str) -> Iterator[bool]:
    """Take the run's live lock exclusively, so no process starts the run again (or
    reconciles it) meanwhile. Yields False when the lock is busy."""
    with dispatch._live_lock_path(run_id).open("a+") as handle:
        # A concurrent reader's probe holds the lock for a moment: try a few times.
        for attempt in range(4):
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if attempt == 3:
                    yield False
                    return
                time.sleep(0.05)
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _reconcile(run_id: str) -> bool:
    if fcntl is None or not runs._RUN_ID.fullmatch(run_id or ""):
        return False
    log = RunLog(run_id)
    if not _over(log.read_all()):
        return False
    if dispatch._run_is_live(run_id) is not False:  # None: cannot tell, so do nothing
        return False
    if _resume_lock_held(run_id):
        return False
    with _live_lock(run_id) as held:
        if not held:
            return False
        events = log.read_all()  # another reader may have settled it meanwhile
        if not _over(events):
            return False
        return _settle(log, run_id, events)


def _tree(data: dict[str, Any] | None) -> TaskWorktree | None:
    if not data:
        return None
    return TaskWorktree(path=Path(data["path"]), branch=data["branch"],
                        repo_root=Path(data["repo_root"]),
                        base_commit=data.get("base_commit", ""))


def _worktrees_on_disk(repo: str | None) -> dict[str, TaskWorktree]:
    """The repo's existing worktrees by branch. Each one's HEAD stands for its base
    commit: a task that died before spawning its engine never moved it."""
    found: dict[str, TaskWorktree] = {}
    if not repo:
        return found
    try:
        from tools.subagent_worktree import resolve_repo_root

        root = resolve_repo_root(repo)
        listed = _run_git(["worktree", "list", "--porcelain"], cwd=str(root)) if root else None
        for block in (listed.stdout.split("\n\n") if listed and not listed.returncode else ()):
            fields = dict(line.partition(" ")[::2] for line in block.splitlines())
            branch = fields.get("branch", "").removeprefix("refs/heads/")
            if branch and Path(fields.get("worktree", "")).is_dir():
                found[branch] = TaskWorktree(path=Path(fields["worktree"]), branch=branch,
                                             repo_root=Path(root),
                                             base_commit=fields.get("HEAD", ""))
    except Exception:  # noqa: BLE001 - a missing worktree only means nothing to resume in
        logger.warning("hermes-control: could not list the worktrees of %s", repo, exc_info=True)
    return found


_ACTIVITY = ("text", "thinking", "tool_start", "tool_end", "permission_request",
             "result", "error")


def _final_result(mine: list[ControlEvent]) -> ControlEvent | None:
    """The engine's last word on its latest attempt: a ``result`` or ``error`` that
    nothing else from the engine follows. A limit, a reroute or a resume starts a new
    attempt, so an earlier attempt's result is not final."""
    start = max((i + 1 for i, e in enumerate(mine)
                 if e.kind in ("limit", "rerouted", "resumed")), default=0)
    last = next((e for e in reversed(mine[start:]) if e.kind in _ACTIVITY), None)
    if last is None or last.kind not in ("result", "error") or last.raw_kind == "parse_failure":
        return None
    return last


def _outcome(final: ControlEvent) -> tuple[bool, str]:
    ok = final.kind == "result" and bool(final.payload.get("ok", True))
    return ok, "" if ok else str(final.payload.get("message") or "engine reported failure")


def _last_text(mine: list[ControlEvent]) -> str:
    text = next((e.payload.get("text") for e in reversed(mine)
                 if e.kind == "text" and e.payload.get("text")), "")
    return dispatch._redact(text or "")


def _committed_entry(task, mine: list[ControlEvent], commit: ControlEvent, engine: str,
                     tree: TaskWorktree | None, rerouted) -> dict[str, Any]:
    """The summary entry of a task whose commit is in the log, from what the log kept
    of its check: the normal landing's reason when it ran, ours when it never did."""
    sha = commit.payload.get("sha")
    verify = next((e.payload for e in reversed(mine) if e.kind == "verify"), None)
    evidence = next((e.payload for e in reversed(mine) if e.kind == "proof"), None)
    if not sha:
        reason = commit.payload.get("reason") or "not committed"
        entry = dispatch._failed_entry(task, reason, worktree=tree, rerouted=rerouted)
        entry.update(engine=commit.engine or engine, verdict="not-committed",
                     text=_last_text(mine))
        return entry
    # A commit without a `verify` event is logged by a landing that ran no check at all:
    # a crash inside land() leaves no commit event, so the check never "died".
    reason = UNPROVEN_REASON if task.verify else NO_VERIFY_REASON
    outcome = None
    if verify is not None:
        outcome = VerifyOutcome(
            command=verify.get("command") or "", passed=bool(verify.get("passed")),
            exit_code=verify.get("exit_code"), output_tail=verify.get("output_tail") or "",
            unproven_reason=verify.get("unproven_reason"))
        reason = verify.get("unproven_reason")
        if reason is None and not outcome.passed:
            reason = ("verification timed out" if outcome.exit_code is None else
                      f"verification failed (exit {outcome.exit_code})")
    entry = dispatch._failed_entry(task, reason, worktree=tree, rerouted=rerouted)
    entry.update(engine=commit.engine or engine, ok=True, text=_last_text(mine), commit=sha,
                 branch=commit.payload.get("branch") or entry["branch"])
    entry["verdict"] = proof.task_verdict(sha, outcome, task.verify, reason, evidence)
    if outcome is not None:
        entry.update(
            verify=verify_payload(outcome),
            **({"proof": dispatch._redact(evidence)} if evidence else {}))
    return entry


def _landed_entry(task, mine: list[ControlEvent], engine: str, final: ControlEvent,
                  tree: TaskWorktree, rerouted, sha: str) -> dict[str, Any]:
    """A task whose branch holds a commit its log never recorded: unproven, as the check
    that followed the commit died with the process."""
    entry = dispatch._failed_entry(task, UNPROVEN_REASON, worktree=tree, rerouted=rerouted)
    entry.update(engine=final.engine or engine, ok=True, text=_last_text(mine), commit=sha,
                 verdict=proof.task_verdict(sha, None, task.verify, UNPROVEN_REASON))
    return entry


def _failed(entry: dict[str, Any], task) -> bool:
    """dispatch's rule for a task its dependents must skip."""
    if entry.get("paused"):
        return False
    verify = entry.get("verify")
    return (not entry["ok"] or bool(task.verify and verify is None)
            or (verify is not None and not verify["passed"]))


def _dependency_order(planned: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The plan's tasks, each after the tasks it depends on (plan order where it already
    is). Unknown ids and cycles are ignored."""
    by_id = {item["id"]: item for item in planned}
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(item: dict[str, Any]) -> None:
        if item["id"] in seen:
            return
        seen.add(item["id"])
        for dep in item.get("depends_on") or ():
            if dep in by_id:
                visit(by_id[dep])
        ordered.append(item)

    for item in planned:
        visit(item)
    return ordered


def _moved_commit(tree: TaskWorktree | None) -> str | None:
    """The task branch's head when it moved past the worktree's base commit: the engine's
    change was committed, though the landing's events were never logged."""
    if tree is None or not tree.base_commit:
        return None
    try:
        found = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{tree.branch}"],
                         cwd=str(tree.repo_root))
    except Exception:  # noqa: BLE001 - an unreadable branch keeps the pause
        return None
    sha = found.stdout.strip()
    return sha if not found.returncode and sha and sha != tree.base_commit else None


def _settle(log: RunLog, run_id: str, events: list[ControlEvent]) -> bool:
    plan = next((event.payload for event in reversed(events) if event.kind == "plan"), None)
    if not plan or not plan.get("tasks"):
        return False
    if not all(planned.get("goal") for planned in plan["tasks"]):
        # A log from before plans recorded goals: a paused task could not be resumed.
        return False
    repo = plan.get("repo")
    log.continue_sequences()
    ctx = dispatch._Context(run_id=run_id, log=log, repo=repo, signed_in=[], caps={}, policy={},
                            timeout=None, stall_settings={})
    settled: dict[str, dict[str, Any]] = {}
    failed_ids: set[str] = set()
    wrote = skipped = False
    on_disk: dict[str, TaskWorktree] | None = None

    def add(task, entry: dict[str, Any]) -> None:
        settled[task.id] = entry
        if _failed(entry, task):
            failed_ids.add(task.id)

    for planned in _dependency_order(plan["tasks"]):
        task_id = planned["id"]
        mine = [event for event in events if event.task_id == task_id]
        spawned = next((e.payload for e in reversed(mine) if e.kind == "spawned"), None) or {}
        tree = _tree(spawned.get("worktree"))
        if tree is None:
            # The worktree exists before the engine slot wait, which logs nothing a
            # reader could use: a process that died there left no `spawned` event.
            if on_disk is None:
                on_disk = _worktrees_on_disk(repo)
            tree = on_disk.get(f"hermes-subagent/subagent-{worktree_id(run_id, task_id)}")
        reroute = next((e.payload for e in reversed(mine) if e.kind == "rerouted"), None)
        engine = next((e.engine for e in reversed(mine) if e.engine), planned.get("engine"))
        sessions = [e for e in mine if e.kind == "session"]
        session_id = (sessions[-1].payload.get("session_id")
                      if sessions and sessions[-1].engine == engine else None)
        model, effort = planned.get("model"), planned.get("effort")
        moved = next((e.payload for e in reversed(mine)
                      if e.kind in ("rerouted", "resumed") and "model" in e.payload), None)
        if moved is not None:
            model, effort = moved.get("model"), moved.get("effort")
        task = pauses.task_from_dict({
            "id": task_id, "goal": planned.get("goal") or "", "engine": engine,
            "policy": planned.get("policy"), "files_hint": planned.get("files_hint"),
            "depends_on": planned.get("depends_on"), "why": planned.get("why"),
            "model": model, "effort": effort, "verify": planned.get("verify"),
            "red": planned.get("red")})
        rerouted = reroute if reroute is not None else planned.get("rerouted")
        pause_id = pauses.pause_id(run_id, task_id)

        commit = next((e for e in reversed(mine) if e.kind == "commit"), None)
        if commit is not None:
            # Committed work must never run again: report what its log says happened.
            add(task, _committed_entry(task, mine, commit, engine, tree, rerouted))
            continue
        existing = pauses.load(pause_id)
        if existing is not None:  # paused before the crash, e.g. by a limit
            add(task, dispatch._paused_entry(
                existing.control_task(), existing, worktree=existing.task_worktree(),
                rerouted=existing.rerouted))
            continue
        error = next((e.payload.get("message") or "" for e in reversed(mine)
                      if e.kind == "error"), "")
        if error.startswith("skipped:"):
            add(task, dispatch._failed_entry(task, error, worktree=tree, rerouted=rerouted))
            continue
        final = _final_result(mine)
        if final is not None:
            ok, message = _outcome(final)
            if not task.policy.write or not ok:
                # Nothing is left to land: a read-only task is done with its result, and a
                # write task whose engine failed has no change to commit.
                entry = dispatch._failed_entry(task, None if ok else message, worktree=tree,
                                               rerouted=rerouted)
                entry.update(engine=final.engine or engine, ok=ok, text=_last_text(mine))
                add(task, entry)
                continue
            moved_sha = _moved_commit(tree)
            if moved_sha is not None:
                # It died inside land(), after the commit and before the landing's events
                # were logged: resuming would run the engine on top of its own commit.
                add(task, _landed_entry(task, mine, engine, final, tree, rerouted, moved_sha))
                continue
        failed = next((d for d in task.depends_on if d in failed_ids), None)
        if failed is not None and final is None:
            reason = f"skipped: depends on {failed}, which failed"
            dispatch._log(ctx, task_id, engine, "error", {"message": reason})
            wrote = True
            add(task, dispatch._failed_entry(task, reason, worktree=tree, rerouted=rerouted))
            continue
        # No final result, or a write task whose ok result died while landing.
        try:
            with dispatch._resume_record_lock(run_id, pause_id):
                if not any(e.kind == "interrupted" for e in mine):
                    dispatch._log(ctx, task_id, engine, "interrupted",
                                  {"reason": INTERRUPTED_REASON, "id": pause_id})
                    wrote = True
                record = dispatch._pause(
                    ctx, task, worktree=tree, engine=engine, session_id=session_id,
                    reason=INTERRUPTED_REASON, until=None, rerouted=rerouted,
                    pid=spawned.get("pid"), started=spawned.get("started"))
                wrote = True
        except dispatch.NotReady:  # a resume of this record is starting
            skipped = True
            continue
        add(task, dispatch._paused_entry(task, record, worktree=tree, rerouted=rerouted))
    if skipped:
        # Leave the run open: a later read settles the task that was skipped.
        return wrote
    entries = [settled[item["id"]] for item in plan["tasks"] if item["id"] in settled]
    dispatch._log_summary(log, run_id, entries, repo)
    return True
