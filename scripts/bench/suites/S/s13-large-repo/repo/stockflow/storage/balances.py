"""Balance lookup and validation for pending stock movements."""
from .keys import account_key

def balance_of(state, tenant, account):
    return state["balances"].get((tenant, account), 0)

def next_balance(balances, event):
    value = balances.get(account_key(event), 0) + event["delta"]
    if value < 0:
        raise ValueError("negative balance")
    return value
