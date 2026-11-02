"""Fail-closed benchmark quota checks. Retain only window labels and percentages."""

from __future__ import annotations

import math
import json
import time

from hermes_control import limits
from hermes_control.runlog import run_log_dir


class QuotaGuard:
    def __init__(self, max_5h=90.0, max_weekly=80.0, *, max_age=1800,
                 allow_stale_claude=False, reserve=5.0):
        for name, value in (("max_5h", max_5h), ("max_weekly", max_weekly)):
            if not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 <= value <= 100:
                raise ValueError(f"{name}: must be a percentage between 0 and 100")
        if type(reserve) not in (int, float) or not math.isfinite(reserve) or not 0 <= reserve <= 100:
            raise ValueError("reserve: must be a percentage between 0 and 100")
        self.reserve = float(reserve)
        self._engine_windows = {}
        self._increases = {}
        self.max_5h = float(max_5h)
        self.max_weekly = float(max_weekly)
        if type(max_age) not in (int, float) or not math.isfinite(max_age) or max_age < 0:
            raise ValueError("max_age: must be finite and nonnegative")
        self.max_age = float(max_age)
        self.allow_stale_claude = allow_stale_claude
        self.last_reason = ""
        self.last_windows = {}

    def __call__(self, engine, model=None) -> bool:
        """Accept an engine name or a lane dict; unknown Gemini models count all buckets."""
        if isinstance(engine, dict):
            model, engine = engine.get("model"), engine["engine"]
        self.last_windows = {}
        self._engine_windows[engine] = self.last_windows
        self.last_reason = ""
        try:
            limits.forget(engine)
            reading = limits.usage(engine)
        except Exception:
            # Provider error text can contain account identifiers. Never retain it.
            self.last_reason = f"{engine}: quota read error"
            return False
        if engine == "claude" and not self.allow_stale_claude:
            try:
                at = json.loads((run_log_dir() / "usage-claude.json").read_text(encoding="utf-8"))["at"]
                if type(at) not in (int, float) or not math.isfinite(at):
                    raise ValueError("invalid report timestamp")
                age = max(0, time.time() - at)
            except (OSError, ValueError, KeyError, TypeError):
                self.last_reason = "claude reading age unknown"
                return False
            if age > self.max_age:
                self.last_reason = f"claude reading is {age / 60:.0f} min old"
                return False
        if reading.error:
            self.last_reason = f"{engine}: quota read error"
            return False
        if not reading.windows:
            self.last_reason = f"{engine}: no quota windows"
            return False
        buckets = ()
        if engine == "gemini" and model:
            model = model.lower()
            if model.startswith(("claude-", "gpt-")):
                buckets = ("claude and gpt",)
            elif model.startswith("gemini-4-argon"):
                buckets = ("gemini 4 argon", "gemini models")
            elif model.startswith("gemini-"):
                buckets = ("gemini models",)
        counted = []
        for window in reading.windows:
            label = window["label"] if isinstance(window, dict) else window.label
            used = window.get("used_percent") if isinstance(window, dict) else window.used_percent
            lower = (label or "").lower()
            if buckets and not any(bucket in lower for bucket in buckets):
                continue
            cap = self.max_5h if "5-hour" in lower or "5h" in lower else (
                self.max_weekly if "weekly" in lower else None)
            if cap is None:
                continue
            counted.append((label, used, cap))
            self.last_windows[label] = (used if type(used) in (int, float)
                                        and math.isfinite(used) else None)
        if reading.blocked:
            self.last_reason = f"{engine}: provider blocked"
            return False
        if not counted:
            self.last_reason = f"{engine}: no matching quota windows"
            return False
        for label, used, cap in counted:
            if type(used) not in (int, float) or not math.isfinite(used):
                self.last_reason = f"{engine}: unknown usage for {label}"
                return False
            margin = max(self.reserve, self._increases.get(engine, {}).get(label, 0.0))
            if used + margin >= cap:
                self.last_reason = (f"{engine}: {label} at {used:g}% + margin {margin:g} "
                                    f"(limit {cap:g}%)")
                return False
        return True

    def snapshot(self, lane):
        engine = lane["engine"] if isinstance(lane, dict) else lane
        previous = self._engine_windows.get(engine, {})
        self(lane)
        increases = self._increases.setdefault(engine, {})
        for label, used in self.last_windows.items():
            start = previous.get(label)
            if (type(start) in (int, float) and math.isfinite(start)
                    and type(used) in (int, float) and math.isfinite(used)
                    and used >= start):
                increases[label] = max(increases.get(label, 0.0), used - start)
        return dict(self.last_windows)


def before(guard, lane):
    """Check once and copy the redacted reading, including for injectable test guards."""
    allowed = guard(lane)
    if not allowed:
        print(getattr(guard, "last_reason", "quota guard stopped attempt"))
    return allowed, dict(getattr(guard, "last_windows", {}))


def after(guard, lane):
    return guard.snapshot(lane) if isinstance(guard, QuotaGuard) else {}
