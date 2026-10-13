import argparse
import json
import time
from datetime import datetime, timezone

from hermes_control import lessons, runlog, watch


def test_seen_dates_and_stale_sections(capsys):
    now = time.time()
    events = []
    for index, (mode, age) in enumerate([("stall", 20), ("stall", 19), ("stall", 18),
                                        ("timeout", 2), ("timeout", 1)]):
        events.append(dict(run_id="seen", task_id="", engine="", seq=index,
                           ts=now - age * 86400, kind="summary", payload={"tasks": [
                               {"task_id": str(index), "failure_mode": mode}]}))
    path = runlog.run_log_dir() / "run-seen.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in reversed(events)) + "\n")
    before = path.read_bytes()
    rows = {row["key"]: row for row in lessons.build(now=now)}
    stale = rows["failure_mode:stall"]
    assert stale["first_seen"] == now - 20 * 86400
    assert stale["last_seen"] == now - 18 * 86400
    assert stale["stale"] is True
    assert rows["failure_mode:timeout"]["stale"] is False
    assert lessons.build(now=now, limit=1)[0]["key"] == "failure_mode:timeout"
    assert not lessons.build(now=now, recent=21)[1]["stale"]
    parser = argparse.ArgumentParser()
    watch.setup_cli(parser)
    assert parser.parse_args(["lessons"]).recent == 14
    assert watch.run_cli(parser.parse_args(["lessons"])) == 0
    text = capsys.readouterr().out
    assert "first" in text and "last" in text
    assert text.index("Failure mode: timeout") < text.index("Not seen in the last 14 days")
    assert text.index("Not seen in the last 14 days") < text.index("Failure mode: stall")
    assert datetime.fromtimestamp(stale["last_seen"], timezone.utc).strftime("%Y-%m-%d") in text
    assert watch.run_cli(parser.parse_args(["lessons", "--recent", "21", "--json"])) == 0
    assert all(not row["stale"] for row in json.loads(capsys.readouterr().out))
    assert path.read_bytes() == before


def test_recent_boundary_is_active():
    now = time.time()
    path = runlog.run_log_dir() / "run-boundary.jsonl"
    path.write_text(json.dumps(dict(run_id="boundary", task_id="", engine="", seq=0,
                                   ts=now - 14 * 86400, kind="summary", payload={"tasks": [
                                       {"task_id": "one", "failure_mode": "stall"},
                                       {"task_id": "two", "failure_mode": "stall"}]})) + "\n")
    assert lessons.build(now=now)[0]["stale"] is False
