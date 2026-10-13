"""Telling "the worker hit its usage limit" apart from any other failure."""

import json
import subprocess
from pathlib import Path

from hermes_control import limits
from hermes_control.dispatch import run_task
from hermes_control.engines.base import ControlTask, EngineAdapter, EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter
from hermes_control.events import ControlResult, LimitHit, RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worktree import create_task_worktree

RESET = 1790164800


def _task(engine="claude", **kw):
    return ControlTask(id="t1", goal="g", engine=engine, policy=ExecPolicy(write=True), **kw)


def _parse(adapter, *objects):
    raws = []
    for obj in objects:
        raws.extend(adapter.parse_line(json.dumps(obj)))
    return raws


def _claude_limit_event(status):
    return {"type": "rate_limit_event",
            "rate_limit_info": {"status": status, "resetsAt": RESET, "rateLimitType": "five_hour"}}


CLAUDE_OK = {"type": "result", "subtype": "success", "is_error": False, "result": "done",
             "session_id": "S1"}


# --- claude ------------------------------------------------------------------------

def test_a_success_result_flagged_as_an_error_is_a_failure():
    """claude -p reports some failures, a usage limit among them, as subtype
    "success" with is_error set; that must not count as a finished task."""
    raws = _parse(ClaudeAdapter(), {**CLAUDE_OK, "is_error": True, "result": "API Error: 500"})

    assert raws[-1].kind == "error"
    assert ClaudeAdapter().extract_result(_task(), raws, diff="D").ok is False


def test_claude_rejected_by_its_limit_is_a_limit_hit_with_the_reset_time():
    adapter = ClaudeAdapter()
    raws = _parse(adapter, _claude_limit_event("rejected"),
                  {**CLAUDE_OK, "is_error": True, "result": "You've hit your limit · resets 3am"})

    hit = adapter.limit_hit(raws)

    assert hit is not None and hit.engine == "claude"
    assert hit.resets_at == RESET


def test_claude_limit_text_alone_is_enough_and_carries_its_reset():
    adapter = ClaudeAdapter()
    raws = _parse(adapter, {**CLAUDE_OK, "is_error": True,
                            "result": f"Claude AI usage limit reached|{RESET}"})

    assert adapter.limit_hit(raws).resets_at == RESET


def test_a_run_that_finished_despite_a_rejected_report_did_not_hit_a_limit():
    adapter = ClaudeAdapter()
    raws = _parse(adapter, _claude_limit_event("rejected"), CLAUDE_OK)

    assert adapter.limit_hit(raws) is None


def test_an_ordinary_claude_failure_is_not_a_limit_hit():
    adapter = ClaudeAdapter()
    raws = _parse(adapter, _claude_limit_event("allowed"),
                  {"type": "result", "subtype": "error_during_execution", "session_id": "S"})

    assert adapter.limit_hit(raws) is None


def test_claude_resumes_a_session_only_when_asked():
    adapter = ClaudeAdapter()

    assert "--resume" not in adapter.build_argv(_task(), cwd=Path("/wt"))
    argv = adapter.build_argv(_task(resume_session="S1"), cwd=Path("/wt"))
    assert argv[argv.index("--resume") + 1] == "S1"


# --- codex -------------------------------------------------------------------------

CODEX_LIMIT = ("You've hit your usage limit. Upgrade to Plus to continue using Codex "
               "(https://chatgpt.com/explore/plus), or try again at Oct 24th, 2026 1:51 AM.")


def test_codex_error_lines_become_error_events_with_their_message():
    raws = _parse(CodexAdapter(), {"type": "error", "message": CODEX_LIMIT})

    assert raws[0].kind == "error" and raws[0].payload["message"] == CODEX_LIMIT


def test_codex_stopped_by_its_usage_limit_is_a_limit_hit():
    adapter = CodexAdapter()
    raws = _parse(adapter, {"type": "thread.started", "thread_id": "T1"},
                  {"type": "error", "message": CODEX_LIMIT},
                  {"type": "turn.failed", "error": {"message": CODEX_LIMIT}})

    hit = adapter.limit_hit(raws)

    assert hit is not None and hit.engine == "codex"
    assert "usage limit" in hit.message


def test_an_ordinary_codex_failure_is_not_a_limit_hit():
    adapter = CodexAdapter()
    raws = _parse(adapter, {"type": "turn.failed", "error": {"message": "boom"}})

    assert adapter.limit_hit(raws) is None


def test_codex_never_resumes_a_session():
    """`codex exec resume` takes neither --sandbox nor -C, so a resumed session
    could run outside the sandbox; codex starts a new one in the worktree."""
    argv = CodexAdapter().build_argv(_task(engine="codex", resume_session="T1"), cwd=Path("/wt"))

    assert "resume" not in argv and "T1" not in argv
    assert argv[argv.index("-s") + 1] == "workspace-write"


# --- run_task ----------------------------------------------------------------------

class ScriptedAdapter(EngineAdapter):
    """Prints the given JSON events, parses each back as one RawEvent."""

    name = "claude"
    efforts = ()
    sign_in_command = "true"

    def __init__(self, events, hit=None):
        self.events, self.hit = events, hit

    def probe(self):
        return EngineInfo(name=self.name, version="1", path="/bin/true")

    def auth_problem(self, info):
        return None

    def build_argv(self, task, *, cwd):
        return ["python3", "-c", "".join(f"print({json.dumps(e)!r});" for e in self.events)]

    def parse_line(self, line):
        event = json.loads(line)
        return [RawEvent(kind=event["kind"], payload=event.get("payload", {}))]

    def extract_result(self, task, raws, diff):
        return ControlResult(task_id=task.id, engine=self.name, ok=False, text="", session_id="S1",
                             diff=diff, unproven_reason="engine reported failure")

    def limit_hit(self, raws):
        return self.hit


def _repo(tmp_path):
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"],
                 ["config", "user.name", "T"], ["commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return create_task_worktree(str(tmp_path), "t1", run_id="r1")


def test_run_task_reports_the_limit_the_engine_hit(tmp_path):
    hit = LimitHit(engine="claude", message="usage limit", resets_at=RESET)
    adapter = ScriptedAdapter([{"kind": "error", "payload": {"message": "usage limit"}}], hit=hit)

    result = run_task(_task(), run_id="r1", log=RunLog("r1", directory=tmp_path / "logs"),
                      worktree=_repo(tmp_path), adapter=adapter)

    assert result.limit == hit


def test_claudes_usage_report_in_a_run_becomes_the_fallback_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(limits, "_claude_auth_status", lambda: ("me@example.com", "pro"))
    info = {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.5, "resetsAt": RESET}}}
    adapter = ScriptedAdapter([{"kind": "usage", "payload": {"rate_limit_info": info}}])

    run_task(_task(), run_id="r1", log=RunLog("r1", directory=tmp_path / "logs"),
             worktree=_repo(tmp_path), adapter=adapter)

    report = json.loads(limits._report_path().read_text(encoding="utf-8"))
    assert report["info"] == info
    assert report["account"] == "me@example.com"
