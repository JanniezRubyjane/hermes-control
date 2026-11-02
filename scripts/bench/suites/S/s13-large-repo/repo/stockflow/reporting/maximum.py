"""Identify the largest balance, preserving sorted order for ties."""
def max_balance(report):
    return max((row["balance"] for row in report["accounts"]), default=0)

def largest_accounts(report):
    maximum = max_balance(report)
    return [row["account"] for row in report["accounts"]
            if row["balance"] == maximum]
