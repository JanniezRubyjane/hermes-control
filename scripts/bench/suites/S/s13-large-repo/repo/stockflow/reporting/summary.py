"""Project the complete stream before selecting a tenant's accounts."""
from ..domain.projection import rebuild
from ..domain.accounts import account_rows
from ..domain.totals import account_total

def report(events, tenant):
    balances = rebuild(events)
    accounts = account_rows(balances, tenant)
    return {"tenant": tenant, "accounts": accounts, "total": account_total(accounts)}
