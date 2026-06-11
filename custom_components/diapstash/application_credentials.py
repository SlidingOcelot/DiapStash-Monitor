"""Application credentials for DiapStash (OAuth2 server endpoints)."""
from homeassistant.components.application_credentials import AuthorizationServer
from homeassistant.core import HomeAssistant

from .const import OAUTH_AUTHORIZATION_URL, OAUTH_TOKEN_URL


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the DiapStash OIDC authorization server URLs."""
    return AuthorizationServer(
        authorize_url=OAUTH_AUTHORIZATION_URL,
        token_url=OAUTH_TOKEN_URL,
    )
