"""Resolve the configured cache before creating any directory."""
from pathlib import Path


def cache_path(root, config):
    root = Path(root).resolve()
    value = config.get("cache_dir", ".cache/catalog")
    message = "cache_dir must stay inside repository"
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(message)
    directory = (root / value).resolve()
    if directory == root or not directory.is_relative_to(root):
        raise ValueError(message)
    return directory / "feed.json"
