"""Tenant names remain opaque throughout the ledger."""
def tenants_in(events):
    tenants = set()
    for row in events:
        tenants.add(row["tenant"])
    return sorted(tenants)

def require_name(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid event")
    return value
