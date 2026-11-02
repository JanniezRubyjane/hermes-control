import archivekit as a
def test_paths():
    assert a.normalize("a//./b") == "a/b"
    assert a.join("a", "b") == "a/b"
def test_round_trip():
    rows = [{"path": "a.txt", "size": 3}]
    assert a.decode(a.encode(rows)) == rows
    assert a.summary(rows) == {"count": 1, "bytes": 3}
