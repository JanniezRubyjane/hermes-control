"""Reject pytest versions or locations that shadow the pinned uv overlay."""
import os
from pathlib import Path
import sys


expected = sys.argv[1]
try:
    import pytest
    version = pytest.__version__
    absolute = Path(os.path.abspath(pytest.__file__))
    resolved = absolute.resolve()
except Exception as error:
    message = (
        f"cannot import pytest ({type(error).__name__}); "
        f"expected the uv overlay pytest=={expected}"
    )
else:
    shadowed = any(
        location.is_relative_to(root)
        for entry in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if entry
        for root in (Path(os.path.abspath(entry)), Path(entry).resolve())
        for location in (absolute, resolved)
    )
    if version == expected and not shadowed:
        sys.exit(0)
    message = (
        f"pytest {version} at {resolved} is not the uv overlay pytest=={expected}"
    )

print(f"check: {message}", file=sys.stderr)
sys.exit(2)
