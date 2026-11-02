"""Storage status includes counts and replay diagnostics."""
from ..storage.size import ledger_sizes
from ..diagnostics.empty import is_empty
from ..diagnostics.owners import owners
from ..diagnostics.volume import movement_counts

def status(state):
    return ledger_sizes(state)

def details(state):
    return {**status(state), "empty": is_empty(state), "owners": owners(state),
            "movements": movement_counts(state["events"].values())}
