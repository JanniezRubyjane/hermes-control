"""Gemini write reports and actionable headless permission refusals."""

import dataclasses
import sys
import time

import pytest

from hermes_control import dispatch, pauses, routing
from hermes_control.engines import registry
from hermes_control.engines.gemini import GeminiAdapter
from hermes_control.events import RawEvent
from hermes_control.policy import ExecPolicy
from hermes_control.worker_report import REPORT_REQUEST
from test_gemini_r3 import settings, task, tree  # noqa: F401 - shared fixtures


REASON = (
    "gemini refused write_file in headless mode: write_file: a path outside the repo; "
    "keep the goal inside it"
)


@pytest.mark.parametrize("engine,write,verify,guidance,report", [
    ("gemini", True, "true", True, True),
    ("claude", True, "true", False, False),
    ("gemini", False, "true", False, False),
    ("gemini", True, None, False, True),
])
def test_worker_guidance_and_report(settings, engine, write, verify, guidance, report):
    chosen = dataclasses.replace(task(verify=verify), engine=engine, policy=ExecPolicy(write=write))
    prepared = dispatch.with_report_request(dispatch.with_check_guidance(chosen, adapter=GeminiAdapter()))
    assert ("How to work efficiently here:" in prepared.goal) == guidance
    assert (REPORT_REQUEST in prepared.goal) == report
    assert dispatch.with_report_request(prepared) == prepared


def test_denied_result_is_actionable_guard():
    result = GeminiAdapter().extract_result(task(), [RawEvent("result", {
        "ok": True, "denied_actions": [{"action": "write_file"}],
    })], "")
    assert result.ok is False
    assert result.failure_mode == "guard"
    assert result.unproven_reason == REASON


@pytest.mark.parametrize("resume", [False, True])
def test_nonzero_denied_process_preserves_guard(tree, settings, tmp_path, monkeypatch, resume):
    # As in test_gemini_guard's fake_worker, use Python through real streaming
    # and dispatch paths, with safe temporary machine settings and no agy probe.
    script = tmp_path / "fake_agy.py"
    script.write_text('''
import json
import sys
print(json.dumps({"event": "init", "conversation_id": "saved-session"}))
print(json.dumps({"event": "result", "result": {
    "status": "SUCCESS", "denied_actions": [{"action": "write_file"}]}}))
sys.exit(7)
''')
    names = ("claude", "codex", "gemini")
    for module in (dispatch, routing, registry):
        monkeypatch.setattr(module, "ENGINE_NAMES", names)
    monkeypatch.setattr(dispatch, "engine_status", lambda _: {
        "claude": "signed out", "codex": "signed out", "gemini": None})
    monkeypatch.setattr(dispatch, "_parallel", lambda: (
        {"max_tasks": 1, "per_engine": {name: 1 for name in names}}, None))
    monkeypatch.setattr(dispatch, "create_task_worktree_with_reason", lambda *a, **k: (tree, None))
    monkeypatch.setattr(GeminiAdapter, "build_argv", lambda *a, **k: [sys.executable, str(script)])
    if resume:
        record = pauses.Pause("denied-t", "denied", pauses.task_to_dict(task()), str(tree.repo_root),
                              pauses.worktree_to_dict(tree), "gemini", "saved-session", "limit",
                              None, time.time())
        pauses.save(record)
        entry = dispatch.resume_paused(record.id, timeout=10)
        assert pauses.load(record.id) is None
    else:
        result = dispatch.dispatch([{
            "id": "t", "goal": "edit files", "engine": "gemini", "why": "test refusal",
            "policy": "write", "model": "gemini-4-argon",
        }], parent_cwd=str(tree.repo_root), run_id="denied", timeout=10)
        assert "error" not in result, result
        entry = result["tasks"][0]
    assert entry["ok"] is False
    assert entry["failure_mode"] == "guard"
    assert entry["unproven_reason"] == REASON
    assert entry["commit"] == "" and entry["paused"] is None
