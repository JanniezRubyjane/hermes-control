"""Merge feed fragments in arrival order and optionally discard replays."""
from .dedup import unique_events

def merge_rows(left, right):
    rows = list(left)
    rows.extend(right)
    return rows

def merge_unique(left, right):
    return unique_events(merge_rows(left, right))
