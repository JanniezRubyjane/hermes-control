"""How much of each engine's subscription is used, and when to hold work back.

Design: docs/limits-and-resume.md. Each engine's usage is read from that engine's
own account: codex answers ``account/rateLimits/read`` over ``codex app-server``;
for claude, usage comes from its last rate_limit_event during a run, with account
and subscription information from ``claude auth status``.

A reading that fails never holds work back: the cap is a preference, and a real
limit is still caught when the worker hits it.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .engines.base import engine_env
from .engines.registry import ENGINE_NAMES, get_adapter
from .model_policy import PLUGIN_ID
from .proctree import group_popen_kwargs, stop_group

DEFAULT_CAP = 80
CACHE_SECONDS = 60
_CODEX_TIMEOUT_SECONDS = 20
_ACCOUNT_GRACE_SECONDS = 5
_CLAUDE_ACCOUNT_CACHE_SECONDS = 5 * 60

# claude's own names for its usage windows.
_CLAUDE_LABELS = {"five_hour": "5-hour", "seven_day": "weekly",
                  "seven_day_opus": "Opus weekly", "seven_day_sonnet": "Sonnet weekly"}
_CODEX_LABELS = {300: "5-hour", 10080: "weekly", 43200: "30-day"}


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: float | None
    resets_at: float | None  # epoch seconds


@dataclass(frozen=True)
class Usage:
    engine: str
    windows: tuple[Window, ...] = ()
    blocked: bool = False  # the provider has stopped this account
    blocked_until: float | None = None
    source: str = ""
    plan: str | None = None
    account: str | None = None  # the signed-in account (email) this reading is for
    error: str | None = None  # why there is no reading


def when(epoch: float | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M %z", time.localtime(epoch)) if epoch else "at an unknown time"


# --- the cap setting -------------------------------------------------------------

def parse_caps(raw: Any) -> dict[str, int]:
    """``{engine: max_percent}`` for every engine; missing ones get DEFAULT_CAP."""
    caps = {name: DEFAULT_CAP for name in ENGINE_NAMES}
    if raw is None:
        return caps
    if not isinstance(raw, Mapping):
        raise ValueError("limits must be a mapping of engine to {max_percent}")
    for engine, spec in raw.items():
        if engine not in ENGINE_NAMES:
            raise ValueError(f"limits: unknown engine {engine!r}; known: {', '.join(ENGINE_NAMES)}")
        if isinstance(spec, Mapping):
            extra = set(spec) - {"max_percent"}
            if extra:
                raise ValueError(f"limits.{engine}: unknown key(s) {', '.join(sorted(extra))}")
            spec = spec.get("max_percent", DEFAULT_CAP)
        if isinstance(spec, bool) or not isinstance(spec, int) or not 1 <= spec <= 100:
            raise ValueError(f"limits.{engine}.max_percent must be a whole number between 1 and 100, "
                             f"got {spec!r}")
        caps[engine] = spec
    return caps


def read_caps() -> dict[str, int]:
    from hermes_cli.config import load_config_readonly
    from hermes_cli.plugins_state import _plugin_settings_entry

    entry = _plugin_settings_entry(load_config_readonly() or {}, PLUGIN_ID) or {}
    settings = entry.get("settings")
    return parse_caps(settings.get("limits") if isinstance(settings, Mapping) else None)


def save_caps(raw: Any) -> dict[str, dict[str, int]]:
    """Validate, then write every engine through Hermes's plugin-settings writer."""
    from hermes_cli.plugins_state import save_plugin_setting

    settings = {engine: {"max_percent": cap} for engine, cap in parse_caps(raw).items()}
    save_plugin_setting(PLUGIN_ID, ("limits",), settings)
    return settings


# --- reading usage -----------------------------------------------------------------

def codex_usage_from_rate_limits(result: Mapping[str, Any],
                                 account: Mapping[str, Any] | None = None) -> Usage:
    """From codex app-server's ``account/rateLimits/read`` result, and ``account/read``'s
    account (its email and plan) when given."""
    snapshot = result.get("rateLimits") or {}
    account = account or {}
    windows = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, Mapping):
            continue
        minutes = window.get("windowDurationMins")
        label = _CODEX_LABELS.get(minutes) or (f"{minutes}-minute" if minutes else key)
        used = window.get("usedPercent")
        windows.append(Window(label, float(used) if used is not None else None,
                              float(window["resetsAt"]) if window.get("resetsAt") else None))
    blocked = bool(snapshot.get("rateLimitReachedType")) or result.get("ordinaryUsageAllowed") is False
    exhausted = [w for w in windows if w.used_percent is not None and w.used_percent >= 100]
    resets = (w.resets_at for w in (exhausted or windows) if w.resets_at is not None)
    until = (max(resets, default=None) if exhausted else min(resets, default=None)) if blocked else None
    return Usage(engine="codex", windows=tuple(windows), blocked=blocked, blocked_until=until,
                 source="codex app-server (codex's own login)",
                 plan=account.get("planType") or snapshot.get("planType"),
                 account=account.get("email"))


def claude_account(status_json: str) -> tuple[str | None, str | None]:
    """``(email, subscription)`` from ``claude auth status``'s JSON (claude-code 2.1.281:
    ``email``, ``subscriptionType`` such as "max" or "pro")."""
    try:
        status = json.loads(status_json)
    except (TypeError, ValueError):
        return None, None
    if not isinstance(status, Mapping):
        return None, None
    return status.get("email") or None, status.get("subscriptionType") or None


def _claude_auth_status() -> tuple[str | None, str | None]:
    info = get_adapter("claude").probe()
    if info is None:
        return None, None
    try:
        proc = subprocess.run([info.path, "auth", "status"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=_CODEX_TIMEOUT_SECONDS,
                              stdin=subprocess.DEVNULL, env=engine_env())
    except (OSError, subprocess.SubprocessError):
        return None, None
    return claude_account(proc.stdout)


def claude_usage_from_report(info: Mapping[str, Any], *, at: float) -> Usage:
    """From the ``rate_limit_info`` of claude's ``rate_limit_event``."""
    windows = tuple(
        Window(_CLAUDE_LABELS.get(key, key),
               round(float(window["utilization"]) * 100, 1)
               if window.get("utilization") is not None else None,
               float(window["resetsAt"]) if window.get("resetsAt") else None)
        for key, window in (info.get("unifiedWindows") or {}).items() if isinstance(window, Mapping))
    blocked = info.get("status") == "rejected"
    return Usage(engine="claude", windows=windows, blocked=blocked,
                 blocked_until=float(info["resetsAt"]) if blocked and info.get("resetsAt") else None,
                 source=f"claude's own report during a run at {when(at)}")


def _report_path() -> Path:
    from .runlog import run_log_dir

    return run_log_dir() / "usage-claude.json"


def record_claude_report(info: Mapping[str, Any], *, at: float) -> None:
    """Keep claude's latest ``rate_limit_info`` as its only usage reading."""
    global _claude_account_cache
    with _lock:
        cached = _claude_account_cache
    if cached is not None and time.monotonic() - cached[0] < _CLAUDE_ACCOUNT_CACHE_SECONDS:
        account = cached[1]
    else:
        try:
            account, _ = _claude_auth_status()
        except Exception:  # noqa: BLE001 - account lookup must not fail a completed task
            account = None
        with _lock:
            _claude_account_cache = (time.monotonic(), account)

    temp_path = None
    try:
        path = _report_path()
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", delete=False) as temporary:
            temp_path = Path(temporary.name)
            json.dump({"at": at, "info": dict(info), "account": account}, temporary)
        os.replace(temp_path, path)
    except Exception:  # noqa: BLE001 - reporting is a best-effort side effect
        pass
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001 - cleanup must not fail a completed task
                pass
    forget("claude")


def _claude(refresh: bool = False) -> Usage:
    """claude's usage, with the account and subscription ``claude auth status`` names.
    *refresh* changes nothing here: every read asks claude afresh."""
    email, subscription = _claude_auth_status()
    global _claude_account_cache
    with _lock:
        _claude_account_cache = (time.monotonic(), email)
    usage = _claude_usage(current_account=email)
    return dataclasses.replace(usage, account=email or usage.account, plan=subscription or usage.plan)


def _claude_usage(*, current_account: str | None = None) -> Usage:
    """Read claude's last report, or unknown usage when no valid report exists."""
    try:
        report = json.loads(_report_path().read_text(encoding="utf-8"))
        info, at = report["info"], float(report["at"])
        if report.get("account") is not None and report["account"] != current_account:
            return Usage(engine="claude", error="claude usage report belongs to a different account")
        windows = info.get("unifiedWindows") or {}
        resets = [float(value) for value in [info.get("resetsAt"),
                  *(w.get("resetsAt") for w in windows.values() if isinstance(w, Mapping))] if value]
        expires = (max(resets) if resets else at + 5 * 3600)
        if report.get("account") is None:
            expires = min(expires, at + 5 * 3600)
        if time.time() > expires:
            return Usage(engine="claude", error="claude usage report is stale")
        return claude_usage_from_report(info, at=at)
    except (OSError, ValueError, KeyError, TypeError):
        return Usage(engine="claude", error="no reading yet: claude has not reported its usage in a run")


def _codex(refresh: bool = False) -> Usage:
    """codex's usage and account, from its app-server. *refresh* first renews codex's
    sign-in (``account/read`` with ``refreshToken``), so a plan bought since shows."""
    info = get_adapter("codex").probe()
    if info is None:
        return Usage(engine="codex", error="codex is not installed")
    process = subprocess.Popen([info.path, "app-server"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                               encoding="utf-8", errors="replace", env=engine_env(),
                               **group_popen_kwargs())
    answer: dict[int, Any] = {}

    def read() -> None:
        # Collect every answer until codex's output ends (it is killed below). Answers
        # may come back in any order: a sign-in renewal delays account/read.
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") in (1, 2, 3):
                answer[message["id"]] = message

    reader = threading.Thread(target=read, daemon=True)
    try:
        reader.start()
        for request in ({"id": 1, "method": "initialize",
                         "params": {"clientInfo": {"name": "hermes-control", "version": "1"}}},
                        {"method": "initialized"},
                        {"id": 2, "method": "account/read", "params": {"refreshToken": refresh}},
                        {"id": 3, "method": "account/rateLimits/read", "params": None}):
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        deadline = time.monotonic() + _CODEX_TIMEOUT_SECONDS
        while 3 not in answer and time.monotonic() < deadline and reader.is_alive():
            time.sleep(0.02)
        # The limits are in; the account is worth a short wait, not a stalled page.
        grace = time.monotonic() + _ACCOUNT_GRACE_SECONDS
        while 2 not in answer and time.monotonic() < grace and reader.is_alive():
            time.sleep(0.02)
    except OSError as exc:
        return Usage(engine="codex", error=f"codex app-server: {exc}")
    finally:
        with contextlib.suppress(OSError):
            stop_group(process, 2)
        if reader.ident is not None:
            reader.join(5)
    reply = answer.get(3)
    if reply is None:
        return Usage(engine="codex", error="codex app-server did not answer account/rateLimits/read")
    if "result" not in reply:
        return Usage(engine="codex", error=f"codex app-server: {json.dumps(reply.get('error'))[:200]}")
    account = ((answer.get(2) or {}).get("result") or {}).get("account")
    return codex_usage_from_rate_limits(reply["result"], account if isinstance(account, Mapping) else None)


# Each takes *refresh*: renew the engine's sign-in first, where that shows a changed plan.
_FETCHERS: dict[str, Callable[..., Usage]] = {"claude": _claude, "codex": _codex}
_cache: dict[str, tuple[float, Usage]] = {}
_generations: dict[str, int] = {}
_lock = threading.Lock()
_claude_account_cache: tuple[float, str | None] | None = None


def usage(engine: str, *, refresh: bool = False) -> Usage:
    """*engine*'s reading, at most CACHE_SECONDS old unless *refresh*."""
    with _lock:
        cached = _cache.get(engine)
        generation = _generations.get(engine, 0)
    if cached and not refresh and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        reading = _FETCHERS[engine](refresh=refresh)
    except Exception as exc:  # noqa: BLE001 - a subprocess, a network call, a parse
        reading = Usage(engine=engine, error=f"{type(exc).__name__}: {exc}"[:300])
    with _lock:
        # A limit/report received during this fetch invalidates its cache write too.
        if _generations.get(engine, 0) == generation:
            _cache[engine] = (time.monotonic(), reading)
    return reading


def forget(engine: str) -> None:
    """Drop *engine*'s cached reading: it just hit a limit, or reported new usage."""
    with _lock:
        _generations[engine] = _generations.get(engine, 0) + 1
        _cache.pop(engine, None)


# --- holding work back ---------------------------------------------------------------

def held_back(reading: Usage, *, cap: int, now: float) -> tuple[str, float | None] | None:
    """``(reason, until)`` when *reading* says to send no more work, else None."""
    if reading.error:
        return None
    if reading.blocked and (reading.blocked_until is None or reading.blocked_until > now):
        return (f"{reading.engine} has reached its usage limit; resets {when(reading.blocked_until)}",
                reading.blocked_until)
    over = [w for w in reading.windows
            if w.used_percent is not None and w.used_percent >= cap
            and (w.resets_at is None or w.resets_at > now)]
    if not over:
        return None
    # Work may flow again only once every window over the cap has reset.
    until = None if any(w.resets_at is None for w in over) else max(w.resets_at for w in over)
    spent = " and ".join(f"{w.used_percent:.0f}% of its {w.label} limit"
                         for w in sorted(over, key=lambda w: -w.used_percent))
    resets = "resets" if len(over) == 1 else "the last of them resets"
    return f"{reading.engine} is at {spent} (cap {cap}%); {resets} {when(until)}", until


def engine_hold(engine: str, *, caps: Mapping[str, int] | None = None,
                refresh: bool = False) -> tuple[str, float | None] | None:
    """Whether *engine* is held back right now, by its cap or a real limit."""
    caps = caps if caps is not None else read_caps()
    return held_back(usage(engine, refresh=refresh), cap=caps.get(engine, DEFAULT_CAP),
                     now=time.time())


def describe(engine: str, *, caps: Mapping[str, int], refresh: bool = False) -> dict[str, Any]:
    """One engine's reading for the dashboard."""
    reading = usage(engine, refresh=refresh)
    hold = held_back(reading, cap=caps.get(engine, DEFAULT_CAP), now=time.time())
    return {**asdict(reading), "cap": caps.get(engine, DEFAULT_CAP),
            "held_back": hold[0] if hold else None, "held_until": hold[1] if hold else None}
