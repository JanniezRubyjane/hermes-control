"""Private review persistence, validation, concurrency, and copy-note contracts."""

import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hermes_control import review_notes


@pytest.fixture
def store(tmp_path, monkeypatch):
    task = {"id": "task", "branch": "task/branch", "commit": {"sha": "0123456789abcdef"}}
    diff = {"files": [{"path": "app.py"}, {"path": "z.py"}], "error": None}
    monkeypatch.setattr(review_notes.runs, "run_view", lambda _: {"tasks": [task | {"id": "task"},
                           task | {"id": "b-c"}, task | {"id": "c"}]})
    monkeypatch.setattr(review_notes.review_diff, "build", lambda *_: diff)
    root = tmp_path / "control"
    root.mkdir()
    monkeypatch.setattr(review_notes, "run_log_dir", lambda: root)
    return root, task, diff


def comment(**updates):
    return {"path": "app.py", "side": "new", "line": 3, "text": "Please fix this"} | updates


def save(comments=None, version=0, run="run", task="task"):
    return review_notes.put(run, task, {"version": version,
                                       "comments": [comment()] if comments is None else comments})


def test_empty_read_does_not_create_review_directory(store):
    root, _, _ = store
    assert review_notes.get("run", "task") == {
        "version": 0, "commit": "0123456789abcdef", "comments": [], "note": ""}
    assert not (root / "reviews").exists()


def test_paths_collision_modes_and_schema(store):
    root, _, _ = store
    first = save(run="a-b", task="c")
    second = save([comment(text="different")], run="a", task="b-c")
    paths = [root / "reviews/a-b/c.json", root / "reviews/a/b-c.json"]
    assert paths[0] != paths[1]
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        record = json.loads(path.read_text())
        assert set(record) == {"version", "run_id", "task_id", "commit", "updated_at", "comments"}
        assert isinstance(record["updated_at"], float)
        assert set(record["comments"][0]) == {"id", "path", "side", "line", "text", "created_at"}
        assert re.fullmatch("[0-9a-f]{12}", record["comments"][0]["id"])
        assert isinstance(record["comments"][0]["created_at"], float)
        assert not list(path.parent.glob("*.tmp"))
    assert stat.S_IMODE((root / "reviews").stat().st_mode) == 0o700
    assert review_notes.get("a-b", "c") == first
    assert review_notes.get("a", "b-c") == second
    assert first["comments"][0]["text"] != second["comments"][0]["text"]


def test_version_replace_edit_delete_and_conflict(store):
    first = save()
    existing = first["comments"][0]
    edited = save([comment(id=existing["id"], text="  Changed  ")], version=1)
    assert edited["version"] == 2
    assert edited["comments"][0] == existing | {"text": "Changed"}
    with pytest.raises(review_notes.VersionConflict):
        save([comment(text="stale tab")], version=1)
    assert review_notes.get("run", "task") == edited
    assert save([], version=2) == {"version": 3, "commit": "0123456789abcdef", "comments": [], "note": ""}


def test_concurrent_writers_cannot_both_save_the_same_version(store):
    def write(_):
        try:
            return save()["version"]
        except review_notes.VersionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(write, range(2)), key=str) == [1, "conflict"]
    assert review_notes.get("run", "task")["version"] == 1


@pytest.mark.parametrize("field,value", [
    ("path", "fake.py"), ("path", "../app.py"), ("path", None), ("path", []),
    ("side", "ctx"), ("side", None), ("side", []),
    ("line", 0), ("line", -1), ("line", 10_000_001), ("line", True),
    ("line", 1.0), ("line", "1"), ("line", None),
    ("text", ""), ("text", " \n\t "), ("text", "x" * 2001), ("text", None),
    ("id", "bad"), ("id", "A" * 12), ("id", None), ("id", []),
    ("repo", "/tmp/demo"), ("branch", "fake"), ("file", "app.py"),
])
def test_comment_validation_is_generic(store, field, value):
    with pytest.raises(ValueError, match="^invalid review$"):
        save([comment(**{field: value})])
    assert review_notes.get("run", "task")["version"] == 0


@pytest.mark.parametrize("body", [
    None, [], {}, {"version": 0}, {"comments": []},
    {"version": True, "comments": []}, {"version": -1, "comments": []},
    {"version": 0.0, "comments": []}, {"version": "0", "comments": []},
    {"version": 0, "comments": None}, {"version": 0, "comments": {}},
    {"version": 0, "comments": [comment()] * 201},
    {"version": 0, "comments": [None]}, {"version": 0, "comments": [{}]},
    {"version": 0, "comments": [comment(id="a" * 12)] * 2},
    {"version": 0, "comments": [], "commit": "request-sha"},
    {"version": 0, "comments": [], "repo": "/tmp/demo"},
    {"version": 0, "comments": [], "branch": "fake"},
    {"version": 0, "comments": [], "path": "/tmp/demo"},
    {"version": 0, "comments": [comment(text="x" * 2000)] * 130},
    {"version": 0, "comments": [comment(text="雪" * 2000)] * 50},
])
def test_body_validation(store, body):
    with pytest.raises(ValueError, match="^invalid review$"):
        review_notes.put("run", "task", body)


@pytest.mark.parametrize("run,task", [
    ("../bad", "task"), ("a.b", "task"), ("", "task"), ("r" * 65, "task"),
    ("run", "../bad"), ("run", ".task"), ("run", "_task"), ("run", "-task"),
    ("run", ""), ("run", "t" * 65), ("run\n", "task"), ("run", "task\n"),
])
def test_ids_checked_before_any_path_or_lookup(store, monkeypatch, run, task):
    def forbidden(*_):
        pytest.fail("invalid id reached a path or run lookup")
    monkeypatch.setattr(review_notes, "run_log_dir", forbidden)
    monkeypatch.setattr(review_notes.runs, "run_view", forbidden)
    for operation in [lambda: review_notes.get(run, task), lambda: save(run=run, task=task)]:
        with pytest.raises(ValueError, match="^invalid review$"):
            operation()


def test_maximum_ids_and_task_punctuation(store, monkeypatch):
    task_id = "a" + "._-" * 21
    monkeypatch.setattr(review_notes.runs, "run_view", lambda _: {"tasks": [{"id": task_id}]})
    assert save([], run="r" * 64, task=task_id)["version"] == 1


def test_boundaries_and_explicit_new_id(store):
    result = save([comment(line=1, text=" a ", id="abcdef012345"),
                   comment(line=10_000_000, side="old", text="x" * 2000)])
    assert result["comments"][0]["id"] == "abcdef012345"
    assert result["comments"][0]["text"] == "a"
    result = save([comment(line=i + 1) for i in range(200)], version=1)
    assert len(result["comments"]) == 200


def test_paths_from_diff_or_stored_only_after_prune(store):
    _, _, diff = store
    result = save()
    diff.update(files=[], error="branch gone")
    assert review_notes.get("run", "task") == result
    assert save([comment(text="still editable")], version=1)["version"] == 2
    with pytest.raises(ValueError, match="^invalid review$"):
        save([comment(path="z.py")], version=2)
    assert save([], version=2)["version"] == 3
    with pytest.raises(ValueError, match="^invalid review$"):
        save(version=3)


def test_exact_note_sort_newlines_and_recorded_commit(store):
    root, task, diff = store
    diff["commit"] = "not-the-recorded-sha"
    result = save([comment(path="z.py", text="Last\ncomment\r\ncontinued"),
                   comment(line=2, side="old", text="Old"),
                   comment(line=2, side="new", text="New"),
                   comment(line=1, text=" First ")])
    assert result["note"] == (
        "Review comments on hermes-control task task (run run)\n"
        "branch task/branch at commit 0123456789ab (not merged into main):\n"
        "- app.py:1 (new) — First\n"
        "- app.py:2 (new) — New\n"
        "- app.py:2 (old) — Old\n"
        "- z.py:3 (new) — Last comment continued")
    assert json.loads((root / "reviews/run/task.json").read_text())["commit"] == task["commit"]["sha"]
    task.update(branch=None, commit=None)
    assert "branch unknown at commit unknown (not merged into main):" in save(version=1)["note"]


def test_whole_note_is_redacted(store, monkeypatch):
    from agent.redact import redact_sensitive_text
    token = "sk-" + "aB3dE6gH9jK2mN5pQ8sT1vW4yZ7" * 2
    assert redact_sensitive_text(token, force=True) != token
    result = save([comment(text="Fix token " + token)])
    assert token not in result["note"]
    import agent.redact
    calls = []
    def redact(text, *, force):
        calls.append((text, force))
        return "redacted whole note"
    monkeypatch.setattr(agent.redact, "redact_sensitive_text", redact)
    assert review_notes.get("run", "task")["note"] == "redacted whole note"
    assert calls[0][1] is True
    assert calls[0][0].startswith("Review comments on hermes-control task")


def test_unknown_run_and_task(store, monkeypatch):
    monkeypatch.setattr(review_notes.runs, "run_view", lambda _: None)
    with pytest.raises(LookupError):
        review_notes.get("missing", "task")
    monkeypatch.setattr(review_notes.runs, "run_view", lambda _: {"tasks": []})
    with pytest.raises(LookupError):
        save()


def test_atomic_failure_preserves_previous_file(store, monkeypatch):
    first = save()
    def fail(*_):
        raise OSError("replace failed")
    monkeypatch.setattr(review_notes.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        save(version=1)
    assert review_notes.get("run", "task") == first
    assert not list(store[0].rglob("*.tmp"))


def test_writes_stay_outside_dashboard(store, monkeypatch):
    dashboard = Path(__file__).resolve().parents[1] / "hermes_control/dashboard"
    before = {p: p.stat().st_mtime_ns for p in dashboard.rglob("*") if p.is_file()}
    opened = []
    original = os.open
    def track(path, flags, mode=0o777):
        if flags & os.O_CREAT:
            opened.append(Path(path))
        return original(path, flags, mode)
    monkeypatch.setattr(review_notes.os, "open", track)
    save()
    assert opened and all(p.is_relative_to(store[0] / "reviews") for p in opened)
    assert before == {p: p.stat().st_mtime_ns for p in dashboard.rglob("*") if p.is_file()}


@pytest.mark.parametrize("raw", [
    "{", "null", "[]", "{}", "\"text\"", "[" * 2000 + "0" + "]" * 2000,
    '{"version":1,"run_id":"run","task_id":"task","commit":null,"comments":[null]}',
])
def test_damaged_storage_is_generic_invalid(store, raw):
    save()
    path = store[0] / "reviews/run/task.json"
    path.write_text(raw)
    for operation in (lambda: review_notes.get("run", "task"), lambda: save(version=1)):
        with pytest.raises(ValueError, match="^invalid review$"):
            operation()


@pytest.mark.parametrize("field,value", [
    ("id", []), ("path", []), ("side", []), ("line", "3"),
    ("text", None), ("text", "\ud800"), ("created_at", {}),
    ("created_at", float("nan")), ("created_at", float("inf")),
])
def test_malformed_stored_comment_is_generic_invalid(store, field, value):
    save()
    path = store[0] / "reviews/run/task.json"
    record = json.loads(path.read_text())
    record["comments"][0][field] = value
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="^invalid review$"):
        review_notes.get("run", "task")
    with pytest.raises(ValueError, match="^invalid review$"):
        save(version=1)


def test_deep_put_body_is_generic_invalid(store):
    nested = []
    for _ in range(2000):
        nested = [nested]
    with pytest.raises(ValueError, match="^invalid review$"):
        review_notes.put("run", "task", nested)


def test_note_collapses_unicode_line_separators(store):
    text = "First\u2028second\u2029third\u0085fourth\r\n\u2028last"
    result = save([comment(text=text)])
    assert result["comments"][0]["text"] == text
    assert result["note"].splitlines()[-1] == "- app.py:3 (new) — First second third fourth last"
    assert len(result["note"].splitlines()) == 3
