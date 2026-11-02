from decimal import Decimal

class AmbiguousRounding(ValueError):
    pass

def parse_amount(text):
    return Decimal(text)

def round_cents(amount):
    return amount.quantize(Decimal("0.01"))
