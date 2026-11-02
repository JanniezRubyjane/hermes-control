from inventory import reserve
def test_reserve():
    stock = {"book": 2}
    reserve(stock, "book", 1)
    assert stock == {"book": 1}
    for quantity in (0, -1, 2):
        try:
            reserve(stock, "book", quantity)
        except ValueError:
            pass
        else:
            assert False, quantity
    assert stock == {"book": 1}
