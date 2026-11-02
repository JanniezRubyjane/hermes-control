"""Movement volume counts debits independently of signed totals."""
from ..domain.signs import direction

def absolute_volume(events):
    return sum(abs(row["delta"]) for row in events)

def movement_counts(events):
    counts = {"credit": 0, "debit": 0, "zero": 0}
    for row in events:
        counts[direction(row)] += 1
    return counts
