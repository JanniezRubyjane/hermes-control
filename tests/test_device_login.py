"""The dashboard's codex sign-in: a relay for `codex login --device-auth`.

Driven against a fake codex script that behaves like codex-cli 0.155.1: `login
status` exits 0 only once signed in; `login --device-auth` prints a link and a
one-time code, then waits for approval. Files next to the script steer it.
"""

import os
import threading
import time

import pytest

from hermes_control import device_login
from hermes_control.engines.base import EngineInfo
from hermes_control.engines.codex import CodexAdapter

# Every test here starts real child processes and stops them again.
pytestmark = pytest.mark.live_system_guard_bypass

FAKE_CODEX = r"""#!/bin/sh
here=$(dirname "$0")
case "$1 $2" in
  "login status")
    if [ -f "$here/signed-in" ]; then echo "Logged in using ChatGPT"; exit 0; fi
    echo "Not logged in"; exit 1 ;;
  "login --device-auth")
    echo $$ >> "$here/pids"
    if [ -f "$here/silent" ]; then echo "error: network down"; exit 1; fi
    if [ -f "$here/slow" ]; then sleep 1; fi
    printf '\033[1mWelcome to Codex\033[0m\n'
    echo "1. Open this link in your browser and sign in to your account"
    echo "   https://auth.openai.com/codex/device"
    echo "2. Enter this one-time code (expires in 15 minutes)"
    echo "   ABCD-EFGHI"
    while [ ! -f "$here/approve" ]; do
      if [ -f "$here/reject" ]; then echo "error: access denied"; exit 3; fi
      sleep 0.1
    done
    touch "$here/signed-in"; exit 0 ;;
esac
exit 2
"""


class FakeCodex(CodexAdapter):
    def __init__(self, path):
        self._path = path

    def probe(self):
        return EngineInfo(name="codex", version="codex-cli 0.0.0", path=self._path) if self._path else None


@pytest.fixture
def codex(tmp_path):
    script = tmp_path / "codex"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    script.chmod(0o755)
    yield FakeCodex(str(script))
    for session_id, session in list(device_login._sessions.items()):
        if session.state == "pending":
            device_login.cancel_login(session_id)
    device_login._sessions.clear()


def _pids(codex):
    path = os.path.join(os.path.dirname(codex._path), "pids")
    with open(path, encoding="utf-8") as handle:
        return [int(line) for line in handle.read().split()]


def _gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _touch(codex, name):
    open(os.path.join(os.path.dirname(codex._path), name), "w", encoding="utf-8").close()


def _wait_for(session_id, codex, state, seconds=5):
    deadline = time.monotonic() + seconds
    while True:
        view = device_login.login_status(session_id, codex)
        if view["state"] == state or time.monotonic() > deadline:
            return view
        time.sleep(0.05)


def test_the_link_and_code_codex_prints_are_relayed(codex):
    view = device_login.start_login(codex)

    assert view["state"] == "pending"
    assert view["url"] == "https://auth.openai.com/codex/device"
    assert view["code"] == "ABCD-EFGHI"
    assert view["expires_at"] > time.time() + device_login.LOGIN_TTL_SECONDS - 60


def test_approving_the_code_signs_codex_in(codex):
    view = device_login.start_login(codex)
    _touch(codex, "approve")

    assert _wait_for(view["session_id"], codex, "signed_in")["state"] == "signed_in"


def test_a_login_codex_itself_rejects_is_failed_with_its_words(codex):
    view = device_login.start_login(codex)
    _touch(codex, "reject")

    settled = _wait_for(view["session_id"], codex, "failed")
    assert settled["state"] == "failed"
    assert "exited 3" in settled["detail"] and "access denied" in settled["detail"]


def test_no_device_code_means_failed_and_nothing_left_running(codex):
    _touch(codex, "silent")

    view = device_login.start_login(codex)

    assert view["state"] == "failed"
    assert "network down" in view["detail"]
    assert _gone(_pids(codex)[0])


def test_an_unapproved_code_expires_and_its_process_is_stopped(codex):
    view = device_login.start_login(codex)

    later = device_login.login_status(
        view["session_id"], codex, now=lambda: time.time() + device_login.LOGIN_TTL_SECONDS + 1)

    assert later["state"] == "expired"
    assert _gone(_pids(codex)[0])


def test_cancel_stops_the_login(codex):
    view = device_login.start_login(codex)

    assert device_login.cancel_login(view["session_id"])["state"] == "cancelled"
    assert _gone(_pids(codex)[0])


def test_a_second_sign_in_replaces_the_first(codex):
    first = device_login.start_login(codex)
    second = device_login.start_login(codex)

    assert device_login.login_status(first["session_id"], codex)["state"] == "cancelled"
    assert second["state"] == "pending"
    assert _gone(_pids(codex)[0])


def test_a_signed_in_codex_is_not_signed_in_again(codex):
    _touch(codex, "signed-in")

    with pytest.raises(device_login.LoginRefused, match="already signed in"):
        device_login.start_login(codex)


def test_a_missing_codex_cannot_start_a_sign_in(codex):
    with pytest.raises(device_login.LoginRefused, match="not installed"):
        device_login.start_login(FakeCodex(None))


def test_an_unknown_session_is_a_key_error(codex):
    with pytest.raises(KeyError):
        device_login.login_status("nope", codex)


def test_two_sign_ins_started_together_leave_one_live_code(codex):
    """A second tab (or a retry) can start while the first is still waiting for its
    code; the first must already be visible so the second can replace it."""
    _touch(codex, "slow")  # the fake takes a second to print its code
    first = {}
    starter = threading.Thread(target=lambda: first.update(device_login.start_login(codex)))
    starter.start()
    time.sleep(0.3)
    second = device_login.start_login(codex)
    starter.join(10)

    states = sorted(device_login.login_status(view["session_id"], codex)["state"]
                    for view in (first, second))
    assert states == ["cancelled", "pending"]


class GatedCodex(FakeCodex):
    """FakeCodex whose sign-in check can be held open, like a hung `codex login status`."""

    def __init__(self, path):
        super().__init__(path)
        self.gated = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def auth_problem(self, info):
        if self.gated:
            self.entered.set()
            self.release.wait(10)
        return super().auth_problem(info)


def test_a_hung_sign_in_check_does_not_stall_other_requests(codex):
    """The check after a successful login shells out with a 20 s timeout; holding
    the module lock across it froze every other Engines request that long."""
    gated = GatedCodex(codex._path)
    view = device_login.start_login(gated)
    _touch(codex, "approve")
    gated.gated = True

    def poll_until_settled():
        while device_login.login_status(view["session_id"], gated)["state"] == "pending":
            time.sleep(0.05)

    poller = threading.Thread(target=poll_until_settled)
    poller.start()
    try:
        assert gated.entered.wait(5), "the poll never reached the sign-in check"
        other = threading.Thread(target=lambda: pytest.raises(KeyError, device_login.login_status,
                                                              "nope", gated))
        other.start()
        other.join(2)
        assert not other.is_alive(), "an unrelated request waited on the hung check"
    finally:
        gated.release.set()
        poller.join(10)


@pytest.mark.integration
def test_the_real_codex_prints_a_device_link_and_code(tmp_path, monkeypatch):
    import shutil

    if not shutil.which("codex"):
        pytest.skip("codex not installed")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))  # an account-less home; the real login is untouched
    adapter = CodexAdapter()

    view = device_login.start_login(adapter)
    try:
        assert view["state"] == "pending"
        assert view["url"].startswith("https://auth.openai.com/")
        assert len(view["code"]) >= 9
    finally:
        assert device_login.cancel_login(view["session_id"])["state"] == "cancelled"
        device_login._sessions.clear()
