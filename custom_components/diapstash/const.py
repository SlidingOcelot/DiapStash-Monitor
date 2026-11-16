"""Constants for the DiapStash integration."""

DOMAIN = "diapstash"

API_BASE_URL = "https://api.diapstash.com"
OAUTH_AUTHORIZATION_URL = "https://account.diapstash.com/oidc/auth"
OAUTH_TOKEN_URL = "https://account.diapstash.com/oidc/token"

# Scopes requested during OAuth2 authorisation:
#   offline_access       — required to receive a refresh token alongside the access token.
#                          Without it, DiapStash issues only a short-lived session token
#                          with no refresh capability, causing HA to fail after the first
#                          expiry.
#   cloud-sync.history   — read access to changes (latest-change endpoint).
#   cloud-sync.accidents — read access to accidents. The API still accepts
#                          cloud-sync.history for accidents but has deprecated it, so this
#                          scope must be enabled on the user's API client.
#   cloud-sync.types     — read access to custom diaper types.
#
# Note: "openid" is intentionally omitted. The DiapStash authorisation server grants it
# implicitly but rejects the scope with error=invalid_scope when it is listed explicitly
# in the authorisation request.
SCOPES = ["offline_access", "cloud-sync.history", "cloud-sync.accidents", "cloud-sync.types"]

# Accident location value for toilet-training visits.
# When an accident is logged with this location it means the toilet was used
# (intentionally or not) rather than soiling the diaper. Such accidents are excluded
# from the change-based sensors and daily counts so they do not inflate diaper state.
ACCIDENT_LOCATION_TOILET = "toilet"

# --- Polling and rate limits ---
# The DiapStash API allows 120 requests per hour per (API client, user) pair; the
# server reports the real limit and remaining budget in the RateLimit headers.
# Each poll makes API_CALLS_PER_POLL requests (latest-change + accidents).
CONF_SCAN_INTERVAL = "scan_interval"  # minutes, stored in entry.options
DEFAULT_SCAN_INTERVAL = 5  # minutes
# 2 calls/poll every 2 min = 60 req/hr: half the budget, leaving room for reloads,
# restarts and re-authorisation without hitting HTTP 429.
MIN_SCAN_INTERVAL = 2  # minutes
MAX_SCAN_INTERVAL = 60  # minutes
API_CALLS_PER_POLL = 2
# Requests kept in reserve when stretching the interval to fit the remaining quota.
RATELIMIT_RESERVE = 4

# Number of accidents fetched per poll (newest first). Feeds the current-change
# partitioning, daily/weekly counts and the history attribute.
ACCIDENT_FETCH_SIZE = 200
# Number of accidents kept in the history attribute.
HISTORY_LENGTH = 20

# Event fired once for every newly logged accident.
EVENT_ACCIDENT = "diapstash_accident"
