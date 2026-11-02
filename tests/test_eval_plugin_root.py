"""Offline branch-profile and planner-settings regression checks; no quota used."""
import logging
import sys
from pathlib import Path

import hermes_control as plugin
from hermes_control import model_policy, routing
from hermes_control.engines import registry

EVAL = Path(__file__).resolve().parents[1] / "scripts" / "eval"
sys.path.insert(0, str(EVAL))
import run as eval_run


def test_explicit_plugin_root_overrides_live_links(tmp_path):
    source = tmp_path / "source"
    branch = tmp_path / "branch"
    for name in ("hermes_control", "skills/hermes-control"):
        (branch / name).mkdir(parents=True)
    for name in ("plugins/hermes_control", "skills/software-development/hermes-control"):
        link = source / name
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(tmp_path / "live")
    alias = tmp_path / "branch-alias"
    alias.symlink_to(branch, target_is_directory=True)
    home = eval_run.create_eval_home(source, tmp_path / "out", plugin_root=alias)
    assert (home / "plugins/hermes_control").resolve() == branch / "hermes_control"
    assert (home / "skills/software-development/hermes-control").resolve() == branch / "skills/hermes-control"
    assert (source / "plugins/hermes_control").readlink() == tmp_path / "live"


def test_explicit_root_links_without_existing_install(tmp_path):
    branch = tmp_path / "branch"
    for name in ("hermes_control", "skills/hermes-control"):
        (branch / name).mkdir(parents=True)
    home = eval_run.create_eval_home(tmp_path / "source", tmp_path / "out", plugin_root=branch)
    assert (home / "plugins/hermes_control").resolve() == branch / "hermes_control"
    assert (home / "skills/software-development/hermes-control").resolve() == branch / "skills/hermes-control"


def test_cli_accepts_plugin_root_without_quota(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(eval_run, "verify_command", lambda source: "true")
    assert eval_run.main(["--dry-run", "--plugin-root", str(tmp_path)]) == 0
    assert "records:" in capsys.readouterr().out


LEGACY_DESCRIPTION = (
    'Delegate coding tasks to external agent CLIs. Break the work into tasks that can each be '
    'completed independently, choose an engine per task, and state why. Each task runs in its own'
    " git worktree; a successful edit is committed to the task's branch, and nothing reaches the "
    "working branch until a human merges it. "
    "A change that looks like it contains a secret is not committed; the reason names "
    "path:line and rule, and the worktree keeps the change. "
    "Give every write task a 'verify' command: a task is "
    'only reported proven (unproven_reason null) when that command passes. The model field lists '
    'the models the user allows and what each is for: when a listed use fits a task, choose that '
    'engine and model, unless the user pinned an engine (@claude, @codex), which decides the '
    "engine. Otherwise, engines differ: 'codex' runs inside an OS-level sandbox and writes by "
    "default; 'claude' is prompt-gated and reviews codex's diffs read-only (see the model field "
    "for which model). Use policy 'read-only' for anything that only needs to inspect the repo. "
    "An engine over the user's usage cap, or out of usage, hands its tasks to the other engine; "
    "when neither can take a task it comes back 'paused' and the user resumes it from the "
    "dashboard - report it, never dispatch it again. A run that is still going after the 'wait' "
    "budget comes back with state 'running' and a run_id instead of the result: call "
    'control_status with that run_id to check on it, and again later if it is still running - '
    'never dispatch the same tasks again. Workers answer in English; report their results in the '
    'language the user writes in.'
)


def test_legacy_description_is_byte_identical(monkeypatch):
    monkeypatch.setattr(plugin, "ENGINE_NAMES", ("claude", "codex"))
    assert plugin.build_schema({})["description"] == LEGACY_DESCRIPTION


def test_enabled_engines_and_fallback_reach_registered_planner(monkeypatch):
    engines = ("claude", "codex", "gemini")
    monkeypatch.setattr(plugin, "ENGINE_NAMES", engines)
    monkeypatch.setattr(registry, "ENGINE_NAMES", engines)
    monkeypatch.setattr(plugin, "_policy_at_load", lambda: {})
    monkeypatch.setattr(routing, "read_fallback", lambda policy: {
        "codex": ["gemini", "claude"], "claude": ["gemini/claude-opus-5-5"],
        "gemini": [],
    })
    registered = []
    class Context:
        def register_tool(self, **kwargs):
            registered.append(kwargs)
    plugin.register_tools(Context())
    tool = registered[0]
    description = tool["schema"]["description"]
    assert tool["description"] == description
    for engine in engines:
        assert f"@{engine}" in description
        assert registry.get_adapter(engine).planner_note in description
    assert "codex -> gemini, claude" in description
    assert "claude -> gemini/claude-opus-5-5" in description
    assert "gemini -> no fallback" in description
    assert "reviews codex's diffs" not in description
    assert "the other engine" not in description
    assert "usage is report-only (dashboard), never holds work back" in description


def test_two_engine_custom_fallback_is_described(monkeypatch):
    monkeypatch.setattr(plugin, "ENGINE_NAMES", ("claude", "codex"))
    text = plugin.build_schema({}, {"claude": ["codex/gpt-6-astra"]})["description"]
    assert "claude -> codex/gpt-6-astra" in text
    assert "the other engine" not in text


def test_disabled_model_settings_warn_once(monkeypatch, caplog):
    monkeypatch.setattr(model_policy, "ENGINE_NAMES", ("claude", "codex"))
    monkeypatch.setattr(model_policy, "_warned_disabled_engines", set())
    with caplog.at_level(logging.WARNING, logger=model_policy.__name__):
        for _ in range(3):
            assert model_policy.parse_policy({"gemini": {"ignored": True}}) == {}
    messages = [r.message for r in caplog.records if r.name == model_policy.__name__]
    assert messages == ["models: engine 'gemini' is disabled; skipping its settings"]


def test_cli_forwards_plugin_root_to_profile(tmp_path, monkeypatch):
    source = tmp_path / "source"
    verify = source / "control/verify"
    verify.mkdir(parents=True)
    (verify / "python-path").write_text(sys.executable + "\n")
    prompts = tmp_path / "prompts.json"
    prompts.write_text("[]")
    branch = tmp_path / "branch"
    monkeypatch.setenv("HERMES_HOME", str(source))
    monkeypatch.setattr(eval_run, "PROMPTS", prompts)
    monkeypatch.setattr(eval_run, "verify_command", lambda source: "true")
    create = eval_run.create_eval_home
    seen = []

    def record_create(source, out, plugin_root=None):
        seen.append(plugin_root)
        return create(source, out, plugin_root=plugin_root)

    monkeypatch.setattr(eval_run, "create_eval_home", record_create)
    assert eval_run.main(["--live", "--plugin-root", str(branch),
                          "--out", str(tmp_path / "out")]) == 0
    assert seen == [branch]
