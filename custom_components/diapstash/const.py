"""Constants for the DiapStash integration."""

DOMAIN = "diapstash"

API_BASE_URL = "https://api.diapstash.com"
OAUTH_AUTHORIZATION_URL = "https://account.diapstash.com/oidc/auth"
OAUTH_TOKEN_URL = "https://account.diapstash.com/oidc/token"

SCOPES = ["openid", "offline_access", "cloud-sync.history", "cloud-sync.types"]

DEFAULT_SCAN_INTERVAL = 5   # minutes
# 120 req/hr ÷ 3 requests per regular poll (types cached) = 40 polls/hr max → 1 min floor
MIN_SCAN_INTERVAL = 1       # minutes
