# hc-bench-canary: 7617c87a600124a9
from parcel import Shipment
from parcel.manifest import summarize, summarize_by_service
def test_groups():
    rows = [Shipment("express", 1), Shipment("economy", 500), Shipment("economy", 501), Shipment("express", 1001)]
    original = rows[:]
    assert summarize_by_service(iter(rows)) == [
        {"service": "economy", "count": 2, "weight_grams": 1001, "charge_cents": 400},
        {"service": "express", "count": 2, "weight_grams": 1002, "charge_cents": 850}]
    assert rows == original
    assert summarize(rows)["charge_cents"] == 1250
def test_empty_and_single():
    assert summarize_by_service(iter(())) == []
    assert summarize_by_service([Shipment("economy", 1)])[0]["charge_cents"] == 180
