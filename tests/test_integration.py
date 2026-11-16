"""Tests for the DiapStash integration against a real Home Assistant core."""
from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.diapstash.const import CONF_SCAN_INTERVAL, DOMAIN, EVENT_ACCIDENT

from .conftest import CLIENT_ID, ago, make_accident, make_change, mock_api


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _coordinator(hass: HomeAssistant, entry: MockConfigEntry):
    return hass.data[DOMAIN][entry.entry_id]


def _state(hass: HomeAssistant, entity_id: str):
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} missing"
    return state


async def test_active_change(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """An active change with accidents populates every current-change sensor."""
    start = ago(hours=3, minutes=10)
    accidents = [  # newest first, as the API returns them (sort=when,desc)
        make_accident("a5", ago(minutes=10), "mess", level=3),
        make_accident("a4", ago(minutes=40), "wetting", level=4, leak=True),
        make_accident("t1", ago(hours=1), "wetting", location="toilet"),
        make_accident("a3", ago(hours=2), "wetting", level=1),
        make_accident("a2", ago(hours=5), "wetting", level=2),  # before change: outside
        make_accident("a1", ago(days=3), "mess", level=2, linked=7),  # older change
    ]
    mock_api(aioclient_mock, make_change(start), accidents)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    # Only the two documented endpoints are called, with the client-id header.
    assert aioclient_mock.call_count == 2
    for _method, _url, _data, headers in aioclient_mock.mock_calls:
        assert headers["DS-API-CLIENT-ID"] == CLIENT_ID

    current = _state(hass, "sensor.diapstash_current_diaper")
    assert current.state == "Main Diaper, Booster"  # sorted by 'order'
    assert current.attributes["image_url"] == "https://img/variant.png"  # variant first
    assert current.attributes["image_urls"] == ["https://img/variant.png", "https://img/booster.png"]
    assert current.attributes["diapers"][0]["brand"] == "BrandA"
    assert current.attributes["diaper_count"] == 2

    assert _state(hass, "sensor.diapstash_diaper_state").state == "Wet & Messy"
    assert _state(hass, "sensor.diapstash_wet_accident_count").state == "2"
    assert _state(hass, "sensor.diapstash_messy_accident_count").state == "1"
    assert _state(hass, "sensor.diapstash_peak_wetness_level").state == "4"
    assert _state(hass, "sensor.diapstash_peak_messy_level").state == "3"
    assert _state(hass, "binary_sensor.diapstash_leak").state == "on"
    assert _state(hass, "binary_sensor.diapstash_messy_overflow").state == "off"
    assert _state(hass, "sensor.diapstash_change_started").state != "unknown"
    assert int(_state(hass, "sensor.diapstash_time_in_diaper").state) in (189, 190)

    change_acc = _state(hass, "sensor.diapstash_change_accidents")
    assert change_acc.state == "3"
    attrs = change_acc.attributes
    assert [a["id"] for a in attrs["accidents"]] == ["a3", "a4", "a5"]  # oldest first
    assert attrs["wetting"] == 2 and attrs["mess"] == 1
    assert attrs["toilet_visits"] == 1
    assert attrs["minutes_dry_before_first_accident"] in (69, 70)
    assert attrs["minutes_since_last_accident"] in (9, 10)

    assert _state(hass, "sensor.diapstash_last_accident").state == "mess"
    assert _state(hass, "sensor.diapstash_last_accident").attributes["id"] == "a5"
    outside = _state(hass, "sensor.diapstash_last_outside_accident")
    assert outside.attributes["id"] == "a2"

    week = _state(hass, "sensor.diapstash_accidents_last_7_days")
    assert week.state == "5"  # toilet visit excluded
    assert week.attributes["toilet_visits"] == 1
    assert [a["id"] for a in week.attributes["history"]] == ["a5", "a4", "t1", "a3", "a2", "a1"]

    quota = _state(hass, "sensor.diapstash_api_requests_remaining")
    assert quota.state == "118"
    assert quota.attributes["limit"] == 120
    assert quota.attributes["current_interval_seconds"] == 300


async def test_not_wearing(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A closed latest change means not wearing; change sensors are unknown."""
    change = make_change(ago(hours=5), end=ago(hours=1))
    mock_api(aioclient_mock, change, [make_accident("a1", ago(minutes=30), "mess")])
    await _setup(hass, config_entry)

    assert _state(hass, "sensor.diapstash_current_diaper").state == "not wearing"
    for entity_id in (
        "sensor.diapstash_diaper_state",
        "sensor.diapstash_change_started",
        "sensor.diapstash_time_in_diaper",
        "sensor.diapstash_change_accidents",
        "sensor.diapstash_wet_accident_count",
        "binary_sensor.diapstash_leak",
    ):
        assert _state(hass, entity_id).state == "unknown", entity_id
    assert _state(hass, "sensor.diapstash_last_outside_accident").state == "mess"
    assert _state(hass, "sensor.diapstash_accidents_today").state in ("0", "1")  # near midnight


async def test_no_change_ever(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """latest-change returns 404 for a user without changes: still loads."""
    mock_api(aioclient_mock, None, [])
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor.diapstash_current_diaper").state == "not wearing"
    assert _state(hass, "sensor.diapstash_last_accident").state == "unknown"
    assert _state(hass, "sensor.diapstash_accidents_last_7_days").state == "0"


async def test_new_accident_fires_event(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Existing accidents are not announced; a newly logged one fires one event."""
    events = []
    hass.bus.async_listen(EVENT_ACCIDENT, events.append)

    change = make_change(ago(hours=1))
    old = make_accident("old", ago(minutes=50))
    mock_api(aioclient_mock, change, [old])
    await _setup(hass, config_entry)
    assert events == []

    new = make_accident("new", ago(minutes=1), "mess", level=3)
    mock_api(aioclient_mock, change, [new, old])
    await _coordinator(hass, config_entry).async_refresh()
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["id"] == "new" and data["type"] == "mess" and data["during_change"] is True

    # A further poll with the same data fires nothing.
    await _coordinator(hass, config_entry).async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_interval_stretches_when_quota_low(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Low remaining quota spreads polls over the rest of the window."""
    mock_api(aioclient_mock, None, [], remaining=24, reset=3000)
    await _setup(hass, config_entry)
    # (24 - 4 reserve) // 2 calls = 10 polls left over 3000 s -> 300 s = configured
    assert _coordinator(hass, config_entry).update_interval == timedelta(seconds=300)

    mock_api(aioclient_mock, None, [], remaining=14, reset=3000)
    await _coordinator(hass, config_entry).async_refresh()
    # (14 - 4) // 2 = 5 polls over 3000 s -> 600 s
    assert _coordinator(hass, config_entry).update_interval == timedelta(seconds=600)

    mock_api(aioclient_mock, None, [], remaining=5, reset=1000)
    await _coordinator(hass, config_entry).async_refresh()
    # no full poll left -> wait for the reset
    assert _coordinator(hass, config_entry).update_interval == timedelta(seconds=1005)

    mock_api(aioclient_mock, None, [], remaining=119, reset=3599)
    await _coordinator(hass, config_entry).async_refresh()
    assert _coordinator(hass, config_entry).update_interval == timedelta(minutes=5)


async def test_rate_limited(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 429 backs off until the window resets and marks entities unavailable."""
    mock_api(aioclient_mock, None, [])
    await _setup(hass, config_entry)

    mock_api(aioclient_mock, None, [], remaining=0, reset=1200, change_status=429)
    coordinator = _coordinator(hass, config_entry)
    await coordinator.async_refresh()
    assert coordinator.last_update_success is False
    assert coordinator.update_interval == timedelta(seconds=1205)
    assert _state(hass, "sensor.diapstash_current_diaper").state == "unavailable"


async def test_missing_scope_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """HTTP 403 (missing scope) asks the user to re-authorize."""
    mock_api(aioclient_mock, None, [], accidents_status=403)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)


async def test_options_flow_sets_interval(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """The options flow stores the interval and the reloaded coordinator uses it."""
    mock_api(aioclient_mock, None, [])
    await _setup(hass, config_entry)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 15}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert config_entry.options == {CONF_SCAN_INTERVAL: 15}
    assert _coordinator(hass, config_entry).update_interval == timedelta(minutes=15)


async def test_single_config_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """A second DiapStash entry cannot be added."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
