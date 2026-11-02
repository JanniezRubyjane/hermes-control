import copy
import json
import subprocess

import pytest

from hermes_control import diagram


def commit(repo):
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def block(value, info="hermes-diagram"):
    return f"```{info}\n{json.dumps(value)}\n```"


def test_parse_last_matching_fence(diagram_spec):
    text = block({"first": True}) + "\n```json\n{}\n```\n" + block(diagram_spec)
    assert diagram.parse_spec(text) == diagram_spec
    assert diagram.parse_review(block({"views": []}, "hermes-diagram-review")) == {"views": []}
    assert diagram.parse_spec(block(diagram_spec, "hermes-diagram-review")) is None
    assert diagram.parse_spec(text + "\n```hermes-diagram\ninvalid\n```") is None
    assert diagram.parse_spec("```hermes-diagram-extra\n{}\n```") is None
    assert diagram.parse_spec("```hermes-diagram\n[]\n```") is None
    assert diagram.parse_review("none") is None


def test_parse_size_limit(diagram_spec):
    diagram_spec["unknowns"] = ["x" * (200 * 1024)]
    assert diagram.parse_spec(block(diagram_spec)) is None


def test_request_contracts():
    for request in (diagram.SPEC_REQUEST, diagram.FIX_REQUEST, diagram.CLAUDE_FIX_REQUEST):
        assert "hermes-diagram" in request and "read-only" in request
        assert "unknowns" in request and "evidence" in request and "views" in request
    assert "hermes-diagram-review" in diagram.REVIEW_REQUEST
    assert "read-only" in diagram.REVIEW_REQUEST
    for request in (diagram.SPEC_REQUEST, diagram.FIX_REQUEST, diagram.CLAUDE_FIX_REQUEST):
        assert "30 nodes and 50 edges" in request and "40 characters" in request
        assert "summary must not repeat the commit hash or repository path" in request
    for request in (diagram.FIX_REQUEST, diagram.CLAUDE_FIX_REQUEST):
        assert "merging or dropping minor items" in request and '"resolves"' in request
    assert "Bash is not available" in diagram.CLAUDE_FIX_REQUEST
    assert "Read tool" in diagram.CLAUDE_FIX_REQUEST
    assert "Read" in diagram.REVIEW_REQUEST and "already shows" in diagram.REVIEW_REQUEST


def test_valid_spec_checks_commit_not_working_tree(diagram_repo, diagram_spec):
    sha = commit(diagram_repo)
    (diagram_repo / "app.py").unlink()
    valid, problems = diagram.validate_spec(diagram_spec, diagram_repo, sha)
    assert not problems
    assert all(not n["evidence_invalid"] for n in valid["views"][0]["nodes"])
    assert "evidence_invalid" not in diagram_spec["views"][0]["nodes"][0]


@pytest.mark.parametrize("change", [
    lambda s: s.update(version=True),
    lambda s: s.update(summary="x" * 601),
    lambda s: s.update(title=3),
    lambda s: s.update(unknowns=[3]),
    lambda s: s.update(views=[]),
    lambda s: s.update(views=s["views"] * 7),
    lambda s: s["views"][0].update(kind="other"),
    lambda s: s["views"][0].update(nodes="bad"),
    lambda s: s["views"][0]["nodes"][0].update(id="../bad"),
    lambda s: s["views"][0]["nodes"][0].update(id="x" * 41),
    lambda s: s["views"][0]["nodes"][0].update(label="x" * 81),
    lambda s: s["views"][0]["nodes"][0].update(type="wrong"),
    lambda s: s["views"][0]["nodes"][0].update(group="absent"),
    lambda s: s["views"][0]["nodes"][0].update(note="x" * 201),
    lambda s: s["views"][0]["nodes"][0].update(evidence="bad"),
    lambda s: s["views"][0]["edges"][0].update(id="n0"),
    lambda s: s["views"][0]["edges"][0].update(to="absent"),
    lambda s: s["views"][0]["edges"][0].update(type="wrong"),
    lambda s: s["views"][0].update(groups=[{"id": "n0", "label": "group"}]),
    lambda s: s["views"][0].update(kind="flow"),
    lambda s: s["views"][0].update(kind="sequence"),
    lambda s: s["views"][0]["edges"][0].update(order=True),
    lambda s: s["views"][0].update(nodes=s["views"][0]["nodes"] * 16),
    lambda s: s["views"][0].update(edges=s["views"][0]["edges"] * 121),
])
def test_structural_errors_reject(change, diagram_repo, diagram_spec):
    change(diagram_spec)
    with pytest.raises(diagram.DiagramError):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


@pytest.mark.parametrize("evidence", [[], [{"path": "missing.py", "lines": [1, 1]}],
    [{"path": "../app.py", "lines": [1, 1]}], [{"path": "/app.py", "lines": [1, 1]}],
    [{"path": "app.py", "lines": [0, 1]}], [{"path": "app.py", "lines": [2, 1]}],
    [{"path": "app.py", "lines": [1, 5]}]])
def test_bad_evidence_at_twenty_percent_kept(evidence, diagram_repo, diagram_spec):
    diagram_spec["views"][0]["nodes"][0]["evidence"] = evidence
    valid, problems = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert valid["views"][0]["nodes"][0]["evidence_invalid"] is True
    assert problems


def test_above_twenty_percent_rejected(diagram_repo, diagram_spec):
    for node in diagram_spec["views"][0]["nodes"][:2]:
        node["evidence"] = []
    with pytest.raises(diagram.DiagramError, match="20%"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


def test_evidence_counts_cached_across_views(monkeypatch, diagram_repo, diagram_spec):
    second = copy.deepcopy(diagram_spec["views"][0])
    second["kind"] = "flow"
    second["edges"][0]["order"] = 1
    diagram_spec["views"].append(second)
    original = diagram.subprocess.run
    calls = []
    def spy(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(diagram.subprocess, "run", spy)
    diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    git_reads = [args for args in calls if "cat-file" in args or "show" in args]
    assert git_reads == [["git", "-C", str(diagram_repo), "cat-file", "blob",
                          commit(diagram_repo) + ":app.py"]]


@pytest.mark.parametrize("note", [201 * "x", 42, {}, None])
def test_edge_note_is_checked(note, diagram_repo, diagram_spec):
    diagram_spec["views"][0]["edges"][0]["note"] = note
    with pytest.raises(diagram.DiagramError, match="note.*string <= 200"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


def test_payload_keeps_only_fields_for_each_item_type(diagram_repo, diagram_spec):
    view = diagram_spec["views"][0]
    view["groups"] = [{"id": "core", "label": "Core", "note": "discard", "evidence": []}]
    node = view["nodes"][0]
    node.update(group="core", note="node note", order=1, **{"from": "n1", "to": "n2"},
                verdict="confirmed", review_note="forged", claude_edited=True, arbitrary="discard")
    edge = view["edges"][0]
    edge.update(group="core", note="edge note", order=2, verdict="confirmed", arbitrary="discard")
    expected_node = {k: copy.deepcopy(node[k]) for k in ("id", "label", "group", "type", "evidence", "note")}
    expected_edge = {k: copy.deepcopy(edge[k]) for k in ("id", "from", "to", "label", "type", "order", "evidence", "note")}
    payload = diagram.spec_payload(diagram_spec)
    assert payload["views"][0]["nodes"][0] == expected_node
    assert payload["views"][0]["edges"][0] == expected_edge
    assert payload["views"][0]["groups"] == [{"id": "core", "label": "Core"}]
    valid, _ = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert valid["views"][0]["nodes"][0] == {**expected_node, "evidence_invalid": False}
    assert valid["views"][0]["edges"][0] == {**expected_edge, "evidence_invalid": False}


@pytest.mark.parametrize("order", [True, False, None, "1", 1.5, {}, []])
def test_non_integer_edge_order_rejected(order, diagram_repo, diagram_spec):
    diagram_spec["views"][0]["edges"][0]["order"] = order
    with pytest.raises(diagram.DiagramError, match="order must be an integer"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


@pytest.mark.parametrize("collection", ["nodes", "edges"])
def test_evidence_entries_capped_at_five(collection, diagram_repo, diagram_spec):
    item = diagram_spec["views"][0][collection][0]
    item["evidence"] = [{"path": "app.py", "lines": [1, 2]}] * 5
    diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    item["evidence"].append({"path": "app.py", "lines": [1, 2]})
    with pytest.raises(diagram.DiagramError, match="evidence.*5"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


def test_line_counts_cached_across_validations(monkeypatch, diagram_repo, diagram_spec):
    sha = commit(diagram_repo)
    original = diagram.subprocess.run
    calls = []
    def spy(args, **kwargs):
        calls.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(diagram.subprocess, "run", spy)
    cache = {}
    for _ in range(3):
        diagram.validate_spec(diagram_spec, diagram_repo, sha, line_counts=cache)
    assert calls == [["git", "-C", str(diagram_repo), "cat-file", "blob", sha + ":app.py"]]


@pytest.mark.parametrize("error", [OSError("unreadable"), ValueError("bad config"), RuntimeError("config error")])
def test_url_config_read_error_uses_default(monkeypatch, error):
    from hermes_cli import config
    def broken():
        raise error
    monkeypatch.setattr(config, "load_config_readonly", broken)
    assert diagram.diagram_url("d-20261014-123456") == (
        diagram.DEFAULT_DASHBOARD_URL + "/engines?diagram=d-20261014-123456")


def test_merge_review_and_counts(diagram_spec):
    record = copy.deepcopy(diagram_spec)
    review = {"views": [{"kind": "module", "items": [
        {"id": "n0", "verdict": "confirmed", "note": "checked", "evidence": []},
        {"id": "e1", "verdict": "wrong", "note": "reverse", "evidence": []},
        {"id": "ghost", "verdict": "confirmed", "note": "", "evidence": []}],
        "missing": [{"label": "Store", "evidence": []}]}]}
    diagram.merge_review(record, review)
    assert record["views"][0]["nodes"][0]["verdict"] == "confirmed"
    assert record["views"][0]["nodes"][1]["verdict"] == "unreviewed"
    assert record["views"][0]["edges"][0]["review_note"] == "reverse"
    assert record["review_unknown_ids"] == ["ghost"]
    assert record["counts"]["wrong"] == 1 and record["counts"]["missing"] == 1
    assert not diagram.is_clean(record)
    diagram.merge_review(record, {"views": []})
    assert diagram.is_clean(record)
    assert record["counts"]["unreviewed"] == 5


def test_storage_newest_and_malformed_ids(monkeypatch, tmp_path, diagram_spec):
    directory = tmp_path / "control"
    monkeypatch.setattr(diagram, "run_log_dir", lambda: directory)
    for i in range(2):
        diagram.save_diagram({"id": f"d-20261014-00000{i}", "created_at": i,
                              "views": [], "stages": []})
    (directory / "diagrams" / "d-20261014-abcdef.json").write_text("broken")
    rows = diagram.list_diagrams()
    assert [r["id"] for r in rows] == ["d-20261014-000001", "d-20261014-000000"]
    assert "views" not in rows[0] and "stages" not in rows[0]
    assert diagram.load_diagram(rows[0]["id"])["views"] == []
    assert diagram.load_diagram("d-20261014-ffffff") is None
    legacy = copy.deepcopy(diagram_spec)
    legacy.update(id="d-20261014-111111", created_at=2, stages=[])
    legacy["views"][0]["missing"] = [{"label": "Legacy omission", "evidence": []}]
    diagram.update_counts(legacy)
    diagram.save_diagram(legacy)
    loaded = diagram.load_diagram(legacy["id"])
    assert loaded == legacy
    assert "id" not in loaded["views"][0]["missing"][0]
    assert "status" not in loaded["views"][0]["missing"][0]
    assert not diagram.is_clean(loaded)
    diagram.update_counts(loaded)
    assert loaded["counts"]["missing"] == 1 and loaded["counts"]["missing_settled"] == 0
    assert diagram.list_diagrams()[0]["counts"] == loaded["counts"]
    def touched():
        pytest.fail("invalid ids must not touch filesystem")
    monkeypatch.setattr(diagram, "run_log_dir", touched)
    for bad in ("../secret", "d-20261014-ABCDEF", "d-1-a", "", None):
        assert diagram.load_diagram(bad) is None


@pytest.mark.parametrize("setting, expected", [
    ("https://dash.example/", "https://dash.example"),
    (None, "http://127.0.0.1:9119"), (3, "http://127.0.0.1:9119"),
    ("javascript:alert(1)", "http://127.0.0.1:9119"), ("https://", "http://127.0.0.1:9119")])
def test_url_setting(setting, expected, _isolated_hermes_home):
    import yaml
    (_isolated_hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"entries": {
        "hermes_control": {"settings": {"dashboard_url": setting}}}}}))
    assert diagram.diagram_url("d-20261014-123456") == expected + "/engines?diagram=d-20261014-123456"


def test_stale(diagram_repo):
    record = {"repo": str(diagram_repo), "commit": commit(diagram_repo)}
    assert diagram.is_stale(record) is False
    record["commit"] = "other"
    assert diagram.is_stale(record) is True
    record["repo"] = str(diagram_repo / "gone")
    assert diagram.is_stale(record) is None


def test_validate_enforces_json_size(diagram_repo, diagram_spec):
    diagram_spec["unknowns"] = ["x" * (200 * 1024)]
    with pytest.raises(diagram.DiagramError, match="200"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


def test_directory_cannot_be_file_evidence(diagram_repo, diagram_spec):
    directory = diagram_repo / "package"
    directory.mkdir()
    (directory / "file.py").write_text("hello\n")
    subprocess.run(["git", "-C", str(diagram_repo), "add", "package"], check=True)
    subprocess.run(["git", "-C", str(diagram_repo), "-c", "user.name=Test", "-c",
                    "user.email=test@example.org", "commit", "-qm", "directory"], check=True)
    diagram_spec["views"][0]["nodes"][0]["evidence"] = [{"path": "package", "lines": [1, 1]}]
    valid, problems = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert valid["views"][0]["nodes"][0]["evidence_invalid"] and problems


def test_atomic_storage_replace(monkeypatch, tmp_path):
    monkeypatch.setattr(diagram, "run_log_dir", lambda: tmp_path)
    record = {"id": "d-20261014-123456", "created_at": 1, "title": "before"}
    diagram.save_diagram(record)
    original = diagram.os.replace
    def spy(source, destination):
        assert diagram.load_diagram(record["id"])["title"] == "before"
        assert destination.parent == tmp_path / "diagrams"
        assert diagram.Path(source).parent == destination.parent
        assert json.loads(diagram.Path(source).read_text())["title"] == "after"
        original(source, destination)
    monkeypatch.setattr(diagram.os, "replace", spy)
    record["title"] = "after"
    diagram.save_diagram(record)
    assert diagram.load_diagram(record["id"])["title"] == "after"
    assert not list((tmp_path / "diagrams").glob("*.tmp"))


@pytest.mark.parametrize("invalid", [None, {"views": {}}, {"views": [None]},
    {"views": [{"kind": "module", "items": "bad", "missing": []}]},
    {"views": [{"kind": "module", "items": [{"id": "n0", "verdict": "unreviewed", "note": "", "evidence": []}], "missing": []}]}])
def test_invalid_review_leaves_record_intact(invalid, diagram_spec):
    before = copy.deepcopy(diagram_spec)
    with pytest.raises(diagram.DiagramError):
        diagram.merge_review(diagram_spec, invalid)
    assert diagram_spec == before


def test_long_missing_label_is_cut_not_rejected(diagram_spec):
    # A real claude review (2026-10-14) wrote omissions as sentences longer than the
    # 80-character label limit; rejecting the whole review threw away 185 verdicts.
    record = copy.deepcopy(diagram_spec)
    long_label = "The landing step that verifies the commit in a detached checkout " * 3
    diagram.merge_review(record, {"views": [{"kind": "module", "items": [
        {"id": "n0", "verdict": "confirmed", "note": "checked", "evidence": []}],
        "missing": [{"label": long_label, "note": "see landing.py", "evidence": []}]}]})
    missing = record["views"][0]["missing"][0]
    assert len(missing["label"]) <= 80 and missing["label"].endswith("…")
    assert long_label.strip() in missing["note"] and "see landing.py" in missing["note"]
    assert record["views"][0]["nodes"][0]["verdict"] == "confirmed"
    assert "label <= 80 characters" in diagram.REVIEW_REQUEST


def test_groups_and_ordered_views(diagram_repo, diagram_spec):
    view = diagram_spec["views"][0]
    view["groups"] = [{"id": "core", "label": "Core"}]
    view["nodes"][0]["group"] = "core"
    view["kind"] = "sequence"
    view["edges"][0]["order"] = 1
    valid, problems = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert not problems and valid["views"][0]["edges"][0]["order"] == 1


def test_verdicts_are_scoped_to_views(diagram_spec):
    record = copy.deepcopy(diagram_spec)
    other = copy.deepcopy(record["views"][0])
    other["kind"] = "flow"
    record["views"].append(other)
    diagram.merge_review(record, {"views": [{"kind": "flow", "items": [
        {"id": "n0", "verdict": "confirmed", "note": "flow only", "evidence": []}], "missing": []}]})
    assert record["views"][0]["nodes"][0]["verdict"] == "unreviewed"
    assert record["views"][1]["nodes"][0]["verdict"] == "confirmed"


@pytest.mark.parametrize("parser", [diagram.parse_spec, diagram.parse_review])
def test_parsers_reject_non_json_numbers(parser):
    info = "hermes-diagram-review" if parser is diagram.parse_review else "hermes-diagram"
    for literal in ("NaN", "Infinity", "-Infinity"):
        assert parser(f'```{info}\n{{"value": {literal}}}\n```') is None


def test_view_title_has_no_label_limit(diagram_repo, diagram_spec):
    diagram_spec["views"][0]["title"] = "Title " * 20
    valid, problems = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert not problems and valid["views"][0]["title"] == "Title " * 20


@pytest.mark.parametrize("collection,limit", [("nodes", 30), ("edges", 50)])
def test_readable_size_limits(collection, limit, diagram_repo, diagram_spec):
    view = diagram_spec["views"][0]
    prototype = view[collection][0]
    view[collection] = [{**copy.deepcopy(prototype), "id": f"{collection}{i}"}
                        for i in range(limit)]
    if collection == "nodes":
        view["edges"] = []
    diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    view[collection].append({**copy.deepcopy(prototype), "id": "over"})
    with pytest.raises(diagram.DiagramError, match=f"limit {limit}"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    diagram_spec.update(id="d-20261014-123456", created_at=1)
    diagram.save_diagram(diagram_spec)
    assert diagram.load_diagram(diagram_spec["id"]) == diagram_spec
    assert diagram.list_diagrams()[0]["id"] == diagram_spec["id"]


@pytest.mark.parametrize("collection", ["nodes", "edges", "groups"])
def test_label_caps(collection, diagram_repo, diagram_spec):
    if collection == "groups":
        diagram_spec["views"][0]["groups"] = [{"id": "g", "label": "x" * 40}]
    item = diagram_spec["views"][0][collection][0]
    item["label"] = "x" * 40
    diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    item["label"] += "x"
    with pytest.raises(diagram.DiagramError, match="40 characters"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


@pytest.mark.parametrize("resolves", [None, {}, [None], [{}],
    [{"view": "module", "missing": "missing-1", "by": []}],
    [{"view": "module", "missing": "missing-1", "by": [3]}],
    [{"view": "module", "missing": "missing-1", "dismiss": "x" * 201}],
    [{"view": "module", "missing": "missing-1", "by": ["n0"], "dismiss": "reason"}],
    [{"view": "other", "missing": "missing-1", "by": ["n0"]}]])
def test_invalid_resolves_rejected(resolves, diagram_repo, diagram_spec):
    diagram_spec["resolves"] = resolves
    with pytest.raises(diagram.DiagramError, match="resolves"):
        diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))


def test_resolves_survive_validation(diagram_repo, diagram_spec):
    diagram_spec["resolves"] = [
        {"view": "module", "missing": "missing-1", "by": ["n0"]},
        {"view": "module", "missing": "missing-2", "dismiss": "Covered already"}]
    valid, _ = diagram.validate_spec(diagram_spec, diagram_repo, commit(diagram_repo))
    assert valid["resolves"] == diagram_spec["resolves"]


def test_append_review_preserves_verdicts_and_assigns_missing_ids(diagram_spec):
    view = diagram_spec["views"][0]
    view["nodes"][0]["id"] = "missing-1"
    view["edges"][0]["from"] = "missing-1"
    first = {"views": [{"kind": "module", "items": [{"id": "missing-1",
        "verdict": "wrong", "note": "keep me", "evidence": []}],
        "missing": [{"label": "Store", "evidence": []}]}]}
    diagram.merge_review(diagram_spec, first)
    assert view["missing"] == [{"id": "missing-2", "label": "Store", "note": "",
                                "evidence": [], "status": "open"}]
    rest = copy.deepcopy(first)
    rest["views"][0]["items"][0].update(verdict="confirmed", note="overwrite")
    rest["views"][0]["items"].append({"id": "n1", "verdict": "confirmed",
                                     "note": "new", "evidence": []})
    rest["views"][0]["missing"].append({"label": "Queue", "note": "absent", "evidence": []})
    diagram.merge_review(diagram_spec, rest, append=True)
    assert view["nodes"][0]["review_note"] == "keep me"
    assert view["nodes"][1]["verdict"] == "confirmed"
    assert [m["id"] for m in view["missing"]] == ["missing-2", "missing-3"]
    view["nodes"][0]["verdict"] = "confirmed"
    view["missing"][0].update(status="resolved", by=["n1"])
    view["missing"][1].update(status="dismissed", reason="Covered")
    diagram.update_counts(diagram_spec)
    assert diagram_spec["counts"]["missing"] == 0
    assert diagram_spec["counts"]["missing_settled"] == 2
    assert diagram.is_clean(diagram_spec)


@pytest.mark.parametrize("status", [None, "", "pending", "open", "resolved", "dismissed"])
def test_only_resolved_or_dismissed_missing_entries_are_settled(status):
    missing = {} if status is None else {"status": status}
    record = {"views": [{"nodes": [], "edges": [], "missing": [missing]}]}
    settled = status in ("resolved", "dismissed")
    diagram.update_counts(record)
    assert record["counts"]["missing"] == int(not settled)
    assert record["counts"]["missing_settled"] == int(settled)
    assert diagram.is_clean(record) == settled
