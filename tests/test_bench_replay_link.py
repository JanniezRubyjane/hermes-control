"""Replay contract detection and private suite linking use temporary repositories."""

import json
import stat

import pytest

from test_bench_replay import history, git, write
from bench import replay


@pytest.mark.parametrize("test_only", [True, False])
def test_contract_detected_per_commit(history, tmp_path, test_only):
    repo, _, _ = history
    git(repo, "checkout", "-b", "contract")
    write(repo, "tests/test_contract.py", "assert True\n")
    if not test_only:
        write(repo, "contract.py", "READY = True\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "test: interrupted-run contract")
    write(repo, "contract.py", "READY = False\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "implement contract")
    git(repo, "commit", "--allow-empty", "-m", "empty commit is not a test contract")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "contract", "-m", "Merge hermes-control task contract")
    task_id = "r-" + git(repo, "rev-parse", "HEAD")[:12]
    out = tmp_path / "extracted"
    index = replay.extract(repo, out)
    assert next(task for task in index if task["id"] == task_id)["test_contract"] is test_only
    assert all(task["test_contract"] is False for task in index if task["id"] != task_id)
    assert json.loads((out / "index.json").read_text()) == index


@pytest.fixture
def extracted(history, tmp_path):
    repo, _, _ = history
    out = tmp_path / "extracted"
    index = replay.extract(repo, out)
    index[0].update(status="valid", test_contract=False)
    index[1].update(status="valid", test_contract=True)
    index[2].update(status="invalid-red-on-merge", test_contract=True)
    (out / "index.json").write_text(json.dumps(index))
    return repo, out, index


@pytest.mark.parametrize("include", [False, True])
def test_link_filters_and_absolute_targets(extracted, tmp_path, capsys, include):
    _, out, index = extracted
    suite = tmp_path / "suite"
    args = ["link", "--out", str(out), "--to", str(suite)]
    if include:
        args.append("--include-test-contract")
    assert replay.main(args) == 0
    expected = index[:2] if include else index[:1]
    assert {p.name for p in suite.iterdir()} == {task["id"] for task in expected}
    for task in expected:
        link = suite / task["id"]
        assert link.is_symlink()
        assert link.readlink() == (out / task["id"]).resolve()
    assert stat.S_IMODE(suite.stat().st_mode) == 0o700
    assert capsys.readouterr().out.strip() == (
        f"linked: {len(expected)} skipped-test-contract: {0 if include else 1} skipped-not-valid: 1")


def test_link_refuses_nonempty_destination(extracted, tmp_path, capsys):
    _, out, _ = extracted
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "keep").write_text("leave alone")
    with pytest.raises(SystemExit) as exc:
        replay.main(["link", "--out", str(out), "--to", str(suite)])
    assert exc.value.code == 2
    assert "not empty" in capsys.readouterr().err
    assert (suite / "keep").read_text() == "leave alone"


def test_link_refuses_destination_inside_origin(extracted, capsys):
    repo, out, _ = extracted
    with pytest.raises(SystemExit) as exc:
        replay.main(["link", "--out", str(out), "--to", str(repo / "suite")])
    assert exc.value.code == 2
    assert "repository" in capsys.readouterr().err
    assert not (repo / "suite").exists()


def test_link_skips_missing_invalid_task_and_accepts_legacy_entry(extracted, tmp_path, capsys):
    _, out, index = extracted
    index[0].pop("test_contract")
    index.append({"id": "missing", "status": "invalid-index"})
    (out / "index.json").write_text(json.dumps(index))
    suite = tmp_path / "empty-suite"
    suite.mkdir(mode=0o755)
    assert replay.main(["link", "--out", str(out), "--to", str(suite)]) == 0
    assert {p.name for p in suite.iterdir()} == {index[0]["id"]}
    assert stat.S_IMODE(suite.stat().st_mode) == 0o700
    assert capsys.readouterr().out.strip() == (
        "linked: 1 skipped-test-contract: 1 skipped-not-valid: 2")


@pytest.mark.parametrize("bad_entry", [None, [], "entry", {}, {"id": 7},
                                       {"id": "bad/id"}, {"id": "bad\\id"},
                                       {"id": ""}, {"id": "."}, {"id": ".."},
                                       "duplicate"])
@pytest.mark.parametrize("existing", [False, True])
def test_link_rejects_malformed_index_before_mutation(extracted, tmp_path, capsys,
                                                     bad_entry, existing):
    _, out, index = extracted
    # Put the bad entry last so an earlier eligible entry cannot be linked first.
    index.append(dict(index[0]) if bad_entry == "duplicate" else bad_entry)
    (out / "index.json").write_text(json.dumps(index))
    suite = tmp_path / "suite"
    if existing:
        suite.mkdir(mode=0o755)
        suite.chmod(0o755)
    with pytest.raises(ValueError):
        replay.link(out, suite)
    with pytest.raises(SystemExit) as exc:
        replay.main(["link", "--out", str(out), "--to", str(suite)])
    assert exc.value.code == 2
    assert "replay:" in capsys.readouterr().err
    if existing:
        assert list(suite.iterdir()) == []
        assert stat.S_IMODE(suite.stat().st_mode) == 0o755
    else:
        assert not suite.exists()


@pytest.mark.parametrize("problem", ["symlink-inside", "symlink-outside",
                                     "missing-spec", "missing-directory", "file"])
@pytest.mark.parametrize("existing", [False, True])
def test_link_rejects_invalid_task_before_mutation(extracted, tmp_path, capsys,
                                                  problem, existing):
    _, out, index = extracted
    index[1]["test_contract"] = False
    (out / "index.json").write_text(json.dumps(index))
    task_id = index[1]["id"]
    directory = out / task_id
    if problem.startswith("symlink"):
        target = (out if problem == "symlink-inside" else tmp_path) / "moved-task"
        directory.rename(target)
        directory.symlink_to(target, target_is_directory=True)
    elif problem == "missing-spec":
        (directory / "spec.json").unlink()
    else:
        directory.rename(out / "moved-task")
        if problem == "file":
            directory.write_text("not a directory")
    suite = tmp_path / "suite"
    if existing:
        suite.mkdir(mode=0o755)
        suite.chmod(0o755)
    with pytest.raises(ValueError, match=task_id):
        replay.link(out, suite)
    with pytest.raises(SystemExit) as exc:
        replay.main(["link", "--out", str(out), "--to", str(suite)])
    assert exc.value.code == 2
    assert task_id in capsys.readouterr().err
    if existing:
        assert list(suite.iterdir()) == []
        assert stat.S_IMODE(suite.stat().st_mode) == 0o755
    else:
        assert not suite.exists()
