"""DataUpdateCoordinator for DiapStash."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DiapStashApiClient, DiapStashRateLimitError, _parse_ratelimit_header
from .const import ACCIDENT_LOCATION_TOILET, DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Hard timeout per full refresh cycle. Keeps a slow API from blocking HA's event loop.
_API_TIMEOUT = 30


def _parse_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string into a timezone-aware datetime.

    DiapStash uses 'Z' suffix for UTC. Python's fromisoformat() does not understand
    'Z' before 3.11, so we replace it with '+00:00' for broad compatibility.
    Returns None on missing or malformed input so callers can treat it as "unknown".
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class DiapStashCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator that fetches all DiapStash data used by the sensors.

    All sensors share one coordinator so the integration makes exactly one set of
    API calls per poll cycle instead of one per sensor.
    """

    def __init__(self, hass: HomeAssistant, client: DiapStashApiClient) -> None:
        # Store the intended interval separately so we can always heal back to it
        # after a rate-limit backoff. The coordinator's update_interval attribute is
        # mutated during backoff; _normal_update_interval is the canonical default.
        self._normal_update_interval = timedelta(
            minutes=max(DEFAULT_SCAN_INTERVAL, MIN_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=self._normal_update_interval,
        )
        self._client = client

        # typeId → display name / image URL caches.
        # Populated on demand: only types actually referenced by the active change are
        # fetched. Types are immutable so the cache never needs to be invalidated.
        # A type mapped to its raw str(id) indicates a previous lookup returned no
        # result from either endpoint and will not be retried.
        self._cached_diaper_types: dict[int, str] = {}
        self._cached_diaper_type_images: dict[int, str] = {}
        self._cached_diaper_variant_images: dict[str, str] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        # Heal any prior rate-limit backoff at the start of each successful cycle.
        # This ensures the interval self-corrects without requiring a restart.
        self.update_interval = self._normal_update_interval

        try:
            async with asyncio.timeout(_API_TIMEOUT):
                return await self._fetch()

        # ConfigEntryAuthFailed must bubble up first so HA can show the re-auth UI.
        # It is a subclass of HomeAssistantError, so the order of the two handlers matters.
        except ConfigEntryAuthFailed:
            raise

        # HA's OAuth2 session raises HomeAssistantError when token refresh fails.
        # Re-raise as ConfigEntryAuthFailed so HA disables the integration and shows
        # a Repair notification that triggers the async_step_reauth flow in config_flow.py.
        except HomeAssistantError as err:
            _LOGGER.warning(
                "DiapStash token refresh failed — the refresh token has likely expired "
                "(14-day TTL). HA will show a Repair notification: click Fix to re-authorize."
            )
            raise ConfigEntryAuthFailed(str(err)) from err

        # On HTTP 429 the API returns a RateLimit header with quota and reset time.
        # We extend update_interval until the quota window resets (+5 s buffer),
        # then raise UpdateFailed so HA marks the sensors unavailable until recovery.
        except DiapStashRateLimitError as err:
            rl = _parse_ratelimit_header(err.ratelimit_header)
            reset_in = rl.get("t") or rl.get("w")  # 't' = IETF draft, 'w' = DiapStash docs
            if reset_in:
                self.update_interval = timedelta(seconds=reset_in + 5)
                _LOGGER.warning(
                    "DiapStash rate limit reached. Backing off for %d s (quota resets in %d s).",
                    reset_in + 5,
                    reset_in,
                )
            else:
                _LOGGER.warning("DiapStash rate limit reached. No reset time in RateLimit header.")
            raise UpdateFailed("DiapStash rate limit reached") from err

        except Exception as err:
            raise UpdateFailed(f"Error communicating with DiapStash API: {err}") from err

    async def _fetch(self) -> dict[str, Any]:
        """Fetch all data from the DiapStash API and return it as a single dict.

        Data flow:
          1. Fetch the active change (most recent change with endTime == null).
          2. Fetch all recent accidents and partition them into two lists:
               - accidents_for_change:  accidents that happened during the active change.
               - accidents_outside_change: accidents that happened between changes.
          3. Fetch the globally most-recent accident (for the LastAccidentSensor).
          4. Fetch the diaper type catalogue (cached; refreshed only on cache miss).
        """
        import aiohttp

        # --- Step 1: Active change ---
        # A change is considered active when its endTime is null.
        # Returns None when no change is active (not wearing a diaper).
        try:
            current_change = await self._client.get_current_change()
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token expired") from err
            raise

        # --- Step 2: Accident partitioning ---
        # Accidents are always fetched, regardless of whether a change is active.
        # This is needed to populate accidents_outside_change when not wearing.
        accidents_for_change: list[dict[str, Any]] = []
        accidents_outside_change: list[dict[str, Any]] = []
        try:
            all_accidents = await self._client.get_accidents()
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token expired") from err
            raise

        # Extract change identity for comparison. When current_change is None these
        # will both be None and all null-linked accidents fall into outside_change.
        change_id = (current_change or {}).get("id")
        change_start = _parse_utc((current_change or {}).get("startTime"))

        for a in all_accidents:
            # Toilet accidents represent toilet-training visits, not diaper soiling.
            # Exclude them from both lists so they don't influence diaper state,
            # accident counts, peak levels, or outside-change automations.
            if a.get("location") == ACCIDENT_LOCATION_TOILET:
                continue

            linked = a.get("linkedChangeId")

            if linked is not None:
                # The accident has been back-linked to a specific change by the server.
                # DiapStash only fills this field when the change closes, so during an
                # active change all accidents will have linkedChangeId == null.
                if linked == change_id:
                    accidents_for_change.append(a)
                # Accidents linked to a past (closed) change are ignored in both lists;
                # they are historical data not relevant to the current sensor readings.

            else:
                # linkedChangeId is null — the accident has not been linked yet.
                # This is the normal state for all accidents during an active change.
                # We decide which list it belongs to by comparing its timestamp against
                # the change start time:
                #   >= change_start → happened during the current change (unlinked yet)
                #   <  change_start → happened before this change started (bare accident)
                # When there is no active change, all null-linked accidents are bare.
                if change_start is not None:
                    when = _parse_utc(a.get("when"))
                    if when is not None and when >= change_start:
                        accidents_for_change.append(a)
                    else:
                        accidents_outside_change.append(a)
                else:
                    # No active change → every null-linked accident is outside a change.
                    accidents_outside_change.append(a)

        # --- Step 3: Global last accident ---
        # Fetched separately from get_accidents() because it is a dedicated single-item
        # call that is independent of change context; the LastAccidentSensor shows it
        # regardless of whether a change is currently active.
        try:
            last_accident = await self._client.get_last_accident()
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token expired") from err
            raise

        # --- Step 4: Diaper type cache (on-demand) ---
        # Fetch type details only for typeIds present in the current change that are
        # not already cached. The public endpoint is tried first; if it returns 404
        # the type is user-created and the custom endpoint is tried as a fallback.
        # Types are immutable so the cache is never invalidated.
        # A 401 propagates immediately to trigger re-auth. Other per-type errors are
        # logged at debug and retried on the next poll without caching the failure.
        # A type that returns 404 from both endpoints is stored as str(id) so it is
        # not retried repeatedly (unknown/deleted types).
        if current_change:
            needed_ids: set[int] = set()
            for d in current_change.get("diapers") or []:
                raw = d.get("typeId")
                try:
                    needed_ids.add(int(raw))
                except (TypeError, ValueError):
                    pass
            for type_id in needed_ids - self._cached_diaper_types.keys():
                try:
                    type_obj = await self._client.get_diaper_type(type_id)
                    if type_obj is None:
                        type_obj = await self._client.get_custom_diaper_type(type_id)
                except aiohttp.ClientResponseError as err:
                    if err.status == 401:
                        raise ConfigEntryAuthFailed("DiapStash token expired") from err
                    _LOGGER.debug("Could not fetch type %d (HTTP %d); will retry", type_id, err.status)
                    continue
                except Exception:
                    _LOGGER.debug("Could not fetch type %d; will retry next poll", type_id)
                    continue

                if type_obj:
                    self._cached_diaper_types[type_id] = type_obj.get("name", str(type_id))
                    pi = type_obj.get("primaryImage") or {}
                    if pi.get("url"):
                        self._cached_diaper_type_images[type_id] = pi["url"]
                    for v in type_obj.get("variants") or []:
                        vpi = v.get("primaryImage") or {}
                        if v.get("id") and vpi.get("url"):
                            self._cached_diaper_variant_images[str(v["id"])] = vpi["url"]
                else:
                    # 404 from both endpoints — store raw id string to prevent retries.
                    self._cached_diaper_types[type_id] = str(type_id)

        return {
            "current_change": current_change,
            "accidents_for_change": accidents_for_change,
            "accidents_outside_change": accidents_outside_change,
            "last_accident": last_accident,
            "diaper_types": self._cached_diaper_types,
            "diaper_type_images": self._cached_diaper_type_images,
            "diaper_variant_images": self._cached_diaper_variant_images,
        }
