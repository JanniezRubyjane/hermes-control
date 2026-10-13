"""The planner's configured provider and model, without checking credentials."""

from __future__ import annotations

from typing import Any


def status() -> dict[str, Any]:
    """Keep dashboard fields; unknown sign-in state is represented by None."""
    result = {"signed_in": None, "provider": "", "model": "", "source": "Hermes config",
              "detail": "sign-in state not checked"}
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly()
        model = config.get("model") if isinstance(config, dict) else None
        if isinstance(model, dict):
            result["provider"] = model.get("provider") if isinstance(model.get("provider"), str) else ""
            result["model"] = model.get("default") if isinstance(model.get("default"), str) else ""
        elif isinstance(model, str):
            result["model"] = model
    except Exception:  # noqa: BLE001 - config may be unavailable in another Hermes version
        result["source"] = ""
        result["detail"] = "Hermes config unavailable; sign-in state not checked"
    return result
