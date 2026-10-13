"""Machine-wide engine slots, exercised with independent thread-owned handles."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.parametrize("limit", [1, 2])
def test_holders_fill_limit_and_waiter_logs_once(limit, monkeypatch, _isolated_hermes_home):
    fcntl = pytest.importorskip("fcntl")
    from hermes_control.slots import engine_slot

    contended = threading.Event()
    original_flock = fcntl.flock

    def flock(fd, operation):
        try:
            return original_flock(fd, operation)
        except BlockingIOError:
            contended.set()
            raise

    monkeypatch.setattr(fcntl, "flock", flock)
    release = threading.Event()
    started = [threading.Event() for _ in range(limit + 1)]
    waits = []

    def hold(index):
        with engine_slot("codex", limit, on_wait=waits.append, poll=0.01):
            started[index].set()
            assert release.wait(5)

    with ThreadPoolExecutor(limit + 1) as executor:
        futures = []
        try:
            for index in range(limit):
                futures.append(executor.submit(hold, index))
                assert started[index].wait(2)
            directory = _isolated_hermes_home / "control" / "slots" / "codex"
            assert {p.name for p in directory.iterdir()} == {
                f"slot-{n}.lock" for n in range(limit)}
            contended.clear()
            futures.append(executor.submit(hold, limit))
            assert contended.wait(2)
            assert not started[limit].wait(0.6)
            assert not started[limit].wait(0.6)
            assert waits == [{"engine": "codex", "state": "waiting"}]
        finally:
            release.set()
        for future in futures:
            future.result(timeout=2)
    assert started[limit].is_set()
    assert len(waits) == 2
    assert waits[1]["engine"] == "codex" and waits[1]["state"] == "acquired"
    seconds = waits[1]["seconds"]
    assert isinstance(seconds, float) and seconds >= 1
    assert seconds == round(seconds, 1)


@pytest.mark.parametrize("callback_raises", [False, True])
def test_exception_releases_slot(monkeypatch, callback_raises):
    pytest.importorskip("fcntl")
    from hermes_control import slots

    def fail(_seconds):
        raise RuntimeError("callback failed")

    with monkeypatch.context() as patch:
        if callback_raises:
            ticks = iter([0.0, 2.0])
            patch.setattr(slots, "monotonic", lambda: next(ticks))
        message = "holder failed"
        with pytest.raises(RuntimeError, match=message):
            with slots.engine_slot("codex", 1, on_wait=fail if callback_raises else None):
                raise RuntimeError("holder failed")

    # A fresh thread must acquire the same slot after either failure.
    with ThreadPoolExecutor(1) as executor:
        def acquire():
            with slots.engine_slot("codex", 1):
                return True
        assert executor.submit(acquire).result(timeout=2)


def test_no_fcntl_yields_without_limiting(monkeypatch, _isolated_hermes_home):
    from hermes_control import slots

    monkeypatch.setattr(slots, "fcntl", None)
    waits = []
    with slots.engine_slot("codex", 1, on_wait=waits.append):
        with slots.engine_slot("codex", 1, on_wait=waits.append):
            pass
    assert waits == []
    assert not (_isolated_hermes_home / "control" / "slots").exists()


def test_callback_failures_cannot_prevent_waiting_or_acquiring(monkeypatch, caplog):
    from hermes_control import slots

    pytest.importorskip("fcntl")
    original = slots.fcntl.flock
    attempts = []

    def flock(handle, operation):
        if operation & slots.fcntl.LOCK_NB:
            attempts.append(True)
            if len(attempts) == 1:
                raise BlockingIOError()
        return original(handle, operation)

    monkeypatch.setattr(slots.fcntl, "flock", flock)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(slots, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(slots, "sleep", lambda seconds: None)
    states = []

    def fail(payload):
        states.append(payload["state"])
        raise OSError("log unavailable")

    with caplog.at_level("DEBUG", logger="hermes_control.slots"):
        with slots.engine_slot("codex", 1, on_wait=fail):
            assert states == ["waiting", "acquired"]
    assert "log unavailable" in caplog.text
