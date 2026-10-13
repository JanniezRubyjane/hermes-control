import pytest

from hermes_control.dispatch import (order_tasks, parse_tasks, with_check_guidance,
                                     with_verify_note)
from hermes_control.policy import ExecPolicy


def test_parse_tasks_defaults_policy_to_read_only():
    tasks = parse_tasks([{"id": "t1", "goal": "look around", "engine": "claude"}])
    assert tasks[0].policy == ExecPolicy(write=False)


def test_parse_tasks_maps_write_policy():
    tasks = parse_tasks([{"id": "t1", "goal": "g", "engine": "codex", "policy": "write",
                          "why": "pattern work"}])
    assert tasks[0].policy.write is True
    assert tasks[0].why == "pattern work"


def test_parse_tasks_rejects_an_unknown_engine():
    with pytest.raises(ValueError, match="unknown engine"):
        parse_tasks([{"id": "t1", "goal": "g", "engine": "gemini"}])


def test_parse_tasks_rejects_a_missing_goal():
    with pytest.raises(ValueError, match="goal"):
        parse_tasks([{"id": "t1", "engine": "claude"}])


@pytest.mark.parametrize("task_id", ["a/../../../x", "a b", "a*", "a)", ".hidden", "a" * 65])
def test_parse_tasks_rejects_unsafe_ids(task_id):
    with pytest.raises(ValueError, match=r"id.*\[A-Za-z0-9\]"):
        parse_tasks([{"id": task_id, "goal": "g", "engine": "claude"}])


@pytest.mark.parametrize("task_id", ["a", "Z9._-", "a" * 64])
def test_parse_tasks_accepts_safe_ids(task_id):
    assert parse_tasks([{"id": task_id, "goal": "g", "engine": "claude"}])[0].id == task_id


def test_parse_tasks_default_id_is_safe():
    assert parse_tasks([{"goal": "g", "engine": "claude"}])[0].id == "task-1"


def test_parse_tasks_rejects_a_goal_that_starts_with_a_flag():
    with pytest.raises(ValueError, match="goal.*start.*word"):
        parse_tasks([{"id": "a", "goal": "  --allowedTools=Bash(*)", "engine": "claude"}])


def test_parse_tasks_rejects_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate"):
        parse_tasks([{"id": "t1", "goal": "a", "engine": "claude"},
                     {"id": "t1", "goal": "b", "engine": "claude"}])


def test_parse_tasks_requires_why_when_asked():
    with pytest.raises(ValueError, match="why"):
        parse_tasks([{"id": "t1", "goal": "g", "engine": "claude", "why": ""}],
                    require_why=True)


def test_order_tasks_respects_dependencies():
    tasks = parse_tasks([
        {"id": "b", "goal": "second", "engine": "claude", "depends_on": ["a"]},
        {"id": "a", "goal": "first", "engine": "claude"},
    ])
    assert [t.id for t in order_tasks(tasks)] == ["a", "b"]


def test_order_tasks_is_stable_for_independent_tasks():
    tasks = parse_tasks([{"id": "x", "goal": "1", "engine": "claude"},
                         {"id": "y", "goal": "2", "engine": "claude"}])
    assert [t.id for t in order_tasks(tasks)] == ["x", "y"]


def test_order_tasks_rejects_a_cycle():
    tasks = parse_tasks([
        {"id": "a", "goal": "1", "engine": "claude", "depends_on": ["b"]},
        {"id": "b", "goal": "2", "engine": "claude", "depends_on": ["a"]},
    ])
    with pytest.raises(ValueError, match="cycle"):
        order_tasks(tasks)


def test_order_tasks_rejects_an_unknown_dependency():
    tasks = parse_tasks([{"id": "a", "goal": "1", "engine": "claude",
                          "depends_on": ["ghost"]}])
    with pytest.raises(ValueError, match="ghost"):
        order_tasks(tasks)


def test_parse_tasks_keeps_the_verify_command():
    tasks = parse_tasks([{"id": "t1", "goal": "g", "engine": "codex", "why": "w",
                          "verify": "  python3 -m pytest -q  "},
                         {"id": "t2", "goal": "g", "engine": "codex", "why": "w",
                          "verify": "   "},
                         {"id": "t3", "goal": "g", "engine": "codex", "why": "w"}])
    assert [task.verify for task in tasks] == ["python3 -m pytest -q", None, None]


def test_codex_write_check_guidance_follows_verify_note_once():
    task = parse_tasks([{"id": "t1", "goal": "change it", "engine": "codex",
                         "policy": "write", "verify": "python3 -m pytest -q"}])[0]
    goal = with_check_guidance(with_verify_note(task)).goal
    paragraph = ("How to work efficiently here: you may run the check command above "
                 "yourself inside your sandbox at any time. While you work, run only "
                 "the tests that cover the files you change (for example the matching "
                 "test file), and run the full check command once when you think you "
                 "are done; if it fails, fix and run it again. Read files in the line "
                 "ranges you need rather than printing whole files, and read a whole "
                 "file when you need its full context. Never skip, weaken "
                 "or delete a test to make the check pass.")
    assert goal == ("change it\n\nWhen you finish, Hermes-Control will commit your "
                    "changes and check them by running this command in this directory, "
                    "in a sandbox with no network:\npython3 -m pytest -q\n"
                    "Make that command pass.\n\n" + paragraph)
    assert goal.count(paragraph) == 1


@pytest.mark.parametrize("engine,policy,verify", [
    ("codex", "write", None),
    ("codex", "read-only", "python3 -m pytest -q"),
    ("claude", "write", "python3 -m pytest -q"),
])
def test_check_guidance_is_only_for_codex_write_with_verify(engine, policy, verify):
    task = parse_tasks([{"id": "t1", "goal": "g", "engine": engine,
                         "policy": policy, "verify": verify}])[0]
    assert with_check_guidance(task) == task



def test_parse_tasks_keeps_the_effort():
    tasks = parse_tasks([{"id": "t1", "goal": "g", "engine": "codex", "effort": " high "}])
    assert tasks[0].effort == "high"


@pytest.mark.parametrize("field, value", [
    ("model", "--dangerously-skip-permissions"),
    ("model", "sonnet --add-dir /"),
    ("effort", "-high"),
])
def test_a_model_or_effort_that_could_pose_as_a_flag_is_refused(field, value):
    """Both become argv values after --model / --effort; the planner is an LLM."""
    with pytest.raises(ValueError, match=field):
        parse_tasks([{"id": "t1", "goal": "g", "engine": "claude", field: value}])
