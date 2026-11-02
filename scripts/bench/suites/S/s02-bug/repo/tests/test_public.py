from decimal import Decimal
from invoicing import Invoice
def test_checkout():
    assert Invoice([("12.50", 2)]).checkout() == Decimal("25.00")
