"""Constants for the Trimlight Edge integration."""

DOMAIN = "trimlight"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

API_BASE_URL = "https://trimlight.ledhue.com/trimlight"

UPDATE_INTERVAL = 30  # seconds

# Device switch states (from Trimlight V2 API docs section 5).
SWITCH_STATE_OFF = 0
SWITCH_STATE_MANUAL = 1
SWITCH_STATE_TIMER = 2

# Daily schedule repetition values (API docs appendix [6]).
SCHEDULE_REPETITION_TODAY = 0
SCHEDULE_REPETITION_EVERYDAY = 1
SCHEDULE_REPETITION_WEEKDAYS = 2
SCHEDULE_REPETITION_WEEKEND = 3

# API minimum delay between requests (seconds).
# The Trimlight cloud server returns error 20000 on rapid requests.
API_REQUEST_MIN_INTERVAL = 0.3

# API result codes that mean the client credentials were rejected
# (invalid clientId / signature per API docs section 2). These trigger a
# reauth flow instead of being treated as a transient failure.
API_AUTH_ERROR_CODES = frozenset({10001})

# Transient-error retry policy for cloud requests. The Trimlight cloud
# occasionally returns a 502/504 or drops a connection; a couple of quick
# retries ride those out instead of failing the whole update.
API_MAX_REQUEST_ATTEMPTS = 3
API_RETRY_BASE_BACKOFF = 0.5  # seconds; doubled on each retry

# Custom effect saved on the device for HA color picker control.
HA_COLOR_EFFECT_NAME = "HA Color"

# Effect category used by the device (determined from device data).
EFFECT_CATEGORY_CUSTOM = 2

# Custom effect mode: STATIC (from API docs appendix [5]).
EFFECT_MODE_STATIC = 0
