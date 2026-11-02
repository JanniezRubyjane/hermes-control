import json
from pathlib import Path

def read_feed(root):
    return json.loads((Path(root) / "fixtures/catalog.json").read_text())

def validate_feed(rows):
    return rows
