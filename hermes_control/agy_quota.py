"""Report-only quota snapshots from the Antigravity app's local language server."""

from __future__ import annotations

import json
import re
import ssl
import subprocess
import time
import urllib.request
from datetime import datetime
from typing import Any

from .limits import Usage, Window

_TIMEOUT = 3
_ENDPOINT = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
_TOKEN = re.compile(r"(?:^|\s)--csrf_token(?:=|\s+)([A-Za-z0-9_-]+)(?=\s|$)")
_APP_DATA = re.compile(r"(?:^|\s)--app_data_dir(?:=|\s+)antigravity(?=\s|$)")
_PORT = re.compile(r"127\.0\.0\.1:(\d+) \(LISTEN\)\s*$")
_COUNTDOWN = re.compile(
    r"fully refresh in (\d+\s+(?:days?|hours?|minutes?)"
    r"(?:,\s*\d+\s+(?:days?|hours?|minutes?))*)\b")
_DURATION = re.compile(r"(\d+)\s+(day|hour|minute)s?\b")
_SECONDS = {"day": 86400, "hour": 3600, "minute": 60}


def _unknown(reason: str) -> Usage:
    return Usage(engine="gemini", report_only=True, error=reason)


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def parse_quota(payload: Any, now: float) -> Usage:
    """Map every bucket to a usage window; expired snapshots remain unknown."""
    response = payload.get("response") if isinstance(payload, dict) else None
    groups = response.get("groups") if isinstance(response, dict) else None
    if not isinstance(groups, list) or not groups:
        return _unknown("no Antigravity quota groups")
    windows = []
    age = None
    exact_age = None
    age_note = ""
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("buckets"), list):
            continue
        name = group.get("displayName", "")
        for bucket in group["buckets"]:
            if not isinstance(bucket, dict):
                continue
            window = bucket.get("window", "")
            remaining = bucket.get("remainingFraction")
            used = (100 * (1 - remaining)
                    if type(remaining) in (int, float) and 0 <= remaining <= 1 else None)
            reset = _epoch(bucket.get("resetTime"))
            description = bucket.get("description")
            if exact_age is None and window == "5h" and not description and reset is not None:
                exact_age = now - (reset - 5 * 3600)
            countdown = _COUNTDOWN.search(description) if isinstance(description, str) else None
            if age is None and reset is not None and countdown:
                parts = _DURATION.findall(countdown[1])
                duration = sum(int(n) * _SECONDS[unit] for n, unit in parts)
                age = now - (reset - duration)
                smallest_unit = min((unit for _, unit in parts), key=_SECONDS.__getitem__)
                if smallest_unit != "minute":
                    age_note = f" (countdown in whole {smallest_unit}s)"
            if reset is not None and reset <= now:
                used, reset = None, None
            if window == "5h" and not description:
                reset = None
            windows.append(Window(f"{name} {window}", used, reset))
    if not windows:
        return _unknown("no Antigravity quota buckets")
    if exact_age is not None:
        age, age_note = exact_age, ""
    snapshot = "snapshot age unknown" if age is None else (
        f"snapshot about {max(0, int(age // 3600))} h old" if age >= 3600 else
        f"snapshot about {max(0, int(age // 60))} min old")
    return Usage(engine="gemini", windows=tuple(windows), report_only=True,
                 source=f"the Antigravity app's language server, {snapshot}{age_note}; "
                        "report only, never holds work back")


def _candidates(output: str) -> list[tuple[int, str]]:
    """Extract only PIDs and in-memory secrets; never retain the process listing."""
    candidates = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) != 2 or not fields[0].isdigit():
            continue
        executable = fields[1].split(None, 1)[0]
        components = executable.split("/")
        if components[-1] != "language_server":
            continue
        if not (_APP_DATA.search(fields[1]) or
                any(part.lower().endswith("antigravity.app") for part in components)):
            continue
        token = _TOKEN.search(fields[1])
        if token and int(fields[0]) > 0:
            candidates.append((int(fields[0]), token[1]))
    return sorted(candidates, key=lambda item: item[0], reverse=True)[:3]


def _ports(output: str) -> list[int]:
    return list(dict.fromkeys(int(match[1]) for line in output.splitlines()
                             if (match := _PORT.search(line)) and 0 < int(match[1]) < 65536))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Keep the token and unverified TLS strictly on the original loopback URL.
        return None


def fetch_quota() -> Usage:
    """Best-effort local discovery. Exceptions must never expose secret argv/headers."""
    try:
        candidates = _candidates(subprocess.run(
            ["ps", "-ax", "-o", "pid=,command="], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, errors="replace", check=True,
            timeout=_TIMEOUT).stdout)
    except Exception:  # no exception text: it may contain process arguments
        return _unknown("Antigravity process discovery failed")
    if not candidates:
        return _unknown("no Antigravity language server found")
    reason = "no Antigravity loopback port found"
    try:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()), _NoRedirect())
        for pid, token in candidates:
            try:
                ports = _ports(subprocess.run(
                    ["lsof", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                    errors="replace", check=True, timeout=_TIMEOUT).stdout)
            except Exception:
                reason = "Antigravity port discovery failed"
                continue
            for port in ports:
                request = urllib.request.Request(
                    f"https://127.0.0.1:{port}{_ENDPOINT}", data=b"{}", method="POST",
                    headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                             "X-Codeium-Csrf-Token": token})
                try:
                    with opener.open(request, timeout=_TIMEOUT) as reply:
                        if reply.status != 200:
                            reason = "Antigravity quota request returned non-200"
                            continue
                        try:
                            payload = json.load(reply)
                        except (ValueError, UnicodeError):
                            reason = "invalid Antigravity quota JSON"
                            continue
                    return parse_quota(payload, time.time())
                except Exception:
                    reason = "Antigravity quota request failed"
    except Exception:
        return _unknown("Antigravity quota reading failed")
    return _unknown(reason)
