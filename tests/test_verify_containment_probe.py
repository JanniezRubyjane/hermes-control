"""A working sandbox must also refuse a write outside the checkout."""

import os

import pytest

from hermes_control import verify
from hermes_control.verify import _sandbox_temp_dirs


REASON = "not verified: the sandbox did not block a write outside the checkout"


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    home = tmp_path / "child home"
    home.mkdir()
    # The subprocess home can differ from the controller's inherited HOME.
    environment = verify._verify_env()
    environment["HOME"] = str(home)
    monkeypatch.setattr(verify, "_verify_env", lambda: environment.copy())
    # These fake sandboxes model a HOME outside the writable temp directories.
    monkeypatch.setattr(verify, "_sandbox_temp_dirs", lambda: (tmp_path / "other temp",))

    def make(probe_action):
        codex = tmp_path / "fake codex"
        codex.write_text(
            '#!/bin/sh\n'
            'while [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done\n'
            'shift\n'
            'printf "%s\\n" "$3" >> calls.txt\n'
            'case "$3" in\n'
            f'  *".hermes-control-sandbox-probe-"*) {probe_action} ;;\n'
            'esac\n'
            'exec "$@"\n', encoding="utf-8")
        codex.chmod(0o700)
        return str(codex)

    return checkout, home, make


@pytest.mark.parametrize("action", ['exec "$@"', '"$@"; exit 1'])
def test_a_sandbox_that_creates_the_probe_is_not_trusted_and_cleans_up(sandbox, action):
    checkout, home, make = sandbox

    outcome = verify.run_verify("touch check-ran", checkout, codex_path=make(action))

    assert outcome.passed is False
    assert outcome.exit_code is None
    assert outcome.unproven_reason == REASON
    assert not (checkout / "check-ran").exists()
    assert not list(home.glob(".hermes-control-sandbox-probe-*"))
    calls = (checkout / "calls.txt").read_text().splitlines()
    assert len(calls) == 2
    assert calls[0] == verify._PROBE_COMMAND
    assert calls[1].startswith(': > "$HOME/.hermes-control-sandbox-probe-')


@pytest.mark.parametrize("exit_code", [0, 3])
def test_a_sandbox_that_refuses_the_write_runs_the_check_as_before(sandbox, exit_code):
    checkout, home, make = sandbox
    command = f'printf "%s\\n" "$HOME"; touch check-ran; exit {exit_code}'

    outcome = verify.run_verify(command, checkout, codex_path=make("exit 1"))

    assert outcome.passed is (exit_code == 0)
    assert outcome.exit_code == exit_code
    assert outcome.output_tail == f"{home}\n"
    assert outcome.unproven_reason == (
        None if exit_code == 0 else "verification failed (exit 3)")
    assert (checkout / "check-ran").exists()
    assert not list(home.glob(".hermes-control-sandbox-probe-*"))
    assert (checkout / "calls.txt").read_text().splitlines()[-1] == command


def test_a_zero_exit_without_a_file_is_not_proof_of_containment(sandbox):
    checkout, _, make = sandbox

    outcome = verify.run_verify("touch check-ran", checkout, codex_path=make("exit 0"))

    assert outcome.unproven_reason == REASON
    assert not (checkout / "check-ran").exists()


def test_a_home_inside_the_checkout_cannot_prove_containment(sandbox, monkeypatch):
    checkout, _, make = sandbox
    environment = {**os.environ, "HOME": str(checkout)}
    monkeypatch.setattr(verify, "_verify_env", lambda: environment.copy())

    outcome = verify.run_verify("touch check-ran", checkout, codex_path=make("exit 1"))

    assert outcome.unproven_reason == REASON
    assert not (checkout / "check-ran").exists()


def test_probe_names_do_not_overwrite_an_existing_user_file(sandbox):
    checkout, home, make = sandbox
    existing = home / ".hermes-control-sandbox-probe"
    existing.write_text("keep me")

    outcome = verify.run_verify("true", checkout, codex_path=make('exec "$@"'))

    assert outcome.unproven_reason == REASON
    assert existing.read_text() == "keep me"
    assert list(home.glob(".hermes-control-sandbox-probe*")) == [existing]


def test_a_missing_home_cannot_prove_containment(sandbox):
    checkout, home, make = sandbox
    home.rmdir()

    outcome = verify.run_verify("touch check-ran", checkout, codex_path=make('exec "$@"'))

    assert outcome.passed is False
    assert outcome.exit_code is None
    assert outcome.unproven_reason == (
        "not verified: cannot test sandbox containment because HOME is not writable")
    assert not (checkout / "check-ran").exists()
    assert (checkout / "calls.txt").read_text().splitlines() == [verify._PROBE_COMMAND]


def test_a_read_only_home_cannot_prove_containment(sandbox):
    checkout, home, make = sandbox
    home.chmod(0o500)
    try:
        outcome = verify.run_verify("touch check-ran", checkout, codex_path=make('exec "$@"'))

        assert outcome.passed is False
        assert outcome.exit_code is None
        assert outcome.unproven_reason == (
            "not verified: cannot test sandbox containment because HOME is not writable")
        assert not (checkout / "check-ran").exists()
        assert (checkout / "calls.txt").read_text().splitlines() == [verify._PROBE_COMMAND]
        assert not list(home.glob(".hermes-control-sandbox-probe-*"))
    finally:
        home.chmod(0o700)


def test_a_home_under_tmp_cannot_prove_containment(sandbox, monkeypatch):
    checkout, home, make = sandbox
    # Restore the real temp-directory check for this case.
    monkeypatch.setattr(verify, "_sandbox_temp_dirs", _sandbox_temp_dirs)

    outcome = verify.run_verify("touch check-ran", checkout, codex_path=make('exec "$@"'))

    assert outcome.passed is False
    assert outcome.exit_code is None
    assert outcome.unproven_reason == (
        "not verified: cannot test sandbox containment because "
        "HOME is inside the temp dir the sandbox may write")
    assert not (checkout / "check-ran").exists()
    assert (checkout / "calls.txt").read_text().splitlines() == [verify._PROBE_COMMAND]
    assert not list(home.glob(".hermes-control-sandbox-probe-*"))


@pytest.mark.live_system_guard_bypass  # run_verify terminates its own timed-out probe
def test_a_timed_out_probe_leaves_no_file(sandbox, monkeypatch):
    checkout, home, make = sandbox
    monkeypatch.setattr(verify, "_PROBE_TIMEOUT_SECONDS", 1)

    outcome = verify.run_verify("touch check-ran", checkout,
                                codex_path=make('"$@"; exec sleep 60'))

    assert outcome.passed is False
    assert outcome.exit_code is None
    assert outcome.unproven_reason == REASON
    assert not (checkout / "check-ran").exists()
    assert not list(home.glob(".hermes-control-sandbox-probe-*"))
    assert len((checkout / "calls.txt").read_text().splitlines()) == 2
