"""Look up account details without exposing mutable report rows."""
def find_account(report, account):
    for row in report["accounts"]:
        if row["account"] == account:
            return dict(row)
    return None

def reported_balance(report, account, default=0):
    row = find_account(report, account)
    return default if row is None else row["balance"]
