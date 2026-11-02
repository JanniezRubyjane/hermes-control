from .rates import quote
def summarize(shipments):
    rows = list(shipments)
    return {"count": len(rows), "charge_cents": sum(map(quote, rows))}
def summarize_by_service(shipments):
    groups = {}
    for shipment in shipments:
        row = groups.setdefault(shipment.service, dict(service=shipment.service, count=0, weight_grams=0, charge_cents=0))
        row["count"] += 1
        row["weight_grams"] += shipment.weight_grams
        row["charge_cents"] += quote(shipment)
    return [groups[key] for key in sorted(groups)]
