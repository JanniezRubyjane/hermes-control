import argparse
import copy
import json

import pytest
import yaml

from hermes_control import diagram, diagram_pipeline, watch
from hermes_control.engines import registry


def block(payload, review=False):
    return "```hermes-diagram" + ("-review" if review else "") + "\n" + json.dumps(payload) + "\n```"


def review(dirty=False):
    return {"views": [{"kind": "module", "items": [
        {"id": "n0", "verdict": "wrong" if dirty else "confirmed", "note": "check n0",
         "evidence": [{"path": "app.py", "lines": [1, 2]}]},
        *[{"id": ident, "verdict": "unverifiable", "note": "cannot settle", "evidence": []}
          for ident in ("n1", "n2", "n3", "e1")]],
        "missing": [{"label": "Store", "evidence": []}] if dirty else []}]}


def read_event(path="/worker/app.py", offset=None, limit=None):
    inputs = {"file_path": path}
    if offset is not None:
        inputs["offset"] = offset
    if limit is not None:
        inputs["limit"] = limit
    return {"kind": "tool_start", "payload": {"name": "Read", "input": inputs}}


def scripted(repo, replies):
    calls = []

    def dispatch(tasks, *, parent_cwd):
        assert parent_cwd == str(repo)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["policy"] == "read-only"
        calls.append(task)
        reply = replies[len(calls) - 1]
        return {"run_id": f"run{len(calls)}", "tasks": [{
            "task_id": task["id"],
            "engine": task["engine"],
            "model": task.get("model"),
            "ok": True,
            "text": reply,
        }]}

    return dispatch, calls


def write_settings(home, settings):
    (home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"entries": {"hermes_control": {"settings": settings}}}}),
        encoding="utf-8",
    )


def enable(monkeypatch, names):
    # The live plugin resolves ENGINE_NAMES once per process and every module
    # sees the same tuple; mirror that instead of patching only the registry.
    from hermes_control import model_policy
    for module in (registry, model_policy):
        monkeypatch.setattr(module, "ENGINE_NAMES", names)


def test_unset_author_falls_back_to_gemini_when_codex_is_disabled(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    # Turning codex off must not stop diagrams: with no diagram.author set the
    # author is the first enabled of codex, gemini, and spec_author records it.
    enable(monkeypatch, ("claude", "gemini"))
    write_settings(_isolated_hermes_home, {"models": {"gemini": {"default": "gemini-4-argon"}}})
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "done"
    assert record["spec_author"] == "gemini"
    assert [c["engine"] for c in calls] == ["gemini", "claude"]
    assert calls[0]["model"] == "gemini-4-argon"


def test_unset_author_with_no_author_engine_enabled_fails_before_dispatch(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude",))
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    with pytest.raises(diagram.DiagramError, match=r"diagram\.author.*codex.*gemini"):
        diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert calls == []
    assert diagram.list_diagrams() == []


def test_default_author_is_codex_and_uses_codex_default_model(
    _isolated_hermes_home, diagram_repo, diagram_spec
):
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "done"
    assert record["spec_author"] == "codex"
    assert [c["engine"] for c in calls] == ["codex", "claude"]
    assert "model" not in calls[0]

    write_settings(_isolated_hermes_home, {"models": {"codex": {"default": "gpt-5.6-terra"}}})
    dispatch2, calls2 = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record2 = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch2)
    assert record2["status"] == "done"
    assert record2["spec_author"] == "codex"
    assert calls2[0]["engine"] == "codex"
    assert calls2[0]["model"] == "gpt-5.6-terra"
    assert "model" not in calls2[1]


def test_author_gemini_routes_spec_and_fix_with_default_model_and_records_spec_author(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude", "codex", "gemini"))
    write_settings(_isolated_hermes_home, {
        "engines": {"gemini": {"enabled": True}},
        "diagram": {"author": "gemini"},
        "models": {"gemini": {"default": "gemini-4-argon"}},
    })
    fixed = copy.deepcopy(diagram_spec)
    fixed["summary"] = "Fixed by gemini"
    dispatch, calls = scripted(diagram_repo, [
        block(diagram_spec),
        block(review(True), True),
        block(fixed),
        block(review(), True),
    ])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "done"
    assert record["summary"] == "Fixed by gemini"
    assert record["spec_author"] == "gemini"
    assert diagram.load_diagram(record["id"])["spec_author"] == "gemini"
    assert [c["engine"] for c in calls] == ["gemini", "claude", "gemini", "claude"]
    assert calls[0]["model"] == "gemini-4-argon"
    assert "model" not in calls[1]
    assert calls[2]["model"] == "gemini-4-argon"
    assert "model" not in calls[3]
    assert [s["engine"] for s in record["stages"]] == ["gemini", "claude", "gemini", "claude"]
    assert record["stages"][0]["model"] == "gemini-4-argon"
    assert record["stages"][2]["model"] == "gemini-4-argon"


def test_caller_model_overrides_gemini_default_and_codex_model_alias_works(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude", "codex", "gemini"))
    write_settings(_isolated_hermes_home, {
        "diagram": {"author": "gemini"},
        "models": {"gemini": {"default": "gemini-4-argon"}},
    })
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(
        diagram_repo, "design", ["module"],
        codex_model="gemini-custom", claude_model="claude-custom",
        dispatch_fn=dispatch,
    )
    assert record["status"] == "done"
    assert record["spec_author"] == "gemini"
    assert calls[0]["engine"] == "gemini" and calls[0]["model"] == "gemini-custom"
    assert calls[1]["engine"] == "claude" and calls[1]["model"] == "claude-custom"

    dispatch2, calls2 = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record2 = diagram_pipeline.run(
        diagram_repo, "design", ["module"],
        author_model="gemini-explicit",
        dispatch_fn=dispatch2,
    )
    assert record2["status"] == "done"
    assert calls2[0]["engine"] == "gemini" and calls2[0]["model"] == "gemini-explicit"


def test_claude_fix_keeps_or_updates_gemini_spec_author(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude", "codex", "gemini"))
    write_settings(_isolated_hermes_home, {"diagram": {"author": "gemini"}})

    unchanged = copy.deepcopy(diagram_spec)
    unchanged["summary"] = "Only prose changed"
    dispatch, _ = scripted(diagram_repo, [
        block(diagram_spec), block(review(True), True),
        block(diagram_spec), block(review(True), True),
        block(unchanged),
    ])
    record = diagram_pipeline.run(
        diagram_repo, "design", ["module"], dispatch_fn=dispatch,
        event_reader=lambda _: [read_event()],
    )
    assert record["status"] == "done"
    assert record["spec_author"] == "gemini"

    changed = copy.deepcopy(diagram_spec)
    changed["views"][0]["nodes"][0]["label"] = "Claude edited"
    dispatch2, _ = scripted(diagram_repo, [
        block(diagram_spec), block(review(True), True),
        block(diagram_spec), block(review(True), True),
        block(changed),
    ])
    record2 = diagram_pipeline.run(
        diagram_repo, "design", ["module"], dispatch_fn=dispatch2,
        event_reader=lambda _: [read_event()],
    )
    assert record2["status"] == "done"
    assert record2["spec_author"] == "claude"


@pytest.mark.parametrize("diagram_setting", [
    "gemini",
    123,
    False,
    ["gemini"],
    None,
    {"author": "claude"},
    {"author": "other"},
    {"author": ""},
    {"author": None},
    {"author": 1},
    {"author": True},
    {"author": "gemini", "extra": True},
    {"writer": "gemini"},
])
def test_invalid_diagram_setting_and_claude_rejected_before_dispatch(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec, diagram_setting
):
    enable(monkeypatch, ("claude", "codex", "gemini"))
    write_settings(_isolated_hermes_home, {"diagram": diagram_setting})
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    with pytest.raises(diagram.DiagramError, match="diagram"):
        diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert calls == []
    assert diagram.list_diagrams() == []


def test_claude_author_error_names_setting_and_claude(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude", "codex", "gemini"))
    write_settings(_isolated_hermes_home, {"diagram": {"author": "claude"}})
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    with pytest.raises(diagram.DiagramError, match=r"diagram\.author.*claude"):
        diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert calls == []


def test_disabled_gemini_refused_before_dispatch(
    monkeypatch, _isolated_hermes_home, diagram_repo, diagram_spec
):
    enable(monkeypatch, ("claude", "codex"))
    write_settings(_isolated_hermes_home, {
        "diagram": {"author": "gemini"},
        "models": {"gemini": {"default": "gemini-4-argon"}},
    })
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    with pytest.raises(diagram.DiagramError, match=r"diagram.*gemini|gemini.*diagram"):
        diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert calls == []
    assert diagram.list_diagrams() == []


@pytest.mark.parametrize("flag", ["--codex-model", "--author-model"])
def test_cli_author_model_flags_forward_to_run(flag, monkeypatch, diagram_repo):
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    received = {}

    def fake_run(*args, **kwargs):
        received.update(kwargs)
        return {"status": "done", "title": "", "summary": "", "counts": {}, "reason": "", "url": ""}

    monkeypatch.setattr(diagram_pipeline, "run", fake_run)
    args = parser.parse_args(["diagram", "--repo", str(diagram_repo), "--ask", "design", flag, "gemini-4-argon"])
    assert watch.run_cli(args) == 0
    assert (received.get("author_model") or received.get("codex_model")) == "gemini-4-argon"
