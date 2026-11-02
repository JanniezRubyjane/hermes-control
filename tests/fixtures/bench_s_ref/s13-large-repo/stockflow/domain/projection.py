"""Replay uses the same validation and identity rules as a journal batch."""
from ..storage.journal import append_batch, empty_state


def rebuild(events):
    state = empty_state()
    append_batch(state, events)
    return state["balances"]
