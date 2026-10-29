"""Constants for the Trimlight Edge integration."""

DOMAIN = "trimlight"

CONF_CLIENT_ID = "client_id"
CONF_CLIENT_SECRET = "client_secret"

API_BASE_URL = "https://trimlight.ledhue.com/trimlight"

UPDATE_INTERVAL = 30  # seconds

# Device switch states (API docs device fields, sections 3 and 4.1).
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

# API result codes that mean the client credentials were rejected. The API
# docs' result-code table (appendix [1]) lists 10001 ("Error") and 10002
# ("Wrong password"); request and device-data problems come back as 12xxx
# and 20000 codes instead. A request whose timestamp falls outside the
# cloud's allowed window is rejected the same way (docs note 1.5), so a
# wrong host clock looks like bad credentials. These trigger a reauth flow
# instead of being treated as a transient failure.
API_AUTH_ERROR_CODES = frozenset({10001, 10002})

# API result code the device detail call returns when the cloud can't parse
# what the controller has stored ("Device data could not be parsed. Please
# synchronize the device and retry"). It's data-dependent rather than
# transient — users have traced it to duplicate or oddly named saved effects,
# which only the Trimlight app can fix — so it's surfaced as a repair issue.
API_UNPARSEABLE_DEVICE_DATA_CODE = 12006

# Transient-error retry policy for cloud requests. The Trimlight cloud
# occasionally returns a 502/504 or drops a connection; a couple of quick
# retries ride those out instead of failing the whole update.
API_MAX_REQUEST_ATTEMPTS = 3
API_RETRY_BASE_BACKOFF = 0.5  # seconds; doubled on each retry

# Custom effect saved on the device for HA color picker control.
HA_COLOR_EFFECT_NAME = "HA Color"

# Effect categories (API docs sections 4.2 and 12, "Effect fields details"):
# 0 is a built-in effect, 1 a custom effect. The docs' request *examples*
# show 1/2 instead, which is where the 2 this integration used to send came
# from; the cloud now validates against the table and rejects anything else
# with error 12008 ("Valid values: 0 (built-in effect), 1 (custom effect)").
EFFECT_CATEGORY_BUILTIN = 0
EFFECT_CATEGORY_CUSTOM = 1

# Custom effect mode: STATIC (from API docs appendix [5]).
EFFECT_MODE_STATIC = 0
