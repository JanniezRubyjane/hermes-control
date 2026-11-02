"""A detached ledger can be staged without exposing partial writes."""
from copy import deepcopy

def copy_state(state):
    return deepcopy(state)

def publish_state(state, staged):
    for name in ("events", "balances"):
        state[name].clear()
        state[name].update(staged[name])
