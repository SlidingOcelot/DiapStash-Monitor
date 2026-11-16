"""Logbook descriptions for DiapStash accident events."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_ACCIDENT

_TYPE_TEXT = {"wetting": "Wetting", "mess": "Mess"}
_LOCATION_TEXT = {"diaper": "in diaper", "toilet": "on the toilet", "self": "without diaper"}


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    """Describe DiapStash accident events in the logbook."""

    @callback
    def _describe(event: Event) -> dict[str, Any]:
        data = event.data
        parts = [_TYPE_TEXT.get(data.get("type"), "Accident")]
        location = _LOCATION_TEXT.get(data.get("location"))
        if location:
            parts.append(location)
        if data.get("level") is not None:
            parts.append(f"level {data['level']:g}")
        if data.get("cause_leak"):
            parts.append("leaked")
        return {"name": "DiapStash", "message": "logged: " + ", ".join(parts)}

    async_describe_event(DOMAIN, EVENT_ACCIDENT, _describe)
