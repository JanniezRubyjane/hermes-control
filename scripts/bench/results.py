"""Append-only JSONL attempts. Corrupt records are skipped and counted on read."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

ARMS = {"0-bare", "1-controller", "2-full"}


def short_error(text, limit=300):
    """Keep error reasons on one line and within the result-row budget."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 3)] + "." * min(3, limit)


class ResultRows(list):
    malformed_lines: int = 0

    @property
    def malformed_count(self):
        return self.malformed_lines


def append_result(path: str | Path, record: dict) -> None:
    record = dict(record)
    grade = record.pop("grade", None)
    if grade is not None:
        record.update(asdict(grade) if is_dataclass(grade) else grade)
    if record.get("arm") not in ARMS:
        raise ValueError("arm: expected 0-bare, 1-controller, or 2-full")
    required = ("bench_sha suite task_id engine model effort rep started ended wall_s "
                "worker_s verify_exit verify_green solved hidden_exit hidden_tail "
                "gate_violations touched_protected diff_files diff_lines answer_match "
                "tokens run_id").split()
    for name in required:
        if name not in record:
            raise ValueError(f"{name}: required result field")
    if type(record["verify_green"]) is not bool and not (
            record["arm"] == "0-bare" and record["verify_green"] is None):
        raise ValueError("verify_green: must be a bool")
    if not isinstance(record["tokens"], dict):
        raise ValueError("tokens: must be an object")
    line = json.dumps(record, ensure_ascii=False, allow_nan=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_results(path: str | Path) -> ResultRows:
    rows = ResultRows()
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid number {value}")))
                if not isinstance(value, dict):
                    raise ValueError("result must be an object")
                rows.append(value)
            except (ValueError, TypeError):
                rows.malformed_lines += 1
    return rows
