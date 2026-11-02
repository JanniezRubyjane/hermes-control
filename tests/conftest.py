"""Test harness for Hermes-Control as a standalone plugin repo.

The plugin is imported as the top-level package ``hermes_control`` (pytest.ini
puts the repo root on sys.path), and Hermes itself comes from the interpreter
running the suite: scripts/check.sh runs pytest on top of the installed Hermes.
"""

import atexit
import os
import shutil
import tempfile

import pytest

# Module-level settings (engines.registry.ENGINE_NAMES) are read when hermes_control is
# first imported, before any fixture runs; point HERMES_HOME at an empty home first so
# the operator's real config (e.g. gemini enabled) never shapes the suite.
IMPORT_HERMES_HOME = tempfile.mkdtemp(prefix="hc-test-home-")
os.environ["HERMES_HOME"] = IMPORT_HERMES_HOME
atexit.register(shutil.rmtree, IMPORT_HERMES_HOME, ignore_errors=True)


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
def _no_old_engine_group(monkeypatch):
    """Scripted engines exit before a test resumes their task, and a sandbox may not
    let ``ps`` or signal 0 answer for them: the resume gate sees no earlier group.
    tests/test_resume_engine_gate.py exercises the real check."""
    from hermes_control import proctree

    monkeypatch.setattr(proctree, "group_still_running", lambda pid, started: False)


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


@pytest.fixture
def diagram_repo(tmp_path):
    """Real commit and evidence, without reading the operator's repository."""
    import subprocess

    repo = tmp_path / "diagram-repo"
    repo.mkdir()
    (repo / "app.py").write_text("def start():\n    return 1\n\nstart()\n", encoding="utf-8")
    for args in (["init", "-q"], ["add", "app.py"],
                 ["-c", "user.name=Test", "-c", "user.email=test@example.org",
                  "commit", "-qm", "initial"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


@pytest.fixture
def diagram_spec():
    evidence = [{"path": "app.py", "lines": [1, 2]}]
    return {"version": 1, "title": "App", "summary": "Application design", "unknowns": [],
            "views": [{"kind": "module", "title": "Modules", "groups": [],
                       "nodes": [{"id": f"n{i}", "label": f"Node {i}", "type": "function",
                                  "evidence": evidence} for i in range(4)],
                       "edges": [{"id": "e1", "from": "n0", "to": "n1", "label": "calls",
                                  "type": "calls", "evidence": evidence}]}]}
