"""Aggregate movements and account rows without losing integer precision."""
def net_delta(events):
    total = 0
    for row in events:
        total += row["delta"]
    return total

def account_total(rows):
    total = 0
    for row in rows:
        total += row["balance"]
    return total
