"""The service accepts integer seconds or an ordered reset duration."""
import re

_DURATION = re.compile(r"(?:(?P<h>[0-9]+)h)?(?:(?P<m>[0-9]+)m)?(?:(?P<s>[0-9]+)s)?")


def parse_retry_after(text):
    text = text.strip()
    if re.fullmatch(r"[0-9]+", text):
        return int(text)
    prefix = "Resets in "
    if not text.startswith(prefix):
        raise ValueError("invalid retry delay")
    duration = text[len(prefix):]
    match = _DURATION.fullmatch(duration)
    if not duration or match is None:
        raise ValueError("invalid retry delay")
    hours = int(match["h"] or 0)
    minutes = int(match["m"] or 0)
    seconds = int(match["s"] or 0)
    return hours * 3600 + minutes * 60 + seconds
