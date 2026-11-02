"""Unavailable checks must not turn Gemini write tasks into command retries."""

import dataclasses
from types import SimpleNamespace

from hermes_control import dispatch
from test_gemini_r3 import task


def test_allowed_gemini_checks_explain_unavailable_check_recovery():
    adapter = SimpleNamespace(headless_commands_allowed=lambda: True)
    goal = dispatch.with_check_guidance(task(verify="check"), adapter=adapter).goal

    for sentence in (
        "How to work efficiently here: you may run the check command above yourself "
        "inside your sandbox at any time.",
        "While you work, run only the tests that cover the files you change, and run "
        "the full check command once when you think you are done; if it fails, fix "
        "and run it again.",
        "You must not run git: Hermes-Control commits and diffs for you.",
        "Read files in the line ranges you need rather than printing whole files, "
        "and read a whole file when you need its full context.",
        "Never skip, weaken or delete a test to make the check pass.",
    ):
        assert sentence in goal
    for phrase in (
        "not set up", "cannot find its interpreter or test runner", "is refused",
        "do not run it or a variant of it again", "agy can deny a repeated command",
        "a denied action ends the task", "finish the change",
        "say in your report that the check could not run",
        "Hermes-Control runs the check after you finish.",
    ):
        assert phrase in goal
    assert goal.index("do not run it") > goal.index("if it fails, fix and run it again.")


def test_disallowed_gemini_check_guidance_is_unchanged():
    adapter = SimpleNamespace(headless_commands_allowed=lambda: False)
    assert dispatch.with_check_guidance(task(verify="check"), adapter=adapter).goal == (
        "fix it\n\nHow to work efficiently here: do not run any shell command, including tests "
        "and git: agy's settings deny commands in headless mode and a denied action "
        "ends the task. Hermes-Control runs the check after you finish. "
        "Never skip, weaken or delete a test to make the check pass.")


def test_codex_check_guidance_is_unchanged():
    chosen = dataclasses.replace(task(verify="check"), engine="codex")
    adapter = SimpleNamespace(headless_commands_allowed=lambda: True)
    assert dispatch.with_check_guidance(chosen, adapter=adapter).goal == (
        "fix it\n\nHow to work efficiently here: you may run the check command above "
        "yourself inside your sandbox at any time. While you work, run only the tests "
        "that cover the files you change (for example the matching test file), and run "
        "the full check command once when you think you are done; if it fails, fix and "
        "run it again. Read files in the line ranges you need rather than printing "
        "whole files, and read a whole file when you need its full context. Never skip, "
        "weaken or delete a test to make the check pass.")
