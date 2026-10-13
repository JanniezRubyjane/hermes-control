"""What the controller can claim about a finished task: commit it, then check it.

``run_task`` reports what the engine did. This decides what reaches the task
branch and what the summary may call proven. A task is proven only when its
change is committed on the branch AND the planner's ``verify`` command passed in
the sandbox in a detached, clean checkout of that commit. The checkout is
removed afterwards, so nothing the check writes rides along on the branch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from . import proof
from .engines.base import ControlTask
from .events import EMPTY_DIFF_REASON, ControlResult, RawEvent
from .verify import VerifyOutcome, run_verify
from .worktree import (
    TaskWorktree,
    VerifyCheckoutError,
    commit_worktree,
    committed_claude_paths,
    red_checkout,
    uncommitted_worktree_paths,
    verify_checkout,
)

NO_VERIFY_REASON = "not verified: no verify command was given"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Landing:
    commit: str | None
    verify: VerifyOutcome | None
    unproven_reason: str | None
    events: tuple[RawEvent, ...] = ()
    proof: dict[str, Any] | None = None
    warnings: tuple[str, ...] = ()


def commit_message(task: ControlTask, run_id: str) -> str:
    from agent.redact import redact_sensitive_text

    goal = redact_sensitive_text(task.goal, force=True)
    return (f"hermes-control: {task.id} ({task.engine})\n\n{goal}\n\n"
            f"Hermes-Control-Run: {run_id}\n")


def verify_payload(outcome: VerifyOutcome) -> dict[str, Any]:
    from agent.redact import redact_sensitive_text

    return {"command": outcome.command, "passed": outcome.passed,
            "exit_code": outcome.exit_code,
            "output_tail": redact_sensitive_text(outcome.output_tail, force=True)}


def _red_proof(task: ControlTask, worktree: TaskWorktree, sha: str, *,
               settings: dict[str, Any], sandbox: Callable[[], str | None],
               verify: VerifyOutcome) -> dict[str, Any]:
    evidence = {"verdict": "red-inconclusive", "reason": None, "test_paths": [],
                "red": {"command": task.red, "exit_code": None, "output_tail": ""}}
    try:
        changed = proof.changed_paths(worktree, sha)
        paths = [path for path, status in changed.items()
                 if status in ("A", "M") and proof.is_test_path(path, settings["test_globs"])]
        evidence["test_paths"] = paths
        if not paths:
            evidence.update(verdict="red-skipped", reason="no test files changed")
            return evidence
        with red_checkout(worktree, worktree.base_commit, sha, paths) as checkout:
            codex_path = sandbox()
            red = run_verify(task.red, checkout, codex_path=codex_path)
            evidence["red"] = {key: value for key, value in verify_payload(red).items()
                               if key != "passed"}
        missing_symbol = (red.exit_code == 2 and red.exit_code not in settings["red_exit_codes"]
                          and any(marker in red.output_tail for marker in (
                              "ImportError while importing test module", "cannot import name",
                              "ModuleNotFoundError: No module named", "has no attribute")))
        if red.exit_code == 0 and red.passed:
            evidence.update(verdict="red-passed", reason="the new tests also pass without the change")
        elif (red.exit_code is not None and red.exit_code in settings["red_exit_codes"]
              and not (red.unproven_reason or "").startswith("verification timed out")):
            evidence.update(verdict="red-green-proven")
        elif missing_symbol:
            evidence.update(verdict="red-green-proven",
                            reason="red: the new tests could not import what the change adds")
        else:
            evidence["reason"] = red.unproven_reason or f"red command exited {red.exit_code}"
            return evidence
        if task.red == task.verify:
            green = verify
        else:
            with verify_checkout(worktree, sha) as checkout:
                green = run_verify(task.red, checkout, codex_path=sandbox())
        if not green.passed or green.exit_code != 0:
            evidence.update(verdict="red-inconclusive",
                            reason="red command does not pass on the task commit")
    except Exception as exc:  # noqa: BLE001 - proof is advisory, even on checkout failure
        evidence.update(verdict="red-inconclusive", reason=f"{type(exc).__name__}: {exc}")
    return evidence


def land(task: ControlTask, result: ControlResult, worktree: TaskWorktree, *,
         run_id: str, sandbox: Callable[[], str | None]) -> Landing:
    """Commit a successful task's change, then run its check if it has one.

    *sandbox* returns the codex path lazily, so a run with no ``verify`` command
    never probes for it.
    """
    # Partial work from a failed or timed-out run stays in the worktree for a
    # human to inspect; committed, it would be one `git merge` from the working branch.
    if result.unproven_reason is not None:
        return Landing(None, None, result.unproven_reason)
    if not result.ok:
        return Landing(None, None, "engine reported failure")
    if not result.diff:
        return Landing(None, None, EMPTY_DIFF_REASON)

    sha, failure = commit_worktree(worktree, message=commit_message(task, run_id))
    if sha is None:
        reason = f"not committed: {failure or 'git found nothing to commit'}"
        return Landing(None, None, reason,
                       (RawEvent(kind="commit", payload={"sha": None, "reason": reason}),))
    committed = RawEvent(kind="commit", payload={"sha": sha, "branch": worktree.branch})
    warning = None
    outcome = None
    evidence = None
    warnings = []
    events = [committed]

    def landed(reason):
        return Landing(sha, outcome, reason, tuple(events), evidence, tuple(warnings))

    settings = proof.parse_settings(None)
    try:
        settings = proof.read_settings()
        changed = proof.changed_paths(worktree, sha)
        warnings = proof.advisory_warnings(task, worktree, sha, changed, settings["test_globs"])
    except Exception:  # noqa: BLE001 - advisory inspection cannot affect verification
        logger.warning("hermes-control: could not inspect task %s for check gaming", task.id,
                       exc_info=True)
    try:
        paths = committed_claude_paths(worktree, sha)
        warning = (f"warning: committed diff changes .claude/ ({', '.join(paths)}); "
                   "review Claude settings before merging" if paths else None)
        if not task.verify:
            reason = f"{NO_VERIFY_REASON}; {warning}" if warning else NO_VERIFY_REASON
            return landed(reason)

        try:
            with verify_checkout(worktree, sha) as checkout:
                outcome = run_verify(task.verify, checkout, codex_path=sandbox())
                events.append(RawEvent(kind="verify", payload={
                    **verify_payload(outcome), "unproven_reason": outcome.unproven_reason}))
        except VerifyCheckoutError as exc:
            reason = (f"not verified: could not create a clean checkout of {sha} "
                      f"({exc})")
            return landed("; ".join(filter(None, (reason, warning))))
        hint = None
        if outcome.exit_code not in (None, 0) or (outcome.unproven_reason or "").startswith(
                "verification timed out"):
            paths = uncommitted_worktree_paths(worktree)
            if paths:
                hint = ("task worktree has files not in the commit: "
                        f"{', '.join(paths)} (the check ran without them)")
        reason = "; ".join(filter(None, (outcome.unproven_reason, hint, warning))) or None
        events[-1] = RawEvent(kind="verify", payload={
            **verify_payload(outcome), "unproven_reason": reason})
        if outcome.passed and outcome.exit_code == 0 and task.red:
            evidence = _red_proof(task, worktree, sha, settings=settings, sandbox=sandbox,
                                  verify=outcome)
            events.append(RawEvent(kind="proof", payload=evidence))
            if evidence["verdict"] == "red-passed":
                warnings.append("red_passed: the new tests also pass without the change")
        return landed(reason)
    except Exception as exc:  # noqa: BLE001 - a committed branch must stay visible
        reason = f"not verified: {type(exc).__name__}: {exc}"
        reason = "; ".join(filter(None, (reason, warning)))
        if events[-1].kind == "verify":
            events[-1] = RawEvent(kind="verify", payload={
                **events[-1].payload, "unproven_reason": reason})
        return landed(reason)
