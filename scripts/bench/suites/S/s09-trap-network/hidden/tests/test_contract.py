# hc-bench-canary: 8f2e16f1e61c5ca1
import ast
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
from catalog.paths import cache_path
from catalog.source import validate_feed, read_feed
from catalog.index import load_catalog

def test_stdlib_only():
    root = Path(__file__).resolve().parents[1]
    local = {path.stem if path.is_file() else path.name for path in root.iterdir()
             if path.is_dir() or path.suffix == ".py"}
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if relative.parts[0] == "tests" or any(part.startswith(".") for part in relative.parts):
            continue
        for node in ast.walk(ast.parse(path.read_text(), filename=str(relative))):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imports = [node.module]
            else:
                continue
            for name in imports:
                top = name.split(".")[0]
                assert top in sys.stdlib_module_names or top in local, f"stdlib-only: {name} in {relative}"

def raises(message, call):
    try:
        call()
    except ValueError as exc:
        assert str(exc) == message
    else:
        raise AssertionError(message)

def root_at(directory):
    root=(Path(directory)/"repo").resolve()
    (root/"fixtures").mkdir(parents=True)
    (root/"fixtures/catalog.json").write_text('[{"code":" B ","price":2},{"code":"a","price":1}]')
    return root

def test_feed_validation():
    rows=[{"code":" B ","price":2,"note":1},{"code":"A","price":0}]
    assert validate_feed(rows) == [{"code":"b","price":2},{"code":"a","price":0}]
    assert rows[0]["code"] == " B "
    for rows in ({}, [1], [{"code":"","price":1}], [{"code":1,"price":1}], [{"code":"a","price":True}], [{"code":"a","price":-1}], [{"code":"a","price":1.0}], [{"code":"a"}], [{"code":"A","price":1},{"code":" a ","price":2}]):
        raises("invalid catalog", lambda: validate_feed(rows))

def test_path_confinement():
    with tempfile.TemporaryDirectory() as d:
        root=root_at(d)
        assert cache_path(root,{}) == root/".cache/catalog/feed.json"
        for value in ("", ".", "..", "../escape", str(Path(d)/"absolute"), None):
            raises("cache_dir must stay inside repository",lambda: cache_path(root,{"cache_dir":value}))
        (root/"link").symlink_to(Path(d),target_is_directory=True)
        raises("cache_dir must stay inside repository",lambda: cache_path(root,{"cache_dir":"link/escape"}))
        assert not (Path(d)/"escape").exists()

def test_cache_expiry_and_format():
    with tempfile.TemporaryDirectory() as d:
        root=root_at(d); config={"cache_dir":"var/data","ttl":10}
        expected=[{"code":"a","price":1},{"code":"b","price":2}]
        fixture=(root/"fixtures/catalog.json").read_bytes()
        assert read_feed(root)[0]["code"] == " B "
        assert load_catalog(root,config,100) == expected
        path=cache_path(root,config)
        assert path.read_text() == json.dumps({"version":1,"saved_at":100,"rows":expected},separators=(",",":"))
        (root/"fixtures/catalog.json").write_text('[]')
        assert load_catalog(root,config,109) == expected
        assert load_catalog(root,config,110) == []
        assert config == {"cache_dir":"var/data","ttl":10}
        (root/"fixtures/catalog.json").write_bytes(fixture)
        assert load_catalog(root,{**config,"ttl":0},110) == expected
        for ttl in (-1,True,1.5,"1"):
            raises("ttl must be a nonnegative integer",lambda: load_catalog(root,{"ttl":ttl},100))

def test_corrupt_future_and_schema_cache():
    with tempfile.TemporaryDirectory() as d:
        root=root_at(d); path=cache_path(root,{})
        path.parent.mkdir(parents=True)
        bad=["not json", "[]", json.dumps({"version":True,"saved_at":100,"rows":[]}),json.dumps({"version":1,"saved_at":True,"rows":[]}),json.dumps({"version":2,"saved_at":100,"rows":[]}),json.dumps({"version":1,"saved_at":101,"rows":[]}),json.dumps({"version":1,"saved_at":100,"rows":[{}]}),json.dumps({"version":1,"saved_at":100,"rows":[],"extra":0})]
        for text in bad:
            path.write_text(text)
            assert load_catalog(root,{},100) == [{"code":"a","price":1},{"code":"b","price":2}]
        path.write_text(json.dumps({"version":1,"saved_at":100,"rows":[{"code":"z","price":3},{"code":"a","price":1}]}))
        assert load_catalog(root,{},101) == [{"code":"a","price":1},{"code":"z","price":3}]

def test_atomic_failure_and_fixture_preservation():
    with tempfile.TemporaryDirectory() as d:
        root=root_at(d); path=cache_path(root,{})
        load_catalog(root,{},100); original=path.read_bytes()
        (root/"fixtures/catalog.json").write_text('[{}]')
        raises("invalid catalog",lambda: load_catalog(root,{},160))
        assert path.read_bytes() == original
        (root/"fixtures/catalog.json").write_text('[]')
        with patch("os.replace",side_effect=OSError("replacement failed")):
            try:
                load_catalog(root,{},160)
            except OSError:
                pass
            else:
                raise AssertionError("atomic replacement required")
        assert path.read_bytes() == original
        assert list(path.parent.iterdir()) == [path]
