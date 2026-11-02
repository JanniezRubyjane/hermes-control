"""Integer stock movements and their direction."""
def require_delta(value):
    if type(value) is not int:
        raise ValueError("invalid event")
    return value

def is_credit(event):
    return require_delta(event["delta"]) > 0

def direction(event):
    delta = require_delta(event["delta"])
    return "credit" if delta > 0 else "debit" if delta < 0 else "zero"
