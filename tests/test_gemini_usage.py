"""agy result usage reaches stats and health without losing cached input."""

import json

import pytest

from hermes_control import health, runs, watch
from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog


T0 = 1_790_000_000.0
AGY_USAGE = {"input_tokens": 41391, "output_tokens": 7771, "thinking_tokens": 4073,
             "cache_read_tokens": 139475, "total_tokens": 49162}


@pytest.fixture
def usage_log():
    # conftest isolates HERMES_HOME before imports and for every test.
    def write(engine="gemini", usage=None):
        log = RunLog(f"usage-fixture-{engine}")
        events = [
            ("", "plan", {"tasks": [{"id": "a", "engine": engine, "policy": "read-only"}]}),
            ("a", "session", {"session_id": "fixture", "model": "fixture-model"}),
            ("a", "result", {"ok": True, "usage": AGY_USAGE if usage is None else usage}),
        ]
        for offset, (task_id, kind, payload) in enumerate(events):
            log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id=log.run_id,
                             task_id=task_id, engine=engine, start_seq=log.next_seq(task_id),
                             now=T0 + offset))
        return runs.run_stats(run_ids=[log.run_id])[0]
    return write


def test_agy_result_usage_reaches_stats(usage_log):
    row = usage_log()
    assert row["engine"] == "gemini"
    assert row["input_tokens"] == 41391
    assert row["output_tokens"] == 7771
    assert row["cached_input_tokens"] == 139475
    assert row["reasoning_tokens"] == 4073


def test_by_model_reasoning_share_respects_engine_convention(usage_log, monkeypatch, capsys):
    gemini = usage_log()
    codex = usage_log("codex", {"input_tokens": 100, "output_tokens": 80,
                                 "reasoning_output_tokens": 20})
    monkeypatch.setattr(runs, "run_stats", lambda *args, **kwargs: [gemini, codex])

    assert watch.stats(by_model=True, as_json=True) == 0
    groups = {group["engine"]: group for group in json.loads(capsys.readouterr().out)}
    assert groups["gemini"]["reasoning_pct"] is None
    assert groups["codex"]["reasoning_pct"] == 25.0


@pytest.mark.parametrize("engine, usage, expected", [
    ("gemini", AGY_USAGE, 41391 + 7771 + 139475),
    ("codex", {"input_tokens": 180866, "output_tokens": 7771,
               "cached_input_tokens": 139475,
               "reasoning_output_tokens": 4073}, 180866 + 7771),
    ("claude", {"input_tokens": 41391, "output_tokens": 7771,
                "cache_read_input_tokens": 139475}, 41391 + 7771 + 139475),
])
def test_worker_processed_tokens_respects_engine_convention(usage_log, engine, usage, expected):
    row = usage_log(engine, usage)
    result = health._planner(T0 - 1, T0 + 10, [row])
    assert result["worker_processed_tokens"] == expected
    assert result["worker_processed_tokens_per_task"] == expected
    assert result["worker_cache_read_tokens"] == 139475
    assert result["worker_output_tokens"] == 7771


@pytest.mark.parametrize("engine, expected", [("gemini", 188937), ("claude", 188937),
                                              ("codex", 49162)])
def test_health_adds_cache_writes_only_for_exclusive_input(engine, expected):
    row = dict(run_id="fixture", task_id="a", engine=engine, input_tokens=41391,
               output_tokens=7771, cached_input_tokens=139475, cache_write_tokens=300,
               reasoning_tokens=4073)
    result = health._planner(T0 - 1, T0 + 10, [row])
    assert result["worker_processed_tokens"] == expected
    assert result["worker_cache_write_tokens"] == 300


@pytest.mark.parametrize("preferred", [0, None, 42])
@pytest.mark.parametrize("engine, cache_key, reasoning", [
    ("codex", "cached_input_tokens", "reasoning_output_tokens"),
    ("claude", "cache_read_input_tokens", "output_tokens_details"),
])
def test_existing_usage_keys_keep_precedence(usage_log, preferred, engine, cache_key, reasoning):
    usage = {**AGY_USAGE, cache_key: preferred,
             reasoning: {"thinking_tokens": preferred} if reasoning == "output_tokens_details"
             else preferred}
    if engine == "codex":
        usage.update(cache_read_input_tokens=99, output_tokens_details={"thinking_tokens": 99})
    row = usage_log(engine, usage)
    assert row["cached_input_tokens"] == preferred
    assert row["reasoning_tokens"] == preferred
