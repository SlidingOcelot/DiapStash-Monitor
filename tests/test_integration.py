"""Tests for the DiapStash integration against a real Home Assistant core."""
from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.diapstash.const import CONF_SCAN_INTERVAL, DOMAIN, EVENT_ACCIDENT

from .conftest import CLIENT_ID, ago, make_accident, make_change, make_stats, mock_api


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
        make_accident("a2", ago(hours=5), "wetting", level=2, location="self"),  # outside
        make_accident("d1", ago(hours=6), "wetting", level=2),  # in diaper, unlinked: not outside
        make_accident("a1", ago(days=3), "mess", level=2, linked=7),  # older change
    ]
    mock_api(aioclient_mock, make_change(start), accidents)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    # latest-change + accidents, plus stats, streak and changes on the first poll,
    # all with the client-id header.
    assert aioclient_mock.call_count == 5
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
    assert week.state == "6"  # toilet visit excluded
    assert week.attributes["toilet_visits"] == 1
    assert [a["id"] for a in week.attributes["history"]] == ["a5", "a4", "t1", "a3", "a2", "d1", "a1"]

    quota = _state(hass, "sensor.diapstash_api_requests_remaining")
    assert quota.state == "118"
    assert quota.attributes["limit"] == 120
    assert quota.attributes["current_interval_seconds"] == 300


async def test_not_wearing(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A closed latest change means not wearing; change sensors are unknown."""
    change = make_change(ago(hours=5), end=ago(hours=1))
    mock_api(aioclient_mock, change, [make_accident("a1", ago(minutes=30), "mess", location="self")])
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


async def test_events_survive_restart(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """After a restart, recent unseen accidents are announced; old and seen ones not."""
    events = []
    hass.bus.async_listen(EVENT_ACCIDENT, events.append)

    old = make_accident("old", ago(hours=3))
    mock_api(aioclient_mock, None, [old])
    await _setup(hass, config_entry)
    assert events == []  # first-ever poll only seeds the stored IDs

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    # Logged while HA was down: one 5 minutes ago, one 2 hours ago (too old to alert).
    recent = make_accident("recent", ago(minutes=5), "mess")
    stale = make_accident("stale", ago(hours=2))
    mock_api(aioclient_mock, None, [recent, stale, old])
    await _setup(hass, config_entry)

    assert [e.data["id"] for e in events] == ["recent"]

    # The stale one is now known too: nothing fires on the next poll.
    await _coordinator(hass, config_entry).async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_last_accident_skips_toilet_visits(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A toilet visit is not the last accident but is shown in its attributes."""
    toilet = make_accident("t1", ago(minutes=10), "mess", location="toilet")
    real = make_accident("a1", ago(hours=2), "wetting")
    mock_api(aioclient_mock, None, [toilet, real])
    await _setup(hass, config_entry)

    last = _state(hass, "sensor.diapstash_last_accident")
    assert last.state == "wetting"
    assert last.attributes["id"] == "a1"
    assert last.attributes["last_toilet_visit"] == toilet["when"]
    assert last.attributes["last_toilet_visit_type"] == "mess"


def _history_fixture():
    """An ongoing change continuing a streak, plus two completed changes."""
    current = make_change(ago(hours=3, minutes=10), change_id=3)
    previous = make_change(
        ago(hours=8), end=ago(hours=3, minutes=20), change_id=2, leak=True, state="WET"
    )
    older = make_change(ago(days=2, hours=4), end=ago(days=2), change_id=1, state="DRY")
    streak = {
        "changeCount": 3,
        "diaperCount": 4,
        "totalTimeInDiaperMinutes": 600,
        "startAt": ago(days=1).isoformat(),
        "endAt": previous["endTime"],
    }
    return current, [current, previous, older], streak


async def test_history_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Lifetime stats, streak and change history populate their sensors."""
    current, changes, streak = _history_fixture()
    accidents = [
        make_accident("a1", ago(hours=5), linked=2),
        make_accident("u1", ago(days=2, hours=1)),  # unlinked, during the older change
    ]
    mock_api(aioclient_mock, current, accidents, stats=make_stats(), streak=streak, changes=changes)
    await _setup(hass, config_entry)

    total = _state(hass, "sensor.diapstash_total_changes")
    assert total.state == "120"
    assert total.attributes["last_7_days"] == 9 and total.attributes["night_changes"] == 40
    assert _state(hass, "sensor.diapstash_total_diapers_used").state == "150"
    # 10 h completed + 3 h 10 min worn now
    assert float(_state(hass, "sensor.diapstash_total_time_in_diapers").state) in (13.17, 13.18)
    assert _state(hass, "sensor.diapstash_average_change_duration").state == "240"

    # The current change started 10 min after the streak ended: it continues it.
    streak_state = _state(hass, "sensor.diapstash_diaper_streak")
    assert float(streak_state.state) in (13.17, 13.18)  # 600 + 190 min
    assert streak_state.attributes["active"] is True
    assert streak_state.attributes["changes"] == 4
    assert streak_state.attributes["diapers"] == 6

    history = _state(hass, "sensor.diapstash_changes_last_30_days")
    assert history.state == "3"
    attrs = history.attributes
    assert attrs["completed"] == 2
    assert attrs["average_duration_minutes"] == 260  # (280 + 240) / 2
    assert attrs["leaks"] == 1 and attrs["leak_rate_percent"] == 50
    assert attrs["wet"] == 1 and attrs["dry"] == 1
    assert attrs["cost"] == 3.5
    assert attrs["by_type"] == [
        {"name": "Main Diaper", "changes": 2, "average_duration_minutes": 260, "leaks": 1}
    ]
    assert [c["id"] for c in attrs["changes"]] == [3, 2, 1]
    assert [c["accidents"] for c in attrs["changes"]] == [0, 1, 1]
    assert attrs["changes"][0]["duration_minutes"] is None  # ongoing

    prev = _state(hass, "sensor.diapstash_previous_change")
    assert prev.state == "Main Diaper, Booster"
    assert prev.attributes["duration_minutes"] == 280
    assert prev.attributes["leak"] is True
    assert prev.attributes["accidents"] == 1
    # The unlinked in-diaper accident is not an accident outside a diaper.
    assert _state(hass, "sensor.diapstash_last_outside_accident").state == "unknown"

    assert _state(hass, "sensor.diapstash_changes_today").state in ("1", "2", "3")


async def test_streak_broken_by_long_gap(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A change started long after the last streak ended starts a new streak."""
    current = make_change(ago(hours=1), change_id=9)
    streak = {
        "changeCount": 5, "diaperCount": 5, "totalTimeInDiaperMinutes": 1200,
        "startAt": ago(days=3).isoformat(), "endAt": ago(hours=2).isoformat(),
    }
    mock_api(aioclient_mock, current, [], streak=streak)
    await _setup(hass, config_entry)
    state = _state(hass, "sensor.diapstash_diaper_streak")
    assert float(state.state) in (1.0, 1.02)
    assert state.attributes["changes"] == 1 and state.attributes["diapers"] == 2


async def test_history_fetched_only_when_needed(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Stats are re-fetched when the latest change changes, not on every poll."""
    change = make_change(ago(hours=1), change_id=5)
    mock_api(aioclient_mock, change, [])
    await _setup(hass, config_entry)
    coordinator = _coordinator(hass, config_entry)

    mock_api(aioclient_mock, change, [])
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 2  # latest-change + accidents only

    closed = make_change(ago(hours=1), end=ago(minutes=1), change_id=5)
    mock_api(aioclient_mock, closed, [])
    await coordinator.async_refresh()
    assert aioclient_mock.call_count == 5  # the change ended: stats re-fetched


async def test_history_errors_do_not_break_main_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """A failing stats endpoint leaves the history sensors unavailable, nothing else."""
    mock_api(aioclient_mock, make_change(ago(hours=1)), [], history_status=500)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor.diapstash_current_diaper").state == "Main Diaper, Booster"
    assert _state(hass, "sensor.diapstash_total_changes").state == "unavailable"


async def test_history_skipped_when_quota_low(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """With little quota left the stats fetch is postponed."""
    mock_api(aioclient_mock, None, [], remaining=8, reset=3000)
    await _setup(hass, config_entry)
    assert aioclient_mock.call_count == 2
    assert _state(hass, "sensor.diapstash_total_changes").state == "unavailable"


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


async def test_token_refresh_does_not_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker
) -> None:
    """Storing a refreshed token in entry.data must not reload the entry."""
    mock_api(aioclient_mock, None, [])
    await _setup(hass, config_entry)
    coordinator = _coordinator(hass, config_entry)

    token = {**config_entry.data["token"], "access_token": "refreshed"}
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, "token": token})
    await hass.async_block_till_done()

    assert _coordinator(hass, config_entry) is coordinator  # same instance: no reload
    assert _state(hass, "sensor.diapstash_current_diaper").state == "not wearing"


async def test_single_config_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """A second DiapStash entry cannot be added."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
