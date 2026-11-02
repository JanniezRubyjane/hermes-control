# hc-bench-canary: a44110a424ed3ecb
from seats import reserve, release, quote_fee, availability
def test_contract():
    assert reserve(10, 3, 7) == 10
    assert release(7, 2) == 5
    assert quote_fee(100, True) == 90
    assert availability(3, 5) == 0
