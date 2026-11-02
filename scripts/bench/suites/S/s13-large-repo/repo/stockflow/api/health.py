"""Readiness and ledger consistency for the operator view."""
from ..diagnostics.equality import consistent
from ..diagnostics.negative import negative_accounts

def health():
    return {"status": "ready"}

def ledger_health(state):
    valid = consistent(state) and not negative_accounts(state)
    return {"status": "ready" if valid else "inconsistent"}
