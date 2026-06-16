"""Config flow for DiapStash — OAuth2 via Application Credentials."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import config_entry_oauth2_flow

from .const import DOMAIN, SCOPES


class DiapStashOAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler,
    domain=DOMAIN,
):
    """Handle the OAuth2 config flow for DiapStash."""

    DOMAIN = DOMAIN

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

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> dict[str, Any]:
        """Create the config entry after a successful OAuth2 authorization."""
        return self.async_create_entry(title="DiapStash", data=data)
