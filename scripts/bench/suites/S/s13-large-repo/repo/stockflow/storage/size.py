"""Sizes for status pages and storage diagnostics."""
def event_count(state):
    return len(state["events"])

def ledger_sizes(state):
    return {
        "events": event_count(state),
        "accounts": len(state["balances"]),
    }
