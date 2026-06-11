"""DiapStash sensor platform."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DiapStashCoordinator

_STATE_NOT_WEARING = "not wearing"
_STATE_DRY = "Dry"
_STATE_WET = "Wet"
_STATE_MESSY = "Messy"
_STATE_WET_MESSY = "Wet & Messy"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DiapStash sensors from a config entry."""
    coordinator: DiapStashCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            CurrentDiaperSensor(coordinator, entry),
            DiaperStateSensor(coordinator, entry),
            LastAccidentSensor(coordinator, entry),
        ]
    )


class _DiapStashEntity(CoordinatorEntity[DiapStashCoordinator]):
    """Base entity that wires a unique_id and device info."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "DiapStash",
            "manufacturer": "DiapStash",
            "entry_type": "service",
        }


class CurrentDiaperSensor(_DiapStashEntity, SensorEntity):
    """Shows the diaper type(s) currently being worn, or 'not wearing'."""

    _attr_name = "Current Diaper"
    _attr_icon = "mdi:baby"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_diaper")

    @property
    def native_value(self) -> str:
        current_change: dict[str, Any] | None = self.coordinator.data.get("current_change")
        if current_change is None:
            return _STATE_NOT_WEARING

        diapers: list[dict[str, Any]] = current_change.get("diapers") or []
        if not diapers:
            return "unknown"

        type_map: dict[int, str] = self.coordinator.data.get("diaper_types", {})
        names = [
            type_map.get(d.get("typeId"), str(d.get("typeId", "unknown")))
            for d in sorted(diapers, key=lambda d: d.get("order", 0))
        ]
        return ", ".join(names)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        current_change: dict[str, Any] | None = self.coordinator.data.get("current_change")
        if current_change is None:
            return {}
        return {
            "change_id": current_change.get("id"),
            "start_time": current_change.get("startTime"),
            "change_period": current_change.get("changePeriod"),
            "note": current_change.get("note"),
            "tags": current_change.get("tags"),
        }


_DIAPER_STATE_ICONS: dict[str | None, str] = {
    _STATE_DRY: "mdi:emoticon-happy-outline",
    _STATE_WET: "mdi:water",
    _STATE_MESSY: "mdi:emoticon-poop",
    _STATE_WET_MESSY: "mdi:water-alert",
    None: "mdi:help-circle-outline",
}


class DiaperStateSensor(_DiapStashEntity, SensorEntity):
    """Shows the current diaper state derived from linked accidents."""

    _attr_name = "Diaper State"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "diaper_state")

    @property
    def icon(self) -> str:
        return _DIAPER_STATE_ICONS.get(self.native_value, "mdi:help-circle-outline")

    @property
    def native_value(self) -> str | None:
        current_change: dict[str, Any] | None = self.coordinator.data.get("current_change")
        if current_change is None:
            return None  # renders as "unavailable" in HA

        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        has_wet = any(a.get("type") == "wetting" for a in accidents)
        has_messy = any(a.get("type") == "mess" for a in accidents)

        if has_wet and has_messy:
            return _STATE_WET_MESSY
        if has_wet:
            return _STATE_WET
        if has_messy:
            return _STATE_MESSY
        return _STATE_DRY

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        return {"accident_count": len(accidents)}


class LastAccidentSensor(_DiapStashEntity, SensorEntity):
    """Shows the most recent accident with all its properties as attributes."""

    _attr_name = "Last Accident"
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_accident")

    @property
    def native_value(self) -> str | None:
        accident: dict[str, Any] | None = self.coordinator.data.get("last_accident")
        if accident is None:
            return None
        # Primary state: accident type (wetting / mess)
        return accident.get("type")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        accident: dict[str, Any] | None = self.coordinator.data.get("last_accident")
        if not accident:
            return {}
        return {
            "id": accident.get("id"),
            "when": accident.get("when"),
            "precision_time": accident.get("precisionTime"),
            "level": accident.get("level"),
            "cause": accident.get("cause"),
            "cause_leak": accident.get("causeLeak"),
            "location": accident.get("location"),
            "position": accident.get("position"),
            "linked_change_id": accident.get("linkedChangeId"),
            "created_at": accident.get("createdAt"),
            "updated_at": accident.get("updatedAt"),
        }
