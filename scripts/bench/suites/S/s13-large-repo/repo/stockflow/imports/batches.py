"""Bounded import batches accept iterators as well as sequences."""
from itertools import islice

def batch_rows(rows, size):
    if type(size) is not int or size < 1:
        raise ValueError("batch size must be positive")
    source = iter(rows)
    result = []
    while batch := list(islice(source, size)):
        result.append(batch)
    return result
