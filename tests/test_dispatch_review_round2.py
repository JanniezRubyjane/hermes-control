"""Regression coverage for dispatch review items A7 and B6."""

from hermes_control import dispatch as dispatch_module, parallel, pauses, routing
from hermes_control.engines.base import ControlTask
from hermes_control.events import ControlResult
from hermes_control.policy import ExecPolicy
from tests.test_dispatch_parallel import engines, events, repo, task  # noqa: F401


def test_parallel_disabled_engine_pauses_without_losing_completed_task(repo, monkeypatch, engines):
    for module in (dispatch_module, parallel, routing):
        monkeypatch.setattr(module, "ENGINE_NAMES", ("claude",))
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "plugins": {"entries": {"hermes_control": {"settings": {
            "parallel": {"max_tasks": 2},
            "routing": {"fallback": {"codex": []}},
        }}}}})

    summary = dispatch_module.dispatch(
        [task("done", engine="claude"), task("disabled", engine="codex")],
        parent_cwd=str(repo))

    plan = next(event["payload"] for event in events(summary) if event["kind"] == "plan")
    assert plan["parallel"] == {
        "max_tasks": 2, "per_engine": {"claude": 2}, "error": None}
    done, disabled = summary["tasks"]
    assert done["ok"] and done["commit"] and done["paused"] is None
    assert not disabled["ok"] and not disabled["commit"]
    assert disabled["paused"] is not None
    assert disabled["rerouted"]["stayed"] is True
    assert pauses.load(disabled["paused"]["id"]).engine == "codex"
    assert not engines["codex"].tasks


def test_paused_entry_redacts_result_text():
    task = ControlTask(id="paused", goal="test", engine="claude", policy=ExecPolicy(write=False))
    record = pauses.Pause(
        id="run-paused", run_id="run", task=pauses.task_to_dict(task), repo=None,
        worktree=None, engine="claude", session_id=None, reason="usage limit",
        resets_at=None, paused_at=0)
    secret = "sk-" + "ant-api03-" + "A" * 95
    result = ControlResult(task_id=task.id, engine=task.engine, ok=False,
                           text=f"key is {secret}", session_id=None)

    entry = dispatch_module._paused_entry(task, record, result=result)

    assert secret not in entry["text"]
    assert entry["text"].startswith("key is ")
    assert entry["text"] != "key is "
