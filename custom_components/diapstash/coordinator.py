"""DataUpdateCoordinator for DiapStash."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import DiapStashApiClient, DiapStashRateLimitError, _parse_ratelimit_header
from .const import (
    ACCIDENT_LOCATION_DIAPER,
    ACCIDENT_LOCATION_TOILET,
    API_CALLS_PER_POLL,
    CHANGE_FETCH_SIZE,
    CHANGE_HISTORY_DAYS,
    HISTORY_API_CALLS,
    HISTORY_REFRESH_INTERVAL,
    STREAK_BREAK_MINUTES,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_ACCIDENT,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    RATELIMIT_RESERVE,
    RESTART_EVENT_MAX_AGE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Hard timeout per full refresh cycle. Keeps a slow API from blocking updates.
_API_TIMEOUT = 30


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware datetime.

    DiapStash uses a 'Z' suffix for UTC; it is replaced with '+00:00' for
    compatibility with older Python versions. Returns None on missing or malformed
    input so callers can treat it as "unknown".
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def accident_summary(accident: dict[str, Any]) -> dict[str, Any]:
    """Snake_case view of an accident, used for attributes and event data."""
    return {
        "id": accident.get("id"),
        "type": accident.get("type"),
        "when": accident.get("when"),
        "precision_time": accident.get("precisionTime"),
        "level": accident.get("level"),
        "cause": accident.get("cause"),
        "cause_leak": accident.get("causeLeak"),
        "location": accident.get("location"),
        "position": accident.get("position"),
        "linked_change_id": accident.get("linkedChangeId"),
    }


def _diaper_details(change: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten the inline type details of a change's diapers, in wearing order.

    The latest-change endpoint embeds each diaper's type (name, brand, primary image)
    and the selected variant, so names and pictures need no extra API calls. The
    variant picture is preferred over the type picture when both exist.
    """
    result = []
    for d in sorted(change.get("diapers") or [], key=lambda d: d.get("order", 0)):
        type_info = d.get("type") or {}
        variant = type_info.get("variant") or {}
        image = (variant.get("primaryImage") or {}).get("url") or (
            type_info.get("primaryImage") or {}
        ).get("url")
        result.append(
            {
                "name": (type_info.get("name") or "").strip() or str(d.get("typeId")),
                "brand": ((type_info.get("brand") or {}).get("name") or "").strip() or None,
                "variant": variant.get("name"),
                "size": d.get("size"),
                "type_id": d.get("typeId"),
                "image_url": image,
            }
        )
    return result


class DiapStashCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator that fetches all DiapStash data used by the entities.

    Every poll makes exactly API_CALLS_PER_POLL requests (latest-change and
    accidents); all sensors, counts and history are derived from those two responses.
    """

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: DiapStashApiClient
    ) -> None:
        self._entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self.configured_interval,
        )
        self._client = client
        # IDs of accidents already seen, persisted across restarts. None only on the
        # very first poll after installing, which seeds the set without firing events.
        self._seen_accident_ids: set[str] | None = None
        # True for the first poll after loading IDs from storage (see _fire_new_accident_events).
        self._restored = False
        # Lifetime stats, streak and recent changes: fetched less often than the rest,
        # see _maybe_fetch_history. The last good copy is kept between fetches.
        self._history: dict[str, Any] | None = None
        self._history_fetched_at: datetime | None = None
        self._history_signature: tuple[Any, ...] | None = None
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}.seen_accidents"
        )

    async def async_load_seen_accidents(self) -> None:
        """Load the accident IDs announced before the last restart."""
        stored = await self._store.async_load()
        if stored and isinstance(stored.get("ids"), list):
            self._seen_accident_ids = set(stored["ids"])
            self._restored = True

    async def async_remove_store(self) -> None:
        """Delete the stored accident IDs (integration removed)."""
        await self._store.async_remove()

    @property
    def configured_interval(self) -> timedelta:
        """Poll interval chosen in the integration options, clamped to safe bounds."""
        minutes = self._entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            minutes = DEFAULT_SCAN_INTERVAL
        return timedelta(minutes=min(max(minutes, MIN_SCAN_INTERVAL), MAX_SCAN_INTERVAL))

    @property
    def ratelimit(self) -> dict[str, int]:
        """Latest rate-limit state reported by the API (remaining, reset, limit)."""
        return self._client.ratelimit

    def _interval_for_quota(self) -> timedelta:
        """Return the configured interval, stretched if the remaining quota is too low.

        The remaining requests must last until the quota window resets. If they would
        not at the configured pace, polls are spread evenly over the rest of the
        window (keeping RATELIMIT_RESERVE requests spare for restarts and reloads).
        If not even one more poll fits, wait for the window to reset.
        """
        configured = self.configured_interval
        remaining = self.ratelimit.get("remaining")
        reset_in = self.ratelimit.get("reset")
        if remaining is None or reset_in is None:
            return configured

        polls_left = (remaining - RATELIMIT_RESERVE) // API_CALLS_PER_POLL
        if polls_left < 1:
            interval = timedelta(seconds=reset_in + 5)
        else:
            interval = max(configured, timedelta(seconds=reset_in / polls_left))

        if interval > configured:
            _LOGGER.info(
                "DiapStash quota: %d requests left, window resets in %d s. "
                "Next update in %d s instead of %d s.",
                remaining,
                reset_in,
                interval.total_seconds(),
                configured.total_seconds(),
            )
        return interval

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            async with asyncio.timeout(_API_TIMEOUT):
                data = await self._fetch()

        # ConfigEntryAuthFailed must bubble up first so HA can show the re-auth UI.
        # It is a subclass of HomeAssistantError, so the order of the handlers matters.
        except ConfigEntryAuthFailed:
            raise

        # On HTTP 429 wait until the quota window resets (+5 s buffer), then raise
        # UpdateFailed so HA marks the entities unavailable until recovery.
        except DiapStashRateLimitError as err:
            rl = _parse_ratelimit_header(err.ratelimit_header)
            reset_in = rl.get("t", rl.get("w"))
            if reset_in:
                self.update_interval = timedelta(seconds=reset_in + 5)
                _LOGGER.warning(
                    "DiapStash rate limit reached. Backing off for %d s.", reset_in + 5
                )
            else:
                _LOGGER.warning("DiapStash rate limit reached. No reset time in RateLimit header.")
            raise UpdateFailed("DiapStash rate limit reached") from err

        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token rejected") from err
            if err.status == 403:
                # Missing scope, e.g. cloud-sync.accidents once the deprecated
                # cloud-sync.history fallback is removed. Re-authorising requests the
                # current SCOPES list.
                raise ConfigEntryAuthFailed(
                    "DiapStash denied access (missing scope). Enable all required scopes "
                    "on your DiapStash API client and re-authorize."
                ) from err
            raise UpdateFailed(f"DiapStash API error: HTTP {err.status}") from err

        # HA's OAuth2 session raises HomeAssistantError when token refresh fails.
        # Re-raise as ConfigEntryAuthFailed so HA shows a Repair notification that
        # triggers the reauth flow in config_flow.py.
        except HomeAssistantError as err:
            _LOGGER.warning(
                "DiapStash token refresh failed — the refresh token has likely expired "
                "(7-day lifetime). HA will show a Repair notification: click Fix to re-authorize."
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        except Exception as err:
            raise UpdateFailed(f"Error communicating with DiapStash API: {err}") from err

        self.update_interval = self._interval_for_quota()
        return data

    async def _fetch(self) -> dict[str, Any]:
        """Fetch the latest change and recent accidents, and derive all sensor data."""
        latest_change = await self._client.get_latest_change()
        all_accidents = await self._client.get_accidents()

        # The latest change is the current one only while its endTime is null.
        current_change = (
            latest_change
            if latest_change is not None and latest_change.get("endTime") is None
            else None
        )

        accidents_for_change, accidents_outside_change = self._partition(
            current_change, all_accidents
        )
        await self._fire_new_accident_events(all_accidents, accidents_for_change)
        history = await self._maybe_fetch_history(latest_change)

        return {
            "latest_change": latest_change,
            "current_change": current_change,
            "diapers": _diaper_details(current_change) if current_change else [],
            # Newest first, toilet visits excluded.
            "accidents_for_change": accidents_for_change,
            "accidents_outside_change": accidents_outside_change,
            # Newest first, everything including toilet visits.
            "accidents": all_accidents,
            # Newest real accident (toilet visits excluded) and newest toilet visit.
            "last_accident": next(
                (a for a in all_accidents if a.get("location") != ACCIDENT_LOCATION_TOILET),
                None,
            ),
            "last_toilet_visit": next(
                (a for a in all_accidents if a.get("location") == ACCIDENT_LOCATION_TOILET),
                None,
            ),
            # Lifetime stats, streak and recent changes, or None if never fetched.
            "history": history,
        }

    async def _maybe_fetch_history(
        self, latest_change: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Refresh stats, streak and recent changes when they may have changed.

        They only change when a change starts, ends or is edited (visible in the
        latest change's id, endTime and updatedAt), so they are fetched then, plus
        every HISTORY_REFRESH_INTERVAL for the rolling windows. The fetch is skipped
        while the remaining quota cannot spare it; errors other than auth and rate
        limits keep the previous data, so the main sensors are never affected.
        """
        now = dt_util.utcnow()
        signature = (
            (latest_change or {}).get("id"),
            (latest_change or {}).get("endTime"),
            (latest_change or {}).get("updatedAt"),
        )
        due = (
            self._history is None
            or signature != self._history_signature
            or self._history_fetched_at is None
            or now - self._history_fetched_at >= timedelta(minutes=HISTORY_REFRESH_INTERVAL)
        )
        remaining = self.ratelimit.get("remaining")
        if not due or (
            remaining is not None
            and remaining < RATELIMIT_RESERVE + HISTORY_API_CALLS + API_CALLS_PER_POLL
        ):
            return self._history

        # Also the retry time after a failure, so errors cannot burn the quota.
        self._history_fetched_at = now
        since = now - timedelta(days=CHANGE_HISTORY_DAYS)
        try:
            stats = await self._client.get_stats()
            streak = await self._client.get_streak(STREAK_BREAK_MINUTES)
            changes = await self._client.get_changes(
                since.isoformat().replace("+00:00", "Z"), CHANGE_FETCH_SIZE
            )
        except (DiapStashRateLimitError, ConfigEntryAuthFailed):
            raise
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise
            _LOGGER.warning("DiapStash statistics unavailable: HTTP %s", err.status)
            return self._history
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning("DiapStash statistics unavailable: %s", err)
            return self._history

        self._history_signature = signature
        self._history = {
            "stats": stats,
            "streak": streak,
            # Newest first; includes the ongoing change, if any.
            "changes": changes,
            "since": since,
            "fetched_at": now,
        }
        return self._history

    @staticmethod
    def _partition(
        current_change: dict[str, Any] | None, all_accidents: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Split accidents into those during the current change and those outside one.

        Toilet visits are excluded from both lists. Accidents linked to a past change
        are historical and belong to neither list. DiapStash only fills linkedChangeId
        when a change closes, so during an active change its accidents are unlinked
        and are matched by timestamp instead: unlinked accidents at or after the
        change start belong to it. Earlier unlinked ones are outside a change unless
        they were logged "in diaper": DiapStash does not always link those to the
        change that was worn, so they are past accidents, not ones without a diaper.
        """
        for_change: list[dict[str, Any]] = []
        outside: list[dict[str, Any]] = []
        change_id = (current_change or {}).get("id")
        change_start = _parse_utc((current_change or {}).get("startTime"))

        for a in all_accidents:
            if a.get("location") == ACCIDENT_LOCATION_TOILET:
                continue
            linked = a.get("linkedChangeId")
            if linked is not None:
                if linked == change_id:
                    for_change.append(a)
                continue
            when = _parse_utc(a.get("when"))
            if change_start is not None and when is not None and when >= change_start:
                for_change.append(a)
            elif a.get("location") != ACCIDENT_LOCATION_DIAPER:
                outside.append(a)
        return for_change, outside

    async def _fire_new_accident_events(
        self,
        all_accidents: list[dict[str, Any]],
        accidents_for_change: list[dict[str, Any]],
    ) -> None:
        """Fire EVENT_ACCIDENT once for every accident not seen in a previous poll.

        The very first poll after installing only records the existing accidents. The
        seen IDs are stored, so after a restart accidents logged while HA was down are
        still announced - but only those created within RESTART_EVENT_MAX_AGE minutes.
        Events are fired oldest first so automations and the logbook see them in order.
        """
        ids = {a["id"] for a in all_accidents if a.get("id")}
        if self._seen_accident_ids is None:
            self._seen_accident_ids = ids
            await self._store.async_save({"ids": sorted(ids)})
            return

        cutoff = None
        if self._restored:
            cutoff = dt_util.utcnow() - timedelta(minutes=RESTART_EVENT_MAX_AGE)
            self._restored = False

        in_change = {a.get("id") for a in accidents_for_change}
        for accident in reversed(all_accidents):
            accident_id = accident.get("id")
            if not accident_id or accident_id in self._seen_accident_ids:
                continue
            if cutoff is not None:
                created = _parse_utc(accident.get("createdAt") or accident.get("when"))
                if created is None or created < cutoff:
                    continue
            self.hass.bus.async_fire(
                EVENT_ACCIDENT,
                {
                    **accident_summary(accident),
                    "during_change": accident_id in in_change,
                    "config_entry_id": self._entry.entry_id,
                },
            )
        # Only accidents still in the fetched window are remembered; one that drops
        # out of the window cannot come back as "new".
        # Written only when the set changes, i.e. when accidents are logged.
        if ids != self._seen_accident_ids:
            self._seen_accident_ids = ids
            await self._store.async_save({"ids": sorted(ids)})
