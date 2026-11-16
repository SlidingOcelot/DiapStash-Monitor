"""Config flow for DiapStash — OAuth2 via Application Credentials."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    SCOPES,
)


class DiapStashOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle the OAuth2 config flow for DiapStash."""

    DOMAIN = DOMAIN

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> DiapStashOptionsFlow:
        """Return the options flow (update interval)."""
        return DiapStashOptionsFlow()

    @property
    def logger(self) -> logging.Logger:
        return logging.getLogger(__name__)

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        """Extra parameters appended to the authorisation URL.

        scope: Explicitly request the scopes defined in const.SCOPES. HA's
          application_credentials path does not automatically propagate scopes from
          the token URL registration, so they must be sent here.

        prompt=consent: Forces the DiapStash consent screen even when the user has
          previously authorised this client. Required to guarantee that the server
          issues a fresh refresh token alongside the access token. Without it,
          re-authorising an existing grant may skip the consent screen and return an
          access token only — leaving HA without a refresh token and causing auth
          failures after the access token expires (~1 hour).
        """
        return {"scope": " ".join(SCOPES), "prompt": "consent"}

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> dict[str, Any]:
        """Called automatically by HA when ConfigEntryAuthFailed is raised.

        HA creates a new config flow with source=SOURCE_REAUTH and calls this method.
        We show a confirmation form first so the user understands what is happening
        before the OAuth browser window opens.
        """
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Confirm dialog — clicking Submit opens the DiapStash authorization page."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        # Proceed to the standard OAuth2 authorization flow.
        return await self.async_step_user()

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create the config entry or update the existing one when re-authenticating.

        During re-auth (source == SOURCE_REAUTH), we update the existing entry with
        the new token data and reload it rather than creating a duplicate entry.
        During initial setup, we create a fresh entry as normal.
        """
        if self.context.get("source") == SOURCE_REAUTH:
            entry_id = self.context.get("entry_id")
            existing = self.hass.config_entries.async_get_entry(entry_id)
            if existing:
                self.hass.config_entries.async_update_entry(existing, data=data)
                await self.hass.config_entries.async_reload(existing.entry_id)
                return self.async_abort(reason="reauth_successful")
        return self.async_create_entry(title="DiapStash", data=data)


class DiapStashOptionsFlow(OptionsFlow):
    """Options: how often to poll the DiapStash API.

    Each poll uses 2 of the 120 requests per hour the API allows. The minimum of
    MIN_SCAN_INTERVAL minutes keeps polling at no more than half of that budget; the
    coordinator additionally slows down on its own when the server reports a low
    remaining quota.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> dict[str, Any]:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="min",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
