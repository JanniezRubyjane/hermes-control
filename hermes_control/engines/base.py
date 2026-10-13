"""The seam every external coding-agent CLI is fitted to.

``parse_line`` is deliberately pure: one stdout line in, zero or more
``RawEvent``s out, no clock and no counter. That is what lets adapters be
tested against captured real CLI output with exact equality.
"""

from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

if TYPE_CHECKING:
    from ..worktree import TaskWorktree

from ..events import ControlResult, LimitHit, RawEvent
from ..policy import ExecPolicy

# What claude and codex print when an account is out of usage (codex-cli 0.155.1:
# "You've hit your usage limit. ..."; claude-code: "Claude AI usage limit
# reached|<epoch>", "You've hit your limit · resets 3am").
_LIMIT_TEXT = re.compile(r"usage limit|hit your (?:usage )?limit|rate limit|quota exceeded",
                         re.IGNORECASE)
_EPOCH_AFTER_BAR = re.compile(r"\|(\d{10})\b")


def says_limit(message: str) -> bool:
    return bool(_LIMIT_TEXT.search(message or ""))


def reset_in_text(message: str) -> float | None:
    """The epoch claude appends after a bar ("...limit reached|1790164800")."""
    match = _EPOCH_AFTER_BAR.search(message or "")
    return float(match.group(1)) if match else None


# A signed-in check answers in well under a second; an older CLI without the
# status subcommand may instead wait for input, which must not stall a dispatch.
_SIGN_IN_CHECK_TIMEOUT = 20


def engine_env() -> dict[str, str]:
    """Credential-scoped environment for an engine subprocess (design §6).

    ``inherit_credentials=True`` because the workers are user-blessed CLIs that
    legitimately need their own provider credentials - the point is that Tier 1
    (GitHub tokens, gateway/bot tokens, Hermes-internal secrets) is stripped
    regardless. The sign-in check uses it too, so it sees exactly the
    credentials the run will.
    """
    from tools.environments.local import hermes_subprocess_env

    return hermes_subprocess_env(inherit_credentials=True)


def _one_line(output: str) -> str:
    return " ".join(output.split())[:200]


def sign_in_problem(info: "EngineInfo", status_args: Sequence[str], *, sign_in_command: str,
                    summarize: Callable[[str], str] = _one_line) -> str | None:
    """Run the CLI's own status command: exit 0 means signed in. *summarize* turns
    what it printed into the few words worth repeating to the user."""
    try:
        proc = subprocess.run(
            [info.path, *status_args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=_SIGN_IN_CHECK_TIMEOUT, stdin=subprocess.DEVNULL,
            env=engine_env())
    except (OSError, subprocess.SubprocessError) as exc:
        return f"sign-in check failed ({' '.join(status_args)}: {exc})"
    if proc.returncode == 0:
        return None
    said = summarize(proc.stdout or proc.stderr or "") or f"exit {proc.returncode}"
    return f"not signed in (`{info.name} {' '.join(status_args)}`: {said}); " \
           f"run `{sign_in_command}`"


@dataclass(frozen=True)
class EngineInfo:
    """What a successful ``probe()`` learned about an installed engine."""

    name: str
    version: str
    path: str


@dataclass(frozen=True)
class ControlTask:
    """One unit of delegated work.

    ``why`` carries the planner's stated reason for the engine choice; it is
    required by the design whenever the engine was not left as ``auto`` and is
    written to the run log so a non-deterministic router stays auditable.
    ``verify`` is the planner's shell check, run by the controller in a sandbox
    after the change is committed; only a pass makes the task proven.
    """

    id: str
    goal: str
    engine: str
    policy: ExecPolicy
    files_hint: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    why: str = ""
    model: str | None = None
    verify: str | None = None
    effort: str | None = None
    # Continue this earlier session instead of starting a new one, where the
    # engine can do so safely (claude); an engine that cannot ignores it.
    resume_session: str | None = None
    # Optional shell check of the changed tests on base code, then committed code.
    red: str | None = None


class EngineAdapter(ABC):
    """Normalizes one external CLI into the Hermes-Control contract."""

    name: str
    # The command a person runs to sign this CLI in; named in every "not signed
    # in" reason and shown on the dashboard's Engines tab.
    sign_in_command: str
    # The effort levels this CLI accepts, weakest first.
    efforts: tuple[str, ...]
    # Whether ``ControlTask.resume_session`` continues that conversation. When
    # False, a task that stopped part-way starts a new session in its worktree.
    resumes_sessions: bool = False

    @abstractmethod
    def probe(self) -> EngineInfo | None:
        """Engine details, or None when it cannot be used right now."""

    @abstractmethod
    def auth_problem(self, info: EngineInfo) -> str | None:
        """None when the CLI at ``info.path`` is signed in, otherwise why not -
        in words the planner can repeat to the user, including how to fix it."""

    def repository_problem(self, worktree: TaskWorktree) -> str | None:
        """Refuse repository-controlled CLI configuration before starting a worker."""
        return None

    @abstractmethod
    def build_argv(self, task: ControlTask, *, cwd: Path) -> list[str]:
        """Full argv for a headless, non-interactive run of *task* in *cwd*."""

    @abstractmethod
    def parse_line(self, line: str) -> list[RawEvent]:
        """Normalize one stdout line. Empty list means "suppress this line"."""

    @abstractmethod
    def extract_result(self, task: ControlTask, raws: Sequence[RawEvent],
                       diff: str) -> ControlResult:
        """Terminal result for *task* from its full event stream and worktree diff."""

    def limit_hit(self, raws: Sequence[RawEvent]) -> LimitHit | None:
        """A LimitHit when the run failed because the account ran out of usage."""
        return None
