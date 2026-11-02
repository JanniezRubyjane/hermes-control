from billing.invoice import calculate

def test_existing():
    assert calculate([{"id":"a","amount":"1.00","qty":2}])["total"] == "2.00"
