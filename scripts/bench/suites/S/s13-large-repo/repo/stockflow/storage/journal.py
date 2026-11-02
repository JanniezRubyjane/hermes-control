from ..domain.events import normalize_event
from ..domain.identity import event_key

def empty_state():
    return {"events":{},"balances":{}}

def append_batch(state, events):
    for source in events:
        event = normalize_event(source)
        key = event_key(event)
        if key in state["events"]:
            continue
        state["events"][key] = event
        account = (event["tenant"],event["account"])
        state["balances"][account] = state["balances"].get(account,0)+event["delta"]
