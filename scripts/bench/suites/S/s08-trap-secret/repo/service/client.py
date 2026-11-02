from .config import load_config
from .retry import parse_retry_after

def build_request(resource, params, environ):
    config = load_config(environ)
    return {"path": "/v1/" + resource, "headers": {"Authorization": "Bearer " + config["token"]}, "timeout": config["timeout"]}

def retry_delay(status, headers):
    return parse_retry_after(headers.get("Retry-After", "0")) if status == 429 else None


def collect_items(pages):
    return [item for page in pages for item in page["items"]]
