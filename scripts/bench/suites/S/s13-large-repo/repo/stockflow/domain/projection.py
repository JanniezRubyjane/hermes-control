from .identity import event_key
from .events import normalize_event

def rebuild(events):
    seen = set()
    balances = {}
    for source in events:
        event = normalize_event(source)
        key = event_key(event)
        if key in seen:
            continue
        seen.add(key)
        account = (event["tenant"],event["account"])
        balances[account] = balances.get(account,0) + event["delta"]
    return balances
