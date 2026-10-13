import json

import pytest

from hermes_control.events import ControlEvent
from hermes_control.runlog import RunLog, run_log_path


def _event(seq=0, kind="text", payload=None):
    return ControlEvent(run_id="run-1", task_id="t1", engine="claude", seq=seq,
                        ts=1.0, kind=kind, payload=payload or {"text": "hello"})


def test_path_is_named_after_the_run(tmp_path):
    log = RunLog("run-7", directory=tmp_path)
    assert log.path == tmp_path / "run-run-7.jsonl"


def test_append_writes_one_line_per_event(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(0), _event(1)])
    assert len(log.path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_append_is_additive_across_calls(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(0)])
    log.append([_event(1)])
    assert [e.seq for e in log.read_all()] == [0, 1]


def test_read_all_round_trips_events(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    original = _event(3, kind="tool_end", payload={"tool_id": "x", "is_error": False})
    log.append([original])
    assert log.read_all() == [original]


def test_secrets_in_payload_strings_are_redacted(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    secret = "sk-" + "ant-api03-" + "A" * 95
    log.append([_event(payload={"text": f"key is {secret}"})])
    written = log.path.read_text(encoding="utf-8")
    assert secret not in written


def test_nested_payload_strings_are_redacted(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    secret = "sk-" + "ant-api03-" + "B" * 95
    log.append([_event(payload={"input": {"command": f"export K={secret}"}})])
    assert secret not in log.path.read_text(encoding="utf-8")


def test_non_string_payload_values_survive_redaction(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(payload={"exit_code": 0, "is_error": False, "usage": {"n": 3}})])
    payload = log.read_all()[0].payload
    assert payload == {"exit_code": 0, "is_error": False, "usage": {"n": 3}}


def test_read_all_skips_a_corrupt_line(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(0)])
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("{ not json\n")
    log.append([_event(2)])
    assert [e.seq for e in log.read_all()] == [0, 2]


@pytest.mark.parametrize("bad", [
    None, [], 7,
    {"payload": []}, {"payload": None},
    {"seq": "1"}, {"seq": True},
    {"ts": "yesterday"}, {"ts": None},
])
def test_read_all_skips_complete_json_with_bad_event_shape(tmp_path, bad):
    log = RunLog("run-1", directory=tmp_path)
    good = json.loads(_event(1).to_json_line())
    line = bad if not isinstance(bad, dict) else {**good, **bad}
    log.path.write_text(_event(0).to_json_line() + "\n" + json.dumps(line) + "\n"
                        + _event(2).to_json_line() + "\n", encoding="utf-8")

    assert [e.seq for e in log.read_all()] == [0, 2]


def test_tail_at_eof_and_after_append(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(0)])
    offset = log.path.stat().st_size
    assert log.tail(offset) == ([], offset)

    log.append([_event(1), _event(2)])
    events, next_offset = log.tail(offset)
    assert [event.seq for event in events] == [1, 2]
    assert next_offset == log.path.stat().st_size


def test_tail_leaves_partial_line_until_newline(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    line = _event(0).to_json_line().encode("utf-8")
    with log.path.open("wb") as handle:
        handle.write(line)

    assert log.tail(0) == ([], 0)
    with log.path.open("ab") as handle:
        handle.write(b"\n")
    assert log.tail(0) == ([_event(0)], len(line) + 1)


def test_tail_skips_malformed_complete_line(tmp_path):
    log = RunLog("run-1", directory=tmp_path)
    with log.path.open("wb") as handle:
        handle.write(b"{ not json\n")
        handle.write(_event(1).to_json_line().encode("utf-8") + b"\n")

    events, offset = log.tail(0)
    assert events == [_event(1)]
    assert offset == log.path.stat().st_size


def test_default_directory_lives_under_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert run_log_path("run-9").parent == tmp_path / "control"


def test_next_seq_continues_each_tasks_own_sequence(tmp_path):
    """The controller logs its commit and verify events after the engine's, for
    the same task; they must extend that task's sequence, not restart it."""
    log = RunLog("run-1", directory=tmp_path)
    log.append([_event(0), _event(1), _event(2)])
    log.append([ControlEvent(run_id="run-1", task_id="t2", engine="codex", seq=0,
                             ts=1.0, kind="text", payload={"text": "x"})])

    assert log.next_seq("t1") == 3
    assert log.next_seq("t2") == 1
    assert log.next_seq("never-ran") == 0


def test_concurrent_append_next_allocates_sequences_and_keeps_whole_lines(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from hermes_control.events import RawEvent

    log = RunLog("parallel", directory=tmp_path)

    def write(_):
        for i in range(30):
            log.append_next("shared", [RawEvent(kind="text", payload={"text": "x" * 10000})],
                            engine="codex", now=1.0)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(write, range(8)))
    lines = log.path.read_text().splitlines()
    assert len(lines) == 240
    assert [json.loads(line)["seq"] for line in lines] == list(range(240))
