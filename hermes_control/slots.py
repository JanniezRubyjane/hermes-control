"""Engine capacity shared by every run using the same Hermes home."""

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from time import monotonic, sleep
from typing import Any

try:
    import fcntl
except ImportError:  # Windows has no flock; retain unrestricted execution there.
    fcntl = None

from .runlog import run_log_dir

logger = logging.getLogger(__name__)


@contextmanager
def engine_slot(engine: str, limit: int, *,
                on_wait: Callable[[dict[str, Any]], None] | None = None,
                poll: float = 0.5) -> Iterator[None]:
    """Hold one OS-owned slot until exit, including exceptional exits.

    Keep the lock files in place: unlinking one could let another process lock
    a different inode for the same slot. The OS releases locks on process death.
    """
    if fcntl is None:
        yield
        return
    if limit < 1:
        raise ValueError("engine slot limit must be at least 1")
    directory = run_log_dir() / "slots" / engine
    directory.mkdir(parents=True, exist_ok=True)

    def report(payload: dict[str, Any]) -> None:
        if on_wait is not None:
            try:
                on_wait(payload)
            except Exception:  # noqa: BLE001 - observability must never fail the task
                logger.debug("hermes-control: could not log engine slot wait", exc_info=True)

    started = monotonic()
    waiting = False
    while True:
        for index in range(limit):
            with (directory / f"slot-{index}.lock").open("a") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    seconds = monotonic() - started
                    if waiting or seconds > 1:
                        report({"engine": engine, "state": "acquired",
                                "seconds": round(seconds, 1)})
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                return
        if not waiting:
            waiting = True
            report({"engine": engine, "state": "waiting"})
        sleep(poll)
