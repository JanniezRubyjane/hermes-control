"""Read an exported journal, attaching line numbers to malformed data."""
import json

def decode_lines(text):
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"invalid journal line {number}") from exc
    return rows
