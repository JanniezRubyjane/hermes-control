"""Tests for the batched-probe working-style rule prepended to Codex and Gemini goals."""

from pathlib import Path

import pytest

from hermes_control import dispatch, landing, worktree
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from hermes_control.runlog import RunLog
from hermes_control.worker_report import REPORT_REQUEST


EXPECTED_WORK_STYLE = (
    "Working style (turns cost more than bytes): batch independent read-only probes "
    "into ONE shell call, separated with echo '--- name' markers, and cap each "
    "output with head or rg -m. Never print a whole large file; read only the slice "
    "you need (rg -n with context, or sed -n 'A,Bp'). Stop as soon as every item "
    "asked for has evidence; do not re-verify what you already saw. This never "
    "shortens the tests, checks or report the task asks for. In your final message, "
    "mark each claim as seen (cite file:line) or inferred; a fact you did not find "
    "is unknown."
)


class _FakeAdapter:
    name = "gemini"

    def __init__(self, outcome):
        self._outcome = outcome

    def headless_commands_allowed(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class _StubEngine:
    name = "codex"


@pytest.mark.parametrize("write", [True, False])
def test_codex_write_and_read_only_tasks_get_work_style(write):
    original = ControlTask("t", "inspect files", "codex", ExecPolicy(write=write))
    styled = dispatch.with_work_style(original)
    assert dispatch.WORK_STYLE == EXPECTED_WORK_STYLE
    assert styled.goal == f"{EXPECTED_WORK_STYLE}\n\nTask: inspect files"


@pytest.mark.parametrize("write", [True, False])
def test_gemini_adapter_allowed_gets_work_style(write):
    original = ControlTask("t", "inspect files", "gemini", ExecPolicy(write=write))
    styled = dispatch.with_work_style(original, adapter=_FakeAdapter(True))
    assert styled.goal == f"{EXPECTED_WORK_STYLE}\n\nTask: inspect files"


@pytest.mark.parametrize("outcome", [False, None, RuntimeError("denied")])
def test_gemini_adapter_disallowed_or_raising_skips_work_style(outcome):
    original = ControlTask("t", "inspect files", "gemini", ExecPolicy(write=True))
    assert dispatch.with_work_style(original, adapter=_FakeAdapter(outcome)) == original


def test_gemini_default_adapter_lookup_is_guarded(monkeypatch):
    original = ControlTask("t", "inspect files", "gemini", ExecPolicy(write=False))
    monkeypatch.setattr(dispatch, "get_adapter", lambda _: _FakeAdapter(True))
    assert dispatch.with_work_style(original).goal == f"{EXPECTED_WORK_STYLE}\n\nTask: inspect files"
    monkeypatch.setattr(dispatch, "get_adapter", lambda _: (_ for _ in ()).throw(RuntimeError("no agy")))
    assert dispatch.with_work_style(original) == original


@pytest.mark.parametrize("write", [True, False])
def test_claude_tasks_never_get_work_style(write):
    original = ControlTask("t", "inspect files", "claude", ExecPolicy(write=write))
    assert dispatch.with_work_style(original) == original


@pytest.mark.parametrize("engine,adapter", [
    ("codex", None),
    ("gemini", _FakeAdapter(True)),
])
def test_applying_with_work_style_twice_equals_applying_once(engine, adapter):
    original = ControlTask("t", "inspect files", engine, ExecPolicy(write=True))
    once = dispatch.with_work_style(original, adapter=adapter)
    twice = dispatch.with_work_style(once, adapter=adapter)
    assert twice == once
    assert once.goal.count(EXPECTED_WORK_STYLE) == 1


def test_prepared_codex_write_goal_and_landed_commit_message(monkeypatch, tmp_path):
    original = ControlTask(
        "t", "fix parser bug", "codex", ExecPolicy(write=True),
        verify="pytest -q", files_hint=("hermes_control/dispatch.py",),
    )
    seen = []

    def fake_run_task_engine(prepared, **_kwargs):
        seen.append(prepared)
        return ControlResult(task_id=prepared.id, engine="codex", ok=True, text="done",
                             session_id="s1", diff="+1\n")

    monkeypatch.setattr(dispatch, "_run_task_engine", fake_run_task_engine)
    tree = worktree.TaskWorktree(tmp_path, "b", tmp_path, "deadbeef")
    dispatch.run_task(original, run_id="run-1", log=RunLog("run-1"), worktree=tree,
                      adapter=_StubEngine())
    assert len(seen) == 1
    prepared = seen[0]
    assert prepared.goal.startswith(f"{EXPECTED_WORK_STYLE}\n\nTask: fix parser bug")
    assert "The planner expects the relevant code to be here" in prepared.goal
    assert "When you finish, Hermes-Control will commit your changes" in prepared.goal
    assert "How to work efficiently here:" in prepared.goal
    assert REPORT_REQUEST in prepared.goal

    landed = dispatch._landed(original, prepared)
    msg = landing.commit_message(landed, "run-1")
    assert dispatch.WORK_STYLE not in msg
    assert "Working style (" not in msg
    assert "fix parser bug" in msg
