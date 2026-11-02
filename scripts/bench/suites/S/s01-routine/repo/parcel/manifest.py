from .rates import quote
def summarize(shipments):
    rows = list(shipments)
    return {"count": len(rows), "charge_cents": sum(map(quote, rows))}
