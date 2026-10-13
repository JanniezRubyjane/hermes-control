from pathlib import Path

import yaml

SKILL = Path(__file__).resolve().parents[1] / "skills" / "hermes-control" / "SKILL.md"


def _frontmatter():
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_skill_file_exists():
    assert SKILL.is_file()


def test_skill_reload_stays_small_and_references_exist():
    body = SKILL.read_text(encoding="utf-8")
    assert len(body) <= 6000
    assert _frontmatter()["version"] == "0.1.0"
    for name in ("verify", "review", "limits", "dependencies"):
        path = f"references/{name}.md"
        assert f"file_path='{path}'" in body
        assert (SKILL.parent / path).is_file()


def test_frontmatter_has_the_required_fields():
    data = _frontmatter()
    for field in ("name", "description", "version", "license", "platforms"):
        assert field in data
    assert data["name"] == "hermes-control"


def test_skill_declares_both_cli_prerequisites():
    assert set(_frontmatter()["prerequisites"]["commands"]) == {"claude", "codex"}


def test_skill_documents_the_engine_security_difference():
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "sandbox" in body and "prompt-gated" in body


def test_skill_documents_the_pin_override_and_the_why_requirement():
    body = SKILL.read_text(encoding="utf-8")
    assert "@claude" in body and "@codex" in body
    assert "why" in body.lower()


def test_skill_has_a_real_author_not_a_placeholder():
    # The skill ships with the plugin: same author as plugin.yaml, never a placeholder.
    manifest = yaml.safe_load((SKILL.parents[2] / "hermes_control" / "plugin.yaml").read_text(encoding="utf-8"))
    assert _frontmatter()["author"] == manifest["author"] == "JanniezRubyjane"


def test_skill_says_depends_on_is_ordering_only():
    """The skill used to say "use `depends_on` when one task needs another's
    result", which a planner cannot act on: every task branches from HEAD, so B
    would return a plausible diff built on the wrong base."""
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "orders tasks" in body
    assert "passes nothing between them" in body
    assert "never sees an earlier" in body or "never sees another" in body


def test_skill_distinguishes_a_failed_diff_capture_from_an_empty_one():
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "diff capture failed" in body


def test_skill_explains_what_files_hint_actually_does():
    body = SKILL.read_text(encoding="utf-8").lower()
    assert "appended to the goal" in body


def test_skill_ties_proven_to_a_passing_verify_command():
    """The dispatcher only clears unproven_reason when the planner's `verify`
    passed; a skill that never asks for one makes every task unprovable."""
    body = SKILL.read_text(encoding="utf-8")
    assert "`verify`" in body
    assert "no verify command was given" in body
    assert "absolute path" in body.lower()  # the worktree has no virtualenv


def test_verify_reference_and_tool_field_name_clean_checkout_of_commit():
    from hermes_control import CONTROL_TOOL_SCHEMA

    reference = (SKILL.parent / "references/verify.md").read_text(encoding="utf-8").lower()
    description = CONTROL_TOOL_SCHEMA["parameters"]["properties"]["tasks"]["items"][
        "properties"]["verify"]["description"].lower()
    for text in (reference, description):
        assert "clean checkout of the task's commit" in " ".join(text.split())


def test_skill_says_task_branches_carry_a_commit_to_merge():
    body = SKILL.read_text(encoding="utf-8")
    assert "`commit`" in body
    assert "git merge" in body


def test_skill_tells_the_planner_to_report_a_rerouted_task():
    """A task pinned to a signed-out engine runs elsewhere; the user must hear
    that from the planner, together with how to sign the engine back in."""
    body = SKILL.read_text(encoding="utf-8")
    assert "`rerouted`" in body
    assert "signed in" in body


def test_skill_tells_the_planner_how_to_pick_a_model_and_effort():
    """The allow-list is only useful if the planner reads the uses and picks by them."""
    body = " ".join(SKILL.read_text(encoding="utf-8").split())  # prose wraps anywhere
    assert "`effort`" in body and "`model`" in body
    assert "names the allowed ones" in body


def _prose():
    """The skill's text, lowercased with line wrapping undone, for phrase checks."""
    return " ".join(SKILL.read_text(encoding="utf-8").lower().split())


def test_the_users_model_settings_choose_the_engine_too():
    """Seen in the evaluation: a check listed for codex's light model went to claude,
    because the engine notes decided the engine before any listed use was read."""
    body = _prose()
    assert "the model settings come first" in body
    assert "choose that engine and that model" in body


def test_an_explicit_request_to_use_hermes_control_is_honored():
    """Seen in the evaluation: "use Hermes-Control to summarize" was answered by the
    planner itself, because the skill said summaries are not worth a dispatch."""
    body = _prose()
    assert "when the user asks you to use hermes-control" in body
    # The resolution, not only the trigger: dispatch even work that only reads.
    assert "dispatch — even when the task only reads" in body


def test_a_pin_still_decides_the_engine_over_a_listed_use():
    """Review: "settings come first" read as the top rule could override @codex; the
    code does not enforce pins, so the skill is the only guard."""
    body = _prose()
    assert "a pin still decides the engine" in body


def test_the_split_recipe_defers_to_the_listed_uses():
    """Review: the split recipe named a read-only claude task outright, so a planner
    following it skipped a model listed for exactly that reading."""
    body = _prose()
    assert "on the engine and model the settings list for that reading" in body


def test_the_skill_promises_no_separate_quota():
    """Review: a claude worker may run on the very account the planner runs on, so
    "that model's quota, not yours" is false in a common setup."""
    body = _prose()
    assert "not yours" not in body


def test_skill_tells_the_planner_to_check_back_on_a_running_dispatch():
    """A run still going when control_dispatch's wait budget runs out comes back
    early with state 'running'; the planner must check back with control_status
    instead of losing the run or dispatching it again beside itself."""
    body = _prose()
    assert "control_status" in body
    assert "never dispatch the same tasks again" in body


def test_skill_routes_codex_writes_to_claude_review():
    body = _prose()
    assert "codex writes, claude reviews" in body
    assert "never the model that wrote the diff" in body
    assert "at most two review rounds" in body


def test_skill_and_schema_guard_safe_task_inputs():
    from hermes_control import CONTROL_TOOL_SCHEMA

    body = _prose()
    pattern = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    assert pattern.lower() in body
    assert "goal must start with a word, not '-'" in body
    task = CONTROL_TOOL_SCHEMA["parameters"]["properties"]["tasks"]["items"]
    assert pattern in task["properties"]["id"]["description"]


def test_skill_says_failed_dependencies_are_skipped():
    body = _prose()
    assert "dependency failed" in body
    assert "verify never ran" in body
    assert "skipped and reported failed" in body


def test_skill_relays_claude_worktree_warning():
    body = _prose()
    assert "refuses to continue" in body and ".claude/" in body
    assert "warning in `unproven_reason`" in body
    assert "relay" in body


def test_skill_never_redispatches_a_paused_task():
    assert "never dispatch a paused task again" in _prose()


def test_skill_reports_each_tasks_cost_after_review():
    body = _prose()
    assert "after a review" in body
    assert "each task's cost (calls, seconds)" in body


def test_running_section_describes_wait_on_control_status():
    section = _prose().split("## a long run comes back as running", 1)[1]
    section = section.split("## hand the branches back", 1)[0]
    assert "control_status" in section
    assert "`wait`" in section
    assert "300" in section
    assert "runs out" in section, "a wait can end with the run still going"


def test_the_wait_the_skill_describes_exists_on_control_status():
    """Review: the phrase checks above passed while no `wait` existed on the tool.
    Tie the skill's claim to the real schema and the real clamp."""
    from hermes_control import CONTROL_STATUS_TOOL_SCHEMA
    from hermes_control.dispatch import MAX_STATUS_WAIT_SECONDS

    wait = CONTROL_STATUS_TOOL_SCHEMA["parameters"]["properties"]["wait"]
    assert wait["type"] == "integer"
    assert str(MAX_STATUS_WAIT_SECONDS) in wait["description"]
    assert f"(at most {MAX_STATUS_WAIT_SECONDS})" in _prose()


def test_skill_says_a_claude_worker_can_run_its_check_through_a_pre_approved_script():
    """Open item 1: under `claude -p --permission-mode acceptEdits` every Bash call
    was refused, so a claude worker never saw its own check fail and could not
    correct itself. The controller now pre-approves a sandboxed script it can run
    before finishing, but the proof a task is done is still the controller's own
    run after the commit."""
    body = _prose()
    assert "pre-approved" in body
    assert "still the controller's" in body


def test_skill_tells_the_planner_to_report_in_the_users_language():
    """Workers answer in English, and a planner reporting their results slid into
    English with a user writing in another language. The report must stay in
    the language the user writes in; code, paths and commands stay as they are."""
    body = _prose()
    assert "language the user writes in" in body
    assert "even when the workers' results are in english" in body
