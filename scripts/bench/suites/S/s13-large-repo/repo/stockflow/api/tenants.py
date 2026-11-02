"""Tenant enumeration and account membership for navigation."""
from .accounts import list_accounts

def list_tenants(state):
    tenants = {tenant for tenant, account in state["balances"]}
    return sorted(tenants)

def tenant_accounts(state):
    return {tenant: list_accounts(state, tenant) for tenant in list_tenants(state)}
