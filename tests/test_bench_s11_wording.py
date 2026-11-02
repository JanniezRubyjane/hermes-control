import json
from pathlib import Path


def test_s11_goal_explicitly_requires_whitespace_stripping():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts/bench/suites/S/s11-ambiguous/spec.json"
    spec = json.loads(path.read_text())

    assert "strips surrounding whitespace" in spec["goal"]
