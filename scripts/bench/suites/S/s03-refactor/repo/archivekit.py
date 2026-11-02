"""Logical archive catalog utilities; public names are stable.

Names describe archive entries, not local files.
Paths use POSIX separators regardless of host operating system.
The empty path is the archive root.
Traversal outside that root is always rejected.
Rows are dictionaries containing path and size.
Additional row metadata is preserved by copying operations.
Selection preserves input order.
Ranked queries specify their own deterministic ordering.
Serialization retains Unicode names.
No function opens files or consults the filesystem.
"""

from pathlib import PurePosixPath

def normalize(path):
    """Normalize a logical archive path.

    Archive names use forward slashes on every platform.
    Empty segments and dot segments are removed.
    Parent traversal is forbidden, even when it would stay inside the root.
    Absolute names are rejected rather than silently made relative.
    The empty name represents the archive root.
    No filesystem access is performed.
    """
    if path.startswith("/") or ".." in path.split("/"):
        raise ValueError("unsafe archive path")
    return "/".join(p for p in path.split("/") if p not in ("", "."))

def basename(path):
    """Return the last normalized path component.

    This is a logical name operation.
    A trailing slash is ignored.
    Dot components are ignored.
    The root has an empty basename.
    Unsafe names fail consistently with normalize.
    The result never contains a slash.
    """
    return normalize(path).rsplit("/", 1)[-1]

def parent(path):
    """Return the normalized containing directory.

    Top-level entries belong to the empty root.
    Root itself also belongs to root.
    Trailing separators do not add a level.
    Dot components do not add a level.
    Unsafe traversal raises ValueError.
    This does not consult a filesystem.
    """
    name = normalize(path)
    return name.rsplit("/", 1)[0] if "/" in name else ""

def suffix(path):
    """Return the final filename suffix including its dot.

    Directory components are ignored.
    Hidden dotfiles without another dot have no suffix.
    A trailing dot is not treated as a suffix.
    Case is preserved in the result.
    Multiple suffixes return only the final one.
    Unsafe paths still raise ValueError.
    """
    return PurePosixPath(basename(path)).suffix

def stem(path):
    """Return the basename with its final suffix removed.

    Directory components are discarded.
    Hidden dotfiles retain their leading dot.
    Only the final suffix is removed.
    Case is preserved.
    Root yields an empty string.
    Unsafe paths still raise ValueError.
    """
    return PurePosixPath(basename(path)).stem

def join(*pieces):
    """Join logical archive components.

    Empty pieces are allowed.
    Each piece must be relative.
    Parent traversal in any piece raises ValueError.
    The output is normalized once after joining.
    With no pieces the result is the root.
    Inputs are never modified.
    """
    for piece in pieces:
        normalize(piece)
    return normalize("/".join(pieces))

def is_within(path, directory):
    """Test whether an entry belongs under a directory.

    Equality counts as containment.
    The root contains every valid entry.
    Component boundaries matter.
    A directory named a does not contain ab.
    Both names are normalized before comparison.
    Invalid names raise ValueError.
    """
    name, root = normalize(path), normalize(directory)
    return not root or name == root or name.startswith(root + "/")

def ancestors(path):
    """Return containing directories from nearest to root.

    The entry itself is excluded.
    The root is represented by an empty string.
    Root itself has no ancestors.
    A top-level name has root as its single ancestor.
    Ordering is stable and deterministic.
    Invalid paths raise ValueError.
    """
    name = normalize(path)
    result = []
    while name:
        name = parent(name)
        result.append(name)
    return result
from collections import Counter

def index_entries(entries):
    """Index catalog rows by normalized path.

    Each row supplies path and size.
    Other metadata is preserved.
    Later duplicates replace earlier entries.
    Input dictionaries are copied.
    Input ordering determines duplicate precedence.
    Size is not coerced or rounded.
    """
    return {normalize(row["path"]): dict(row, path=normalize(row["path"])) for row in entries}

def total_size(entries):
    """Sum the sizes of all supplied rows.

    This does not deduplicate names.
    Empty input has total zero.
    Sizes may be arbitrary nonnegative integers.
    The input may be a single-pass iterator.
    Missing sizes raise KeyError.
    No input records are changed.
    """
    return sum(row["size"] for row in entries)

def by_suffix(entries):
    """Count entries by lowercase final suffix.

    Files without a suffix use the empty string key.
    Paths are normalized through suffix.
    Counts include duplicate rows.
    Empty input produces an empty dictionary.
    The iterator is consumed once.
    The returned mapping is independent of the input.
    """
    return dict(Counter(suffix(row["path"]).lower() for row in entries))

def select(entries, directory):
    """Select rows contained by a logical directory.

    Containment includes the directory entry itself.
    Selection preserves input order.
    Returned rows are copies.
    Root selects every row.
    Partial component prefixes do not match.
    Invalid directory names raise ValueError.
    """
    directory = normalize(directory)
    return [dict(row) for row in entries if is_within(row["path"], directory)]

def largest(entries, limit=1):
    """Return the largest entries up to a limit.

    Descending size is the primary ordering.
    Raw path text breaks size ties.
    A zero limit produces no rows.
    A negative limit is invalid.
    Returned rows are independent dictionaries.
    The original ordering is not changed.
    """
    if limit < 0:
        raise ValueError("negative limit")
    return [dict(r) for r in sorted(entries, key=lambda r: (-r["size"], r["path"]))[:limit]]

def directory_sizes(entries):
    """Sum sizes by immediate containing directory.

    This does not recursively aggregate ancestors.
    Top-level entries use the empty root key.
    Duplicate rows all contribute.
    Input can be an iterator.
    Empty input yields an empty mapping.
    Paths are validated through parent.
    """
    totals = Counter()
    for row in entries:
        totals[parent(row["path"])] += row["size"]
    return dict(totals)

def duplicates(entries):
    """List normalized paths occurring more than once.

    Duplicate detection is based on the full logical name.
    Size and other metadata are ignored.
    Each duplicate name occurs once in the result.
    The result is lexically sorted.
    Dot and repeated slash components are normalized.
    Input records remain untouched.
    """
    counts = Counter(normalize(row["path"]) for row in entries)
    return sorted(name for name, count in counts.items() if count > 1)

def rename_prefix(entries, old, new):
    """Copy entries while replacing a directory prefix.

    Only complete component prefixes match.
    Equality replaces the complete name.
    Nonmatching rows retain their path spelling.
    Returned dictionaries never alias input rows.
    Both prefix arguments must be safe logical names.
    Empty old prefix means the root.
    """
    old, new = normalize(old), normalize(new)
    result = []
    for row in entries:
        copy = dict(row)
        name = normalize(row["path"])
        if is_within(name, old):
            tail = name[len(old):].lstrip("/")
            copy["path"] = "/".join(p for p in (new, tail) if p)
        result.append(copy)
    return result
import json

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
