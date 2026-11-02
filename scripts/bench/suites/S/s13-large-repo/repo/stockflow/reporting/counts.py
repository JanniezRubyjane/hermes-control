"""Account counts used by report previews."""
from .zeros import zero_accounts

def account_count(report):
    return len(report["accounts"])

def activity_counts(report):
    zero = len(zero_accounts(report))
    return {"active": account_count(report) - zero, "zero": zero}
