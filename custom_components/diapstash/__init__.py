"""The DiapStash integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_entry_oauth2_flow

from .api import DiapStashApiClient
from .const import DOMAIN
from .coordinator import DiapStashCoordinator

# All HA platform types that this integration registers entities on.
# Adding "binary_sensor" here causes HA to call async_setup_entry in binary_sensor.py
# during integration load. Order does not matter — HA sets up platforms in parallel.
PLATFORMS = ["sensor", "binary_sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up DiapStash from a config entry.

    The coordinator is stored in hass.data keyed by entry_id so each platform's
    async_setup_entry can retrieve it. We use hass.data rather than entry.runtime_data
    because this integration targets HA versions that may not support runtime_data.
    """
    # Resolve the OAuth2 implementation registered via application_credentials.
    # This provides the token URL, client id, and handles token refresh automatically.
    implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
        hass, entry
    )
    oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
    client = DiapStashApiClient(
        oauth_session=oauth_session,
        client_id=implementation.client_id,
    )

    # Fail immediately if no refresh token is stored. This happens when offline_access
    # was not granted during authorization (the consent screen was skipped or denied).
    # Without a refresh token the integration will silently fail after the first 1-hour
    # access-token expiry instead of prompting the user to re-authorize now.
    token = entry.data.get("token", {})
    if not token.get("refresh_token"):
        raise ConfigEntryAuthFailed(
            "No refresh token stored. Re-authorize and accept the offline_access "
            "consent prompt to enable long-lived token refresh."
        )

    coordinator = DiapStashCoordinator(hass, client)
    # Perform the first data fetch synchronously so HA can report setup failure
    # immediately (e.g. auth error, network unreachable) rather than deferring it.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a DiapStash config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
