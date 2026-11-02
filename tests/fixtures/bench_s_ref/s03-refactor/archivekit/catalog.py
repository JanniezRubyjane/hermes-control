from collections import Counter
from .paths import normalize, parent, suffix, is_within

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
