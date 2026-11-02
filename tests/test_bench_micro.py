"""Offline controller benchmark checks; workers are stubbed; no engine quota is used."""

import json
import os
from pathlib import Path
import sys
import sqlite3

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bench"))
import micro  # noqa: E402


def test_readonly_and_runlog_json(tmp_path, monkeypatch):
    original_home = os.environ.get("HERMES_HOME")
    if original_home is None:
        pytest.skip("requires an isolated HERMES_HOME from the test environment")
    run_bench = micro.run_bench
    monkeypatch.setattr(micro, "run_bench", lambda **kwargs: run_bench(root=tmp_path, **kwargs))

    output = tmp_path / "result.json"
    assert micro.main(["--reps", "2", "--only", "dispatch-readonly,runlog-append",
                       "--out", str(output)]) == 0
    data = json.loads(output.read_text())
    assert set(data) == {"sha", "dirty", "python", "scenarios"}
    assert len(data["sha"]) == 40
    assert isinstance(data["dirty"], bool)
    assert data["python"]
    assert set(data["scenarios"]) == {"dispatch-readonly", "runlog-append"}
    for measured in data["scenarios"].values():
        assert measured["sandbox"] is None
        assert measured["reps"] == 2
        assert measured["skipped"] is False
        assert 0 < measured["min_ms"] <= measured["median_ms"] <= measured["p90_ms"]
    log = data["scenarios"]["runlog-append"]
    assert log["median_us"] == pytest.approx(log["median_ms"] * 1000)
    assert os.environ["HERMES_HOME"] == original_home
    assert not list(Path(original_home).iterdir())


@pytest.mark.parametrize("name,before,after,expected", [
    ("stats", 100, 151, 1),
    ("stats", 100, 140, 0),  # relative gate only
    ("stats", 1000, 1060, 0),  # absolute gate only
    ("stats", 100, 150, 0),  # equality does not cross the absolute gate
    ("stats", 1000, 1100, 0),  # equality does not cross the relative gate
    ("stats", 0, 51, 1),
    ("runlog-append", .010, .016, 1),
    ("runlog-append", .010, .014, 0),
    ("runlog-append", .100, .106, 0),
    ("runlog-append", .010, .015, 0),
])
def test_baseline_exit_codes(tmp_path, monkeypatch, name, before, after, expected):
    baseline = tmp_path / "base.json"
    baseline.write_text(json.dumps({"scenarios": {name: {"median_ms": before, "skipped": False}}}))
    monkeypatch.setattr(micro, "run_bench", lambda **kwargs: {
        "sha": "abc", "python": "3", "scenarios": {
            name: {"median_ms": after, "skipped": False}}})
    assert micro.main(["--baseline", str(baseline)]) == expected


@pytest.mark.parametrize("skip_current", [True, False])
def test_skipped_never_fails(skip_current):
    skipped = {"median_ms": None, "skipped": "no sandbox"}
    timed = {"median_ms": 1000, "skipped": False}
    current, previous = (skipped, timed) if skip_current else (timed, skipped)
    assert micro.compare({"scenarios": {"health": current}},
                         {"scenarios": {"health": previous}}) == 0


def test_missing_sandbox_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(micro.verify, "find_sandbox", lambda: None)
    monkeypatch.setattr(micro.dispatch, "dispatch", lambda *a, **kw: pytest.fail("dispatched"))
    data = micro.run_bench(reps=2, only=["dispatch-write-verify"], root=tmp_path)
    measured = data["scenarios"]["dispatch-write-verify"]
    assert "sandbox" in measured["skipped"]
    assert measured["sandbox"] is None
    assert measured["median_ms"] is None
    assert measured["reps"] == 0


def test_write_lands_real_commit(tmp_path):
    data = micro.run_bench(reps=1, only=["dispatch-write-noverify"], root=tmp_path)
    assert data["scenarios"]["dispatch-write-noverify"]["median_ms"] > 0


def test_explicit_home_is_read_only(tmp_path, monkeypatch):
    source = tmp_path / "source"
    control = source / "control"
    control.mkdir(parents=True, mode=0o755)
    (control / "run-existing.jsonl").write_text("")
    with sqlite3.connect(source / "state.db") as connection:
        connection.execute("CREATE TABLE fixture (value TEXT)")
        connection.execute("INSERT INTO fixture VALUES ('preserve me')")

    def snapshot():
        return {(p.relative_to(source), p.stat().st_size, p.stat().st_mtime_ns)
                for p in source.rglob("*")}

    def check_copy(operation):
        def checked(**kwargs):
            copied_home = Path(os.environ["HERMES_HOME"])
            assert copied_home != source
            for relative in ("control/run-existing.jsonl", "state.db"):
                assert (copied_home / relative).read_bytes() == (source / relative).read_bytes()
            return operation(**kwargs)
        return checked

    monkeypatch.setattr(micro.watch, "stats", check_copy(micro.watch.stats))
    monkeypatch.setattr(micro.health, "report", check_copy(micro.health.report))
    before = snapshot()
    data = micro.run_bench(reps=1, only=["stats", "health"], home=source, root=tmp_path)
    assert set(data["scenarios"]) == {"stats", "health"}
    assert snapshot() == before


@pytest.mark.parametrize("contents", [None, "{", "[]", "null", '{}',
    '{"scenarios": []}', '{"scenarios": {"stats": []}}',
    '{"scenarios": {"stats": {"median_ms": "slow"}}}',
    '{"scenarios": {"stats": {"median_ms": null}}}',
    '{"scenarios": {"stats": {"median_ms": NaN}}}'])
def test_bad_baseline_is_argument_error(tmp_path, monkeypatch, capsys, contents):
    baseline = tmp_path / "baseline.json"
    if contents is not None:
        baseline.write_text(contents)
    monkeypatch.setattr(micro, "run_bench", lambda **kw: pytest.fail("ran benchmark"))
    with pytest.raises(SystemExit) as exc:
        micro.main(["--baseline", str(baseline)])
    assert exc.value.code == 2
    error = capsys.readouterr().err
    assert len([line for line in error.splitlines() if "error:" in line]) == 1
    assert "--baseline:" in error
    assert "Traceback" not in error


def test_nonexistent_home_is_argument_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(micro, "run_bench", lambda **kw: pytest.fail("ran benchmark"))
    with pytest.raises(SystemExit) as exc:
        micro.main(["--home", str(tmp_path / "missing")])
    assert exc.value.code == 2
    assert "error: --home must be an existing directory" in capsys.readouterr().err


@pytest.mark.parametrize("status,dirty", [("", False), (" M scripts/bench/micro.py", True)])
def test_checkout_dirty_metadata(tmp_path, monkeypatch, status, dirty):
    monkeypatch.setattr(micro, "_git", lambda repo, *args:
                        status if args == ("status", "--porcelain") else "a" * 40)
    data = micro.run_bench(only=[], root=tmp_path)
    assert data["sha"] == "a" * 40
    assert data["dirty"] is dirty


def test_verify_sandbox_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(micro.verify, "find_sandbox", lambda: "/fixture/codex")
    monkeypatch.setattr(micro, "_dispatch", lambda *args: None)
    data = micro.run_bench(reps=1, only=["dispatch-write-verify"], root=tmp_path)
    assert data["scenarios"]["dispatch-write-verify"]["sandbox"] == "codex"
