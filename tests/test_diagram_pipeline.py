import copy
import json
import re

import pytest

from hermes_control import diagram, diagram_pipeline


def block(payload, review=False):
    return '```hermes-diagram' + ('-review' if review else '') + '\n' + json.dumps(payload) + '\n```'


def review(dirty=False):
    return {"views": [{"kind": "module", "items": [
        {"id": "n0", "verdict": "wrong" if dirty else "confirmed", "note": "check n0",
         "evidence": [{"path": "app.py", "lines": [1, 2]}]},
        *[{"id": ident, "verdict": "unverifiable", "note": "cannot settle", "evidence": []}
          for ident in ("n1", "n2", "n3", "e1")]],
        "missing": [{"label": "Store", "evidence": []}] if dirty else []}]}


def scripted(repo, replies, *, reroute=False):
    calls = []
    def dispatch(tasks, *, parent_cwd):
        assert parent_cwd == str(repo)
        assert len(tasks) == 1
        task = tasks[0]
        assert task["policy"] == "read-only"
        stage = task["why"].removeprefix("diagram pipeline: ")
        calls.append(task)
        persisted = diagram.list_diagrams()[0]
        assert persisted["status"] == {"spec": "writing", "review": "reviewing", "fix": "fixing",
                                      "review-rest": "reviewing", "re-review-rest": "re-reviewing",
                                      "re-review": "re-reviewing", "claude-fix": "claude-fixing"}[stage]
        reply = replies[len(calls) - 1]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, dict):
            return reply
        return {"run_id": f"run{len(calls)}", "tasks": [{"task_id": task["id"],
            "engine": "claude" if reroute else task["engine"], "model": "actual-model",
            "ok": True, "text": reply}]}
    return dispatch, calls


def test_clean_first_review(diagram_repo, diagram_spec):
    flow = copy.deepcopy(diagram_spec["views"][0])
    flow["kind"] = "flow"
    flow["edges"][0]["order"] = 1
    diagram_spec["views"].append(flow)
    checked = review()
    checked_flow = copy.deepcopy(checked["views"][0])
    checked_flow["kind"] = "flow"
    checked["views"].append(checked_flow)
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(checked, True)], reroute=True)
    ticks = iter(range(100, 200))
    record = diagram_pipeline.run(diagram_repo, "design", [], codex_model="c", claude_model="a",
                                  dispatch_fn=dispatch, now=lambda: next(ticks))
    assert record["status"] == "done"
    assert record["kinds"] == ["module", "flow"]
    assert {v["kind"] for v in record["views"]} == set(record["kinds"])
    assert [s["stage"] for s in record["stages"]] == ["spec", "review"]
    assert all(s["engine"] == "claude" and s["model"] == "actual-model" for s in record["stages"])
    assert all(s["finished_at"] >= s["started_at"] for s in record["stages"])
    assert record["spec_author"] == "codex"
    assert calls[0]["model"] == "c" and calls[1]["model"] == "a"
    assert "module" in calls[0]["goal"] and "flow" in calls[0]["goal"]
    assert "verdict" not in calls[1]["goal"].split("Stored spec JSON:\n")[-1]
    assert re.fullmatch(r"d-\d{8}-[0-9a-f]{6}", record["id"])
    assert diagram.load_diagram(record["id"]) == record


def test_one_codex_fix_then_clean(diagram_repo, diagram_spec):
    fixed = copy.deepcopy(diagram_spec)
    fixed["summary"] = "Fixed summary"
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(review(True), True),
                                             block(fixed), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "done" and record["summary"] == "Fixed summary"
    assert [s["stage"] for s in record["stages"]] == ["spec", "review", "fix", "re-review"]
    assert all("model" not in t for t in calls)
    assert "check n0" in calls[2]["goal"] and "Store" in calls[2]["goal"]


def test_claude_fix_marks_changes_preserves_verdicts_and_unresolved_missing(diagram_repo, diagram_spec):
    corrected = copy.deepcopy(diagram_spec)
    corrected["views"][0]["nodes"][0]["label"] = "Corrected"
    corrected["views"][0]["nodes"].append({"id": "store", "label": "Store", "type": "store",
                                           "evidence": [{"path": "app.py", "lines": [1, 1]}]})
    corrected["resolves"] = [{"view": "module", "missing": "missing-1", "by": ["store"]}]
    dirty = review(True)
    dirty["views"][0]["missing"].append({"label": "Still absent", "evidence": []})
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(True), True),
        block(diagram_spec), block(dirty, True), block(corrected)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "done" and record["spec_author"] == "claude"
    nodes = record["views"][0]["nodes"]
    assert nodes[0]["claude_edited"] and nodes[0]["verdict"] == "unreviewed"
    assert nodes[-1]["claude_edited"] and nodes[-1]["verdict"] == "unreviewed"
    assert not nodes[1]["claude_edited"] and nodes[1]["verdict"] == "unverifiable"
    assert nodes[2]["verdict"] == "unverifiable"
    assert record["counts"]["claude_edited"] == 2
    assert [(m["label"], m["status"]) for m in record["views"][0]["missing"]] == [
        ("Store", "resolved"), ("Still absent", "open")]


@pytest.mark.parametrize("stage", range(5))
@pytest.mark.parametrize("failure", ["task", "error", "exception", "invalid"])
def test_stage_failures_preserve_last_spec(stage, failure, diagram_repo, diagram_spec):
    replies = [block(diagram_spec), block(review(True), True), block(diagram_spec),
               block(review(True), True), block(diagram_spec)]
    replies[stage] = {"task": {"tasks": [{"ok": False, "text": "worker failed"}]},
                      "error": {"error": "dispatch failed", "tasks": []},
                      "exception": RuntimeError("worker crashed"), "invalid": "no fence"}[failure]
    dispatch, calls = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "failed" and record["reason"]
    assert len(calls) == stage + 1 and len(record["stages"]) == stage + 1
    assert record["stages"][-1]["ok"] is False
    if stage:
        assert record["title"] == "App" and len(record["views"][0]["nodes"]) == 4
    if stage in (2, 4):
        assert record["views"][0]["nodes"][0]["verdict"] == "wrong"
    assert diagram.load_diagram(record["id"]) == record


def test_invalid_fix_preserves_reviewed_spec(diagram_repo, diagram_spec):
    invalid = copy.deepcopy(diagram_spec)
    invalid["views"][0]["edges"][0]["to"] = "ghost"
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(True), True), block(invalid)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "failed"
    assert record["views"][0]["edges"][0]["to"] == "n1"


def dirty_replies(spec):
    return [block(spec), block(review(True), True), block(spec),
            block(review(True), True), block(spec)]


@pytest.mark.parametrize("stage", range(5))
def test_stage_rejects_different_base_commit(stage, diagram_repo, diagram_spec):
    stamped = diagram_pipeline.repo_commit(diagram_repo)[1]
    moved = "abcdef0" + "1" * 33
    replies = dirty_replies(diagram_spec)
    replies[stage] = {"run_id": "moved", "tasks": [{"ok": True,
        "base_commit": moved, "text": replies[stage]}]}
    dispatch, calls = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["status"] == "failed"
    assert record["reason"] == (f"repo HEAD moved from {stamped[:7]} to {moved[:7]} "
                                "during the run; re-run the diagram")
    assert len(calls) == stage + 1
    assert record["stages"][-1]["ok"] is False
    if stage:
        assert record["views"][0]["nodes"][0]["verdict"] == ("wrong" if stage in (2, 4) else "unreviewed")
    else:
        assert record["views"] == []


def test_matching_base_commit_is_accepted(diagram_repo, diagram_spec):
    sha = diagram_pipeline.repo_commit(diagram_repo)[1]
    replies = [{"tasks": [{"ok": True, "base_commit": sha, "text": text}]}
               for text in (block(diagram_spec), block(review(), True))]
    dispatch, _ = scripted(diagram_repo, replies)
    assert diagram_pipeline.run(diagram_repo, "design", ["module"],
                                dispatch_fn=dispatch)["status"] == "done"


def test_every_stage_goal_uses_worker_checkout(diagram_repo, diagram_spec):
    dispatch, calls = scripted(diagram_repo, dirty_replies(diagram_spec))
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert len(calls) == 5
    for task in calls:
        assert "current directory is a checkout of the stamped commit " + record["commit"] in task["goal"]
        assert "All evidence paths are relative to it" in task["goal"]
        assert "must not read the original" in task["goal"]
        assert str(diagram_repo) not in task["goal"]


@pytest.mark.parametrize("stage", [0, 2, 4])
@pytest.mark.parametrize("kinds, missing, extra", [
    (None, "flow", ""), (["flow"], "flow", "module"), (["module", "module"], "", "sequence")])
def test_spec_requires_requested_kinds(stage, kinds, missing, extra, diagram_repo, diagram_spec):
    valid = copy.deepcopy(diagram_spec)
    valid["views"] = []
    for kind in dict.fromkeys(kinds or ["module", "flow"]):
        view = copy.deepcopy(diagram_spec["views"][0])
        view["kind"] = kind
        view["edges"][0]["order"] = 1
        valid["views"].append(view)
    invalid = copy.deepcopy(diagram_spec)
    if extra == "sequence":
        view = copy.deepcopy(invalid["views"][0])
        view["kind"] = "sequence"
        view["edges"][0]["order"] = 1
        invalid["views"].append(view)
    dirty = review(True)
    if "module" not in {v["kind"] for v in valid["views"]}:
        dirty["views"][0]["kind"] = "flow"
    for kind in {v["kind"] for v in valid["views"]} - {dirty["views"][0]["kind"]}:
        extra_review = copy.deepcopy(dirty["views"][0])
        extra_review["kind"] = kind
        dirty["views"].append(extra_review)
    replies = [block(valid), block(dirty, True), block(valid), block(dirty, True), block(valid)]
    replies[stage] = block(invalid)
    dispatch, calls = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", kinds, dispatch_fn=dispatch)
    assert record["status"] == "failed"
    if missing:
        assert "missing kinds: " + missing in record["reason"]
    if extra:
        assert "extra kinds: " + extra in record["reason"]
    assert len(calls) == stage + 1


def test_claude_no_item_changes_keeps_codex_author(diagram_repo, diagram_spec):
    final = copy.deepcopy(diagram_spec)
    final["summary"] = "Only prose changed"
    replies = dirty_replies(diagram_spec)
    replies[-1] = block(final)
    dispatch, _ = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "done"
    assert record["spec_author"] == "codex"
    assert record["stages"][-1]["reason"] == "claude made no changes"
    assert record["counts"]["claude_edited"] == 0
    assert record["views"][0]["nodes"][0]["verdict"] == "wrong"


def test_running_stages_are_pending(diagram_repo, diagram_spec):
    dispatch, calls = scripted(diagram_repo, dirty_replies(diagram_spec))
    def check_pending(tasks, **kwargs):
        saved = diagram.load_diagram(diagram.list_diagrams()[0]["id"])
        assert saved["stages"][-1]["ok"] is None
        assert saved["stages"][-1]["finished_at"] is None
        assert all(s["ok"] is True for s in saved["stages"][:-1])
        return dispatch(tasks, **kwargs)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=check_pending)
    assert record["status"] == "done" and len(calls) == 5
    assert all(s["ok"] is True for s in record["stages"])


def test_id_collision_does_not_overwrite_existing(monkeypatch, diagram_repo, diagram_spec):
    existing = {"id": "d-19700101-111111", "created_at": 1, "title": "Keep this"}
    diagram.save_diagram(existing)
    tokens = iter(["111111", "111111", "222222"])
    monkeypatch.setattr(diagram_pipeline.secrets, "token_hex", lambda _: next(tokens))
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"],
                                  dispatch_fn=dispatch, now=lambda: 100)
    assert record["status"] == "done"
    assert record["id"] == "d-19700101-222222"
    assert diagram.load_diagram(existing["id"]) == existing


def read_event(path="/worker/app.py", offset=None, limit=None, *, tool_id=None):
    inputs = {"file_path": path}
    if offset is not None:
        inputs["offset"] = offset
    if limit is not None:
        inputs["limit"] = limit
    payload = {"name": "Read", "input": inputs}
    if tool_id is not None:
        payload["tool_id"] = tool_id
    return {"kind": "tool_start", "payload": payload}


def test_partial_reviews_get_one_targeted_rest_stage(diagram_repo, diagram_spec):
    partial = review(True)
    partial["views"][0]["items"] = partial["views"][0]["items"][:1]
    rest = review(True)
    rest["views"][0]["items"][0].update(verdict="confirmed", note="must not replace")
    rest["views"][0]["missing"].append({"label": "Queue", "evidence": []})
    # The second review and its follow-up both leave four items unreviewed.
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(partial, True),
        block(rest, True), block(diagram_spec), block(partial, True),
        block(partial, True), block(diagram_spec)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "done"
    assert [s["stage"] for s in record["stages"]] == [
        "spec", "review", "review-rest", "fix", "re-review", "re-review-rest", "claude-fix"]
    assert 'return exactly 5 items for view module' in calls[1]["goal"]
    for index in (2, 5):
        goal = calls[index]["goal"]
        assert 'return exactly 4 items for view module' in goal
        ids = goal.split("Items to review JSON:\n")[1].split("\n", 1)[0]
        assert json.loads(ids) == {"module": ["n1", "n2", "n3", "e1"]}
        assert "Stored spec JSON:" in goal
    findings = json.loads(calls[3]["goal"].split("Review findings JSON:\n")[1])
    assert findings["views"][0]["wrong"][0]["id"] == "n0"
    assert [m["label"] for m in findings["views"][0]["missing"]] == ["Store", "Queue"]
    assert "4 items still unreviewed" in record["stages"][5]["reason"]
    assert record["views"][0]["nodes"][0]["verdict"] == "wrong"


@pytest.mark.parametrize("events,expected", [([], "unverifiable"),
    ([read_event(offset=2, limit=1)], "unverifiable"),
    ([read_event(offset=1, limit=1), read_event(offset=2, limit=1)], "confirmed"),
    ([read_event()], "confirmed"),
    ([{"kind": "tool_start", "payload": {"name": "Bash", "input": {"command": "cat app.py"}}}],
     "unverifiable")])
def test_review_confirmations_require_opened_lines(events, expected, diagram_repo, diagram_spec):
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: events)
    node = record["views"][0]["nodes"][0]
    assert node["verdict"] == expected
    if expected == "unverifiable":
        assert node["review_note"].startswith("Downgraded: the reviewer did not open the cited lines. ")
        assert "1 confirmations downgraded" in record["stages"][1]["reason"]


def test_review_can_use_own_evidence_and_default_log_reader(diagram_repo, diagram_spec):
    from hermes_control import runlog
    checked = review()
    checked["views"][0]["items"][0]["evidence"] = [{"path": "app.py", "lines": [4, 4]}]
    runlog.run_log_path("run2").write_text(json.dumps(read_event(offset=4, limit=1)) + "\n")
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(checked, True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch)
    assert record["views"][0]["nodes"][0]["verdict"] == "confirmed"
    assert "skipped" not in record["stages"][1]["reason"]


def test_unreadable_log_is_reported(diagram_repo, diagram_spec):
    def unavailable(_):
        raise OSError("log gone")
    dispatch, _ = scripted(diagram_repo, dirty_replies(diagram_spec))
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=unavailable)
    assert record["status"] == "done"
    for stage in (record["stages"][1], record["stages"][3], record["stages"][4]):
        assert "read checks skipped" in stage["reason"] and "log gone" in stage["reason"]


def test_claude_fix_with_no_reads_keeps_reviewed_spec(diagram_repo, diagram_spec):
    corrected = copy.deepcopy(diagram_spec)
    corrected["summary"] = "Changed without reading"
    corrected["views"][0]["nodes"][0]["label"] = "Changed"
    corrected["resolves"] = [{"view": "module", "missing": "missing-1", "dismiss": "ignore"}]
    replies = dirty_replies(diagram_spec)
    replies[-1] = block(corrected)
    dispatch, _ = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [])
    assert record["status"] == "done" and record["summary"] == diagram_spec["summary"]
    assert record["spec_author"] == "codex"
    assert record["views"][0]["nodes"][0]["verdict"] == "wrong"
    assert record["counts"]["missing"] == 1
    assert record["stages"][-1]["reason"] == "claude-fix read no code; kept the reviewed spec"


def test_claude_fix_reverts_unread_edits_and_deletions(diagram_repo, diagram_spec):
    final = copy.deepcopy(diagram_spec)
    view = final["views"][0]
    view["nodes"][0]["label"] = "Covered edit"
    view["nodes"][0]["evidence"] = [{"path": "app.py", "lines": [1, 1]}]
    view["nodes"][1]["label"] = "Unread edit"
    del view["nodes"][2]  # Non-wrong deletion must be restored.
    view["nodes"].append({"id": "store", "label": "Store", "type": "store",
                          "evidence": [{"path": "app.py", "lines": [2, 2]}]})
    view["edges"].append({"id": "new-edge", "from": "n0", "to": "store", "label": "calls",
                          "type": "calls", "evidence": [{"path": "app.py", "lines": [1, 1]}]})
    final["resolves"] = [{"view": "module", "missing": "missing-1", "by": ["store"]}]
    dispatch, _ = scripted(diagram_repo, dirty_replies(diagram_spec)[:-1] + [block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event(offset=1, limit=1)])
    nodes = {n["id"]: n for n in record["views"][0]["nodes"]}
    assert record["status"] == "done"
    assert nodes["n0"]["label"] == "Covered edit" and nodes["n0"]["claude_edited"]
    assert nodes["n1"]["label"] == "Node 1" and nodes["n1"]["review_note"] == "cannot settle"
    assert "n2" in nodes and "store" not in nodes
    assert [e["id"] for e in record["views"][0]["edges"]] == ["e1"]
    assert record["counts"]["missing"] == 1
    assert "4 edits reverted: evidence not opened" in record["stages"][-1]["reason"]


def test_resolutions_are_explicit_and_passed_to_re_review(diagram_repo, diagram_spec):
    fixed = copy.deepcopy(diagram_spec)
    fixed["resolves"] = [{"view": "module", "missing": "missing-1", "by": ["n0"]},
                         {"view": "module", "missing": "missing-2", "dismiss": "Already covered"}]
    initial = review(True)
    initial["views"][0]["missing"][0]["note"] = "Persistent storage is absent"
    initial["views"][0]["missing"].append({"label": "Initial queue", "note": "Queue details", "evidence": []})
    dirty = review(True)
    dirty["views"][0]["missing"] += [{"label": "Other", "evidence": []},
                                      {"label": "Node 1", "evidence": []}]
    final = copy.deepcopy(diagram_spec)
    final["resolves"] = [
        {"view": "module", "missing": "missing-1", "by": ["n0"]},
        {"view": "module", "missing": "missing-2", "dismiss": "Already covered"},
        {"view": "module", "missing": "unknown", "by": ["n1"]}]
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block(initial, True),
        block(fixed), block(dirty, True), block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert "the fix says these omissions were addressed by" in calls[3]["goal"]
    claims = json.loads(calls[3]["goal"].split("(verify these claims):\n")[1])
    assert claims == {"module": [
        {"missing": "missing-1", "label": "Store", "note": "Persistent storage is absent", "by": ["n0"]},
        {"missing": "missing-2", "label": "Initial queue", "note": "Queue details", "dismiss": "Already covered"}]}
    assert '"missing-1"' in calls[2]["goal"]
    missing = record["views"][0]["missing"]
    assert missing[0]["status"] == "resolved" and missing[0]["by"] == ["n0"]
    assert missing[1]["status"] == "dismissed" and missing[1]["reason"] == "Already covered"
    assert missing[2]["status"] == "open"  # Even though its label equals an existing node.
    assert record["counts"]["missing"] == 1 and record["counts"]["missing_settled"] == 2


def test_language_is_stored_and_in_every_worker_goal(diagram_repo, diagram_spec):
    dispatch, calls = scripted(diagram_repo, dirty_replies(diagram_spec))
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], lang="th", dispatch_fn=dispatch)
    assert record["lang"] == "th"
    for task in calls:
        assert "in th" in task["goal"]
        assert "ids, labels, code names and paths stay English" in task["goal"]


@pytest.mark.parametrize("language", ["en", "th"])
def test_cli_language_is_forwarded(language, monkeypatch, diagram_repo):
    import argparse
    from hermes_control import watch
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    def fake_run(*args, **kwargs):
        assert kwargs["lang"] == language
        return {"status": "done", "title": "", "summary": "", "counts": {}, "reason": "", "url": ""}
    monkeypatch.setattr(diagram_pipeline, "run", fake_run)
    args = ["diagram", "--repo", str(diagram_repo), "--ask", "design"]
    if language != "en":
        args += ["--lang", language]
    assert watch.run_cli(parser.parse_args(args)) == 0


@pytest.mark.parametrize("stage", [2, 4])
def test_fix_over_caps_fails_without_replacing_spec(stage, diagram_repo, diagram_spec):
    oversized = copy.deepcopy(diagram_spec)
    view = oversized["views"][0]
    view["nodes"] += [{**copy.deepcopy(view["nodes"][0]), "id": f"extra{i}"}
                       for i in range(27)]
    replies = dirty_replies(diagram_spec)
    replies[stage] = block(oversized)
    dispatch, calls = scripted(diagram_repo, replies)
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "failed" and len(calls) == stage + 1
    assert "limit 30" in record["stages"][-1]["reason"]
    assert record["views"][0]["nodes"][0]["verdict"] == "wrong"
    assert len(record["views"][0]["nodes"]) == 4


def test_claude_wrong_deletions_allowed_other_deletions_restored(diagram_repo, diagram_spec):
    final = copy.deepcopy(diagram_spec)
    final["views"][0]["nodes"] = final["views"][0]["nodes"][1:]
    final["views"][0]["edges"] = []
    dirty = review(True)
    dirty["views"][0]["items"][-1]["verdict"] = "wrong"
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(dirty, True),
        block(diagram_spec), block(dirty, True), block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "done" and record["spec_author"] == "claude"
    assert record["counts"]["wrong"] == 0
    assert [n["id"] for n in record["views"][0]["nodes"]] == ["n1", "n2", "n3"]
    assert record["views"][0]["edges"] == []


def test_filtered_invalid_fix_keeps_entire_reviewed_spec(diagram_repo, diagram_spec):
    diagram_spec["views"][0]["groups"] = [{"id": "core", "label": "Core"}]
    diagram_spec["views"][0]["nodes"][2]["group"] = "core"
    final = copy.deepcopy(diagram_spec)
    final["summary"] = "Must not install"
    final["views"][0]["groups"] = []
    del final["views"][0]["nodes"][2]
    dispatch, _ = scripted(diagram_repo, dirty_replies(diagram_spec)[:-1] + [block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "done" and record["spec_author"] == "codex"
    assert diagram.spec_payload(record) == diagram_spec
    assert "filtered claude-fix invalid; kept the reviewed spec" in record["stages"][-1]["reason"]


def test_reverted_existing_item_cannot_resolve_omission(diagram_repo, diagram_spec):
    final = copy.deepcopy(diagram_spec)
    final["views"][0]["nodes"][0]["label"] = "Unread edit"
    final["resolves"] = [{"view": "module", "missing": "missing-1", "by": ["n0"]}]
    dispatch, _ = scripted(diagram_repo, dirty_replies(diagram_spec)[:-1] + [block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event(offset=4, limit=1)])
    node = record["views"][0]["nodes"][0]
    assert node["label"] == "Node 0" and node["verdict"] == "wrong"
    assert node["review_note"] == "check n0"
    assert record["counts"]["missing"] == 1
    assert record["counts"]["missing_settled"] == 0


@pytest.mark.parametrize("event,worktree,expected", [
    (read_event("/checkout/app.py"), "/checkout", "confirmed"),
    (read_event("/elsewhere/app.py"), "/checkout", "unverifiable"),
    (read_event("/checkout/../elsewhere/app.py"), "/checkout", "unverifiable"),
    (read_event("/checkout/sub/../app.py"), "/checkout", "confirmed"),
    (read_event("/checkout/sub/app.py"), "/checkout", "unverifiable"),
    (read_event("/elsewhere/not-app.py"), None, "unverifiable"),
    ({**read_event(), "task_id": "other-task"}, None, "unverifiable"),
    ({**read_event(), "kind": "tool_end"}, None, "unverifiable"),
])
def test_read_path_mapping_and_event_filtering(event, worktree, expected, diagram_repo, diagram_spec):
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    def with_worktree(tasks, **kwargs):
        result = dispatch(tasks, **kwargs)
        result["tasks"][0]["worktree"] = worktree
        return result
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=with_worktree,
                                  event_reader=lambda _: [event])
    assert record["views"][0]["nodes"][0]["verdict"] == expected


def test_rest_failure_keeps_main_review(diagram_repo, diagram_spec):
    partial = review(True)
    partial["views"][0]["items"] = partial["views"][0]["items"][:1]
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(partial, True), "no JSON"])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event()])
    assert record["status"] == "failed"
    assert record["stages"][-1]["stage"] == "review-rest"
    assert record["views"][0]["nodes"][0]["verdict"] == "wrong"
    assert record["counts"]["unreviewed"] == 4
    assert record["counts"]["missing"] == 1


def test_rest_confirmations_use_rest_reads(diagram_repo, diagram_spec):
    partial = review()
    partial["views"][0]["items"] = partial["views"][0]["items"][:1]
    rest = review()
    rest["views"][0]["items"][1]["verdict"] = "confirmed"
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(partial, True), block(rest, True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda run_id: [] if run_id == "run3" else [read_event()])
    assert record["status"] == "done"
    nodes = record["views"][0]["nodes"]
    assert nodes[0]["verdict"] == "confirmed"
    assert nodes[1]["verdict"] == "unverifiable"
    assert "1 confirmations downgraded" in record["stages"][-1]["reason"]


def test_review_prompt_includes_empty_views(diagram_repo, diagram_spec):
    diagram_spec["views"][0].update(nodes=[], edges=[])
    dispatch, calls = scripted(diagram_repo, [block(diagram_spec), block({"views": []}, True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [])
    assert record["status"] == "done"
    assert "return exactly 0 items for view module" in calls[1]["goal"]
    assert '"module": []' in calls[1]["goal"]


def test_failed_review_keeps_unreadable_log_diagnostic(diagram_repo, diagram_spec):
    def unreadable(_):
        raise OSError("unavailable")
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), "no review JSON"])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=unreadable)
    assert record["status"] == "failed"
    assert "read checks skipped" in record["stages"][-1]["reason"]
    assert "invalid hermes-diagram-review" in record["stages"][-1]["reason"]


@pytest.mark.parametrize("edge_mode", ["unchanged", "restored", "unread-change", "missing-new-endpoint"])
def test_wrong_node_deletion_drops_attached_edges_and_keeps_valid_edits(edge_mode, diagram_repo, diagram_spec):
    final = copy.deepcopy(diagram_spec)
    view = final["views"][0]
    del view["nodes"][0]
    view["nodes"][0].update(label="Covered edit", evidence=[{"path": "app.py", "lines": [1, 1]}])
    view["nodes"][1]["label"] = "Unread edit"
    if edge_mode == "restored":
        view["edges"] = []
    elif edge_mode == "unread-change":
        view["edges"][0]["from"] = "n2"
    elif edge_mode == "missing-new-endpoint":
        view["nodes"].append({"id": "store", "label": "Store", "type": "store",
                              "evidence": [{"path": "app.py", "lines": [2, 2]}]})
        view["edges"][0].update({"from": "n2", "to": "store",
                                 "evidence": [{"path": "app.py", "lines": [1, 1]}]})
    dispatch, _ = scripted(diagram_repo, dirty_replies(diagram_spec)[:-1] + [block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event(offset=1, limit=1)])
    assert record["status"] == "done" and record["spec_author"] == "claude"
    nodes = {n["id"]: n for n in record["views"][0]["nodes"]}
    assert "n0" not in nodes
    assert nodes["n1"]["label"] == "Covered edit" and nodes["n1"]["claude_edited"]
    assert nodes["n2"]["label"] == "Node 2"
    assert "store" not in nodes
    assert record["views"][0]["edges"] == []
    reverted = 2 if edge_mode == "missing-new-endpoint" else 1
    assert record["stages"][-1]["reason"] == f"{reverted} edits reverted: evidence not opened"


def test_original_checkout_read_does_not_confirm_worker_evidence(diagram_repo, diagram_spec):
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    def with_worktree(tasks, **kwargs):
        result = dispatch(tasks, **kwargs)
        result["tasks"][0]["worktree"] = "/worker"
        return result
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=with_worktree,
                                  event_reader=lambda _: [read_event(str(diagram_repo / "app.py"))])
    assert record["views"][0]["nodes"][0]["verdict"] == "unverifiable"
    assert "1 confirmations downgraded" in record["stages"][1]["reason"]


@pytest.mark.parametrize("offset", [0, -5])
def test_read_nonpositive_offset_starts_at_one(offset, diagram_repo, diagram_spec):
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: [read_event(offset=offset, limit=2)])
    assert record["views"][0]["nodes"][0]["verdict"] == "confirmed"


def test_failed_read_does_not_complete_coverage(diagram_repo, diagram_spec):
    events = [
        read_event(offset=1, limit=1, tool_id="ok"),
        read_event(offset=2, limit=1, tool_id="failed"),
        {"kind": "tool_end", "payload": {"tool_id": "ok", "is_error": False, "content": "line 1"}},
        {"kind": "tool_end", "payload": {"tool_id": "failed", "is_error": True, "content": "Read failed"}}]
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(review(), True)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda _: iter(events))
    assert record["views"][0]["nodes"][0]["verdict"] == "unverifiable"
    assert diagram_pipeline._read_ranges(events, "task") == [("/worker/app.py", 1, 1)]
    events[-1]["task_id"] = "other-task"
    assert diagram_pipeline._read_ranges(events, "task") == [("/worker/app.py", 1, 1), ("/worker/app.py", 2, 2)]


def test_unchanged_resolution_items_must_each_have_read_evidence(diagram_repo, diagram_spec):
    view = diagram_spec["views"][0]
    # Existing items may use any covered range, but an unread range alone cannot settle an omission.
    view["nodes"][0]["evidence"] = [{"path": "app.py", "lines": [2, 2]}]
    view["nodes"][1]["evidence"] = [{"path": "app.py", "lines": [1, 1]}]
    view["nodes"][2]["evidence"] = [{"path": "app.py", "lines": [2, 2]},
                                      {"path": "app.py", "lines": [1, 1]}]
    view["nodes"][3]["evidence"] = []
    view["edges"][0]["evidence"] = [{"path": "app.py", "lines": [2, 2]}]
    dirty = review(True)
    dirty["views"][0]["missing"] = [{"label": f"Omission {i}", "evidence": []} for i in range(5)]
    final = copy.deepcopy(diagram_spec)
    final["resolves"] = [{"view": "module", "missing": f"missing-{i}", "by": ids}
                         for i, ids in enumerate((["n0"], ["n1", "n0"], ["e1"], ["n3"], ["n2"]), 1)]
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(dirty, True),
        block(diagram_spec), block(dirty, True), block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda run_id: [read_event(offset=1, limit=1)]
                                  if run_id == "run5" else [read_event()])
    assert record["status"] == "done"
    assert [m["status"] for m in record["views"][0]["missing"]] == ["open"] * 4 + ["resolved"]
    assert record["counts"]["missing"] == 4 and record["counts"]["missing_settled"] == 1


def test_dismissal_requires_reading_omission_evidence_when_present(diagram_repo, diagram_spec):
    dirty = review(True)
    dirty["views"][0]["missing"] = [
        {"label": "Unread omission", "evidence": [{"path": "app.py", "lines": [1, 1]}]},
        {"label": "Read omission", "evidence": [{"path": "app.py", "lines": [1, 1]},
                                                 {"path": "app.py", "lines": [2, 2]}]},
        {"label": "No evidence", "evidence": []}]
    final = copy.deepcopy(diagram_spec)
    final["resolves"] = [{"view": "module", "missing": f"missing-{i}", "dismiss": "Already covered"}
                         for i in range(1, 4)]
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(dirty, True),
        block(diagram_spec), block(dirty, True), block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch,
                                  event_reader=lambda run_id: [read_event(offset=2, limit=1)]
                                  if run_id == "run5" else [read_event()])
    assert record["status"] == "done"
    assert [m["status"] for m in record["views"][0]["missing"]] == ["open", "dismissed", "dismissed"]
    assert record["counts"]["missing"] == 1 and record["counts"]["missing_settled"] == 2


def test_unreadable_fix_log_cannot_settle_evidenced_omissions(diagram_repo, diagram_spec):
    dirty = review(True)
    dirty["views"][0]["missing"] = [
        {"label": "Resolve", "evidence": []},
        {"label": "Dismiss", "evidence": [{"path": "app.py", "lines": [1, 1]}]},
        {"label": "No evidence", "evidence": []}]
    final = copy.deepcopy(diagram_spec)
    final["resolves"] = [
        {"view": "module", "missing": "missing-1", "by": ["n0"]},
        {"view": "module", "missing": "missing-2", "dismiss": "Already covered"},
        {"view": "module", "missing": "missing-3", "dismiss": "Already covered"}]
    def events(run_id):
        if run_id == "run5":
            raise OSError("log unavailable")
        return [read_event()]
    dispatch, _ = scripted(diagram_repo, [block(diagram_spec), block(dirty, True),
        block(diagram_spec), block(dirty, True), block(final)])
    record = diagram_pipeline.run(diagram_repo, "design", ["module"], dispatch_fn=dispatch, event_reader=events)
    assert record["status"] == "done"
    assert "read checks skipped" in record["stages"][-1]["reason"]
    assert [m["status"] for m in record["views"][0]["missing"]] == ["open", "open", "dismissed"]
