"""Constants for the DiapStash integration."""

DOMAIN = "diapstash"

API_BASE_URL = "https://api.diapstash.com"
OAUTH_AUTHORIZATION_URL = "https://account.diapstash.com/oidc/auth"
OAUTH_TOKEN_URL = "https://account.diapstash.com/oidc/token"

SCOPES = ["cloud-sync.history", "cloud-sync.types"]

SCAN_INTERVAL_SECONDS = 300  # 5 minutes — respectful of no documented rate limit
