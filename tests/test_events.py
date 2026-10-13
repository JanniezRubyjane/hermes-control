import json

from hermes_control.events import ControlEvent, RawEvent, stamp


def test_raw_event_defaults_raw_kind_to_none():
    raw = RawEvent(kind="text", payload={"text": "hi"})
    assert raw.raw_kind is None


def test_stamp_assigns_monotonic_seq_and_identity():
    raws = [RawEvent(kind="text", payload={"text": "a"}),
            RawEvent(kind="result", payload={"ok": True})]
    events = stamp(raws, run_id="run-1", task_id="t1", engine="claude",
                   start_seq=5, now=1000.0)
    assert [e.seq for e in events] == [5, 6]
    assert all(e.run_id == "run-1" and e.task_id == "t1" for e in events)
    assert all(e.engine == "claude" and e.ts == 1000.0 for e in events)


def test_json_line_round_trip_preserves_every_field():
    event = ControlEvent(run_id="run-1", task_id="t1", engine="codex", seq=3,
                         ts=1234.5, kind="tool_end",
                         payload={"tool_id": "item_1", "is_error": False},
                         raw_kind="item.completed/command_execution")
    line = event.to_json_line()
    assert "\n" not in line
    assert ControlEvent.from_json_line(line) == event


def test_json_line_is_valid_json_with_expected_keys():
    event = ControlEvent(run_id="r", task_id="t", engine="claude", seq=0,
                         ts=1.0, kind="text", payload={"text": "x"})
    parsed = json.loads(event.to_json_line())
    assert set(parsed) == {"run_id", "task_id", "engine", "seq", "ts",
                           "kind", "payload", "raw_kind"}
