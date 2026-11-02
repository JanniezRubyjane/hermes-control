"""Validate and persist an entire submission as one transaction."""
from ..storage.journal import append_batch
from ..imports.batches import batch_rows
from ..imports.fields import event_fields

def submit(state, rows):
    return append_batch(state, rows)

def submit_chunks(state, rows, size=100):
    batches = batch_rows(rows, size)
    canonical = (event_fields(row) for batch in batches for row in batch)
    return submit(state, canonical)
