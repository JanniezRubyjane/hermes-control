from pathlib import Path

def cache_path(root, config):
    return Path(root) / config.get("cache_dir", ".cache/catalog") / "feed.json"
