
def unique_events(events):
    seen = set()
    result = []
    for event in events:
        key = event["id"]
        if key not in seen:
            seen.add(key)
            result.append(event)
    return result
