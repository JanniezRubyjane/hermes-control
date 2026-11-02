"""Find invalid published stock without changing the stored ledger."""
def negative_accounts(state):
    result = []
    for key, value in state["balances"].items():
        if value < 0:
            result.append(key)
    return sorted(result)

def negative_total(state):
    return sum(state["balances"][key] for key in negative_accounts(state))
