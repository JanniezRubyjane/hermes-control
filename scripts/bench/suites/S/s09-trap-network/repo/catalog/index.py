import json
from .paths import cache_path
from .source import read_feed, validate_feed

def load_catalog(root, config, now):
    path = cache_path(root, config)
    if path.exists():
        return json.loads(path.read_text())["rows"]
    rows = validate_feed(read_feed(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version":1,"saved_at":now,"rows":rows}))
    return rows
