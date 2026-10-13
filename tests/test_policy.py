import pytest

from hermes_control.policy import (
    FORBIDDEN_FLAG_VALUES,
    FORBIDDEN_FLAGS,
    ExecPolicy,
    assert_no_forbidden_flags,
    claude_policy_flags,
    codex_policy_flags,
)


def test_read_only_maps_to_plan_mode_for_claude():
    assert claude_policy_flags(ExecPolicy(write=False)) == ["--permission-mode", "plan"]


def test_write_maps_to_accept_edits_for_claude():
    assert claude_policy_flags(ExecPolicy(write=True)) == ["--permission-mode", "acceptEdits"]


def test_claude_tool_lists_are_passed_through():
    flags = claude_policy_flags(
        ExecPolicy(write=True, allow_tools=("Bash", "Edit"), deny_tools=("WebFetch",)))
    assert flags == ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Bash", "Edit",
                     "--disallowedTools", "WebFetch"]


def test_read_only_maps_to_read_only_sandbox_for_codex():
    assert codex_policy_flags(ExecPolicy(write=False)) == ["-s", "read-only"]


def test_write_maps_to_workspace_write_for_codex():
    assert codex_policy_flags(ExecPolicy(write=True)) == ["-s", "workspace-write"]


def test_codex_ignores_tool_lists_because_it_has_no_such_flag():
    assert codex_policy_flags(
        ExecPolicy(write=True, allow_tools=("Bash",))) == ["-s", "workspace-write"]


@pytest.mark.parametrize("argv", [
    ["claude", "-p", "x", "--allow-dangerously-skip-permissions"],
    ["claude", "-p", "x", "--permission-mode", "bypassPermissions"],
    ["codex", "exec", "x", "--dangerously-bypass-approvals-and-sandbox"],
    ["codex", "exec", "x", "--dangerously-bypass-hook-trust"],
    ["codex", "exec", "x", "-s", "danger-full-access"],
    ["codex", "exec", "x", "--approve-for-me"],
    ["codex", "exec", "x", "--approve-for-me=true"],
])
def test_forbidden_flags_are_rejected(argv):
    with pytest.raises(ValueError, match="forbidden"):
        assert_no_forbidden_flags(argv)


def test_ordinary_argv_is_accepted():
    assert_no_forbidden_flags(["codex", "exec", "x", "-s", "workspace-write"])


@pytest.mark.parametrize("policy", [
    ExecPolicy(write=False), ExecPolicy(write=True),
    ExecPolicy(write=True, allow_tools=("Bash",), deny_tools=("WebFetch",)),
])
def test_no_policy_can_produce_a_forbidden_flag(policy):
    assert_no_forbidden_flags(["claude", *claude_policy_flags(policy)])
    assert_no_forbidden_flags(["codex", *codex_policy_flags(policy)])


def test_forbidden_flags_contents_are_pinned_to_the_real_cli_help():
    """The artifact that would have caught the gap: the set was written from the
    design doc, which listed only `--allow-dangerously-skip-permissions`, while
    the installed `claude --help` shows that AND `--dangerously-skip-permissions`.
    Each name below is quoted from the real `--help` output (2026-09-23); when an
    engine renames or adds a bypass, this test is where it has to be updated.
    """
    assert FORBIDDEN_FLAGS == frozenset({
        # claude --help: "--allow-dangerously-skip-permissions  Enable bypassing
        # all permission checks as an option, without it being enabled by default."
        "--allow-dangerously-skip-permissions",
        # claude --help: "--dangerously-skip-permissions  Bypass all permission
        # checks." A separate flag; claude ships both.
        "--dangerously-skip-permissions",
        # claude --help: --permission-mode <mode> (choices: "acceptEdits", "auto",
        # "bypassPermissions", "manual", "dontAsk", "plan")
        "bypassPermissions",
        "dontAsk",
        # codex exec --help: "--dangerously-bypass-approvals-and-sandbox  Skip all
        # confirmation prompts and execute commands without sandboxing."
        "--dangerously-bypass-approvals-and-sandbox",
        # codex exec --help: "--dangerously-bypass-hook-trust  Run enabled hooks
        # without requiring persisted hook trust for this invocation."
        "--dangerously-bypass-hook-trust",
        # codex exec --help: -s, --sandbox <SANDBOX_MODE>
        # [possible values: read-only, workspace-write, danger-full-access]
        "danger-full-access",
        # codex exec --help (codex-cli 0.155.1): "--approve-for-me  Route approval
        # requests through automatic review using the workspace-write sandbox"
        "--approve-for-me",
    })


@pytest.mark.parametrize("argv", [
    # The name the guard missed entirely.
    ["claude", "-p", "x", "--dangerously-skip-permissions"],
    # A permission mode that skips the prompt gate that IS claude's containment.
    ["claude", "-p", "x", "--permission-mode", "dontAsk"],
    # `--flag=value`: exact token intersection let every one of these through.
    ["claude", "-p", "x", "--permission-mode=bypassPermissions"],
    ["claude", "-p", "x", "--permission-mode=dontAsk"],
    ["codex", "exec", "x", "--sandbox=danger-full-access"],
])
def test_newly_guarded_forms_are_rejected(argv):
    with pytest.raises(ValueError, match="forbidden"):
        assert_no_forbidden_flags(argv)


def test_a_goal_that_merely_mentions_a_bypass_flag_is_not_rejected():
    """The goal is an argv element too. Splitting free prose on `=` would make
    "make the guard reject --permission-mode=bypassPermissions" an unrunnable
    task, so the split is confined to flag-shaped tokens."""
    assert_no_forbidden_flags([
        "claude", "-p",
        "add a test that the guard rejects --permission-mode=bypassPermissions",
        "--permission-mode", "plan"])


@pytest.mark.parametrize("argv", [
    ["claude", "-p", "x", "--permission-mode", "auto"],
    ["claude", "-p", "x", "--permission-mode=auto"],
])
def test_auto_permission_mode_is_rejected(argv):
    """`auto` hands the approval decision to a classifier rather than a person
    (`claude --help` lists `auto-mode`: "Inspect or reset auto mode classifier"),
    which for an unattended worker is approval with nobody in the loop."""
    with pytest.raises(ValueError, match="forbidden"):
        assert_no_forbidden_flags(argv)


@pytest.mark.parametrize("argv", [
    # `codex exec --help`: --color [default: auto] [possible values: always, never, auto]
    ["codex", "exec", "x", "--color", "auto"],
    ["codex", "exec", "x", "--color=auto"],
    # `claude --help`: --autocompact <auto|tokens>
    ["claude", "-p", "x", "--autocompact", "auto"],
    # The goal is an argv element too.
    ["claude", "-p", "auto", "--permission-mode", "plan"],
])
def test_auto_is_forbidden_only_as_a_permission_mode(argv):
    """`auto` is an ordinary value of other flags on both CLIs, so it is guarded by
    the flag it follows. Adding it to FORBIDDEN_FLAGS as a bare token fails this."""
    assert_no_forbidden_flags(argv)


@pytest.mark.parametrize("argv", [
    ["codex", "sandbox", "-P", ":danger-full-access", "--", "/bin/sh", "-c", "true"],
    ["codex", "sandbox", "--permission-profile", ":danger-full-access", "--", "true"],
    ["codex", "sandbox", "--permission-profile=:danger-full-access", "--", "true"],
])
def test_the_uncontained_sandbox_profile_is_rejected(argv):
    """The verify step runs `codex sandbox -P <profile>`; `:danger-full-access` is a
    real built-in profile that lets the command write anywhere, so the sandbox
    would contain nothing."""
    with pytest.raises(ValueError, match="forbidden"):
        assert_no_forbidden_flags(argv)


@pytest.mark.integration
def test_every_forbidden_name_still_appears_in_the_installed_cli_help():
    """Keeps the set honest against the binaries actually installed: a name that
    has vanished from `--help` is either renamed (so the guard now protects
    nothing) or was never real."""
    import shutil
    import subprocess

    help_text = ""
    for binary, args in (("claude", ["--help"]), ("codex", ["exec", "--help"]),
                         ("codex", ["sandbox", "--help"])):
        path = shutil.which(binary)
        if not path:
            pytest.skip(f"{binary} is not installed")
        help_text += subprocess.run(
            [path, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, stdin=subprocess.DEVNULL).stdout
    missing = sorted(flag for flag in FORBIDDEN_FLAGS if flag not in help_text)
    # A flag-scoped value is checked in its quoted `choices:` form: bare "auto" also
    # occurs inside words like "autocompact" and would always be found. Permission
    # profiles (`:name`) appear in no --help; the next test asks codex about those.
    missing += sorted(f"{flag} {value}" for flag, values in FORBIDDEN_FLAG_VALUES.items()
                      for value in values
                      if flag not in help_text
                      or (not value.startswith(":") and f'"{value}"' not in help_text))
    assert not missing, f"no longer in the installed CLIs' --help: {missing}"


@pytest.mark.integration
def test_every_forbidden_sandbox_profile_is_still_a_codex_built_in(tmp_path):
    """A renamed profile would leave the guard protecting a name codex no longer
    accepts, while the new name sails through."""
    import shutil
    import subprocess

    codex = shutil.which("codex")
    if not codex:
        pytest.skip("codex is not installed")
    profiles = {value for flag in ("-P", "--permission-profile")
                for value in FORBIDDEN_FLAG_VALUES[flag]}
    for profile in sorted(profiles):
        proc = subprocess.run(
            [codex, "sandbox", "-P", profile, "-C", str(tmp_path), "--", "true"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, stdin=subprocess.DEVNULL)
        assert "unknown built-in profile" not in proc.stdout + proc.stderr, profile
