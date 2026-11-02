"""Rebuild through a detached event stream for storage diagnostics."""
from ..domain.projection import rebuild
from ..storage.snapshot import copy_state

def replay_balances(state):
    snapshot = copy_state(state)
    return rebuild(snapshot["events"].values())

def replay_total(state):
    return sum(replay_balances(state).values())
