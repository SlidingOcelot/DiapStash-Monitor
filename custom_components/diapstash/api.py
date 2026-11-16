"""DiapStash API client."""
from __future__ import annotations

import logging
from typing import Any

import aiohttp

from homeassistant.helpers import config_entry_oauth2_flow

from .const import ACCIDENT_FETCH_SIZE, API_BASE_URL

_LOGGER = logging.getLogger(__name__)

# Log a warning when fewer than this many requests remain in the current quota window.
# Gives the user a heads-up before hitting HTTP 429 and triggering backoff.
_RATELIMIT_WARN_THRESHOLD = 10


def _parse_ratelimit_header(value: str) -> dict[str, int]:
    """Parse an IETF RateLimit / RateLimit-Policy header (ratelimit-headers draft 8).

    The server sends e.g. 'RateLimit: "per-user-120"; r=118; t=3540' and
    'RateLimit-Policy: "per-user-120"; q=120; w=3600'. The first token is the quoted
    policy name and is skipped. The DiapStash docs call the reset key 'w' while the
    server (express-rate-limit, draft-8) sends 't', so callers accept both.
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
        # Latest rate-limit state reported by the server:
        #   remaining — requests left in the current window
        #   reset     — seconds until the window resets
        #   limit     — requests allowed per window
        # Keys are absent until the server has reported them.
        self.ratelimit: dict[str, int] = {}

    def _record_ratelimit(self, headers: Any) -> None:
        rl_header = headers.get("RateLimit")
        if rl_header:
            rl = _parse_ratelimit_header(rl_header)
            if "r" in rl:
                self.ratelimit["remaining"] = rl["r"]
            reset_in = rl.get("t", rl.get("w"))
            if reset_in is not None:
                self.ratelimit["reset"] = reset_in
        policy = headers.get("RateLimit-Policy")
        if policy:
            limit = _parse_ratelimit_header(policy).get("q")
            if limit is not None:
                self.ratelimit["limit"] = limit

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated GET and return the parsed JSON body.

        Rate-limit handling:
          - HTTP 429 raises DiapStashRateLimitError with the raw RateLimit header so
            the coordinator can calculate the exact backoff duration.
          - Other non-2xx responses raise via raise_for_status() and propagate up.
          - The remaining quota is recorded on every response so the coordinator can
            stretch the poll interval before the quota runs out.
        """
        resp = await self._session.async_request(
            "GET",
            f"{API_BASE_URL}{path}",
            params=params,
            headers={"DS-API-CLIENT-ID": self._client_id},
        )
        self._record_ratelimit(resp.headers)

        # Check for rate-limit before raise_for_status so we can capture the header.
        if resp.status == 429:
            raise DiapStashRateLimitError(resp.headers.get("RateLimit", ""))
        resp.raise_for_status()

        remaining = self.ratelimit.get("remaining")
        if remaining is not None and remaining < _RATELIMIT_WARN_THRESHOLD:
            _LOGGER.warning(
                "DiapStash quota low: %d requests remaining (resets in %s s).",
                remaining,
                self.ratelimit.get("reset", "?"),
            )

        return await resp.json()

    async def get_latest_change(self) -> dict[str, Any] | None:
        """Return the user's ongoing or most recent change, or None if there is none.

        The change is active while its endTime is null. Each diaper in the response
        carries its type details (name, brand, primary image, variant) inline, so no
        separate type lookups are needed.
        """
        try:
            return await self._get("/api/v1/history/latest-change")
        except aiohttp.ClientResponseError as err:
            if err.status == 404:  # the user has no change recorded at all
                return None
            raise

    async def get_accidents(self, size: int = ACCIDENT_FETCH_SIZE) -> list[dict[str, Any]]:
        """Return the most recent accidents, newest first.

        The coordinator uses this one list for the current-change sensors, the
        outside-change sensor, the daily/weekly counts and the history attribute.
        """
        data = await self._get(
            "/api/v1/history/accidents",
            params={"size": size, "sort": "when,desc"},
        )
        return data.get("data", [])
