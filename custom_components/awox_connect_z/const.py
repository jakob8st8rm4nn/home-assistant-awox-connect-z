"""Constants for AwoX Connect.Z."""

# -----------------------------------------------------------------------------
# Integration and config-entry keys
# -----------------------------------------------------------------------------
DOMAIN = "awox_connect_z"

CONF_EMAIL = "email"
CONF_MAC = "mac"
CONF_NAME = "name"
CONF_OWNER_ID = "owner_id"
CONF_DEVICES = "devices"
CONF_MESH_NAME = "mesh_name"
CONF_MESH_PASSWORD = "mesh_password"
CONF_SETUP_METHOD = "setup_method"

SETUP_METHOD_CLOUD = "cloud"
SETUP_METHOD_LOCAL = "local"

CONF_DEFAULT_TRANSITION = "default_transition"
CONF_IDLE_DISCONNECT = "idle_disconnect"
CONF_MAX_CONCURRENT_COMMANDS = "max_concurrent_commands"
CONF_AVAILABILITY_TIMEOUT = "availability_timeout"

DEFAULT_NAME = "AwoX Connect.Z"

# Shared Home Assistant runtime data key for serializing new BLE connection
# establishment across all loaded AwoX config entries.
DATA_RUNTIME_CONNECT_LOCK = "_runtime_connect_lock"


# -----------------------------------------------------------------------------
# User options: defaults and bounds
# -----------------------------------------------------------------------------
# Transition, idle-disconnect and availability timeout values are in seconds.
DEFAULT_TRANSITION = 0.2
MIN_TRANSITION = 0.0
MAX_TRANSITION = 10.0

DEFAULT_IDLE_DISCONNECT = 10.0
MIN_IDLE_DISCONNECT = 5.0
MAX_IDLE_DISCONNECT = 300.0

DEFAULT_MAX_CONCURRENT_COMMANDS = 2
MIN_CONCURRENT_COMMANDS = 1
MAX_CONCURRENT_COMMANDS = 32

# A lamp stays available while HA has a live BLE connection or a Bluetooth
# packet has been seen within this configurable window.
DEFAULT_AVAILABILITY_TIMEOUT = 30.0
MIN_AVAILABILITY_TIMEOUT = 10.0
MAX_AVAILABILITY_TIMEOUT = 300.0


# -----------------------------------------------------------------------------
# Setup and device discovery
# -----------------------------------------------------------------------------
# Extra wait used by the Bluetooth config-flow discovery path.
ADDITIONAL_DISCOVERY_TIMEOUT_SECONDS = 30
# Duration of one local setup/reconfigure scan window.
LOCAL_DISCOVERY_SCAN_SECONDS = 30
# Maximum time for one local credential/device verification attempt.
LOCAL_VERIFICATION_TIMEOUT_SECONDS = 20.0
# Number of local verification attempts allowed to run in parallel.
LOCAL_VERIFICATION_CONCURRENCY = 2
# Timeout for one AwoX cloud HTTP request during setup/reconfigure.
CLOUD_REQUEST_TIMEOUT_SECONDS = 20.0


# -----------------------------------------------------------------------------
# BLE connection, authentication, writes and cleanup
# -----------------------------------------------------------------------------
# Number of discovery passes used by setup/reconfigure verification.
DISCOVERY_ATTEMPTS = 5
# Maximum connector attempts for one setup/reconfigure BLE connection.
SETUP_CONNECT_ATTEMPTS = 4
# Delay grows by this amount for each setup discovery retry, capped below.
DISCOVERY_RETRY_BASE_DELAY_SECONDS = 1.5
DISCOVERY_RETRY_MAX_DELAY_SECONDS = 4.0

# Runtime command budget and technical retry limits.
RUNTIME_COMMAND_TIMEOUT_SECONDS = 30.0
RUNTIME_CONNECT_TIMEOUT_SECONDS = 12.0
RUNTIME_CONNECT_QUEUE_TIMEOUT_SECONDS = 30.0
RUNTIME_AUTH_TIMEOUT_SECONDS = 4.0
RUNTIME_WRITE_TIMEOUT_SECONDS = 4.0
RUNTIME_WRITE_ATTEMPTS = 2

# Protected disconnect cleanup limit.
DISCONNECT_TIMEOUT_SECONDS = 12.0
# Protocol settle pause between pair write and pair response read.
AUTH_RESPONSE_SETTLE_SECONDS = 0.05
# Only log shared runtime-connect queue waits at or above this duration.
RUNTIME_CONNECT_QUEUE_LOG_THRESHOLD_SECONDS = 0.05


# -----------------------------------------------------------------------------
# Command processing and hardware guards
# -----------------------------------------------------------------------------
# Hardware settling guard after a successfully written power-off command.
# Applies only after a power-off write succeeds and only to a subsequent
# command that can turn the lamp back on; it is not a general command delay.
POWER_OFF_SETTLE_SECONDS = 0.5


# -----------------------------------------------------------------------------
# Availability and diagnostic publishing
# -----------------------------------------------------------------------------
# Poll cadence while unavailable so identical deduplicated packets can restore
# availability via Home Assistant's refreshed Bluetooth service timestamp.
AVAILABILITY_RECOVERY_POLL_SECONDS = 1.0
# Small lower bound that avoids a zero/near-zero availability wait loop.
AVAILABILITY_MIN_WAIT_SECONDS = 0.05
# RSSI diagnostic throttling: publish sooner only for a significant change.
RSSI_MIN_PUBLISH_INTERVAL_SECONDS = 5.0
RSSI_SIGNIFICANT_CHANGE_DBM = 5


# -----------------------------------------------------------------------------
# Connect.Z device-address and credential constraints
# -----------------------------------------------------------------------------
# Valid individual lamp mesh destinations. Group and broadcast addresses may
# follow different rules and must not use these bounds automatically.
MIN_MESH_ID = 1
MAX_MESH_ID = 0xFFFE

# Maximum size of each mesh credential after UTF-8 encoding (bytes, not chars).
MESH_CREDENTIAL_MAX_BYTES = 16


# -----------------------------------------------------------------------------
# Light capabilities
# -----------------------------------------------------------------------------
MIN_COLOR_TEMP_KELVIN = 2200
MAX_COLOR_TEMP_KELVIN = 6500

# Native lamp-side effects confirmed on EGLO-ZM-RGB-TW firmware 3.0.2.
# Keep the names stable because Home Assistant stores/forwards effect values as
# strings and exposes them directly through the light entity.
EFFECT_COLOR_CYCLE = "Color cycle"
EFFECT_CANDLE = "Candle"
NATIVE_EFFECTS = (EFFECT_COLOR_CYCLE, EFFECT_CANDLE)


# -----------------------------------------------------------------------------
# Bluetooth / AwoX protocol identifiers
# -----------------------------------------------------------------------------
AWOX_COMPANY_ID = 0x0160
PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
COMMAND_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
STATUS_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"


# -----------------------------------------------------------------------------
# AwoX cloud service identifiers
# -----------------------------------------------------------------------------
PARSE_APP_ID = "55O69FLtoxPt67LLwaHGpHmVWndhZGn9Wty8PLrJ"
PARSE_CLIENT_KEY = "PyR3yV65rytEicteNlQHSVNpAGvCByOrsLiEqJtI"
PARSE_URLS = (
    "https://l4hparse-hc-prod.awox.cloud/parse/",
    "https://l4hparse-prod.awox.cloud/parse/",
)
