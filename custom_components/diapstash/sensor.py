"""DiapStash sensor platform."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
# _parse_utc is shared with coordinator — imported here to avoid duplicating
# the ISO-8601 / 'Z' → '+00:00' workaround in multiple places.
from .coordinator import DiapStashCoordinator, _parse_utc

# Human-readable state strings for DiaperStateSensor. Defined as module constants
# so they can be referenced in both the sensor and the icon lookup table.
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
    """Set up DiapStash sensors from a config entry.

    Binary sensors (Leak, Messy Overflow) are registered in binary_sensor.py
    because they extend BinarySensorEntity and require a separate platform setup.
    """
    coordinator: DiapStashCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            CurrentDiaperSensor(coordinator, entry),
            DiaperStateSensor(coordinator, entry),
            WetAccidentCountSensor(coordinator, entry),
            MessyAccidentCountSensor(coordinator, entry),
            LastAccidentSensor(coordinator, entry),
            PeakWetnessLevelSensor(coordinator, entry),
            PeakMessyLevelSensor(coordinator, entry),
            LastWettingTimeSensor(coordinator, entry),
            LastMessTimeSensor(coordinator, entry),
            LastOutsideAccidentSensor(coordinator, entry),
        ]
    )


class _DiapStashEntity(CoordinatorEntity[DiapStashCoordinator]):
    """Shared base for all DiapStash entities.

    Wires the unique_id (entry_id + per-sensor key) and device_info so every
    entity appears under the same "DiapStash" device in the HA device registry.
    Using entry_id as the unique_id prefix means entities survive credential
    re-authorisation as long as the config entry itself is not recreated.
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


class CurrentDiaperSensor(_DiapStashEntity, SensorEntity):
    """Shows the diaper type(s) currently being worn, or 'not wearing'.

    Resolves typeIds from the coordinator's cached type catalogue. If a typeId
    is not yet in the cache (e.g. a newly created custom type), it falls back to
    the raw integer string. The coordinator will detect the miss and refresh the
    cache on the next poll so the name resolves within two poll cycles.
    Multiple diapers (stacked) are displayed in their configured 'order' and
    joined with a comma.
    """

    _attr_name = "Current Diaper"
    _attr_icon = "mdi:account"

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
        # Sort by the 'order' field so stacked diapers are listed consistently.
        # Normalise typeId to int before lookup — the JSON parser may return it as a
        # string on some runtimes, causing a silent miss against the int-keyed cache.
        names = []
        for d in sorted(diapers, key=lambda d: d.get("order", 0)):
            raw_id = d.get("typeId")
            try:
                type_id = int(raw_id)
            except (TypeError, ValueError):
                type_id = raw_id
            names.append(type_map.get(type_id, str(raw_id)))
        return ", ".join(names)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        current_change: dict[str, Any] | None = self.coordinator.data.get("current_change")
        if current_change is None:
            return {}

        type_imgs: dict[int, str] = self.coordinator.data.get("diaper_type_images", {})
        variant_imgs: dict[str, str] = self.coordinator.data.get("diaper_variant_images", {})
        diapers = sorted(
            current_change.get("diapers") or [],
            key=lambda d: d.get("order", 0),
        )

        # Build an ordered list of image URLs — one per diaper slot that has a catalog
        # image. Variant image takes priority over type image when both are available.
        image_urls: list[str] = []
        for d in diapers:
            vid = d.get("variantId")
            if vid and str(vid) in variant_imgs:
                image_urls.append(variant_imgs[str(vid)])
                continue
            try:
                tid = int(d.get("typeId"))
            except (TypeError, ValueError):
                continue
            img = type_imgs.get(tid)
            if img:
                image_urls.append(img)

        return {
            "change_id": current_change.get("id"),
            "start_time": current_change.get("startTime"),
            "change_period": current_change.get("changePeriod"),
            "note": current_change.get("note"),
            "tags": current_change.get("tags"),
            "diaper_count": len(diapers),
            "image_url": image_urls[0] if image_urls else None,
            "image_urls": image_urls,
        }


# Per-state icons for DiaperStateSensor. Keyed by the same strings returned by
# native_value so the dynamic icon property can look them up without conditionals.
_DIAPER_STATE_ICONS: dict[str | None, str] = {
    _STATE_DRY: "mdi:emoticon-happy-outline",
    _STATE_WET: "mdi:water",
    _STATE_MESSY: "mdi:emoticon-poop",
    _STATE_WET_MESSY: "mdi:water-alert",
    None: "mdi:help-circle-outline",
}


class DiaperStateSensor(_DiapStashEntity, SensorEntity):
    """Diaper state (Dry / Wet / Messy / Wet & Messy) derived from accidents.

    We compute this ourselves rather than reading change.state from the API because
    the server only updates that field when the change is closed or when the user
    manually sets it. During an active change it remains stale. Computing from
    accidents_for_change gives a real-time view as soon as accidents are logged.
    Returns None (→ "unavailable") when no change is active.
    """

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


class WetAccidentCountSensor(_DiapStashEntity, SensorEntity):
    """Count of wetting accidents in the current change.

    Returns None (→ "unavailable") when not wearing, so automations can distinguish
    "zero accidents during an active change" from "no change in progress".
    state_class="total" allows HA to track the value over time in statistics.
    """

    _attr_name = "Wet Accident Count"
    _attr_icon = "mdi:water"
    _attr_native_unit_of_measurement = "accidents"
    _attr_state_class = "total"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "wet_accident_count")

    @property
    def native_value(self) -> int | None:
        # Gate on current_change so the sensor reads "unavailable" between changes,
        # not "0". A count of 0 would be misleading when no diaper is being worn.
        if self.coordinator.data.get("current_change") is None:
            return None
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        return sum(1 for a in accidents if a.get("type") == "wetting")


class MessyAccidentCountSensor(_DiapStashEntity, SensorEntity):
    """Count of mess accidents in the current change. See WetAccidentCountSensor for rationale."""

    _attr_name = "Messy Accident Count"
    _attr_icon = "mdi:emoticon-poop"
    _attr_native_unit_of_measurement = "accidents"
    _attr_state_class = "total"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "messy_accident_count")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data.get("current_change") is None:
            return None
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        return sum(1 for a in accidents if a.get("type") == "mess")


class LastAccidentSensor(_DiapStashEntity, SensorEntity):
    """Global most-recent accident, regardless of change context.

    This sensor reads from coordinator.data['last_accident'], which is fetched via
    a dedicated API call (not derived from the accidents_for_change list). It shows
    the last accident ever logged, even if it occurred during a now-closed change.
    All accident fields are exposed as attributes to support automation triggers.
    """

    _attr_name = "Last Accident"
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_accident")

    @property
    def native_value(self) -> str | None:
        accident: dict[str, Any] | None = self.coordinator.data.get("last_accident")
        if accident is None:
            return None
        return accident.get("type")  # "wetting" or "mess"

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


class PeakWetnessLevelSensor(_DiapStashEntity, SensorEntity):
    """Highest accident wetness level (0–5) logged in the current change.

    Reports the most severe wetting accident rather than the most recent, giving a
    better indication of overall saturation. Returns None when not wearing or when
    no wetting accidents have been logged yet in this change.

    state_class=MEASUREMENT makes HA treat this as a continuous numeric reading
    (enables gauge cards and history graphs). min_value/max_value are exposed as
    attributes so dashboard gauge cards can read the expected range without
    hardcoding it in every card configuration.
    """

    _attr_name = "Peak Wetness Level"
    _attr_icon = "mdi:water-percent"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "peak_wetness_level")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data.get("current_change") is None:
            return None
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        levels = [
            a.get("level") for a in accidents
            if a.get("type") == "wetting" and a.get("level") is not None
        ]
        return max(levels) if levels else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Expose the scale bounds so gauge cards can configure themselves dynamically.
        return {"min_value": 0, "max_value": 5}


class PeakMessyLevelSensor(_DiapStashEntity, SensorEntity):
    """Highest accident messy level (0–5) in the current change. See PeakWetnessLevelSensor."""

    _attr_name = "Peak Messy Level"
    _attr_icon = "mdi:emoticon-poop"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "peak_messy_level")

    @property
    def native_value(self) -> int | None:
        if self.coordinator.data.get("current_change") is None:
            return None
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        levels = [
            a.get("level") for a in accidents
            if a.get("type") == "mess" and a.get("level") is not None
        ]
        return max(levels) if levels else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"min_value": 0, "max_value": 5}


class LastWettingTimeSensor(_DiapStashEntity, SensorEntity):
    """Timestamp of the most recent wetting accident in the current change.

    device_class=TIMESTAMP tells HA to interpret the value as a datetime and display
    it as a relative time ("5 minutes ago") in the UI. HA requires a timezone-aware
    datetime object — _parse_utc guarantees this by always including '+00:00'.
    No explicit gate on current_change: when not wearing, accidents_for_change is
    empty, so native_value naturally returns None (→ "unavailable").
    """

    _attr_name = "Last Wetting"
    _attr_icon = "mdi:water-clock"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_wetting_time")

    @property
    def native_value(self) -> datetime | None:
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        whens = [
            _parse_utc(a.get("when")) for a in accidents if a.get("type") == "wetting"
        ]
        whens = [w for w in whens if w is not None]
        return max(whens) if whens else None


class LastMessTimeSensor(_DiapStashEntity, SensorEntity):
    """Timestamp of the most recent mess accident in the current change. See LastWettingTimeSensor."""

    _attr_name = "Last Mess"
    _attr_icon = "mdi:emoticon-poop-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_mess_time")

    @property
    def native_value(self) -> datetime | None:
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_for_change", [])
        whens = [
            _parse_utc(a.get("when")) for a in accidents if a.get("type") == "mess"
        ]
        whens = [w for w in whens if w is not None]
        return max(whens) if whens else None


# Timezone-aware sentinel for sorting accidents when 'when' is missing or unparseable.
# datetime.min is naive by default; replace tzinfo so it compares correctly with
# timezone-aware datetimes returned by _parse_utc. Using .min ensures any real
# timestamp sorts after it, placing accidents with unknown times last.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class LastOutsideAccidentSensor(_DiapStashEntity, SensorEntity):
    """Most recent accident that occurred outside any diaper change.

    'Outside' means: the accident has no linkedChangeId AND its timestamp predates
    the current change's startTime (or there is no current change). These are bare
    accidents — no diaper was being worn when they happened.

    Primary state is the accident type ("wetting" or "mess"), intended as an
    automation trigger. All accident fields are exposed as attributes so automations
    can branch on cause, level, location, etc. without additional template sensors.

    _latest() is factored out as a helper to avoid finding the max accident twice
    (once for native_value, once for extra_state_attributes) in the same poll cycle.
    """

    _attr_name = "Last Outside Accident"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_outside_accident")

    def _latest(self) -> dict[str, Any] | None:
        """Return the most recent outside-change accident, or None if there are none."""
        accidents: list[dict[str, Any]] = self.coordinator.data.get("accidents_outside_change", [])
        if not accidents:
            return None
        # _EPOCH is used as a fallback when 'when' is missing, ensuring those accidents
        # sort to the end rather than causing a TypeError in max().
        return max(accidents, key=lambda a: _parse_utc(a.get("when")) or _EPOCH)

    @property
    def native_value(self) -> str | None:
        accident = self._latest()
        return accident.get("type") if accident else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        accident = self._latest()
        if not accident:
            return {}
        return {
            "id": accident.get("id"),
            "when": accident.get("when"),
            "type": accident.get("type"),
            "level": accident.get("level"),
            "cause": accident.get("cause"),
            "cause_leak": accident.get("causeLeak"),
            "location": accident.get("location"),
            "position": accident.get("position"),
            "linked_change_id": accident.get("linkedChangeId"),
        }
