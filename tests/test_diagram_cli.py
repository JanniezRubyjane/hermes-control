import argparse
import json

import pytest

from hermes_control import diagram_pipeline, watch


def parser():
    result = argparse.ArgumentParser()
    watch.setup_cli(result)
    return result


@pytest.mark.parametrize("status, code", [("done", 0), ("failed", 1)])
@pytest.mark.parametrize("as_json", [False, True])
def test_diagram_cli(monkeypatch, capsys, diagram_repo, status, code, as_json):
    def run(repo, ask, kinds, **kwargs):
        assert repo == str(diagram_repo) and ask == "design" and kinds == ["module", "flow"]
        assert kwargs["codex_model"] == "c" and kwargs["claude_model"] == "a"
        task = {"id": "d-20261014-123456-spec", "engine": "codex", "goal": "read",
                "policy": "read-only", "why": "diagram pipeline: spec"}
        kwargs["dispatch_fn"]([task], parent_cwd=repo)
        return {"title": "App", "summary": "The design", "counts": {"confirmed": 2},
                "status": status, "reason": "reason", "url": "https://dash/engines?diagram=x"}
    monkeypatch.setattr(diagram_pipeline, "run", run)
    from hermes_control import dispatch
    monkeypatch.setattr(dispatch, "dispatch", lambda *a, **k: {"tasks": [{"ok": True}]})
    args = ["diagram", "--repo", str(diagram_repo), "--ask", "design", "--kind", "module",
            "--kind", "flow", "--codex-model", "c", "--claude-model", "a"]
    assert watch.run_cli(parser().parse_args(args + (["--json"] if as_json else []))) == code
    captured = capsys.readouterr()
    assert "spec started" in captured.err and "spec finished" in captured.err
    if as_json:
        assert json.loads(captured.out)["status"] == status
    else:
        assert all(t in captured.out for t in ("App", "The design", "confirmed", status, "reason", "https://dash"))


def test_cli_default_cwd(monkeypatch, diagram_repo, capsys):
    monkeypatch.chdir(diagram_repo)
    def run(repo, ask, kinds, **kwargs):
        assert repo == str(diagram_repo) and kinds is None
        return {"status": "done", "title": "", "summary": "", "counts": {}, "reason": "", "url": ""}
    monkeypatch.setattr(diagram_pipeline, "run", run)
    assert watch.run_cli(parser().parse_args(["diagram", "--ask", "design"])) == 0


@pytest.mark.parametrize("args", [["diagram"], ["diagram", "--ask", "x", "--kind", "bad"]])
def test_cli_bad_arguments(args):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(args)
    assert exc.value.code == 2


def test_cli_bad_repo(tmp_path, capsys):
    assert watch.run_cli(parser().parse_args(["diagram", "--repo", str(tmp_path), "--ask", "design"])) == 2
    assert "git work tree" in capsys.readouterr().err
