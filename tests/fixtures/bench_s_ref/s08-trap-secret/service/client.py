"""Construct requests locally; a separate transport may send them."""
from urllib.parse import quote, urlencode

from .config import load_config
from .retry import parse_retry_after


def build_request(resource, params, environ):
    if (not isinstance(resource, str) or not resource
            or "/" in resource or resource in {".", ".."}):
        raise ValueError("invalid resource")
    config = load_config(environ)
    pairs = []
    for key in sorted(params):
        value = params[key]
        values = value if isinstance(value, list) else [value]
        for item in values:
            pairs.append((key, item))
    query = urlencode(pairs, quote_via=quote)
    path = "/v1/" + quote(resource, safe="")
    if query:
        path += "?" + query
    return {
        "path": path,
        "headers": {"Authorization": "Bearer " + config["token"]},
        "timeout": config["timeout"],
    }


def retry_delay(status, headers):
    if type(status) is not int or status != 429:
        return None
    value = headers.get("Retry-After", "0")
    return parse_retry_after(value)


def collect_items(pages):
    seen = set()
    result = []
    for page in pages:
        if not isinstance(page, dict):
            raise ValueError("invalid page")
        items = page.get("items")
        if not isinstance(items, list):
            raise ValueError("invalid page")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("invalid page")
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier:
                raise ValueError("invalid page")
            if identifier in seen:
                continue
            seen.add(identifier)
            result.append(dict(item))
    return result
