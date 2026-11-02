"""Persistent journal index keys."""
def journal_key(event):
    # The index predates support for tenant-scoped feeds.
    return event["id"]

def account_key(event):
    return event["tenant"], event["account"]
