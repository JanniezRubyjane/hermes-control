# hc-bench-canary: 1082c79d2fd2b0cc
import archivekit as a
from archivekit import paths, catalog, codec
def test_layout_and_exports():
    import inspect
    from pathlib import Path
    assert Path(a.__file__).name == "__init__.py"
    for module in (paths, catalog, codec):
        for name, value in vars(module).items():
            if (not name.startswith("_") and inspect.isfunction(value)
                    and value.__module__ == module.__name__):
                assert getattr(a, name) is value
def test_paths():
    assert a.basename("a/b.tar.gz/") == "b.tar.gz"
    assert a.parent("a/b/c") == "a/b"
    assert a.parent("single") == ""
    assert a.suffix(".profile") == ""
    assert a.suffix("a.TXT") == ".TXT"
    assert a.stem("a.tar.gz") == "a.tar"
    assert a.join() == ""
    assert a.is_within("ab", "a") is False
    assert a.is_within("a", "a") and a.is_within("a", "")
    assert a.ancestors("a/b/c") == ["a/b", "a", ""]
    assert a.ancestors("") == []
    for name in ("/a", "a/../b", ".."):
        try:
            a.normalize(name)
        except ValueError:
            pass
        else:
            assert False, name
def test_catalog():
    rows = [{"path": "a/x.TXT", "size": 7}, {"path": "a/y", "size": 2}, {"path": "ab/z", "size": 7}]
    assert a.total_size(iter(rows)) == 16
    assert a.by_suffix(rows) == {".txt": 1, "": 2}
    assert a.directory_sizes(rows) == {"a": 9, "ab": 7}
    assert a.select(rows, "a") == rows[:2]
    assert a.select(rows, "a")[0] is not rows[0]
    assert a.largest(rows, 2) == [rows[0], rows[2]]
    assert a.largest(rows, 0) == []
    assert a.duplicates([rows[0], dict(rows[0], path="a//x.TXT")]) == ["a/x.TXT"]
    assert a.index_entries([rows[0], dict(rows[0], size=1)])["a/x.TXT"]["size"] == 1
    renamed = a.rename_prefix(rows, "a", "b")
    assert [r["path"] for r in renamed] == ["b/x.TXT", "b/y", "ab/z"]
    assert rows[0]["path"] == "a/x.TXT"
    assert a.rename_prefix(rows, "", "root")[0]["path"] == "root/a/x.TXT"
    try:
        a.largest(rows, -1)
    except ValueError:
        pass
    else:
        assert False
def test_codec():
    rows = [{"path": "café", "size": 0, "tag": "z"}]
    assert a.encode(rows) == '[{"path":"café","size":0,"tag":"z"}]'
    assert a.from_lines("\n" + a.to_lines(rows) + "\n") == rows
    assert a.decode('[{"path":"a//b","size":2}]')[0]["path"] == "a/b"
    assert a.to_lines([]) == "" and a.from_lines("") == []
    assert a.summary(iter(rows)) == {"count": 1, "bytes": 0}
    for text in ('{}', '[{"path":"a","size":-1}]', '[{"path":"../a","size":0}]'):
        try:
            a.decode(text)
        except ValueError:
            pass
        else:
            assert False, text
