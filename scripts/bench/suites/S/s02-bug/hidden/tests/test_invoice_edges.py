# hc-bench-canary: cde000b90b1e1341
from decimal import Decimal
from invoicing import Invoice
def test_reported():
    assert Invoice([("100", 1)]).checkout(10) == Decimal("90.00")
def test_rounding_after_tax():
    assert Invoice([("0.05", 1)]).checkout(10, 10) == Decimal("0.05")
def test_half_cent():
    assert Invoice([("0.005", 1)]).checkout() == Decimal("0.01")
def test_complete_discount():
    assert Invoice([("9.99", 7)]).checkout(100, 20) == Decimal("0.00")
def test_empty():
    assert Invoice([]).checkout(20, 10) == Decimal("0.00")
def test_fraction_and_generator():
    assert Invoice((line for line in [("1.005", 3), ("2.01", 2)])).checkout("12.5", 7) == Decimal("6.59")
def test_bad_rates():
    for rate in (-1, 101):
        try:
            Invoice([]).checkout(rate)
        except ValueError:
            pass
        else:
            assert False, rate
