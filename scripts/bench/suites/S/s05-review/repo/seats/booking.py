"""Seat reservations. Counts are nonnegative integers; fees are integer cents."""
def reserve(capacity, used, requested):
    """Accept positive requests up to and including remaining capacity."""
    if requested <= 0 or requested > capacity - used:
        raise ValueError("unavailable")
    return used + requested

def release(used, count):
    """Release a positive count no larger than current usage."""
    if count <= 0 or count > used:
        raise ValueError("invalid release")
    return used - count

def quote_fee(base_cents, member=False):
    """Members pay 90 percent, rounded down to whole cents; others pay full fee."""
    return base_cents * 90 // 100 if member else base_cents

def availability(capacity, used):
    """Clamp remaining seats at zero, including imported over-capacity records."""
    return max(0, capacity - used)
