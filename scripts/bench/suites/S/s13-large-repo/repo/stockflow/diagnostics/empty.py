"""Differentiate an unused ledger from one whose balances reached zero."""
from ..storage.size import event_count

def is_empty(state):
    return event_count(state) == 0 and not state["balances"]

def all_balances_zero(state):
    return all(balance == 0 for balance in state["balances"].values())
