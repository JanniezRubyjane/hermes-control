from parcel import Shipment
from parcel.manifest import summarize
def test_manifest():
    assert summarize([Shipment("economy", 501)]) == {"count": 1, "charge_cents": 220}
