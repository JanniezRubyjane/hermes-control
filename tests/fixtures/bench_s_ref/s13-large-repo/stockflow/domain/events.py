"""Canonical events contain only fields participating in ledger semantics."""
from .identity import event_key
from .tenants import require_name
from .signs import require_delta
from ..imports.fields import event_fields


def normalize_event(event):
    tenant, identifier = event_key(event)
    fields = event_fields(event)
    account = require_name(fields["account"])
    delta = require_delta(fields["delta"])
    return {"tenant": tenant, "id": identifier, "account": account, "delta": delta}
