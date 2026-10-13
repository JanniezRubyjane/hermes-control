"""A scripted engine and helpers shared by the dispatch-flow tests (pause, resume,
run views): each run appends the engine's name to work.txt, then finishes or
stops at a usage limit, as scripted."""

import json
import subprocess
import time
import threading

import pytest

from hermes_control import limits
from hermes_control.engines.base import EngineAdapter, EngineInfo
from hermes_control.events import ControlResult, LimitHit, RawEvent

RESET = time.time() + 3600


class ScriptedEngine(EngineAdapter):
    """Each run appends the engine's name to work.txt, then either finishes or
    stops at a usage limit, as scripted. Records the task it was given."""

    efforts = ("low", "high")
    sign_in_command = "true"

    def __init__(self, name, *outcomes):
        self.name = name
        self.resumes_sessions = name == "claude"  # as the real adapters declare
        self.outcomes = list(outcomes)
        # Outcomes for named tasks, taken before the shared queue: with tasks
        # running in parallel the queue's pop order is the start order, a race.
        self.by_task = {}
        self.tasks = []
        self.gates = {}
        self._lock = threading.Lock()

    def block(self, task_id, *, started, release):
        """Pause a task at engine entry until a test releases it."""
        self.gates[task_id] = (started, release)

    def probe(self):
        return EngineInfo(name=self.name, version="1", path="/bin/true")

    def auth_problem(self, info):
        return None

    def build_argv(self, task, *, cwd):
        with self._lock:
            self.tasks.append(task)
            outcome = (self.by_task.pop(task.id) if task.id in self.by_task
                       else self.outcomes.pop(0))
        if task.id in self.gates:
            started, release = self.gates[task.id]
            started.set()
            if not release.wait(10):
                raise RuntimeError(f"test did not release {task.id}")
        return ["python3", "-c",
                f"open('work.txt', 'a').write({self.name!r} + '\\n');"
                f"print({json.dumps({'k': outcome})!r})"]

    def parse_line(self, line):
        kind = json.loads(line)["k"]
        if kind == "ok":
            return [RawEvent(kind="result", payload={"ok": True})]
        return [RawEvent(kind="error", payload={"message": "You've hit your usage limit."})]

    def extract_result(self, task, raws, diff):
        ok = any(r.kind == "result" for r in raws)
        return ControlResult(task_id=task.id, engine=self.name, ok=ok, text="done" if ok else "",
                             session_id=f"{self.name}-session", diff=diff,
                             unproven_reason=None if ok else "engine reported failure")

    def limit_hit(self, raws):
        if any(r.kind == "result" for r in raws):
            return None
        return LimitHit(engine=self.name, message="You've hit your usage limit.", resets_at=RESET)


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    _git(["commit", "-q", "--allow-empty", "-m", "init"], tmp_path)
    return tmp_path


def _engines(monkeypatch, claude, codex):
    engines = {"claude": claude, "codex": codex}
    monkeypatch.setattr("hermes_control.dispatch.get_adapter", engines.__getitem__)
    monkeypatch.setattr("hermes_control.dispatch.find_sandbox", lambda: None)
    return engines


def _reading(monkeypatch, engine, percent=None, *, blocked=False):
    """*engine*'s usage from now on: a percentage of a 5-hour window, or blocked."""
    windows = (limits.Window("5-hour", float(percent), RESET),) if percent is not None else ()
    reading = limits.Usage(engine=engine, windows=windows, blocked=blocked,
                           blocked_until=RESET if blocked else None)
    monkeypatch.setitem(limits._FETCHERS, engine, lambda refresh=False: reading)
    limits.forget(engine)
