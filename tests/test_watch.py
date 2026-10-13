"""`hermes control watch`: the newest run, live, in a terminal."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from hermes_control import limits, watch
from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog

T0 = 1_790_000_000.0
PLAN = {"tasks": [{"id": "add-sub", "engine": "claude", "why": "user pinned", "policy": "write",
                   "depends_on": [], "verify": "python -m unittest -q", "model": "sonnet",
                   "effort": "low", "rerouted": None},
                  {"id": "docs", "engine": "codex", "why": "small edit", "policy": "write",
                   "depends_on": ["add-sub"], "verify": None, "model": None, "effort": None,
                   "rerouted": None}],
        "repo": "/work/app", "planner": {"provider": "anthropic", "model": "claude-fable-5-1",
                                         "session_id": "S-1"}}


def _write(run_id, *events, at=T0):
    log = RunLog(run_id)
    for offset, (task_id, engine, kind, payload) in enumerate(events):
        log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id=run_id, task_id=task_id,
                         engine=engine, start_seq=log.next_seq(task_id), now=at + offset))
    return log.path


RUNNING = (("", "", "plan", PLAN),
           ("add-sub", "codex", "rerouted", {"from": "claude", "reason": "claude is at 90% of its 5-hour limit (cap 80%)"}),
           ("add-sub", "codex", "session", {"session_id": "T", "model": "gpt-5.6-terra"}),
           ("add-sub", "codex", "tool_start", {"tool_id": "1", "name": "command_execution",
                                               "input": {"command": "rg -n def calc.py"}}),
           ("add-sub", "codex", "tool_end", {"tool_id": "1", "is_error": False, "content": "..."}),
           ("add-sub", "codex", "tool_start", {"tool_id": "2", "name": "file_change", "input": {}}))

FINISHED = (("add-sub", "codex", "text", {"text": "Added sub and its test."}),
            ("add-sub", "codex", "result", {"ok": True, "usage": {"output_tokens": 546}}),
            ("add-sub", "codex", "commit", {"sha": "7eff318abc", "branch": "hermes-subagent/x-add-sub"}),
            ("add-sub", "codex", "verify", {"command": "python -m unittest -q", "passed": True,
                                             "exit_code": 0, "output_tail": "OK", "unproven_reason": None}),
            ("docs", "codex", "paused", {"id": "r-docs", "reason": "codex is at 95% of its 30-day limit (cap 80%)",
                                         "resets_at": T0 + 7200, "worktree": None}),
            ("", "", "summary", {"tasks": [
                {"task_id": "add-sub", "engine": "codex", "ok": True, "commit": "7eff318abc",
                 "branch": "hermes-subagent/x-add-sub", "worktree": "/work/app/.worktrees/x",
                 "unproven_reason": None, "rerouted": None, "paused_id": None},
                {"task_id": "docs", "engine": "codex", "ok": False, "commit": "", "branch": "",
                 "worktree": "", "unproven_reason": "paused: ...", "rerouted": None,
                 "paused_id": "r-docs"}]}))


def test_a_running_run_shows_who_is_working_and_on_what(monkeypatch):
    _write("r1", *RUNNING)

    text = watch.frame_text(now=T0 + 10, usage=False)

    assert "run r1" in text and "running" in text and " app " in text
    assert "claude-fable-5-1" in text and "hermes chat --resume S-1" in text
    assert "moved from claude: claude is at 90%" in text
    assert "codex · gpt-5.6-terra" in text and "working" in text
    assert "rg -n def calc.py" in text  # the step it is on
    assert "waiting" in text  # docs has not started


def test_a_paused_run_shows_where_each_task_landed():
    _write("r2", *RUNNING, *FINISHED)

    text = watch.frame_text(now=T0 + 60, usage=False)

    assert "run r2 · paused" in text
    assert "committed 7eff318 · check passed (exit 0)" in text
    assert "git merge --no-ff hermes-subagent/x-add-sub" in text
    assert "paused: codex is at 95% of its 30-day limit" in text
    assert "Resume" in text


def test_a_finished_workers_long_last_message_is_cut_to_a_couple_of_lines():
    long_message = "Changes are complete. " * 40
    _write("r5", *RUNNING, ("add-sub", "codex", "text", {"text": long_message}), *FINISHED[1:])

    text = watch.frame_text(now=T0 + 60, usage=False)
    said = " ".join(text[text.index("said:"):text.index("✓ committed")].split())

    assert len(said) < 220 and said.endswith("…")


def test_it_follows_the_newest_run_unless_given_one():
    old = _write("old1", *RUNNING, *FINISHED)
    new = _write("new2", ("", "", "plan", PLAN))
    os.utime(old, (T0, T0))
    os.utime(new, (T0 + 100, T0 + 100))

    assert "run new2" in watch.frame_text(now=T0 + 120, usage=False)
    assert "run old1" in watch.frame_text("old1", now=T0 + 120, usage=False)
    assert "No run nope" in watch.frame_text("nope", now=T0 + 120, usage=False)


def test_with_no_runs_it_says_it_is_waiting_for_one():
    assert "No runs yet" in watch.frame_text(now=T0, usage=False)


def test_the_usage_line_shows_each_engine_against_its_cap(monkeypatch):
    reading = limits.Usage(engine="claude", windows=(limits.Window("5-hour", 44.0, None),))
    monkeypatch.setitem(limits._FETCHERS, "claude", lambda refresh=False: reading)
    _write("r3", ("", "", "plan", PLAN))

    text = watch.frame_text(now=T0, usage=True)

    assert "claude 5-hour 44% (cap 80%)" in text
    assert "codex usage unknown" in text


def test_the_usage_line_shows_unknown_window_without_crashing(monkeypatch):
    reading = limits.Usage(engine="codex", windows=(
        limits.Window("5-hour", None, T0 + 60),
        limits.Window("weekly", 12.0, T0 + 3600)))
    monkeypatch.setitem(limits._FETCHERS, "codex", lambda refresh=False: reading)

    text = watch.frame_text(now=T0, usage=True)

    assert "codex 5-hour unknown · weekly 12% (cap 80%)" in text


# --- the command ---------------------------------------------------------------------

def _parser():
    parser = argparse.ArgumentParser(prog="hermes control")
    watch.setup_cli(parser)
    return parser


def test_watch_once_prints_one_frame_and_exits(capsys):
    _write("r4", *RUNNING, *FINISHED)

    code = watch.run_cli(_parser().parse_args(["watch", "--once", "--no-usage"]))

    assert code == 0 and "run r4" in capsys.readouterr().out


def test_stats_prints_attempt_rows_and_totals(capsys):
    _write("stats", RUNNING[0],
           ("add-sub", "claude", "session", {"session_id": "first", "model": "sonnet"}),
           ("add-sub", "claude", "result", {"usage": {"input_tokens": 100}}),
           *RUNNING[1:], FINISHED[0],
           ("add-sub", "codex", "result", {"ok": True,
                                              "usage": {"input_tokens": 200,
                                                        "cached_input_tokens": 2058100,
                                                        "output_tokens": 546}}),
           *FINISHED[2:])

    code = watch.run_cli(_parser().parse_args(["stats"]))
    output = capsys.readouterr().out

    assert code == 0
    assert "codex←claude" in output and output.count("add-sub") == 2
    assert "2058.1k" in output
    assert "2 attempts" in output and "1 moved" in output
    assert "2 tool calls" in output and "0 errors" in output
    assert "total cached 2,058,100" in output


def test_stats_json_prints_records_without_table(capsys):
    _write("stats", *RUNNING, *FINISHED)

    code = watch.run_cli(_parser().parse_args(["stats", "--limit", "1", "--json"]))
    rows = json.loads(capsys.readouterr().out)

    assert code == 0 and len(rows) == 1
    assert rows[0]["run_id"] == "stats" and rows[0]["task_id"] == "add-sub"
    assert rows[0]["engine"] == "codex" and rows[0]["moved"] is True
    assert {"planned_engine", "planned_model", "model", "effort", "policy", "how",
            "tools", "tool_errors", "seconds", "input_tokens", "cached_input_tokens",
            "output_tokens", "reasoning_tokens", "cost_usd", "status", "verify_passed",
            "final_attempt"} <= rows[0].keys()
    assert rows[0]["model_recorded"] is True


def _stats_runs():
    plan = {**PLAN, "tasks": [
        {**PLAN["tasks"][0], "id": "one", "engine": "codex", "model": "gpt-test",
         "effort": "high", "depends_on": []},
        {**PLAN["tasks"][0], "id": "two", "engine": "codex", "model": "gpt-test",
         "effort": "high", "depends_on": []},
        {**PLAN["tasks"][1], "id": "old", "depends_on": []},
    ]}
    alpha = _write("alpha", ("", "", "plan", plan),
           ("one", "codex", "session", {"model": "gpt-test", "effort": "high"}),
           ("one", "codex", "tool_start", {"tool_id": "a", "name": "read"}),
           ("one", "codex", "tool_end", {"tool_id": "a", "is_error": True}),
           ("one", "codex", "result", {"usage": {"output_tokens": 100,
                                                  "reasoning_output_tokens": 25},
                                      "total_cost_usd": 1.25}),
           ("one", "codex", "verify", {"passed": True}),
           ("two", "codex", "session", {"model": "gpt-test", "effort": "high"}),
           ("two", "codex", "tool_start", {"tool_id": "b", "name": "read"}),
           ("two", "codex", "tool_end", {"tool_id": "b", "is_error": False}),
           ("two", "codex", "result", {"usage": {"output_tokens": 300,
                                                  "reasoning_output_tokens": 75}}),
           ("two", "codex", "verify", {"passed": False}),
           ("old", "codex", "session", {}),
           ("old", "codex", "result", {"usage": {"output_tokens": 50}}))
    beta = _write("beta", ("", "", "plan", PLAN),
           ("add-sub", "claude", "session", {"model": "sonnet"}),
           ("add-sub", "claude", "result", {"usage": {"output_tokens": 20},
                                              "total_cost_usd": 0.5}))
    os.utime(beta, (T0, T0))
    os.utime(alpha, (T0 + 100, T0 + 100))


def test_stats_labels_unrecorded_model_and_shows_usd(capsys):
    _stats_runs()
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "alpha"])) == 0
    output = capsys.readouterr().out
    assert "(not recorded)" in output
    assert "usd" in output and "1.25" in output and "quota" in output
    assert "beta" not in output


def test_stats_usd_distinguishes_quota_and_shows_subcent_cost(capsys):
    plan = {**PLAN, "tasks": [{**PLAN["tasks"][0], "id": "paid"},
                              {**PLAN["tasks"][0], "id": "empty"},
                              {**PLAN["tasks"][1], "id": "quota", "depends_on": []}]}
    _write("costs", ("", "", "plan", plan),
           ("paid", "claude", "session", {"model": "sonnet"}),
           ("paid", "claude", "result", {"total_cost_usd": 0.004,
                                          "usage": {"output_tokens": 1}}),
           ("empty", "claude", "session", {"model": "sonnet"}),
           ("quota", "codex", "session", {"model": "gpt"}))
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "costs"])) == 0
    lines = capsys.readouterr().out.splitlines()
    assert any("paid" in line and "0.004" in line for line in lines)
    assert any("empty" in line and line.split("│")[-3].strip() == "-"
               for line in lines)
    assert any("quota" in line and "quota" in line.split("│")[-3]
               for line in lines), lines


def test_stats_run_filter_finds_old_runs_and_reports_unknown_ids(capsys):
    _stats_runs()
    args = _parser().parse_args(["stats", "--run", "beta", "--run", "missing",
                                "--limit", "1", "--json"])
    assert watch.run_cli(args) == 1
    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    assert len(rows) == 1 and rows[0]["run_id"] == "beta"
    assert captured.err.strip() == "no run missing"
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "missing", "--json"])) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [] and captured.err.strip() == "no run missing"


def test_stats_by_model_json_aggregates_and_combines_options(capsys):
    _stats_runs()
    args = _parser().parse_args(["stats", "--by-model", "--run", "alpha", "--limit", "1",
                                "--json"])
    assert watch.run_cli(args) == 0
    groups = json.loads(capsys.readouterr().out)
    assert groups == [
        {"engine": "codex", "model": "gpt-test", "effort": "high", "policy": "write",
         "n": 2, "proven": 1, "check_failed": 1, "moved": 0, "median_tools": 1,
         "tool_error_pct": 50.0, "median_seconds": 3, "median_output_tokens": 200,
         "reasoning_pct": 25.0},
        {"engine": "codex", "model": "(not recorded)", "effort": None, "policy": "write",
         "n": 1, "proven": 0, "check_failed": 0, "moved": 0, "median_tools": 0,
         "tool_error_pct": 0.0, "median_seconds": 1, "median_output_tokens": 50,
         "reasoning_pct": None},
    ]
    assert watch.run_cli(_parser().parse_args(["stats", "--by-model", "--run", "missing",
                                                "--json"])) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == [] and captured.err.strip() == "no run missing"


def test_stats_by_model_ignores_unrecorded_token_values(capsys):
    plan = {**PLAN, "tasks": [
        {**PLAN["tasks"][0], "id": task_id, "model": model, "depends_on": []}
        for task_id, model in (("known", "sonnet"), ("unknown", "sonnet"),
                               ("all-missing", "other"))]}
    _write("missing-tokens", ("", "", "plan", plan),
           ("known", "claude", "session", {"model": "sonnet"}),
           ("known", "claude", "result", {"usage": {"output_tokens": 100,
                                                      "reasoning_output_tokens": 25}}),
           ("unknown", "claude", "session", {"model": "sonnet"}),
           ("all-missing", "claude", "session", {"model": "other"}))
    args = _parser().parse_args(["stats", "--run", "missing-tokens", "--by-model", "--json"])
    assert watch.run_cli(args) == 0
    groups = {group["model"]: group for group in json.loads(capsys.readouterr().out)}
    assert groups["sonnet"]["median_output_tokens"] == 100
    assert groups["sonnet"]["reasoning_pct"] == 25
    assert groups["other"]["median_output_tokens"] is None
    assert groups["other"]["reasoning_pct"] is None
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "missing-tokens",
                                                "--by-model"])) == 0
    assert "-" in next(line for line in capsys.readouterr().out.splitlines() if "other" in line)


def test_stats_by_model_proof_belongs_only_to_final_attempt(capsys):
    _write("moved-proof", ("", "", "plan", PLAN),
           ("add-sub", "codex", "session", {"model": "gpt"}),
           ("add-sub", "codex", "limit", {"next": "claude"}),
           ("add-sub", "claude", "session", {"model": "sonnet"}),
           ("add-sub", "claude", "verify", {"passed": True}))
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "moved-proof",
                                                "--json"])) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(row["final_attempt"], row["verify_passed"]) for row in rows] == [
        (False, True), (True, True)]
    assert watch.run_cli(_parser().parse_args(["stats", "--run", "moved-proof",
                                                "--by-model", "--json"])) == 0
    groups = {group["engine"]: group for group in json.loads(capsys.readouterr().out)}
    assert groups["codex"]["proven"] == 0
    assert groups["claude"]["proven"] == 1


def test_stats_by_model_text_has_all_metrics(capsys):
    _stats_runs()
    assert watch.run_cli(_parser().parse_args(["stats", "--by-model", "--run", "alpha"])) == 0
    output = capsys.readouterr().out
    for label in ("engine", "model", "effort", "policy", "n", "proven", "check failed",
                  "moved", "median tools", "tool error %", "median secs", "median output",
                  "reasoning %", "(not recorded)"):
        assert label in output


def test_stats_help_lists_review_options(capsys):
    import pytest

    with pytest.raises(SystemExit) as raised:
        _parser().parse_args(["stats", "--help"])
    assert raised.value.code == 0
    output = capsys.readouterr().out
    assert "--run RUN_ID" in output and "--by-model" in output


def test_control_without_a_subcommand_prints_help(capsys):
    assert watch.run_cli(_parser().parse_args([])) == 1
    assert "watch" in capsys.readouterr().out


def test_register_adds_the_control_command():
    import hermes_control

    class Ctx:
        def __init__(self):
            self.cli = []

        def register_tool(self, **kwargs):
            return object()

        def register_cli_command(self, **kwargs):
            self.cli.append(kwargs)

    ctx = Ctx()
    hermes_control.register(ctx)

    assert [c["name"] for c in ctx.cli] == ["control"]
    assert ctx.cli[0]["handler_fn"] is watch.run_cli


def test_the_real_hermes_cli_runs_it(tmp_path):
    """Through the installed `hermes` entry point, with this repo linked into a
    fresh Hermes home, as scripts/install.sh leaves ~/.hermes."""
    home = tmp_path / "linked-home"
    (home / "plugins").mkdir(parents=True)
    (home / "plugins" / "hermes_control").symlink_to(
        Path(watch.__file__).resolve().parent, target_is_directory=True)
    (home / "config.yaml").write_text("plugins:\n  enabled:\n    - hermes_control\n", encoding="utf-8")
    # A fresh home under the system temp dir makes Hermes's launcher sync the
    # real Hermes venv first (80-100 s, past the timeout); this test is about
    # the `control` command, not Hermes's installer.
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    subprocess.run([sys.executable, "-c",
                    "from hermes_control.events import RawEvent, stamp\n"
                    "from hermes_control.runlog import RunLog\n"
                    "log = RunLog('real1')\n"
                    "log.append(stamp([RawEvent(kind='plan', payload={'tasks': []})], run_id='real1',"
                    " task_id='', engine='', start_seq=0, now=1.0))\n"],
                   env={**env, "PYTHONPATH": str(Path(watch.__file__).resolve().parents[1])},
                   check=True)
    # What the installed `hermes` script runs, on the interpreter running this suite.
    launcher = "import sys; from hermes_cli.main import main; sys.exit(main())"

    proc = subprocess.run([sys.executable, "-c", launcher, "control", "watch", "--once", "--no-usage"],
                          capture_output=True, text=True, env=env, timeout=120)

    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "run real1" in proc.stdout
