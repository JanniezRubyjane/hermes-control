"""Test harness for Hermes-Control as a standalone plugin repo.

The plugin is imported as the top-level package ``hermes_control`` (pytest.ini
puts the repo root on sys.path), and Hermes itself comes from the interpreter
running the suite: scripts/check.sh runs pytest on top of the installed Hermes.
"""

import pytest


@pytest.fixture(autouse=True)
def _stub_claude_support_probe(monkeypatch):
    """Never run the host CLI's help; capability tests restore this probe."""
    from hermes_control.engines import claude

    probe = claude._supports_restricted
    probe.cache_clear()
    monkeypatch.setattr(claude, "_supports_restricted", lambda _: True)
    yield probe
    probe.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Never touch the operator's real ~/.hermes: run logs, config reads and
    plugin discovery all resolve through HERMES_HOME."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _no_real_usage_readings(monkeypatch):
    """No test reads a real account's usage: every engine reads as unknown, which
    never holds work back. Tests that need a reading stub the fetcher they use."""
    from hermes_control import limits

    limits._cache.clear()
    for engine in list(limits._FETCHERS):
        monkeypatch.setitem(limits._FETCHERS, engine,
                            lambda refresh=False, engine=engine: limits.Usage(
                                engine=engine, error="not read in tests"))
    yield
    limits._cache.clear()


@pytest.fixture(autouse=True)
def _clean_run_registry():
    """Runs from one test must not look finished or live to the next."""
    from hermes_control import dispatch

    dispatch._FINISHED.clear()
    dispatch._THREADS.clear()
    yield
    alive = []
    for thread in list(dispatch._THREADS.values()):
        thread.join(timeout=5)
        if thread.is_alive():
            alive.append(thread)
    dispatch._FINISHED.clear()
    dispatch._THREADS.clear()
    assert not alive, f"run threads still alive after the test: {alive}"
