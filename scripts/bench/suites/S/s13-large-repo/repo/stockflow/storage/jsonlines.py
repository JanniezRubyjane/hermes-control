"""Deterministic portable journal exports."""
import json

def encode_lines(events):
    lines = []
    for row in events:
        lines.append(json.dumps(row, sort_keys=True, ensure_ascii=True))
    return "\n".join(lines)

def write_lines(path, events):
    text = encode_lines(events)
    path.write_text(text + ("\n" if text else ""), encoding="utf-8")
