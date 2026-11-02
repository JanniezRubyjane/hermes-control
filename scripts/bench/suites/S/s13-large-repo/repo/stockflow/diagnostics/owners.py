"""Compare event owners with owners present in the balance index."""
from ..domain.tenants import tenants_in

def owners(state):
    return tenants_in(state["events"].values())

def missing_owners(state):
    indexed = {tenant for tenant, account in state["balances"]}
    return [tenant for tenant in owners(state) if tenant not in indexed]
