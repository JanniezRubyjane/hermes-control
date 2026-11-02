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
    """Atomically apply ordered signed stock deltas."""
    updated = stock.copy()
    for item, delta in movements:
        if item not in updated:
            raise KeyError(item)
        balance = updated[item] + delta
        if balance < 0:
            raise ValueError("insufficient stock")
        updated[item] = balance
    stock.update(updated)
