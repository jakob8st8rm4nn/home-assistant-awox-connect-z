"""Constants for AwoX Connect.Z."""

DOMAIN = "awox_connect_z"
PLATFORMS = ["light"]

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
DEFAULT_TRANSITION = 0.2
DEFAULT_IDLE_DISCONNECT = 10.0
DEFAULT_MAX_CONCURRENT_COMMANDS = 1

# A lamp stays available while HA has a live BLE connection or a Bluetooth
# packet has been seen within this configurable window.
DEFAULT_AVAILABILITY_TIMEOUT = 30.0
MIN_AVAILABILITY_TIMEOUT = 10.0
MAX_AVAILABILITY_TIMEOUT = 300.0
AVAILABILITY_RECOVERY_POLL = 1.0
MIN_CONCURRENT_COMMANDS = 1
MAX_CONCURRENT_COMMANDS = 32

MIN_COLOR_TEMP_KELVIN = 2200
MAX_COLOR_TEMP_KELVIN = 6500

PAIR_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1914"
COMMAND_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1912"
STATUS_CHAR_UUID = "00010203-0405-0607-0809-0a0b0c0d1911"

PARSE_APP_ID = "55O69FLtoxPt67LLwaHGpHmVWndhZGn9Wty8PLrJ"
PARSE_CLIENT_KEY = "PyR3yV65rytEicteNlQHSVNpAGvCByOrsLiEqJtI"
PARSE_URLS = (
    "https://l4hparse-hc-prod.awox.cloud/parse/",
    "https://l4hparse-prod.awox.cloud/parse/",
)
