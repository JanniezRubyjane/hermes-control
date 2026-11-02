"""An expiring local cache, replaced only after a valid fixture is ready."""
import json
import os
from pathlib import Path
import tempfile

from .paths import cache_path
from .source import read_feed, validate_feed


def load_catalog(root, config, now):
    ttl = config.get("ttl", 60)
    if type(ttl) is not int or ttl < 0:
        raise ValueError("ttl must be a nonnegative integer")
    path = cache_path(root, config)
    cached_rows = _read_cache(path, now, ttl)
    if cached_rows is not None:
        return cached_rows
    rows = sorted(validate_feed(read_feed(root)), key=lambda row: row["code"])
    payload = {"version": 1, "saved_at": now, "rows": rows}
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(encoded)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return rows


def _read_cache(path, now, ttl):
    try:
        cached = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if set(cached) != {"version", "saved_at", "rows"}:
        return None
    if type(cached["version"]) is not int or cached["version"] != 1:
        return None
    if type(cached["saved_at"]) is not int:
        return None
    age = now - cached["saved_at"]
    if age < 0 or age >= ttl:
        return None
    try:
        rows = validate_feed(cached["rows"])
    except ValueError:
        return None
    return sorted(rows, key=lambda row: row["code"])
