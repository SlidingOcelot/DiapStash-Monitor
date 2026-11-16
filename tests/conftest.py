"""Shared fixtures for DiapStash tests."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from custom_components.diapstash.const import (
    API_BASE_URL,
    DOMAIN,
    OAUTH_AUTHORIZATION_URL,
    OAUTH_TOKEN_URL,
)

LATEST_CHANGE_URL = f"{API_BASE_URL}/api/v1/history/latest-change"
ACCIDENTS_URL = f"{API_BASE_URL}/api/v1/history/accidents"
CLIENT_ID = "test-client-id"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ago(**kwargs: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


def make_change(start: datetime, end: datetime | None = None, change_id: int = 42) -> dict[str, Any]:
    """A latest-change response as returned by the API, with inline type details."""
    return {
        "id": change_id,
        "startTime": iso(start),
        "endTime": iso(end) if end else None,
        "tags": None,
        "note": "test note",
        "state": None,
        "changePeriod": "DAY",
        "leak": None,
        "messyOverflow": False,
        "diapers": [
            {
                "order": 1,
                "typeId": 7,
                "variantId": None,
                "size": "M",
                "type": {
                    "name": "Booster",
                    "brand": {"name": "BrandB"},
                    "primaryImage": {"url": "https://img/booster.png"},
                    "variant": None,
                },
            },
            {
                "order": 0,
                "typeId": 5,
                "variantId": "v1",
                "size": "L",
                "type": {
                    "name": "Main Diaper",
                    "brand": {"name": "BrandA"},
                    "primaryImage": {"url": "https://img/type.png"},
                    "variant": {"name": "Blue", "primaryImage": {"url": "https://img/variant.png"}},
                },
            },
        ],
    }


def make_accident(
    accident_id: str,
    when: datetime,
    type_: str = "wetting",
    level: int | None = 2,
    location: str = "diaper",
    linked: int | None = None,
    leak: bool = False,
) -> dict[str, Any]:
    return {
        "id": accident_id,
        "type": type_,
        "precisionTime": "exact",
        "when": iso(when),
        "linkedChangeId": linked,
        "level": level,
        "causeLeak": leak,
        "cause": "uncontrolledAwake",
        "location": location,
        "position": "sitting",
        "createdAt": iso(when),
        "updatedAt": iso(when),
    }


def ratelimit_headers(remaining: int = 118, reset: int = 3500) -> dict[str, str]:
    return {
        "RateLimit": f'"per-user-120"; r={remaining}; t={reset}',
        "RateLimit-Policy": '"per-user-120"; q=120; w=3600',
    }


def mock_api(
    aioclient_mock: AiohttpClientMocker,
    change: dict[str, Any] | None,
    accidents: list[dict[str, Any]],
    remaining: int = 118,
    reset: int = 3500,
    change_status: int = 200,
    accidents_status: int = 200,
) -> None:
    """(Re)register API responses. Accidents must be passed newest first."""
    aioclient_mock.clear_requests()
    headers = ratelimit_headers(remaining, reset)
    if change is None and change_status == 200:
        change_status = 404
    aioclient_mock.get(
        LATEST_CHANGE_URL,
        json=change if change is not None else {"message": "No change recorded"},
        status=change_status,
        headers=headers,
    )
    aioclient_mock.get(
        ACCIDENTS_URL,
        json={"page": 0, "size": 200, "count": len(accidents), "totalCount": len(accidents), "data": accidents},
        status=accidents_status,
        headers=headers,
    )


@pytest.fixture
async def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry with a valid OAuth token and a registered implementation."""
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        config_entry_oauth2_flow.LocalOAuth2Implementation(
            hass, DOMAIN, CLIENT_ID, "secret", OAUTH_AUTHORIZATION_URL, OAUTH_TOKEN_URL
        ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="DiapStash",
        data={
            "auth_implementation": DOMAIN,
            "token": {
                "access_token": "access",
                "refresh_token": "refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
                "expires_at": time.time() + 3600,
            },
        },
    )
    entry.add_to_hass(hass)
    return entry
