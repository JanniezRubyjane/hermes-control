"""Local fixture loading and canonical row validation."""
import json
from pathlib import Path


def read_feed(root):
    return json.loads((Path(root) / "fixtures/catalog.json").read_text())


def validate_feed(rows):
    if not isinstance(rows, list):
        raise ValueError("invalid catalog")
    seen = set()
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("invalid catalog")
        code = row.get("code")
        price = row.get("price")
        if not isinstance(code, str) or not code.strip():
            raise ValueError("invalid catalog")
        code = code.strip().casefold()
        if type(price) is not int or price < 0 or code in seen:
            raise ValueError("invalid catalog")
        seen.add(code)
        result.append({"code": code, "price": price})
    return result
