"""Account selection shared by reports and API views."""
def accounts_in(events):
    accounts = set()
    for row in events:
        accounts.add((row["tenant"], row["account"]))
    return sorted(accounts)

def account_rows(balances, tenant):
    rows = []
    for (owner, account), balance in balances.items():
        if owner == tenant:
            rows.append({"account": account, "balance": balance})
    return sorted(rows, key=lambda row: row["account"])
