"""codex sign-in from the dashboard, by relaying `codex login --device-auth`.

The official codex client does the whole OAuth exchange and writes its own
auth.json; this module only starts it, reads the public device link and the
one-time code it prints, and watches it exit. No credential passes through
Hermes. claude has no counterpart here on purpose (engines-dashboard.md §3.3).
"""

from __future__ import annotations

import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from tools.ansi_strip import strip_ansi

from .engines.base import EngineAdapter, engine_env
from .engines.registry import engine_state
from .proctree import group_popen_kwargs, kill_tree, wait_bounded

# `codex login --device-auth` (codex-cli 0.155.1): "Enter this one-time code
# (expires in 15 minutes)".
LOGIN_TTL_SECONDS = 15 * 60
# The link and code appear within a second or two; a stalled network should
# fail the click, not hold the request open.
_CODE_WAIT_SECONDS = 30
_KILL_WAIT_SECONDS = 5
_MAX_SESSIONS = 8
_TAIL_LIMIT = 2000
_URL_RE = re.compile(r"https://\S+")
_CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")


class LoginRefused(Exception):
    """codex cannot start a sign-in right now; the message says why."""


@dataclass
class _Session:
    session_id: str
    process: subprocess.Popen
    started: float
    url: str = ""
    code: str = ""
    tail: str = ""
    state: str = "pending"
    detail: str = ""
    printed: threading.Event = field(default_factory=threading.Event)

    def view(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "state": self.state, "url": self.url,
                "code": self.code, "expires_at": self.started + LOGIN_TTL_SECONDS,
                "detail": self.detail}


# Guards the dict and every state change, and is never held across a blocking
# call. The reader thread only appends output.
_sessions: dict[str, _Session] = {}
_lock = threading.Lock()


def _said(session: _Session) -> str:
    return " ".join(session.tail.split())[-300:]


def _read(session: _Session) -> None:
    """Collect output; signal once both the link and the code are known, or at EOF."""
    try:
        for raw in session.process.stdout:
            line = strip_ansi(raw)
            session.tail = (session.tail + line)[-_TAIL_LIMIT:]
            if not session.url and (match := _URL_RE.search(line)):
                session.url = match.group(0)
            if not session.code and (match := _CODE_RE.search(line)):
                session.code = match.group(0)
            if session.url and session.code:
                session.printed.set()
    except (OSError, ValueError):
        pass
    finally:
        session.printed.set()


def _kill(session: _Session) -> None:
    """Stop the login process tree. Blocks up to _KILL_WAIT_SECONDS: never under _lock."""
    kill_tree(session.process)
    wait_bounded(session.process, _KILL_WAIT_SECONDS)


def _decide(session: _Session, state: str, detail: str = "") -> None:
    """Record a pending session's outcome. The first verdict wins, so a poll, a
    cancel and a replacing sign-in racing on the same session cannot overwrite
    each other."""
    with _lock:
        if session.state == "pending":
            session.state = state
            session.detail = detail


def _view(session: _Session) -> dict[str, Any]:
    with _lock:
        return session.view()


def _remember(session: _Session) -> None:
    """Keep at most _MAX_SESSIONS, dropping the oldest settled ones first."""
    _sessions[session.session_id] = session
    settled = [s for s in _sessions.values() if s.state != "pending"]
    while len(_sessions) > _MAX_SESSIONS and settled:
        _sessions.pop(settled.pop(0).session_id)


def start_login(adapter: EngineAdapter) -> dict[str, Any]:
    """Start a device-code sign-in and return once its link and code are known."""
    info, problem = engine_state(adapter)
    if info is None:
        raise LoginRefused(f"codex is {problem}")
    if problem is None:
        raise LoginRefused("codex is already signed in")
    with _lock:
        # One at a time: two live codes for the same CLI only confuse. The new
        # session is registered in the same step, so a sign-in started while this
        # one still waits for its code finds it and replaces it.
        replaced = [s for s in _sessions.values() if s.state == "pending"]
        for old in replaced:
            old.state, old.detail = "cancelled", "replaced by a newer sign-in"
        process = subprocess.Popen(
            [info.path, "login", "--device-auth"], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", env=engine_env(), **group_popen_kwargs())
        session = _Session(session_id=secrets.token_urlsafe(12), process=process,
                           started=time.time())
        _remember(session)
    for old in replaced:
        _kill(old)
    threading.Thread(target=_read, args=(session,), daemon=True).start()
    session.printed.wait(_CODE_WAIT_SECONDS)
    if not (session.url and session.code):
        _kill(session)
        _decide(session, "failed", f"codex printed no device code: {_said(session) or 'no output'}")
    return _view(session)


def login_status(session_id: str, adapter: EngineAdapter, *,
                 now: Callable[[], float] = time.time) -> dict[str, Any]:
    """The session's current state; KeyError for an id this process never issued.

    Only reads and verdicts take _lock: the kill wait and the post-login sign-in
    check (a subprocess with a 20 s timeout) run outside it, so one slow session
    never stalls the rest of the Engines tab.
    """
    with _lock:
        session = _sessions[session_id]
        if session.state != "pending":
            return session.view()
    returncode = session.process.poll()
    if returncode is None:
        if now() - session.started > LOGIN_TTL_SECONDS:
            _kill(session)
            _decide(session, "expired", "the one-time code expired before it was approved")
    elif returncode != 0:
        _decide(session, "failed", f"codex login exited {returncode}: {_said(session)}")
    else:
        _, problem = engine_state(adapter)
        if problem is None:
            _decide(session, "signed_in")
        else:
            _decide(session, "failed", f"codex login finished but codex still reports: {problem}")
    return _view(session)


def cancel_login(session_id: str) -> dict[str, Any]:
    with _lock:
        session = _sessions[session_id]
        cancelling = session.state == "pending"
        if cancelling:
            session.state, session.detail = "cancelled", ""
    if cancelling:
        _kill(session)
    return _view(session)
