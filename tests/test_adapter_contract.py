from pathlib import Path

import pytest

from hermes_control.engines.base import ControlTask, EngineAdapter, EngineInfo
from hermes_control.events import ControlResult, RawEvent
from hermes_control.policy import ExecPolicy


class _FakeAdapter(EngineAdapter):
    name = "fake"

    def probe(self):
        return EngineInfo(name="fake", version="1.0", path="/bin/fake")

    def auth_problem(self, info):
        return None

    def build_argv(self, task, *, cwd):
        return ["fake", task.goal, str(cwd)]

    def parse_line(self, line):
        return [RawEvent(kind="text", payload={"text": line})]

    def extract_result(self, task, raws, diff):
        return ControlResult(task_id=task.id, engine=self.name, ok=True,
                             text="done", session_id=None, diff=diff)


def test_control_task_defaults_are_empty_not_none():
    task = ControlTask(id="t1", goal="g", engine="fake", policy=ExecPolicy(write=False))
    assert task.files_hint == () and task.depends_on == ()
    assert task.why == "" and task.model is None


def test_a_conforming_adapter_satisfies_the_abc():
    adapter = _FakeAdapter()
    task = ControlTask(id="t1", goal="go", engine="fake", policy=ExecPolicy(write=True))
    assert adapter.probe() == EngineInfo(name="fake", version="1.0", path="/bin/fake")
    assert adapter.build_argv(task, cwd=Path("/wt")) == ["fake", "go", "/wt"]
    assert adapter.parse_line("hi") == [RawEvent(kind="text", payload={"text": "hi"})]
    assert adapter.extract_result(task, [], "D").diff == "D"


def test_the_abc_cannot_be_instantiated_directly():
    with pytest.raises(TypeError):
        EngineAdapter()
