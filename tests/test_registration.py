import json
from pathlib import Path

import pytest
import yaml

import hermes_control
from hermes_control import (
    CONTROL_STATUS_TOOL_SCHEMA,
    CONTROL_TOOL_SCHEMA,
    control_available,
    register_tools,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "hermes_control"


class _Ctx:
    def __init__(self):
        self.calls = []

    def register_tool(self, **kwargs):
        self.calls.append(kwargs)
        return object()


def test_register_tools_registers_both_tools_in_the_control_toolset():
    ctx = _Ctx()
    register_tools(ctx)
    assert [call["name"] for call in ctx.calls] == ["control_dispatch", "control_status"]
    assert all(call["toolset"] == "control" for call in ctx.calls)


def test_registration_never_overrides_a_builtin():
    ctx = _Ctx()
    register_tools(ctx)
    assert all(call.get("override", False) is False for call in ctx.calls)


def test_registration_is_gated_by_a_check_fn():
    ctx = _Ctx()
    register_tools(ctx)
    assert all(callable(call["check_fn"]) for call in ctx.calls)


def test_schema_describes_the_task_list():
    properties = CONTROL_TOOL_SCHEMA["parameters"]["properties"]
    assert "tasks" in properties
    task_properties = properties["tasks"]["items"]["properties"]
    for field in ("id", "goal", "engine", "policy", "depends_on", "why"):
        assert field in task_properties
    assert task_properties["engine"]["enum"] == ["claude", "codex"]
    assert task_properties["policy"]["enum"] == ["read-only", "write"]
    assert CONTROL_TOOL_SCHEMA["parameters"]["required"] == ["tasks"]


def test_every_task_field_the_dispatcher_reads_is_offered_to_the_model():
    """A field parse_tasks honours but the schema never declares is one the planner
    cannot know to send - the feature exists and is unreachable."""
    from hermes_control.dispatch import parse_tasks

    class RecordingTask(dict):
        read: set = set()

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

        def __getitem__(self, key):
            self.read.add(key)
            return super().__getitem__(key)

    parse_tasks([RecordingTask(id="a", goal="g", engine="claude", why="w", model="m")])

    task_properties = CONTROL_TOOL_SCHEMA["parameters"]["properties"]["tasks"]["items"][
        "properties"]
    assert RecordingTask.read, "parse_tasks read nothing through the mapping"
    assert RecordingTask.read <= set(task_properties), RecordingTask.read - set(task_properties)


def test_control_is_unavailable_when_no_engine_probes(monkeypatch):
    monkeypatch.setattr("hermes_control.available_engines", lambda: {})
    assert control_available() is False


def test_control_is_available_when_an_engine_probes(monkeypatch):
    monkeypatch.setattr("hermes_control.available_engines",
                        lambda: {"codex": object()})
    assert control_available() is True


def test_module_exposes_register_the_name_the_loader_looks_for():
    """The contract the six tests above did not cover, and the reason the plugin
    did not exist at runtime: `hermes_cli/plugins_loader.py` does
    `getattr(module, "register", None)` and skips the plugin with "no register()
    function" when it is absent. Every other bundled plugin defines `register`;
    this one defined only `register_tools` and so never loaded."""
    assert callable(getattr(hermes_control, "register", None))


def test_register_delegates_to_register_tools():
    ctx = _Ctx()
    hermes_control.register(ctx)
    assert [call["name"] for call in ctx.calls] == ["control_dispatch", "control_status"]


def test_the_real_plugin_loader_loads_this_plugin_and_gets_its_tool(tmp_path, monkeypatch):
    """Drives the loader's actual lookup path against the real plugin directory,
    rather than asserting on a name. Before `register` existed this failed with
    `loaded.error == "no register() function"` and zero tools registered."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest

    manager = PluginManager(scope_key=str(tmp_path))
    manager._load_plugin(PluginManifest(
        name="hermes_control", version="0.1.0", description="t",
        source="bundled", path=str(PLUGIN_DIR)))

    loaded = next(lp for lp in manager._plugins.values()
                  if lp.manifest.name == "hermes_control")
    assert loaded.error is None
    assert loaded.enabled is True
    assert loaded.tools_registered == ["control_dispatch", "control_status"]


def test_control_dispatch_runs_through_the_real_tool_registry(tmp_path, monkeypatch):
    """The registry calls every handler as ``handler(args_dict, **context)`` and
    rejects any result that is not a string (``tools/registry.py`` dispatch and
    ``_normalize_handler_result``). The first real Hermes run died right here with
    "control_dispatch_handler() takes 0 positional arguments but 1 was given",
    because every earlier test called around the registry instead of through it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.registry import registry

    manager = PluginManager(scope_key=str(tmp_path))
    manager._load_plugin(PluginManifest(
        name="hermes_control", version="0.1.0", description="t",
        source="bundled", path=str(PLUGIN_DIR)))

    # An unknown engine fails validation inside dispatch(), so no engine CLI runs.
    result = registry.dispatch(
        "control_dispatch",
        {"tasks": [{"id": "a", "goal": "g", "engine": "gemini", "why": "w"}]},
        scope=str(tmp_path), task_id="t-1", session_id="s-1")

    assert isinstance(result, str)
    payload = json.loads(result)
    assert "unknown engine" in payload.get("error", ""), payload
    assert payload["tasks"] == []


def test_control_status_runs_through_the_real_tool_registry(tmp_path, monkeypatch):
    """The same contract as control_dispatch: the registry calls the handler as
    handler(args_dict, **context), and an unknown run_id must come back as a
    plain string, not raise."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_manifest import PluginManifest
    from tools.registry import registry

    manager = PluginManager(scope_key=str(tmp_path))
    manager._load_plugin(PluginManifest(
        name="hermes_control", version="0.1.0", description="t",
        source="bundled", path=str(PLUGIN_DIR)))

    result = registry.dispatch(
        "control_status", {"run_id": "no-such-run"},
        scope=str(tmp_path), task_id="t-1", session_id="s-1")

    assert isinstance(result, str)
    payload = json.loads(result)
    assert "no run" in payload.get("error", ""), payload


def _manifest() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


def test_manifest_name_matches_the_plugin_directory():
    """`hermes_cli/plugins_cmd.py:_get_plugin_toolset_key` resolves a plugin's
    manifest from disk as `<plugins dir>/<name>`, so a name that does not match
    the directory simply misses."""
    assert _manifest()["name"] == PLUGIN_DIR.name


def test_manifest_declares_the_tools_it_provides():
    """Without `provides_tools`, `_get_plugin_toolset_key` returns None and
    `hermes plugins enable` never adds `control` to the toolsets."""
    assert _manifest()["provides_tools"] == [
        CONTROL_TOOL_SCHEMA["name"], CONTROL_STATUS_TOOL_SCHEMA["name"]]


@pytest.mark.parametrize("field", ["name", "version", "description", "author"])
def test_manifest_has_no_placeholder_metadata(field):
    assert _manifest()[field] and _manifest()[field] != "downstream"



def test_the_planner_is_shown_the_configured_models():
    """The schema is built when Hermes loads the plugin, from the settings then."""
    from hermes_control import build_schema, model_policy

    schema = build_schema(model_policy.parse_policy(
        {"claude": {"allowed": {"sonnet": "routine edits"}}}))
    task = schema["parameters"]["properties"]["tasks"]["items"]["properties"]

    assert "sonnet" in task["model"]["description"]
    assert "routine edits" in task["model"]["description"]
    assert "max" in task["effort"]["description"]


def test_the_tool_description_lets_a_pin_decide_the_engine():
    """Review: 'choose that engine and model' must not read as overriding @claude/@codex."""
    assert "pin" in hermes_control.CONTROL_TOOL_DESCRIPTION.lower()


def test_tool_description_routes_codex_diffs_to_claude_review():
    text = hermes_control.CONTROL_TOOL_DESCRIPTION.lower()
    assert "claude" in text and "reviews codex" in text
    assert "read widely" not in text


def test_tool_description_keeps_the_report_in_the_users_language():
    """Same rule as the skill: worker results are English, the report is not."""
    text = hermes_control.CONTROL_TOOL_DESCRIPTION.lower()
    assert "language the user writes in" in text
