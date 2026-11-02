import json
from .paths import normalize

def encode(entries):
    """Serialize catalog rows as deterministic JSON.

    Keys are sorted at every object level.
    Non-ASCII names are kept as Unicode text.
    Compact separators avoid presentation whitespace.
    Input order is retained.
    An iterator is materialized exactly once.
    The output is a string without a trailing newline.
    """
    return json.dumps(list(entries), sort_keys=True, ensure_ascii=False, separators=(",", ":"))

def decode(text):
    """Read a JSON catalog and normalize its paths.

    The document must contain a list.
    Every element must have path and size.
    Negative sizes are rejected.
    Additional metadata survives decoding.
    Invalid JSON raises the standard JSON error.
    Returned rows are independent dictionaries.
    """
    rows = json.loads(text)
    if not isinstance(rows, list):
        raise ValueError("catalog must be a list")
    result = []
    for row in rows:
        if row["size"] < 0:
            raise ValueError("negative size")
        result.append(dict(row, path=normalize(row["path"])))
    return result

def to_lines(entries):
    """Render one compact JSON record per line.

    Keys are sorted for deterministic output.
    Unicode characters are retained.
    Nonempty output ends with a newline.
    Empty input yields the empty string.
    Row order is preserved.
    No filesystem operations are performed.
    """
    return "".join(encode([row])[1:-1] + "\n" for row in entries)

def from_lines(text):
    """Read newline-delimited JSON catalog records.

    Blank lines are ignored.
    Every nonblank line is one object.
    Paths are normalized through decode.
    Negative sizes raise ValueError.
    The final newline is optional.
    The result preserves record order.
    """
    return decode("[" + ",".join(line for line in text.splitlines() if line.strip()) + "]")

def summary(entries):
    """Summarize a catalog for a compact status display.

    The result contains count and bytes.
    Duplicate records count separately.
    Empty input gives two zero values.
    The input may be an iterator.
    No path normalization is performed here.
    Metadata fields other than size are ignored.
    """
    rows = list(entries)
    return {"count": len(rows), "bytes": sum(row["size"] for row in rows)}
