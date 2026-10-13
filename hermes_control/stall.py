"""Conservative worker silence thresholds from Hermes plugin settings."""
from collections.abc import Mapping
import logging
import math
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STALL_SECONDS = 1200
DEFAULT_WAKE_GRACE_SECONDS = 300


def parse_settings(raw: Any) -> dict[str, float]:
    settings = {"stall_seconds": DEFAULT_STALL_SECONDS,
                "wake_grace_seconds": DEFAULT_WAKE_GRACE_SECONDS}
    if raw is None:
        return settings
    if not isinstance(raw, Mapping):
        logger.warning("hermes-control: stall settings must be a mapping; using defaults")
        return settings
    for key, minimum in (("stall_seconds", 660), ("wake_grace_seconds", 60)):
        value = raw.get(key, settings[key])
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < minimum):
            logger.warning("hermes-control: %s must be finite seconds >= %s; using default %s",
                           key, minimum, settings[key])
        else:
            settings[key] = value
    return settings


def read_settings() -> dict[str, float]:
    try:
        from hermes_cli.config import load_config_readonly
        from hermes_cli.plugins_state import _plugin_settings_entry

        entry = _plugin_settings_entry(load_config_readonly() or {}, "hermes_control") or {}
        return parse_settings(entry.get("settings"))
    except Exception:  # Settings must not strand an accepted run.
        logger.warning("hermes-control: could not read stall settings; using defaults",
                       exc_info=True)
        return parse_settings(None)
