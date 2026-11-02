"""Validate each request's environment without retaining credentials."""
import re


def load_config(environ):
    token = environ.get("SERVICE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("SERVICE_TOKEN is required")
    if "\r" in token or "\n" in token:
        raise ValueError("SERVICE_TOKEN must be one line")
    timeout_text = environ.get("SERVICE_TIMEOUT", "30")
    if not re.fullmatch(r"[0-9]+", timeout_text):
        raise ValueError("SERVICE_TIMEOUT must be 1..120")
    timeout = int(timeout_text)
    if not 1 <= timeout <= 120:
        raise ValueError("SERVICE_TIMEOUT must be 1..120")
    return {"token": token, "timeout": timeout}
