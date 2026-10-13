"""Usage limits during a run: switch engines, pause, resume. docs/limits-and-resume.md."""

import dataclasses

import pytest
from scripted_engine import (  # noqa: F401 - repo is a fixture
    RESET, ScriptedEngine, _engines, _git, _reading, repo)

from hermes_control import model_policy, pauses, runs
from hermes_control.dispatch import NotReady, dispatch, land, resume_paused
from hermes_control.runlog import RunLog


def _run(repo, *tasks, run_id="r1"):
    raw = [{"goal": "add a line", "engine": "claude", "why": "w", "policy": "write", **task}
           for task in tasks]
    return dispatch(raw, parent_cwd=str(repo), run_id=run_id)["tasks"]


# --- before a task starts ----------------------------------------------------------

def test_an_engine_over_its_cap_hands_the_task_to_the_other_before_it_starts(repo, monkeypatch):
    claude, codex = ScriptedEngine("claude"), ScriptedEngine("codex", "ok")
    _engines(monkeypatch, claude, codex)
    _reading(monkeypatch, "claude", 90)

    [entry] = _run(repo, {"id": "a"})

    assert entry["engine"] == "codex" and entry["commit"]
    assert "90% of its 5-hour limit (cap 80%)" in entry["rerouted"]["reason"]
    assert claude.tasks == []
    # Logged when it happens, so a live view shows the move before the run ends.
    moved = [e for e in RunLog("r1").read_all() if e.kind == "rerouted"]
    assert moved and moved[0].payload["from"] == "claude"


def test_reroute_event_records_the_new_engines_resolved_model_and_effort(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude"), ScriptedEngine("codex", "ok"))
    _reading(monkeypatch, "claude", 90)
    policy = model_policy.parse_policy({
        "claude": {"allowed": {"sonnet": {"use_for": "planned", "effort": "low"}}},
        "codex": {"allowed": {"gpt-5.6-sol": {"use_for": "moved", "effort": "high"}}},
    })
    monkeypatch.setattr(model_policy, "load_policy", lambda: policy)

    _run(repo, {"id": "a", "model": "sonnet", "effort": "low"})

    events = RunLog("r1").read_all()
    planned = next(e for e in events if e.kind == "plan").payload["tasks"][0]
    moved = next(e for e in events if e.kind == "rerouted").payload
    assert (planned["model"], planned["effort"]) == ("sonnet", "low")
    assert (moved["model"], moved["effort"]) == ("gpt-5.6-sol", "high")


def test_plan_records_policy_settings_after_initial_engine_route(repo, monkeypatch):
    class SignedOutEngine(ScriptedEngine):
        def auth_problem(self, info):
            return "signed out"

    _engines(monkeypatch, SignedOutEngine("claude"), ScriptedEngine("codex", "ok"))
    policy = model_policy.parse_policy({
        "codex": {"allowed": {"gpt-5.6-sol": {"use_for": "moved", "effort": "high"}}},
    })
    monkeypatch.setattr(model_policy, "load_policy", lambda: policy)

    _run(repo, {"id": "a", "model": "sonnet", "effort": "low"})

    events = RunLog("r1").read_all()
    planned = next(e for e in events if e.kind == "plan").payload["tasks"][0]
    assert (planned["engine"], planned["model"], planned["effort"]) == (
        "codex", "gpt-5.6-sol", "high")


def test_a_task_is_paused_before_it_starts_when_no_engine_can_take_work(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude"), ScriptedEngine("codex"))
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", blocked=True)

    [entry] = _run(repo, {"id": "a"})

    assert entry["paused"]["id"] and entry["worktree"] == "" and entry["ok"] is False
    assert entry["unproven_reason"].startswith("paused:")
    record = pauses.load(entry["paused"]["id"])
    assert record.worktree is None and record.engine == "claude"


# --- a limit mid-task ----------------------------------------------------------------

def test_a_limit_mid_task_lets_the_other_engine_finish_in_the_same_worktree(repo, monkeypatch):
    claude, codex = ScriptedEngine("claude", "limit"), ScriptedEngine("codex", "ok")
    _engines(monkeypatch, claude, codex)

    [entry] = _run(repo, {"id": "a"})

    assert entry["engine"] == "codex" and entry["commit"]
    assert entry["rerouted"]["continued"] is True
    assert "earlier worker (claude)" in codex.tasks[0].goal
    # claude's unfinished edit was kept and committed with codex's.
    assert _git(["show", f"{entry['commit']}:work.txt"], repo).split() == ["claude", "codex"]
    kinds = [e.kind for e in RunLog("r1").read_all()]
    assert "limit" in kinds


class SettingsWritingEngine(ScriptedEngine):
    def build_argv(self, task, *, cwd):
        argv = super().build_argv(task, cwd=cwd)
        argv[-1] = ("from pathlib import Path; Path('.claude').mkdir(exist_ok=True); "
                    "Path('.claude/settings.json').write_text('{}'); " + argv[-1])
        return argv


def test_codex_settings_stop_claude_handover_without_a_commit(repo, monkeypatch):
    claude = ScriptedEngine("claude", "ok")
    _engines(monkeypatch, claude, SettingsWritingEngine("codex", "limit"))

    [entry] = _run(repo, {"id": "a", "engine": "codex"})

    assert claude.tasks == []
    assert entry["ok"] is False and entry["commit"] == "" and entry["paused"] is None
    assert ".claude/settings.json" in entry["unproven_reason"]
    assert "refused to run claude" in entry["unproven_reason"]
    assert any("refused to run claude" in str(event.payload)
               for event in RunLog("r1").read_all() if event.kind == "error")


def test_clean_codex_handover_still_starts_claude(repo, monkeypatch):
    claude = ScriptedEngine("claude", "ok")
    _engines(monkeypatch, claude, ScriptedEngine("codex", "limit"))

    [entry] = _run(repo, {"id": "a", "engine": "codex"})

    assert len(claude.tasks) == 1 and entry["ok"] is True and entry["commit"]


def test_when_neither_engine_can_take_it_the_task_is_paused_with_its_work_kept(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "limit"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)

    [entry] = _run(repo, {"id": "a"})

    assert entry["paused"] and entry["commit"] == ""
    record = pauses.load(entry["paused"]["id"])
    assert (record.engine, record.session_id, record.resets_at) == ("claude", "claude-session", RESET)
    assert (record.worktree_path() / "work.txt").read_text(encoding="utf-8") == "claude\n"
    assert "paused" in [e.kind for e in RunLog("r1").read_all()]


def test_second_engine_limit_resumes_with_its_own_model(repo, monkeypatch):
    engines = _engines(monkeypatch, ScriptedEngine("claude", "limit"),
                       ScriptedEngine("codex", "limit", "ok"))
    policy = {
        "claude": model_policy.EnginePolicy("claude-sonnet-5"),
        "codex": model_policy.EnginePolicy("codex-default"),
    }
    monkeypatch.setattr(model_policy, "load_policy", lambda: policy)

    [entry] = _run(repo, {"id": "a", "model": "claude-sonnet-5", "effort": "high"})
    record = pauses.load(entry["paused"]["id"])
    assert [task.model for task in engines["codex"].tasks] == ["codex-default"]
    assert record.engine == "codex"
    assert record.control_task().model == "codex-default"
    assert record.control_task().effort is None
    assert record.control_task().goal == "add a line"

    resumed = resume_paused(record.id)

    assert resumed["ok"] is True
    assert engines["codex"].tasks[-1].model == "codex-default"
    assert engines["codex"].tasks[-1].effort is None
    assert engines["codex"].tasks[-1].goal.count("An earlier worker") == 1


def test_resume_repairs_an_old_cross_engine_pause_record(repo, monkeypatch):
    engines = _engines(monkeypatch, ScriptedEngine("claude", "limit"),
                       ScriptedEngine("codex", "limit", "ok"))
    policy = {"claude": model_policy.EnginePolicy("claude-sonnet-5"),
              "codex": model_policy.EnginePolicy("codex-default")}
    monkeypatch.setattr(model_policy, "load_policy", lambda: policy)
    [entry] = _run(repo, {"id": "a", "model": "claude-sonnet-5", "effort": "high"})
    record = pauses.load(entry["paused"]["id"])
    legacy_task = dict(record.task, model="claude-sonnet-5", effort="high")
    pauses.save(dataclasses.replace(record, task=legacy_task))

    resumed = resume_paused(record.id)

    assert resumed["ok"] is True
    assert engines["codex"].tasks[-1].model == "codex-default"
    assert engines["codex"].tasks[-1].effort is None


@pytest.mark.parametrize("effort, rerouted", [("high", {"dropped_effort": "high"}),
                                              ("ultra", {})])
def test_resume_repairs_old_effort_without_leaving_a_worktree(repo, monkeypatch,
                                                               effort, rerouted):
    engines = _engines(monkeypatch, ScriptedEngine("claude", "ok"),
                       ScriptedEngine("codex"))
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", blocked=True)
    [entry] = _run(repo, {"id": "a"}, run_id=f"old-effort-{bool(rerouted)}")
    record = pauses.load(entry["paused"]["id"])
    pauses.save(dataclasses.replace(record, task=dict(record.task, effort=effort),
                                     rerouted=rerouted))
    _reading(monkeypatch, "claude", 10)

    resumed = resume_paused(record.id)

    assert resumed["ok"] is True
    assert engines["claude"].tasks[-1].effort is None
    assert resumed["commit"]
    event = next(e for e in RunLog(record.run_id).read_all() if e.kind == "resumed")
    assert event.payload["model"] == engines["claude"].tasks[-1].model
    assert event.payload["effort"] is None


def test_resume_policy_error_does_not_create_worktree(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", blocked=True)
    [entry] = _run(repo, {"id": "a"}, run_id="resume-policy-error")
    record = pauses.load(entry["paused"]["id"])
    _reading(monkeypatch, "claude", 10)
    monkeypatch.setattr(model_policy, "apply_policy",
                        lambda *args: (_ for _ in ()).throw(ValueError("bad policy")))

    with pytest.raises(ValueError, match="bad policy"):
        resume_paused(record.id)

    assert record.task_worktree() is None
    assert pauses.load(record.id) is not None
    assert _git(["branch", "--list", f"*{record.run_id}*"], repo) == ""
    assert len(_git(["worktree", "list", "--porcelain"], repo).split("worktree ")) == 2


def test_tasks_that_depend_on_a_paused_task_wait_too(repo, monkeypatch):
    claude = ScriptedEngine("claude", "limit")
    _engines(monkeypatch, claude, ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)

    first, second = _run(repo, {"id": "a"}, {"id": "b", "depends_on": ["a"]})

    assert first["paused"] and second["paused"]
    assert "waits for paused task a" in second["unproven_reason"]
    assert len(claude.tasks) == 1  # b never started


# --- resume ------------------------------------------------------------------------

def _paused(repo, monkeypatch, *, claude_outcomes=("limit",), run_id="r1"):
    """One task paused mid-task on claude; returns (record id, engines)."""
    engines = _engines(monkeypatch, ScriptedEngine("claude", *claude_outcomes),
                       ScriptedEngine("codex", "ok"))
    _reading(monkeypatch, "codex", blocked=True)
    [entry] = _run(repo, {"id": "a"}, run_id=run_id)
    return entry["paused"]["id"], engines


def test_resume_continues_the_claude_session_in_its_worktree_and_lands_it(repo, monkeypatch):
    pause_id, engines = _paused(repo, monkeypatch, claude_outcomes=("limit", "ok"))
    _reading(monkeypatch, "claude", 10)

    entry = resume_paused(pause_id)

    assert engines["claude"].tasks[-1].resume_session == "claude-session"
    assert entry["commit"] and entry["paused"] is None
    assert _git(["show", f"{entry['commit']}:work.txt"], repo).split() == ["claude", "claude"]
    assert pauses.load(pause_id) is None
    assert "resumed" in [e.kind for e in RunLog("r1").read_all()]


@pytest.mark.parametrize("same_engine", [True, False])
def test_resume_onto_claude_refuses_changed_settings(repo, monkeypatch, same_engine):
    if same_engine:
        pause_id, engines = _paused(repo, monkeypatch, claude_outcomes=("limit", "ok"))
        _reading(monkeypatch, "claude", 10)
    else:
        engines = _engines(monkeypatch, ScriptedEngine("claude", "ok"),
                           ScriptedEngine("codex", "limit"))
        _reading(monkeypatch, "claude", blocked=True)
        [entry] = _run(repo, {"id": "a", "engine": "codex"})
        pause_id = entry["paused"]["id"]
        _reading(monkeypatch, "claude", 10)
        _reading(monkeypatch, "codex", blocked=True)
    settings = pauses.load(pause_id).worktree_path() / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}")

    entry = resume_paused(pause_id)

    assert len(engines["claude"].tasks) == (1 if same_engine else 0)
    assert entry["ok"] is False and entry["commit"] == "" and entry["paused"] is None
    assert ".claude/settings.json" in entry["unproven_reason"]
    assert pauses.load(pause_id) is None


def test_resume_moves_to_the_other_engine_when_the_paused_one_is_still_held(repo, monkeypatch):
    pause_id, engines = _paused(repo, monkeypatch)
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", 5)

    entry = resume_paused(pause_id)

    assert entry["engine"] == "codex" and entry["commit"]
    codex_task = engines["codex"].tasks[-1]
    assert codex_task.resume_session is None and "earlier worker (claude)" in codex_task.goal


@pytest.mark.parametrize("failure_site", ["landing", "summary"])
def test_resume_removes_pause_after_landing_log_failure(repo, monkeypatch, failure_site):
    pause_id, _ = _paused(repo, monkeypatch, claude_outcomes=("limit", "ok"))
    _reading(monkeypatch, "claude", 10)
    if failure_site == "landing":
        original = RunLog.append

        def fail_landing(self, events):
            if any(event.kind == "commit" for event in events):
                raise OSError("landing log full")
            return original(self, events)

        monkeypatch.setattr(RunLog, "append", fail_landing)
    else:
        monkeypatch.setattr("hermes_control.dispatch._log_summary",
                            lambda *args: (_ for _ in ()).throw(OSError("summary log full")))

    entry = resume_paused(pause_id)

    assert entry["commit"] and entry["paused"] is None
    assert pauses.load(pause_id) is None
    if failure_site == "landing":
        assert "not verified: landing log failed: OSError: landing log full" == entry["unproven_reason"]


def test_resume_logs_reroute_only_after_worktree_creation(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude"), ScriptedEngine("codex", "ok"))
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", blocked=True)
    [entry] = _run(repo, {"id": "a"}, run_id="reroute-no-worktree")
    pause_id = entry["paused"]["id"]
    _reading(monkeypatch, "codex", 5)
    monkeypatch.setattr("hermes_control.dispatch.create_task_worktree_with_reason",
                        lambda *args, **kwargs: (None, "worktree creation failed"))

    with pytest.raises(NotReady, match="worktree creation failed"):
        resume_paused(pause_id)

    assert pauses.load(pause_id) is not None
    assert not any(e.kind == "rerouted" for e in RunLog("reroute-no-worktree").read_all())


def test_resume_is_refused_while_no_engine_can_take_work(repo, monkeypatch):
    pause_id, _ = _paused(repo, monkeypatch)
    _reading(monkeypatch, "claude", 95)

    with pytest.raises(NotReady, match="95%"):
        resume_paused(pause_id)
    assert pauses.load(pause_id) is not None


def test_a_task_paused_before_it_started_gets_its_worktree_on_resume(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "claude", 95)
    _reading(monkeypatch, "codex", blocked=True)
    [entry] = _run(repo, {"id": "a"})
    _reading(monkeypatch, "claude", 10)

    resumed = resume_paused(entry["paused"]["id"])

    assert resumed["commit"] and resumed["worktree"]


def test_resume_waits_for_the_task_it_depends_on(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "limit"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    _, second = _run(repo, {"id": "a"}, {"id": "b", "depends_on": ["a"]})
    _reading(monkeypatch, "claude", 10)

    with pytest.raises(NotReady, match="resume a first"):
        resume_paused(second["paused"]["id"])


@pytest.mark.parametrize("chain", [False, True])
def test_failed_resume_skips_paused_dependents(repo, monkeypatch, chain):
    claude = ScriptedEngine("claude", "limit", "ok")
    engines = _engines(monkeypatch, claude, ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    tasks = [{"id": "a", "verify": "false"}, {"id": "b", "depends_on": ["a"]}]
    if chain:
        tasks.append({"id": "c", "depends_on": ["b"]})
    original = _run(repo, *tasks)
    _reading(monkeypatch, "claude", 10)
    entry = resume_paused(original[0]["paused"]["id"])

    skipped = ["b", "c"] if chain else ["b"]
    assert entry["skipped_dependents"] == skipped
    assert all(pauses.load(e["paused"]["id"]) is None for e in original[1:])
    by_id = {task["id"]: task for task in runs.run_view("r1")["tasks"]}
    for task_id in skipped:
        assert by_id[task_id]["status"] == "failed"
        dependency = "b" if task_id == "c" else "a"
        assert by_id[task_id]["status_reason"] == f"skipped: depends on {dependency}, which failed"
    assert len(engines["claude"].tasks) == 2


def test_failed_resume_removes_dependents_before_parent_pause(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "limit", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    first, second = _run(repo, {"id": "a", "verify": "false"},
                         {"id": "b", "depends_on": ["a"]})
    parent_id, dependent_id = first["paused"]["id"], second["paused"]["id"]
    _reading(monkeypatch, "claude", 10)
    original_remove = pauses.remove

    def observe_remove(record_id):
        if record_id == parent_id:
            assert pauses.load(dependent_id) is None
        else:
            assert pauses.load(parent_id) is not None
        return original_remove(record_id)

    monkeypatch.setattr(pauses, "remove", observe_remove)
    entry = resume_paused(parent_id)

    assert entry["skipped_dependents"] == ["b"]


def test_failed_resume_removes_its_own_pause_even_if_skipping_dependents_fails(repo, monkeypatch):
    # The resumed task has already landed: whatever goes wrong while settling its
    # dependents, its own pause record must go, or the user could resume it twice.
    _engines(monkeypatch, ScriptedEngine("claude", "limit", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    first, _ = _run(repo, {"id": "a", "verify": "false"}, {"id": "b", "depends_on": ["a"]})
    _reading(monkeypatch, "claude", 10)

    real_all_paused = pauses.all_paused
    calls = []

    def broken_all_paused():
        # The first call is resume_blocker's, before the task runs; break only
        # the one that collects dependents after it has landed.
        calls.append(1)
        if len(calls) > 1:
            raise OSError("pause store unreadable")
        return real_all_paused()

    monkeypatch.setattr(pauses, "all_paused", broken_all_paused)
    with pytest.raises(OSError):
        resume_paused(first["paused"]["id"])

    assert len(calls) == 2
    assert pauses.load(first["paused"]["id"]) is None


class FailingResumeEngine(ScriptedEngine):
    def limit_hit(self, raws):
        return super().limit_hit(raws) if len(self.tasks) == 1 else None


@pytest.mark.parametrize("failure", ["engine", "verify_never_ran"])
def test_failed_resume_skips_dependents_for_engine_or_missing_verify(repo, monkeypatch, failure):
    claude = (FailingResumeEngine("claude", "limit", "failure") if failure == "engine"
              else ScriptedEngine("claude", "limit", "ok"))
    _engines(monkeypatch, claude, ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    first, second = _run(repo, {"id": "a", "verify": "true" if failure == "verify_never_ran" else None},
                         {"id": "b", "depends_on": ["a"]})
    _reading(monkeypatch, "claude", 10)
    if failure == "verify_never_ran":
        def without_verify(*args, **kwargs):
            return dataclasses.replace(land(*args, **kwargs), verify=None)

        monkeypatch.setattr("hermes_control.dispatch.land", without_verify)

    entry = resume_paused(first["paused"]["id"])

    assert entry["ok"] is (failure == "verify_never_ran")
    assert entry["verify"] is None
    assert entry["skipped_dependents"] == ["b"]
    assert pauses.load(second["paused"]["id"]) is None
    by_id = {task["id"]: task for task in runs.run_view("r1")["tasks"]}
    assert by_id["b"]["status"] == "failed"
    assert by_id["b"]["status_reason"] == "skipped: depends on a, which failed"


def test_successful_resume_keeps_dependent_paused(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "limit", "ok"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    first, second = _run(repo, {"id": "a"}, {"id": "b", "depends_on": ["a"]})
    _reading(monkeypatch, "claude", 10)
    entry = resume_paused(first["paused"]["id"])
    assert entry["skipped_dependents"] == []
    assert pauses.load(second["paused"]["id"]) is not None
    assert runs.run_view("r1")["tasks"][1]["status"] == "paused"


def test_another_limit_during_resume_keeps_the_task_paused(repo, monkeypatch):
    _engines(monkeypatch, ScriptedEngine("claude", "limit", "limit"), ScriptedEngine("codex"))
    _reading(monkeypatch, "codex", blocked=True)
    first, second = _run(repo, {"id": "a"}, {"id": "b", "depends_on": ["a"]})
    pause_id = first["paused"]["id"]
    _reading(monkeypatch, "claude", 10)

    entry = resume_paused(pause_id)

    assert entry["paused"]["id"] == pause_id and entry["commit"] == ""
    assert entry["skipped_dependents"] == []
    assert pauses.load(pause_id) is not None
    assert pauses.load(second["paused"]["id"]) is not None
    assert runs.run_view("r1")["tasks"][1]["status"] == "paused"


def test_discarding_a_paused_task_keeps_its_worktree(repo, monkeypatch):
    pause_id, _ = _paused(repo, monkeypatch)
    worktree = pauses.load(pause_id).worktree_path()

    pauses.remove(pause_id)

    assert pauses.load(pause_id) is None and worktree.is_dir()


def test_a_second_run_of_a_task_continues_its_sequence_in_the_log(repo, monkeypatch):
    """A handover runs the same task twice in one log; each event keeps a unique seq."""
    _engines(monkeypatch, ScriptedEngine("claude", "limit"), ScriptedEngine("codex", "ok"))

    _run(repo, {"id": "a"})

    seqs = [e.seq for e in RunLog("r1").read_all() if e.task_id == "a"]
    assert len(seqs) == len(set(seqs)), seqs


def test_paused_again_survives_summary_log_failure(repo, monkeypatch):
    pause_id, _ = _paused(repo, monkeypatch, claude_outcomes=("limit", "limit"))
    _reading(monkeypatch, "claude", 10)
    monkeypatch.setattr("hermes_control.dispatch._log_summary",
                        lambda *args: (_ for _ in ()).throw(OSError("summary log full")))
    entry = resume_paused(pause_id)
    assert entry["paused"] and pauses.load(entry["paused"]["id"]) is not None
