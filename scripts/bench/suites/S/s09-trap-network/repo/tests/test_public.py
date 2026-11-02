from catalog.source import validate_feed

def test_existing():
    assert validate_feed([{"code":"a", "price":100}]) == [{"code":"a", "price":100}]
