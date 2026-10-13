"""Normalized event and result types shared by every engine adapter.

``RawEvent`` is what an adapter's pure line parser returns; ``stamp`` turns a
batch of them into ``ControlEvent``s carrying run/task identity and a
monotonic sequence. Keeping identity out of the parser is what makes adapter
golden tests free of clocks and counters.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

# Why a task that otherwise looks fine is still unproven. Shared so `dispatch`
# can recognise an adapter's "we measured nothing changed" and replace it when
# the measurement itself failed, without matching a literal in three modules.
EMPTY_DIFF_REASON = "empty diff"

ControlEventKind = Literal[
    "text", "thinking", "tool_start", "tool_end", "permission_request",
    "plan", "usage", "result", "error", "session",
    # Written by the controller itself, after the engine has exited.
    "commit", "verify",
    # Usage limits (docs/limits-and-resume.md): a worker hit one, a task waits
    # for one to reset, a waiting task started again.
    "limit", "paused", "resumed",
    # A task moved to another engine than the one the plan gave it.
    "rerouted",
    # A worker waited for machine-wide engine capacity before starting.
    "slot_wait",
    # Informational notice of a possible machine wake, inferred from clock drift.
    "wake",
]


@dataclass(frozen=True)
class RawEvent:
    """One normalized event, before run/task identity is attached."""

    kind: ControlEventKind
    payload: dict[str, Any]
    raw_kind: str | None = None


@dataclass(frozen=True)
class ControlEvent:
    """A ``RawEvent`` stamped with identity; the run log's line format."""

    run_id: str
    task_id: str
    engine: str
    seq: int
    ts: float
    kind: ControlEventKind
    payload: dict[str, Any]
    raw_kind: str | None = None

    def to_json_line(self) -> str:
        """Single-line JSON. ``payload`` is engine data, so never let it break the line."""
        return json.dumps({
            "run_id": self.run_id, "task_id": self.task_id, "engine": self.engine,
            "seq": self.seq, "ts": self.ts, "kind": self.kind,
            "payload": self.payload, "raw_kind": self.raw_kind,
        }, ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> "ControlEvent":
        data = json.loads(line)
        if not isinstance(data, dict):
            raise ValueError("event must be an object")
        for key in ("run_id", "task_id", "engine", "kind"):
            if not isinstance(data[key], str):
                raise ValueError(f"event {key} must be text")
        if type(data["seq"]) is not int:
            raise ValueError("event seq must be an integer")
        ts = data["ts"]
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not math.isfinite(ts):
            raise ValueError("event ts must be a finite number")
        if not isinstance(data["payload"], dict):
            raise ValueError("event payload must be an object")
        if data.get("raw_kind") is not None and not isinstance(data["raw_kind"], str):
            raise ValueError("event raw_kind must be text or null")
        return cls(
            run_id=data["run_id"], task_id=data["task_id"], engine=data["engine"],
            seq=data["seq"], ts=data["ts"], kind=data["kind"],
            payload=data["payload"], raw_kind=data.get("raw_kind"),
        )


@dataclass(frozen=True)
class LimitHit:
    """The worker stopped because its account hit a usage limit, not because the
    task failed: the work can continue elsewhere or after *resets_at*."""

    engine: str
    message: str
    resets_at: float | None = None  # epoch seconds, when the engine said


@dataclass(frozen=True)
class ControlResult:
    """Terminal outcome of one task. ``unproven_reason`` is non-None when the
    worker's output was not demonstrated to be correct (no tests ran, empty
    diff, timeout) — mirrors Hermes's own subagent-worktree convention."""

    task_id: str
    engine: str
    ok: bool
    text: str
    session_id: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    diff: str = ""
    unproven_reason: str | None = None
    limit: LimitHit | None = None
    failure_mode: Literal["timeout", "stall", "engine-exit", "start-failure", "limit"] | None = None


def stamp(raws: Sequence[RawEvent], *, run_id: str, task_id: str, engine: str,
          start_seq: int, now: float) -> list[ControlEvent]:
    """Attach identity and consecutive sequence numbers starting at *start_seq*."""
    return [
        ControlEvent(run_id=run_id, task_id=task_id, engine=engine,
                     seq=start_seq + offset, ts=now, kind=raw.kind,
                     payload=raw.payload, raw_kind=raw.raw_kind)
        for offset, raw in enumerate(raws)
    ]
