from service.client import build_request, retry_delay

def test_existing():
    assert build_request("items", {}, {"SERVICE_TOKEN":"demo"})["path"] == "/v1/items"
    assert retry_delay(200, {}) is None
