"""Append-only JSONL run log: the single source of truth for what a run did.

Every string that reaches disk goes through Hermes's redaction with
``force=True``, so a worker echoing a credential into its output cannot leave
it in the log. The log still contains verbatim task goals and diffs, so it
carries the same privacy caveat as ``hermes debug share``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Sequence

from .events import ControlEvent, RawEvent, stamp

logger = logging.getLogger(__name__)


def _parse_line(line: str) -> ControlEvent | None:
    if not line.strip():
        return None
    try:
        return ControlEvent.from_json_line(line)
    except (ValueError, KeyError):
        return None


def run_log_dir() -> Path:
    """``<hermes home>/control``, created on demand."""
    from hermes_constants import get_hermes_home

    directory = Path(get_hermes_home()) / "control"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run_log_path(run_id: str) -> Path:
    return run_log_dir() / f"run-{run_id}.jsonl"


def _redact(value: Any) -> Any:
    """Redact strings anywhere in a payload, leaving other types untouched."""
    from agent.redact import redact_sensitive_text

    if isinstance(value, str):
        return redact_sensitive_text(value, force=True)
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class RunLog:
    """One file per run. Appends are best-effort: a logging failure must never
    take down a run that is otherwise succeeding."""

    def __init__(self, run_id: str, *, directory: Path | None = None) -> None:
        self.run_id = run_id
        self._directory = directory
        self.path = (directory / f"run-{run_id}.jsonl") if directory is not None \
            else run_log_path(run_id)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._next_seq: dict[str, int] = {}
        # Set only after a summary actually reaches disk, never on a failed write.
        self.summary_seq: int | None = None

    def next_seq(self, task_id: str) -> int:
        """One past the highest seq appended for *task_id*, so events the controller
        adds after the engine has exited continue that task's sequence."""
        with self._lock:
            return self._next_seq.get(task_id, 0)

    def continue_sequences(self) -> None:
        """Pick up each task's sequence where the file on disk leaves it: a resumed
        task appends to the log of the run it was paused in."""
        with self._lock:
            for event in self.read_all():
                self._next_seq[event.task_id] = max(self.next_seq(event.task_id), event.seq + 1)

    def append_next(self, task_id: str, raws: Sequence[RawEvent], *,
                    engine: str, now: float) -> None:
        """Allocate sequence numbers and append as one atomic operation."""
        with self._lock:
            self.append(stamp(raws, run_id=self.run_id, task_id=task_id, engine=engine,
                              start_seq=self.next_seq(task_id), now=now))

    def append(self, events: Sequence[ControlEvent]) -> None:
        with self._lock:
            if not events:
                return
            lines = []
            for event in events:
                self._next_seq[event.task_id] = max(self.next_seq(event.task_id), event.seq + 1)
                redacted = ControlEvent(
                    run_id=event.run_id, task_id=event.task_id, engine=event.engine,
                    seq=event.seq, ts=event.ts, kind=event.kind,
                    payload=_redact(event.payload), raw_kind=event.raw_kind)
                lines.append(redacted.to_json_line())
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write("\n".join(lines) + "\n")
                for event in events:
                    if event.kind == "summary":
                        self.summary_seq = event.seq
            except OSError:
                logger.warning("hermes-control: could not append to run log %s", self.path,
                               exc_info=True)

    def read_all(self) -> list[ControlEvent]:
        """Every readable event. Corrupt lines are skipped, not fatal — a
        partially-flushed line at the tail is normal while a run is live."""
        if not self.path.exists():
            return []
        return self.tail(0)[0]

    def tail(self, offset: int) -> tuple[list[ControlEvent], int]:
        """Read complete lines starting at a byte offset, leaving an unfinished line unread."""
        events: list[ControlEvent] = []
        with self.path.open("rb") as handle:
            handle.seek(offset)
            while line := handle.readline():
                if not line.endswith(b"\n"):
                    break
                offset = handle.tell()
                event = _parse_line(line.decode("utf-8", errors="replace"))
                if event is not None:
                    events.append(event)
        return events, offset
