"""DiapStash API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.helpers import config_entry_oauth2_flow

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)

# Log a warning when fewer than this many requests remain in the current quota window.
# Gives the user a heads-up before hitting HTTP 429 and triggering backoff.
_RATELIMIT_WARN_THRESHOLD = 10


def _parse_ratelimit_header(value: str) -> dict[str, int]:
    """Parse IETF RateLimit header (draft-ietf-httpapi-ratelimit-headers-08).

    Expected format: 'quota-policy="name"; r=<remaining>; t=<seconds-to-reset>'
    The first token is the quoted policy name and is skipped (split on ';')[1:]).

    DiapStash docs use 'w' as the reset-seconds key; the IETF draft uses 't'.
    Both are parsed so the coordinator backoff logic works regardless of which key
    the server sends.
    """
    result: dict[str, int] = {}
    for part in value.split(";")[1:]:  # skip the quoted name token
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
    """Thin wrapper around the DiapStash REST API.

    All requests are authenticated via the HA OAuth2 session (token refresh is
    handled transparently by OAuth2Session.async_request). The DS-API-CLIENT-ID
    header is required by the DiapStash API on every request alongside the Bearer
    token — it identifies which registered OAuth client is making the call.
    """

    def __init__(
        self,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
        client_id: str,
    ) -> None:
        self._session = oauth_session
        self._client_id = client_id

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated GET and return the parsed JSON body.

        Rate-limit handling:
          - HTTP 429 raises DiapStashRateLimitError with the raw RateLimit header so
            the coordinator can calculate the exact backoff duration.
          - Other non-2xx responses raise via raise_for_status() and propagate up.
          - When remaining quota drops below _RATELIMIT_WARN_THRESHOLD a warning is
            logged proactively so the user can act before backoff kicks in.
        """
        resp = await self._session.async_request(
            "GET",
            f"{API_BASE_URL}{path}",
            params=params,
            headers={"DS-API-CLIENT-ID": self._client_id},
        )

        # Check for rate-limit before raise_for_status so we can capture the header.
        if resp.status == 429:
            raise DiapStashRateLimitError(resp.headers.get("RateLimit", ""))
        resp.raise_for_status()

        # Proactive quota warning on successful responses.
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
        """Return the active change (endTime is null) or None.

        Fetches only the most recent change sorted by startTime desc. A change is
        considered active when its endTime field is null — the server sets endTime
        when the user closes the change in the app. If the most recent change is
        already closed, there is no active change and None is returned.
        """
        data = await self._get(
            "/api/v1/history/changes",
            params={"size": 1, "sort": "startTime,desc"},
        )
        changes = data.get("data", [])
        if changes and changes[0].get("endTime") is None:
            return changes[0]
        return None

    async def get_accidents(self, size: int = 200) -> list[dict[str, Any]]:
        """Return the most recent accidents, newest first.

        size=200 is large enough to cover all accidents for a typical multi-day period
        without hitting API pagination. The coordinator uses this list to populate
        both accidents_for_change and accidents_outside_change, so it must include
        accidents before the current change's startTime.
        """
        data = await self._get(
            "/api/v1/history/accidents",
            params={"size": size, "sort": "when,desc"},
        )
        return data.get("data", [])

    async def get_last_accident(self) -> dict[str, Any] | None:
        """Return the single most recent accident, regardless of change context.

        This is a separate call from get_accidents() even though it returns a subset
        of the same data. It is kept as a dedicated call so the LastAccidentSensor can
        show a globally most-recent accident independently of the coordinator's accident
        partitioning logic.
        """
        data = await self._get(
            "/api/v1/history/accidents",
            params={"size": 1, "sort": "when,desc"},
        )
        accidents = data.get("data", [])
        return accidents[0] if accidents else None

    async def get_diaper_type(self, type_id: int) -> dict[str, Any] | None:
        """Fetch a single type from the public catalogue by ID.

        Returns the type object (the value of the 'type' key in the response) or None
        when the server returns 404 (type does not exist in the public catalogue).
        All other errors propagate so the coordinator can handle them.
        """
        try:
            data = await self._get(f"/api/v1/type/types/{type_id}")
            return data.get("type")
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return None
            raise

    async def get_custom_diaper_type(self, type_id: int) -> dict[str, Any] | None:
        """Fetch a single user-defined type by ID.

        Returns the type object or None on 404. Called as a fallback when the public
        catalogue returns 404, indicating the type is user-created rather than from
        the shared DiapStash catalogue.
        """
        try:
            data = await self._get(f"/api/v1/type/types/custom/{type_id}")
            return data.get("type")
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return None
            raise
