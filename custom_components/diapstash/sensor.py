"""DiapStash sensor platform."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import ACCIDENT_LOCATION_TOILET, DOMAIN, HISTORY_LENGTH
from .coordinator import DiapStashCoordinator, _parse_utc, accident_summary

# Human-readable state strings for DiaperStateSensor.
_STATE_NOT_WEARING = "not wearing"
_STATE_DRY = "Dry"
_STATE_WET = "Wet"
_STATE_MESSY = "Messy"
_STATE_WET_MESSY = "Wet & Messy"

# Timezone-aware sentinel for sorting accidents whose 'when' is missing or unparseable.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DiapStash sensors from a config entry.

    Binary sensors (Leak, Messy Overflow) are registered in binary_sensor.py.
    """
    coordinator: DiapStashCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            CurrentDiaperSensor(coordinator, entry),
            DiaperStateSensor(coordinator, entry),
            ChangeStartedSensor(coordinator, entry),
            TimeInDiaperSensor(coordinator, entry),
            ChangeAccidentsSensor(coordinator, entry),
            WetAccidentCountSensor(coordinator, entry),
            MessyAccidentCountSensor(coordinator, entry),
            PeakWetnessLevelSensor(coordinator, entry),
            PeakMessyLevelSensor(coordinator, entry),
            LastWettingTimeSensor(coordinator, entry),
            LastMessTimeSensor(coordinator, entry),
            LastAccidentSensor(coordinator, entry),
            LastOutsideAccidentSensor(coordinator, entry),
            AccidentsTodaySensor(coordinator, entry),
            AccidentsWeekSensor(coordinator, entry),
            ApiQuotaSensor(coordinator, entry),
        ]
    )


def _when(accident: dict[str, Any]) -> datetime:
    return _parse_utc(accident.get("when")) or _EPOCH


def _minutes_between(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() // 60)


def _type_counts(accidents: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "wetting": sum(1 for a in accidents if a.get("type") == "wetting"),
        "mess": sum(1 for a in accidents if a.get("type") == "mess"),
    }


class _DiapStashEntity(CoordinatorEntity[DiapStashCoordinator]):
    """Shared base for all DiapStash entities.

    unique_id is entry_id + per-sensor key, and every entity is grouped under one
    "DiapStash" device. Entities survive re-authorisation because the config entry
    is kept.
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

    @property
    def _current_change(self) -> dict[str, Any] | None:
        return self.coordinator.data.get("current_change")

    @property
    def _change_accidents(self) -> list[dict[str, Any]]:
        """Accidents of the current change (toilet visits excluded), newest first."""
        return self.coordinator.data.get("accidents_for_change", [])


class _MinuteTickEntity(_DiapStashEntity):
    """Entity whose value depends on the current time.

    Rewrites its state every minute between polls (no API calls), so durations,
    "today" counts and "minutes since" attributes stay current even with a long
    update interval.
    """

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        @callback
        def _tick(_now: datetime) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_interval(self.hass, _tick, timedelta(minutes=1))
        )


# --- Current change ---------------------------------------------------------------


class CurrentDiaperSensor(_DiapStashEntity, SensorEntity):
    """Diaper type(s) currently being worn, or 'not wearing'.

    Names and pictures come inline with the latest change. Stacked diapers are
    listed in wearing order, comma-separated.
    """

    _attr_name = "Current Diaper"
    _attr_icon = "mdi:account"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_diaper")

    @property
    def native_value(self) -> str:
        if self._current_change is None:
            return _STATE_NOT_WEARING
        diapers = self.coordinator.data.get("diapers") or []
        if not diapers:
            return "unknown"
        return ", ".join(d["name"] for d in diapers)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        change = self._current_change
        if change is None:
            return {}
        diapers = self.coordinator.data.get("diapers") or []
        image_urls = [d["image_url"] for d in diapers if d.get("image_url")]
        return {
            "change_id": change.get("id"),
            "start_time": change.get("startTime"),
            "change_period": change.get("changePeriod"),
            "note": change.get("note"),
            "tags": change.get("tags"),
            "diaper_count": len(diapers),
            "diapers": diapers,
            "image_url": image_urls[0] if image_urls else None,
            "image_urls": image_urls,
        }


_DIAPER_STATE_ICONS: dict[str | None, str] = {
    _STATE_DRY: "mdi:emoticon-happy-outline",
    _STATE_WET: "mdi:water",
    _STATE_MESSY: "mdi:emoticon-poop",
    _STATE_WET_MESSY: "mdi:water-alert",
    None: "mdi:help-circle-outline",
}


class DiaperStateSensor(_DiapStashEntity, SensorEntity):
    """Diaper state (Dry / Wet / Messy / Wet & Messy) derived from accidents.

    The server only updates change.state when a change is closed or edited, so the
    state is computed from the accidents logged during the change instead.
    """

    _attr_name = "Diaper State"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "diaper_state")

    @property
    def icon(self) -> str:
        return _DIAPER_STATE_ICONS.get(self.native_value, "mdi:help-circle-outline")

    @property
    def native_value(self) -> str | None:
        if self._current_change is None:
            return None
        counts = _type_counts(self._change_accidents)
        if counts["wetting"] and counts["mess"]:
            return _STATE_WET_MESSY
        if counts["wetting"]:
            return _STATE_WET
        if counts["mess"]:
            return _STATE_MESSY
        return _STATE_DRY

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"accident_count": len(self._change_accidents)}


class ChangeStartedSensor(_DiapStashEntity, SensorEntity):
    """When the current diaper was put on. Shown as relative time in the UI."""

    _attr_name = "Change Started"
    _attr_icon = "mdi:clock-start"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "change_started")

    @property
    def native_value(self) -> datetime | None:
        if self._current_change is None:
            return None
        return _parse_utc(self._current_change.get("startTime"))


class TimeInDiaperSensor(_MinuteTickEntity, SensorEntity):
    """Minutes since the current diaper was put on; updates every minute.

    Unknown while not wearing. Use it in automations such as "remind me after
    4 hours" (above 240).
    """

    _attr_name = "Time in Diaper"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "time_in_diaper")

    @property
    def native_value(self) -> int | None:
        if self._current_change is None:
            return None
        start = _parse_utc(self._current_change.get("startTime"))
        minutes = _minutes_between(start, dt_util.utcnow())
        return max(minutes, 0) if minutes is not None else None


class ChangeAccidentsSensor(_MinuteTickEntity, SensorEntity):
    """Number of accidents during the current change, with a timestamped list.

    Attributes list every accident of the change (oldest first) plus first/last
    times, how long the diaper stayed dry and how long since the last accident.
    Toilet visits during the change are counted separately and not in the state.
    """

    _attr_name = "Change Accidents"
    _attr_icon = "mdi:format-list-numbered"
    _attr_native_unit_of_measurement = "accidents"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The list can be long; keep it out of the recorder database.
    _unrecorded_attributes = frozenset({"accidents"})

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "change_accidents")

    @property
    def native_value(self) -> int | None:
        if self._current_change is None:
            return None
        return len(self._change_accidents)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        change = self._current_change
        if change is None:
            return {}
        accidents = sorted(self._change_accidents, key=_when)
        start = _parse_utc(change.get("startTime"))
        first = _parse_utc(accidents[0].get("when")) if accidents else None
        last = _parse_utc(accidents[-1].get("when")) if accidents else None

        toilet_visits = 0
        if start is not None:
            toilet_visits = sum(
                1
                for a in self.coordinator.data.get("accidents", [])
                if a.get("location") == ACCIDENT_LOCATION_TOILET
                and (w := _parse_utc(a.get("when"))) is not None
                and w >= start
            )

        return {
            **_type_counts(accidents),
            "toilet_visits": toilet_visits,
            "first_accident": first.isoformat() if first else None,
            "last_accident": last.isoformat() if last else None,
            "minutes_dry_before_first_accident": _minutes_between(start, first),
            "minutes_since_last_accident": _minutes_between(last, dt_util.utcnow()),
            "accidents": [accident_summary(a) for a in accidents],
        }


class _ChangeCountSensor(_DiapStashEntity, SensorEntity):
    """Count of accidents of one type in the current change; unknown when not wearing."""

    _attr_native_unit_of_measurement = "accidents"
    _attr_state_class = "total"
    _accident_type: str

    @property
    def native_value(self) -> int | None:
        if self._current_change is None:
            return None
        return _type_counts(self._change_accidents)[self._accident_type]


class WetAccidentCountSensor(_ChangeCountSensor):
    _attr_name = "Wet Accident Count"
    _attr_icon = "mdi:water"
    _accident_type = "wetting"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "wet_accident_count")


class MessyAccidentCountSensor(_ChangeCountSensor):
    _attr_name = "Messy Accident Count"
    _attr_icon = "mdi:emoticon-poop"
    _accident_type = "mess"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "messy_accident_count")


class _PeakLevelSensor(_DiapStashEntity, SensorEntity):
    """Highest level (0–5) of one accident type in the current change."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _accident_type: str

    @property
    def native_value(self) -> int | None:
        if self._current_change is None:
            return None
        levels = [
            a["level"]
            for a in self._change_accidents
            if a.get("type") == self._accident_type and a.get("level") is not None
        ]
        return max(levels) if levels else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Scale bounds for gauge cards.
        return {"min_value": 0, "max_value": 5}


class PeakWetnessLevelSensor(_PeakLevelSensor):
    _attr_name = "Peak Wetness Level"
    _attr_icon = "mdi:water-percent"
    _accident_type = "wetting"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "peak_wetness_level")


class PeakMessyLevelSensor(_PeakLevelSensor):
    _attr_name = "Peak Messy Level"
    _attr_icon = "mdi:emoticon-poop"
    _accident_type = "mess"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "peak_messy_level")


class _LastOfTypeTimeSensor(_DiapStashEntity, SensorEntity):
    """Time of the most recent accident of one type in the current change."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _accident_type: str

    @property
    def native_value(self) -> datetime | None:
        whens = [
            w
            for a in self._change_accidents
            if a.get("type") == self._accident_type
            and (w := _parse_utc(a.get("when"))) is not None
        ]
        return max(whens) if whens else None


class LastWettingTimeSensor(_LastOfTypeTimeSensor):
    _attr_name = "Last Wetting"
    _attr_icon = "mdi:water-clock"
    _accident_type = "wetting"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_wetting_time")


class LastMessTimeSensor(_LastOfTypeTimeSensor):
    _attr_name = "Last Mess"
    _attr_icon = "mdi:emoticon-poop-outline"
    _accident_type = "mess"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_mess_time")


# --- Accidents overall ----------------------------------------------------------


class LastAccidentSensor(_DiapStashEntity, SensorEntity):
    """Most recent accident overall, regardless of change context.

    Toilet visits are not accidents and are excluded, matching the counts; the most
    recent toilet visit is exposed in the last_toilet_visit attributes instead.
    """

    _attr_name = "Last Accident"
    _attr_icon = "mdi:water-alert"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_accident")

    @property
    def native_value(self) -> str | None:
        accident = self.coordinator.data.get("last_accident")
        return accident.get("type") if accident else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        accident = self.coordinator.data.get("last_accident")
        toilet = self.coordinator.data.get("last_toilet_visit")
        attrs: dict[str, Any] = {}
        if accident:
            attrs = {
                **accident_summary(accident),
                "created_at": accident.get("createdAt"),
                "updated_at": accident.get("updatedAt"),
            }
        attrs["last_toilet_visit"] = toilet.get("when") if toilet else None
        attrs["last_toilet_visit_type"] = toilet.get("type") if toilet else None
        return attrs


class LastOutsideAccidentSensor(_DiapStashEntity, SensorEntity):
    """Most recent accident that happened while not wearing a diaper.

    'Outside' means: no linkedChangeId and before the current change started (or
    no change is active). Toilet visits are not included.
    """

    _attr_name = "Last Outside Accident"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "last_outside_accident")

    def _latest(self) -> dict[str, Any] | None:
        accidents = self.coordinator.data.get("accidents_outside_change", [])
        return max(accidents, key=_when) if accidents else None

    @property
    def native_value(self) -> str | None:
        accident = self._latest()
        return accident.get("type") if accident else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        accident = self._latest()
        return accident_summary(accident) if accident else {}


class _AccidentPeriodSensor(_MinuteTickEntity, SensorEntity):
    """Accidents since a point in time (toilet visits counted separately)."""

    _attr_native_unit_of_measurement = "accidents"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def _since(self) -> datetime:
        raise NotImplementedError

    def _in_period(self) -> list[dict[str, Any]]:
        since = self._since()
        return [
            a
            for a in self.coordinator.data.get("accidents", [])
            if (w := _parse_utc(a.get("when"))) is not None and w >= since
        ]

    @property
    def native_value(self) -> int:
        return sum(
            1 for a in self._in_period() if a.get("location") != ACCIDENT_LOCATION_TOILET
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        period = self._in_period()
        not_toilet = [a for a in period if a.get("location") != ACCIDENT_LOCATION_TOILET]
        return {
            **_type_counts(not_toilet),
            "toilet_visits": len(period) - len(not_toilet),
            "since": self._since().isoformat(),
        }


class AccidentsTodaySensor(_AccidentPeriodSensor):
    """Accidents since local midnight."""

    _attr_name = "Accidents Today"
    _attr_icon = "mdi:calendar-today"

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "accidents_today")

    def _since(self) -> datetime:
        return dt_util.start_of_local_day()


class AccidentsWeekSensor(_AccidentPeriodSensor):
    """Accidents in the last 7 days, with the most recent accidents as history.

    The 'history' attribute lists the last HISTORY_LENGTH accidents (newest first,
    toilet visits included) for markdown cards and templates.
    """

    _attr_name = "Accidents Last 7 Days"
    _attr_icon = "mdi:history"
    _unrecorded_attributes = frozenset({"history"})

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "accidents_7_days")

    def _since(self) -> datetime:
        return dt_util.utcnow() - timedelta(days=7)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        recent = self.coordinator.data.get("accidents", [])[:HISTORY_LENGTH]
        return {
            **super().extra_state_attributes,
            "history": [accident_summary(a) for a in recent],
        }


# --- Diagnostics ----------------------------------------------------------------


class ApiQuotaSensor(_DiapStashEntity, SensorEntity):
    """API requests left in the current rate-limit window (diagnostic)."""

    _attr_name = "API Requests Remaining"
    _attr_icon = "mdi:speedometer"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "requests"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: DiapStashCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "api_requests_remaining")

    @property
    def native_value(self) -> int | None:
        return self.coordinator.ratelimit.get("remaining")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        interval = self.coordinator.update_interval
        return {
            "limit": self.coordinator.ratelimit.get("limit"),
            "resets_in_seconds": self.coordinator.ratelimit.get("reset"),
            "configured_interval_minutes": int(
                self.coordinator.configured_interval.total_seconds() // 60
            ),
            "current_interval_seconds": int(interval.total_seconds()) if interval else None,
        }
