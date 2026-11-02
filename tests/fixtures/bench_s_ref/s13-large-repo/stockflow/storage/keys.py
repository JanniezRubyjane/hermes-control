"""Journal and balance keys preserve tenant scope."""
from ..domain.identity import event_key


def journal_key(event):
    return event_key(event)


def account_key(event):
    return event["tenant"], event["account"]
