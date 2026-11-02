"""Account names and current stock for a tenant."""
from ..domain.accounts import account_rows
from ..storage.balances import balance_of

def list_accounts(state, tenant):
    return [row["account"] for row in account_rows(state["balances"], tenant)]

def get_balance(state, tenant, account):
    return {"account": account, "balance": balance_of(state, tenant, account)}
