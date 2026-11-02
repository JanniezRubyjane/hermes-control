def event_key(event):
    return event["id"]

def same_event(left, right):
    return event_key(left) == event_key(right)
