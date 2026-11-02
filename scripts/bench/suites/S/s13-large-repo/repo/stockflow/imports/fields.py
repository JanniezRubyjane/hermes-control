"""Select canonical fields before indexing imported events."""
from .columns import required_columns, missing_columns

def select_fields(row, fields):
    return {key: row[key] for key in fields}

def event_fields(row):
    if missing_columns(row):
        raise ValueError("invalid event")
    return select_fields(row, required_columns())
