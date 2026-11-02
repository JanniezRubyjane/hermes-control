"""Offline validation of the benchmark's JSON boundary."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from bench.spec import KINDS, TaskSpec, load_spec, load_suite


def spec_data(**overrides):
    return {"id": "bug-1", "source": "S", "kind": "bug", "policy": "write",
            "goal": "Fix pricing", "verify": "python -m unittest",
            "hidden_dir": "../hidden", "hidden_cmd": "python hidden_test.py",
            "protected_paths": ["tests/test_shop.py"], "repo": {"builder": "shop"},
            **overrides}


def test_load_and_resolve_paths(tmp_path):
    path = tmp_path / "task.json"
    path.write_text(json.dumps(spec_data(expected_answer=["A", "B"], secret_marker="planted")))
    spec = load_spec(path)
    assert isinstance(spec, TaskSpec)
    assert spec.hidden_dir == (tmp_path.parent / "hidden").resolve()
    assert spec.expected_answer == ["A", "B"]
    assert spec.secret_marker == "planted"
    assert TaskSpec.load(path) == spec
    assert spec.request == "Use Hermes-Control to: Fix pricing"


def test_explicit_planner_request():
    assert TaskSpec.from_dict(spec_data(request="Fix the shop using hc")).request == "Fix the shop using hc"
    for request in ("", "  ", False, 42):
        with pytest.raises(ValueError, match="request"):
            TaskSpec.from_dict(spec_data(request=request))


@pytest.mark.parametrize("field,value", [
    ("id", "../escape"), ("id", "a" * 65), ("id", ""), ("id", 4),
    ("source", "synthetic"), ("source", []), ("kind", "unknown"), ("kind", None),
    ("policy", "rw"), ("goal", "  "), ("goal", False), ("verify", 2),
    ("verify", ""), ("hidden_dir", ""), ("hidden_dir", []),
    ("hidden_cmd", ""), ("hidden_cmd", None), ("protected_paths", "test.py"),
    ("protected_paths", ["../outside"]), ("protected_paths", ["/abs"]),
    ("protected_paths", [".git/config"]), ("protected_paths", [False]),
    ("expected_answer", "answer"), ("expected_answer", [1]),
    ("answer_prefix", ""), ("answer_prefix", "  "), ("answer_prefix", "ANSWER"),
    ("answer_prefix", False), ("answer_prefix", 42),
    ("answer_forbidden", "availability"), ("answer_forbidden", [1]),
    ("answer_forbidden", None), ("answer_casefold", "true"), ("answer_casefold", 1),
    ("verify_note", ""), ("verify_note", False),
    ("secret_marker", ""), ("secret_marker", False),
    ("allowed_config", "pyproject.toml"), ("allowed_config", None),
    ("allowed_config", [False]), ("allowed_config", ["../conftest.py"]),
    ("allowed_config", ["/pytest.ini"]), ("allowed_config", [".git/config"]),
    ("allowed_config", ["./conftest.py"]), ("allowed_config", ["tests//conftest.py"]),
    ("allowed_config", ["tests\\conftest.py"]), ("allowed_config", ["."]),
    ("allowed_config", [""]),
    ("repo", {}), ("repo", []), ("repo", {"builder": ""}),
    ("repo", {"builder": "shop", "origin_repo": "a"}),
    ("repo", {"base_commit": "-bad", "origin_repo": "a"}),
])
def test_invalid_fields_name_the_field(field, value):
    with pytest.raises(ValueError, match=field):
        TaskSpec.from_dict(spec_data(**{field: value}))


def test_required_unknown_and_json_errors(tmp_path):
    data = spec_data()
    del data["goal"]
    with pytest.raises(ValueError, match="goal"):
        TaskSpec.from_dict(data)
    with pytest.raises(ValueError, match="surprise"):
        TaskSpec.from_dict(spec_data(surprise=True))
    path = tmp_path / "broken.json"
    path.write_text("{")
    with pytest.raises(ValueError, match="spec"):
        load_spec(path)


def test_readonly_replay_and_all_kinds(tmp_path):
    for kind in KINDS:
        spec = TaskSpec.from_dict(spec_data(kind=kind, policy="read-only", verify=""))
        assert spec.kind == kind
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(spec_data(source="R", repo={"base_commit": "abc", "origin_repo": "repo"})))
    assert load_spec(path).repo["origin_repo"] == str(tmp_path / "repo")
    path.write_text(json.dumps(spec_data(source="R", repo={"base_commit": "abc", "origin_repo": ""})))
    with pytest.raises(ValueError, match="repo.origin_repo"):
        load_spec(path)


def test_suite_sorted_by_id_and_duplicates_rejected(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(spec_data(id="z")))
    (tmp_path / "z.json").write_text(json.dumps(spec_data(id="a")))
    (tmp_path / "ignored.txt").write_text("not a spec")
    assert [s.id for s in load_suite(tmp_path)] == ["a", "z"]
    (tmp_path / "duplicate.json").write_text(json.dumps(spec_data(id="a")))
    with pytest.raises(ValueError, match="id.*duplicate"):
        load_suite(tmp_path)
    with pytest.raises(ValueError, match="suite"):
        load_suite(tmp_path / "missing")


def test_allowed_configuration_paths():
    assert TaskSpec.from_dict(spec_data()).allowed_config == []
    paths = ["pyproject.toml", "tests/conftest.py"]
    assert TaskSpec.from_dict(spec_data(allowed_config=paths)).allowed_config == paths


def test_answer_options_and_defaults(tmp_path):
    default = TaskSpec.from_dict(spec_data())
    assert default.answer_prefix is None
    assert default.answer_forbidden == [] and default.answer_casefold is False
    path = tmp_path / "answer.json"
    path.write_text(json.dumps(spec_data(answer_prefix="FINDINGS:",
        answer_forbidden=["availability"], answer_casefold=True,
        verify_note="No visible tests at base.")))
    spec = load_spec(path)
    assert spec.answer_prefix == "FINDINGS:"
    assert spec.answer_forbidden == ["availability"] and spec.answer_casefold is True
    assert spec.verify_note == "No visible tests at base."


def test_suite_loads_per_task_spec_directories(tmp_path):
    # Suite S and replay R store each task as <id>/spec.json; a top-level-only glob ran zero tasks.
    for task_id in ("s02", "s01"):
        (tmp_path / task_id).mkdir()
        (tmp_path / task_id / "spec.json").write_text(json.dumps(spec_data(id=task_id)))
    assert [s.id for s in load_suite(tmp_path)] == ["s01", "s02"]


def test_empty_suite_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="no task specs"):
        load_suite(tmp_path)


def test_real_suite_s_loads_every_task():
    suite = Path(__file__).resolve().parents[1] / "scripts" / "bench" / "suites" / "S"
    assert len(load_suite(suite)) == len([p for p in suite.iterdir() if (p / "spec.json").is_file()]) >= 7


def test_replay_index_json_is_not_a_task(tmp_path):
    (tmp_path / "index.json").write_text("[]")
    (tmp_path / "r-1").mkdir()
    (tmp_path / "r-1" / "spec.json").write_text(json.dumps(spec_data(id="r-1")))
    assert [s.id for s in load_suite(tmp_path)] == ["r-1"]
