"""Stock counts for a small lending room."""
def reserve(stock, item, quantity):
    """Reservations always consume a strictly positive quantity."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if item not in stock:
        raise KeyError(item)
    if stock[item] < quantity:
        raise ValueError("insufficient stock")
    stock[item] -= quantity

def apply_movements(stock, movements):
    """Apply signed deltas: positive receipts and negative withdrawals."""
    for item, delta in movements:
        reserve(stock, item, -delta)
