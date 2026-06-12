"""DiapStash API client."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)

_RATELIMIT_WARN_THRESHOLD = 10


def _parse_ratelimit_header(value: str) -> dict[str, int]:
    """Parse IETF RateLimit header (draft-ietf-httpapi-ratelimit-headers-08).

    DiapStash docs use 'w' for reset seconds; IETF uses 't' — both parsed.
    """
    result: dict[str, int] = {}
    for part in value.split(";")[1:]:  # skip quoted name token
        part = part.strip()
        if "=" in part:
            key, _, raw = part.partition("=")
            try:
                result[key.strip()] = int(raw.strip())
            except ValueError:
                pass
    return result


class DiapStashRateLimitError(Exception):
    """Raised on HTTP 429; carries the raw RateLimit header for backoff calculation."""

    def __init__(self, ratelimit_header: str) -> None:
        self.ratelimit_header = ratelimit_header
        super().__init__("Rate limit exceeded")


class DiapStashApiClient:
    """Thin wrapper around the DiapStash REST API."""

    def __init__(
        self,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
        client_id: str,
    ) -> None:
        self._session = oauth_session
        self._client_id = client_id

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._session.async_request(
            "GET",
            f"{API_BASE_URL}{path}",
            params=params,
            headers={"DS-API-CLIENT-ID": self._client_id},
        )
        if resp.status == 429:
            raise DiapStashRateLimitError(resp.headers.get("RateLimit", ""))
        resp.raise_for_status()

        rl_header = resp.headers.get("RateLimit")
        if rl_header:
            rl = _parse_ratelimit_header(rl_header)
            remaining = rl.get("r")
            if remaining is not None and remaining < _RATELIMIT_WARN_THRESHOLD:
                reset_in = rl.get("t") or rl.get("w")
                _LOGGER.warning(
                    "DiapStash quota low: %d requests remaining (resets in %s s).",
                    remaining,
                    reset_in if reset_in is not None else "?",
                )

        return await resp.json()

    async def get_current_change(self) -> dict[str, Any] | None:
        """Return the active change (endTime is null) or None."""
        data = await self._get(
            "/api/v1/history/changes",
            params={"size": 1, "sort": "startTime,desc"},
        )
        changes = data.get("data", [])
        if changes and changes[0].get("endTime") is None:
            return changes[0]
        return None

    async def get_accidents(self, size: int = 200) -> list[dict[str, Any]]:
        """Return the most recent accidents, newest first."""
        data = await self._get(
            "/api/v1/history/accidents",
            params={"size": size, "sort": "when,desc"},
        )
        return data.get("data", [])

    async def get_last_accident(self) -> dict[str, Any] | None:
        """Return the single most recent accident."""
        data = await self._get(
            "/api/v1/history/accidents",
            params={"size": 1, "sort": "when,desc"},
        )
        accidents = data.get("data", [])
        return accidents[0] if accidents else None

    async def get_diaper_types(self) -> dict[int, str]:
        """Return a mapping of typeId -> display name from the public type catalogue."""
        data = await self._get("/api/v1/type/types", params={"size": 200})
        return {t["id"]: t.get("name", str(t["id"])) for t in data.get("data", [])}

    async def get_custom_diaper_types(self) -> dict[int, str]:
        """Return user-defined types (merged on top of catalogue types)."""
        data = await self._get("/api/v1/type/types/custom", params={"size": 200})
        return {t["id"]: t.get("name", str(t["id"])) for t in data.get("data", [])}
