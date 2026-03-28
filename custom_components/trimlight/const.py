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

# API minimum delay between requests (seconds).
# The Trimlight cloud server returns error 20000 on rapid requests.
API_REQUEST_MIN_INTERVAL = 0.3

# Custom effect saved on the device for HA color picker control.
HA_COLOR_EFFECT_NAME = "HA Color"

# Effect category used by the device (determined from device data).
EFFECT_CATEGORY_CUSTOM = 2

# Custom effect mode: STATIC (from API docs appendix [5]).
EFFECT_MODE_STATIC = 0
