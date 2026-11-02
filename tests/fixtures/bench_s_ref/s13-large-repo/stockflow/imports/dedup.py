from ..domain.identity import event_key


def unique_events(events):
    seen = set()
    result = []
    for event in events:
        key = event_key(event)
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result
