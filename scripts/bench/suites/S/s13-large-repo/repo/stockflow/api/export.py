"""Storage export and restore use the same portable event representation."""
from ..storage.jsonlines import encode_lines
from ..storage.decode import decode_lines
from .append import submit

def export_events(state):
    return [dict(row) for row in state["events"].values()]

def export_text(state):
    return encode_lines(export_events(state))

def restore_text(state, text):
    return submit(state, decode_lines(text))
