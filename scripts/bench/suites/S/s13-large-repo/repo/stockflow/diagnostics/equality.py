"""Replaying stored events must recover the published balances."""
from .replay import replay_balances

def consistent(state):
    return replay_balances(state) == state["balances"]

def changed_accounts(state):
    replayed = replay_balances(state)
    keys = set(replayed) | set(state["balances"])
    return sorted(key for key in keys if replayed.get(key) != state["balances"].get(key))
