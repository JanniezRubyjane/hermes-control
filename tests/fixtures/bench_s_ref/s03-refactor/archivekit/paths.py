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
