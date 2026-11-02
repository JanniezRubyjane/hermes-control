"""Opaque identity components must never be joined or normalized."""
from collections.abc import Mapping


def event_key(event):
    if not isinstance(event, Mapping):
        raise ValueError("invalid event")
    tenant = event.get("tenant")
    identifier = event.get("id")
    if (not isinstance(tenant, str) or not tenant
            or not isinstance(identifier, str) or not identifier):
        raise ValueError("invalid event")
    return tenant, identifier


def same_event(left, right):
    return event_key(left) == event_key(right)
