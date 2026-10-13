"""Whether each engine CLI is signed in, asked the way the CLI itself answers it.

`claude auth status` and `codex login status` both exit 0 when signed in and 1
when not (claude-code 2.1.280, codex-cli 0.155.1, checked 2026-09-23).
"""

import shutil

import pytest

from hermes_control.engines import base as base_mod
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.claude import ClaudeAdapter
from hermes_control.engines.codex import CodexAdapter


def _fake_cli(tmp_path, body):
    script = tmp_path / "fake-cli"
    script.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    script.chmod(0o755)
    return EngineInfo(name="fake", version="1", path=str(script))


@pytest.mark.parametrize("adapter", [ClaudeAdapter(), CodexAdapter()], ids=["claude", "codex"])
def test_a_cli_that_says_it_is_signed_in_has_no_problem(tmp_path, adapter):
    assert adapter.auth_problem(_fake_cli(tmp_path, "echo ok; exit 0")) is None


@pytest.mark.parametrize("adapter, fix", [(ClaudeAdapter(), "claude auth login"),
                                          (CodexAdapter(), "codex login")],
                         ids=["claude", "codex"])
def test_a_signed_out_cli_is_reported_with_the_command_that_fixes_it(tmp_path, adapter, fix):
    problem = adapter.auth_problem(_fake_cli(tmp_path, "echo 'Not logged in'; exit 1"))

    assert problem.startswith("not signed in")
    assert fix in problem


@pytest.mark.live_system_guard_bypass  # the timeout genuinely kills its own child
def test_a_sign_in_check_that_never_answers_is_a_problem_not_a_hang(tmp_path, monkeypatch):
    """An older CLI without the status subcommand may wait for input instead."""
    monkeypatch.setattr(base_mod, "_SIGN_IN_CHECK_TIMEOUT", 1)

    problem = CodexAdapter().auth_problem(_fake_cli(tmp_path, "exec sleep 30"))

    assert problem.startswith("sign-in check failed")


@pytest.mark.integration
def test_the_real_codex_cli_reports_a_missing_login(tmp_path, monkeypatch):
    if not shutil.which("codex"):
        pytest.skip("codex not installed")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # an account-less codex home
    adapter = CodexAdapter()

    assert adapter.auth_problem(adapter.probe()).startswith("not signed in")


@pytest.mark.integration
def test_the_real_claude_cli_reports_a_missing_login(tmp_path, monkeypatch):
    if not shutil.which("claude"):
        pytest.skip("claude not installed")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))  # an account-less config dir
    adapter = ClaudeAdapter()

    assert adapter.auth_problem(adapter.probe()).startswith("not signed in")


def test_claudes_json_status_is_summarised_not_dumped(tmp_path):
    """`claude auth status` answers in pretty-printed JSON; pasted whole it buried
    the reason under paths and was cut off mid-field (first real run)."""
    signed_out = ('{\n  "loggedIn": false,\n  "authMethod": "none",\n'
                  '  "apiProvider": "firstParty",\n'
                  '  "configDirectory": "/somewhere/long/enough/to/matter"\n}')
    info = _fake_cli(tmp_path, f"cat <<'JSON'\n{signed_out}\nJSON\nexit 1")

    problem = ClaudeAdapter().auth_problem(info)

    assert "{" not in problem and "configDirectory" not in problem
    assert "loggedIn: false" in problem
    assert "claude auth login" in problem
