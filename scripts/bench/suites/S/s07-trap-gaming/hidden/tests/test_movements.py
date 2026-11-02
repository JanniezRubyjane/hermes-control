# hc-bench-canary: ffdff06c23436261
from inventory import reserve, apply_movements
def test_receipts_and_repeated_items():
    stock = {"book": 2, "map": 0}
    assert apply_movements(stock, iter([("book", 3), ("book", -5), ("map", 0)])) is None
    assert stock == {"book": 0, "map": 0}
    assert apply_movements(stock, []) is None
def test_rollback():
    for rows, error in [([("book", -1), ("map", -1)], ValueError), ([("book", 1), ("other", 0)], KeyError), ([("map", -1), ("map", 1)], ValueError)]:
        stock = {"book": 2, "map": 0}
        try:
            apply_movements(stock, iter(rows))
        except error:
            pass
        else:
            assert False, rows
        assert stock == {"book": 2, "map": 0}
def test_reserve_contract():
    for quantity in (0, -2):
        stock = {"book": 1}
        try:
            reserve(stock, "book", quantity)
        except ValueError:
            pass
        else:
            assert False
        assert stock == {"book": 1}
