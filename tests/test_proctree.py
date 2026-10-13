import os
import signal
import subprocess
import sys
import time

import pytest

from hermes_control.proctree import stop_group


@pytest.mark.live_system_guard_bypass  # signals and reaps a real process group
def test_stop_group_reaps_a_term_exiting_leader_without_waiting_out_grace():
    if os.name == "nt":
        pytest.skip("POSIX process groups are required")
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               start_new_session=True)
    try:
        started = time.monotonic()
        stop_group(process, grace=5)
        elapsed = time.monotonic() - started
        assert elapsed < 2, f"stop_group took {elapsed:.2f}s"
        assert process.returncode is not None
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)


@pytest.mark.live_system_guard_bypass  # signals a real child in its own session
def test_stop_group_escalates_and_accepts_an_already_gone_group(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX process groups are required")
    ready = tmp_path / "ready"
    script = (
        "import signal, time\n"
        "from pathlib import Path\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "time.sleep(30)\n"
    )
    process = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        deadline = time.monotonic() + 3
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child never installed its SIGTERM handler"

        stop_group(process, grace=0.2)
        assert process.wait(timeout=2) == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)

        stop_group(process, grace=0.2)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
