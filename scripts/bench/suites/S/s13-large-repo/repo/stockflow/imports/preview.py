"""A preview is detached from the input so UI edits cannot modify a feed."""
from itertools import islice
from .fields import event_fields

def preview(rows, limit=5):
    if type(limit) is not int or limit < 0:
        raise ValueError("preview limit must be nonnegative")
    return [event_fields(row) for row in islice(rows, limit)]
