"""Which model and effort each task may use, per engine, from config.yaml.

Settings live where Hermes keeps every plugin's settings:
``plugins.entries.hermes_control.settings.models``.
"""

import pytest
import yaml

from hermes_control import model_policy
from hermes_control.engines.base import ControlTask
from hermes_control.policy import ExecPolicy

SETTINGS = {
    "claude": {
        "default": "sonnet",
        "allowed": {
            "sonnet": {"use_for": "routine edits, tests", "effort": "medium"},
            "opus": {"use_for": "hard bugs across modules", "effort": "high"},
        },
    },
    "codex": {"allowed": {"gpt-5.6-terra": "most tasks"}},
}


def _task(engine="claude", model=None, effort=None):
    return ControlTask(id="t1", goal="g", engine=engine, policy=ExecPolicy(write=True),
                       model=model, effort=effort)


# --- reading the settings -----------------------------------------------------

def test_no_settings_means_no_restriction():
    assert model_policy.parse_policy(None) == {}
    assert model_policy.parse_policy({}) == {}
    assert model_policy.parse_policy({"claude": None}) == {}
    assert model_policy.parse_policy({"claude": {}}) == {}
    assert model_policy.apply_policy(_task(model="anything-1"), {}).model == "anything-1"


def test_both_spellings_of_an_allowed_model_are_accepted():
    policy = model_policy.parse_policy(SETTINGS)

    assert policy["claude"].allowed["opus"].effort == "high"
    assert policy["codex"].allowed["gpt-5.6-terra"].use_for == "most tasks"


def test_without_a_default_the_first_allowed_model_is_the_default():
    """Otherwise an empty model would fall through to the CLI's own default,
    which the allow-list may not include."""
    assert model_policy.parse_policy(SETTINGS)["codex"].default == "gpt-5.6-terra"


@pytest.mark.parametrize("settings, message", [
    ({"gemini": {}}, "unknown engine"),
    ({"claude": {"default": "haiku", "allowed": {"sonnet": ""}}}, "not in the allowed"),
    ({"claude": {"allowed": {"sonnet": {"effort": "ludicrous"}}}}, "effort"),
    ({"codex": {"allowed": {"--dangerously-bypass-approvals-and-sandbox": ""}}}, "model name"),
    ({"claude": {"allowed": {"sonnet": {"use_for": "x", "temperature": 1}}}}, "temperature"),
])
def test_invalid_settings_are_rejected_with_the_reason(settings, message):
    with pytest.raises(ValueError, match=message):
        model_policy.parse_policy(settings)


@pytest.mark.parametrize("raw", [False, 0, [], ""])
def test_falsey_non_mapping_settings_are_rejected(raw):
    with pytest.raises(ValueError, match="models settings"):
        model_policy.parse_policy(raw)


@pytest.mark.parametrize("block", [False, 0, [], ""])
def test_falsey_non_mapping_engine_blocks_are_rejected(block):
    with pytest.raises(ValueError, match=f"claude.*{type(block).__name__}"):
        model_policy.parse_policy({"claude": block})


@pytest.mark.parametrize("block, message", [
    ({"allowed": ""}, "allowed"),
    ({"allowed": 0}, "allowed"),
    ({"allowed": False}, "allowed"),
    ({"allowed": [0]}, "name"),
    ({"allowed": [{"name": 0}]}, "name"),
    ({"allowed": {"sonnet": {"use_for": 0}}}, "use_for"),
    ({"allowed": {"sonnet": {"effort": 0}}}, "effort"),
    ({"default": 0}, "default"),
])
def test_wrong_typed_model_fields_are_rejected(block, message):
    with pytest.raises(ValueError, match=message):
        model_policy.parse_policy({"claude": block})


# --- applying them to a task --------------------------------------------------

def test_a_task_without_a_model_gets_the_default_and_its_effort():
    task = model_policy.apply_policy(_task(), model_policy.parse_policy(SETTINGS))

    assert (task.model, task.effort) == ("sonnet", "medium")


def test_an_allowed_model_brings_its_configured_effort():
    task = model_policy.apply_policy(_task(model="opus"), model_policy.parse_policy(SETTINGS))

    assert (task.model, task.effort) == ("opus", "high")


def test_the_planners_effort_wins_over_the_configured_one():
    task = model_policy.apply_policy(_task(model="opus", effort="max"),
                                     model_policy.parse_policy(SETTINGS))

    assert task.effort == "max"


def test_a_model_outside_the_allow_list_is_refused_with_the_list():
    with pytest.raises(ValueError, match=r"not allowed.*sonnet.*opus"):
        model_policy.apply_policy(_task(model="haiku"), model_policy.parse_policy(SETTINGS))


def test_an_effort_the_engine_does_not_have_is_refused():
    """codex has `ultra`; claude does not."""
    with pytest.raises(ValueError, match="effort"):
        model_policy.apply_policy(_task(effort="ultra"), model_policy.parse_policy(SETTINGS))
    assert model_policy.apply_policy(_task(engine="codex", effort="ultra"), {}).effort == "ultra"


# --- what the planner is told ---------------------------------------------------

def test_the_planner_sees_each_allowed_model_with_its_use_and_the_default():
    model_text, effort_text = model_policy.describe(model_policy.parse_policy(SETTINGS))

    for fragment in ("sonnet", "routine edits, tests", "opus", "hard bugs across modules",
                     "default", "gpt-5.6-terra"):
        assert fragment in model_text
    assert "ultra" in effort_text and "max" in effort_text


def test_the_planner_is_told_a_fitting_use_picks_the_engine_as_well():
    model_text, _ = model_policy.describe(model_policy.parse_policy(SETTINGS))

    assert "choose that engine and model" in model_text


def test_without_settings_the_planner_is_told_any_model_goes():
    model_text, _ = model_policy.describe({})

    assert "CLI's own default" in model_text


# --- saving through Hermes's own writer -----------------------------------------

def test_saved_settings_land_where_hermes_keeps_plugin_settings(_isolated_hermes_home):
    model_policy.save_settings(SETTINGS)

    config = yaml.safe_load((_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8"))
    saved = config["plugins"]["entries"]["hermes_control"]["settings"]["models"]
    claude = {entry["name"]: entry for entry in saved["claude"]["allowed"]}
    assert claude["opus"] == {"name": "opus", "use_for": "hard bugs across modules",
                              "effort": "high"}
    assert model_policy.load_policy()["claude"].default == "sonnet"


def test_removing_a_model_really_removes_it(_isolated_hermes_home):
    model_policy.save_settings(SETTINGS)
    model_policy.save_settings({"claude": {"allowed": {"sonnet": "routine"}}})

    assert set(model_policy.load_policy()["claude"].allowed) == {"sonnet"}
    assert "codex" not in model_policy.load_policy()


def test_invalid_settings_are_never_saved(_isolated_hermes_home):
    model_policy.save_settings(SETTINGS)

    with pytest.raises(ValueError):
        model_policy.save_settings({"claude": {"allowed": {"sonnet": {"effort": "ludicrous"}}}})

    assert model_policy.load_policy()["claude"].allowed["sonnet"].effort == "medium"


def test_false_settings_cannot_clear_saved_allow_lists(_isolated_hermes_home):
    model_policy.save_settings(SETTINGS)

    with pytest.raises(ValueError, match="models settings"):
        model_policy.save_settings(False)

    assert set(model_policy.load_policy()["claude"].allowed) == {"sonnet", "opus"}
    assert set(model_policy.load_policy()["codex"].allowed) == {"gpt-5.6-terra"}
