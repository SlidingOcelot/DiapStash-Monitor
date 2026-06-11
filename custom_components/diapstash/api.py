"""DiapStash API client."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow

from .const import API_BASE_URL

_LOGGER = logging.getLogger(__name__)


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
        resp.raise_for_status()
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
