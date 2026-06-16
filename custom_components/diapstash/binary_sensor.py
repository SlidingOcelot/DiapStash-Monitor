"""DiapStash binary sensor platform."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DiapStashCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DiapStash binary sensors from a config entry."""
    coordinator: DiapStashCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LeakBinarySensor(coordinator, entry),
            MessyOverflowBinarySensor(coordinator, entry),
        ]
    )


class _DiapStashBinaryEntity(CoordinatorEntity[DiapStashCoordinator], BinarySensorEntity):
    """Shared base for DiapStash binary sensors.

    Mirrors the pattern in sensor.py: unique_id is entry_id + key, device_info
    groups all entities under the same "DiapStash" device.
    """

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


class LeakBinarySensor(_DiapStashBinaryEntity):
    """On when any accident in the current change has causeLeak=true.

    This is derived from accidents rather than the server-side change.leak field
    because change.leak (like change.state) is only updated when the change closes.
    Reading causeLeak from individual accidents gives a real-time view the moment
    the accident is logged.

    Returns None (→ "unavailable") when not wearing so automations can distinguish
    "no leak during an active change" from "not wearing".
    """

    _attr_name = "Leak"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "leak")

    @property
    def is_on(self) -> bool | None:
        # Return None (unavailable) when not wearing — False would be misleading.
        if self.coordinator.data.get("current_change") is None:
            return None
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        return any(a.get("causeLeak") for a in accidents)


class MessyOverflowBinarySensor(_DiapStashBinaryEntity):
    """On when the current change has a messy overflow.

    Unlike the Leak sensor, this reads change.messyOverflow directly from the
    change object rather than from individual accidents. The Accident schema has
    no per-accident overflow field (only causeLeak for leaks), so an accident-
    derived approach is not possible here without making assumptions about level
    thresholds.

    The trade-off: this value may lag behind reality until the server updates the
    change record, but it is accurate for all cases where the overflow flag is set.
    Returns None (→ "unavailable") when not wearing.
    """

    _attr_name = "Messy Overflow"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "messy_overflow")

    @property
    def is_on(self) -> bool | None:
        current_change: dict[str, Any] | None = self.coordinator.data.get("current_change")
        if current_change is None:
            return None
        return bool(current_change.get("messyOverflow"))
