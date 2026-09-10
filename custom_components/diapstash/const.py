"""Constants for the DiapStash integration."""

DOMAIN = "diapstash"

API_BASE_URL = "https://api.diapstash.com"
OAUTH_AUTHORIZATION_URL = "https://account.diapstash.com/oidc/auth"
OAUTH_TOKEN_URL = "https://account.diapstash.com/oidc/token"

# Scopes requested during OAuth2 authorisation:
#   offline_access     — required to receive a refresh token alongside the access token.
#                        Without it, DiapStash issues only a short-lived session token
#                        (~5 min) with no refresh capability, causing HA to fail after
#                        the first expiry.
#   cloud-sync.history — grants read access to changes and accident history endpoints.
#   cloud-sync.types   — grants read access to the public and custom diaper type catalogues.
#
# Note: "openid" is intentionally omitted. The DiapStash authorisation server grants it
# implicitly but rejects the scope with error=invalid_scope when it is listed explicitly
# in the authorisation request.
SCOPES = ["offline_access", "cloud-sync.history", "cloud-sync.types"]

# Accident location value for toilet-training visits.
# When an accident is logged with this location it means the toilet was used
# (intentionally or not) rather than soiling the diaper. Such accidents are excluded
# from accidents_for_change so they do not inflate diaper-state sensors or counts.
ACCIDENT_LOCATION_TOILET = "toilet"

DEFAULT_SCAN_INTERVAL = 5   # minutes
# Rate limit budget: 120 req/hr ÷ 3 API calls per poll (types cached) = 40 polls/hr max.
# 40 polls/hr = one poll every 1.5 min, so a 1-minute floor still keeps us within budget.
MIN_SCAN_INTERVAL = 1       # minutes
