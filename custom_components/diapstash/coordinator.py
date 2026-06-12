"""DataUpdateCoordinator for DiapStash."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DiapStashApiClient, DiapStashRateLimitError, _parse_ratelimit_header
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, MIN_SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)

_API_TIMEOUT = 30


class DiapStashCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Single coordinator that fetches all DiapStash data used by the sensors."""

    def __init__(self, hass: HomeAssistant, client: DiapStashApiClient) -> None:
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
        self._cached_diaper_types: dict[int, str] = {}

    async def _async_update_data(self) -> dict[str, Any]:
        self.update_interval = self._normal_update_interval  # heal any prior backoff
        try:
            async with asyncio.timeout(_API_TIMEOUT):
                return await self._fetch()
        except ConfigEntryAuthFailed:
            raise
        except DiapStashRateLimitError as err:
            rl = _parse_ratelimit_header(err.ratelimit_header)
            reset_in = rl.get("t") or rl.get("w")
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
        import aiohttp

        try:
            current_change = await self._client.get_current_change()
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token expired") from err
            raise

        accidents_for_change: list[dict[str, Any]] = []
        if current_change is not None:
            change_id = current_change["id"]
            try:
                all_accidents = await self._client.get_accidents()
            except aiohttp.ClientResponseError as err:
                if err.status == 401:
                    raise ConfigEntryAuthFailed("DiapStash token expired") from err
                raise
            accidents_for_change = [
                a for a in all_accidents if a.get("linkedChangeId") == change_id
            ]

        try:
            last_accident = await self._client.get_last_accident()
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                raise ConfigEntryAuthFailed("DiapStash token expired") from err
            raise

        # Fetch public + custom types only on first load; reuse cache on subsequent polls
        if not self._cached_diaper_types:
            try:
                diaper_types: dict[int, str] = await self._client.get_diaper_types()
                custom_types = await self._client.get_custom_diaper_types()
                diaper_types.update(custom_types)
                self._cached_diaper_types = diaper_types
            except Exception:
                _LOGGER.debug("Failed to fetch diaper types; names will fall back to type IDs")

        return {
            "current_change": current_change,
            "accidents_for_change": accidents_for_change,
            "last_accident": last_accident,
            "diaper_types": self._cached_diaper_types,
        }
