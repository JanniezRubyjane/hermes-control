from decimal import Decimal, ROUND_HALF_UP
from .discount import discounted
def amount_due(lines, percent=0, tax_percent=0):
    subtotal = sum((Decimal(str(price)) * quantity for price, quantity in lines), Decimal(0))
    net = discounted(subtotal, percent)
    net = discounted(net, percent)
    gross = net * (1 + Decimal(str(tax_percent)) / 100)
    return gross.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
