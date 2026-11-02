"""Stage a complete batch before publishing balances or accepted events."""
from ..domain.events import normalize_event
from .keys import journal_key
from .balances import next_balance
from .snapshot import copy_state, publish_state


def empty_state():
    return {"events": {}, "balances": {}}


def append_batch(state, events):
    staged = copy_state(state)
    accepted = staged["events"]
    balances = staged["balances"]
    for source in events:
        event = normalize_event(source)
        key = journal_key(event)
        if key in accepted:
            if accepted[key] != event:
                raise ValueError("conflicting event")
            continue
        account = (event["tenant"], event["account"])
        balance = next_balance(balances, event)
        accepted[key] = event
        balances[account] = balance
    publish_state(state, staged)
