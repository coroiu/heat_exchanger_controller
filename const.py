"""Constants for Heat Exchanger Controller."""

from homeassistant.const import Platform

DOMAIN = "heat_exchanger_controller"

PLATFORMS: list[Platform] = [
    Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER, Platform.SWITCH, Platform.BINARY_SENSOR,
]

# --- Setup (stored in entry.data) ---
CONF_NAME = "name"
CONF_INDOOR_TEMP_ENTITY = "indoor_temp_entity"
CONF_OUTDOOR_TEMP_ENTITIES = "outdoor_temp_entities"
CONF_SUPPLY_TEMP_ENTITY = "supply_temp_entity"
CONF_OUTPUT_SERVICE = "output_service"
CONF_OUTPUT_VALUE_KEY = "output_value_key"
CONF_OUTPUT_READBACK_ENTITY = "output_readback_entity"  # optional: reports the potentiometer's actual value
CONF_WEATHER_ENTITY = "weather_entity"
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"

# --- Options (stored in entry.options) ---
CONF_RC_R = "rc_r"
CONF_RC_C = "rc_c"
CONF_RC_UA = "rc_ua"
CONF_RC_G = "rc_g"
CONF_PID_KP = "pid_kp"
CONF_PID_KI = "pid_ki"
CONF_PID_KD = "pid_kd"
CONF_SUPPLY_MIN = "supply_min"
CONF_SUPPLY_MAX = "supply_max"
CONF_OUTPUT_MIN = "output_min"
CONF_OUTPUT_MAX = "output_max"
CONF_PREDICTION_HORIZON = "prediction_horizon"
CONF_OUTER_INTERVAL = "outer_interval"
CONF_INNER_INTERVAL = "inner_interval"
CONF_CLIMATE_MIN_TEMP = "climate_min_temp"
CONF_CLIMATE_MAX_TEMP = "climate_max_temp"
CONF_PUMP_OFF_THRESHOLD = "pump_off_threshold"
CONF_MIN_PUMP_ON_DURATION = "min_pump_on_duration"
CONF_BOOST_TRIGGER_SPAN = "boost_trigger_span"

# --- Climate state + switch state (stored in entry.options, no reload on change) ---
CONF_TARGET_TEMPERATURE = "target_temperature"
CONF_HVAC_MODE = "hvac_mode"
CONF_DRY_RUN = "dry_run"

# Keys that trigger a full reload when changed in options
RELOAD_ON_CHANGE_KEYS = {
    CONF_RC_R, CONF_RC_C, CONF_RC_UA, CONF_RC_G,
    CONF_PID_KP, CONF_PID_KI, CONF_PID_KD,
    CONF_SUPPLY_MIN, CONF_SUPPLY_MAX,
    CONF_OUTPUT_MIN, CONF_OUTPUT_MAX,
    CONF_PREDICTION_HORIZON,
    CONF_OUTER_INTERVAL, CONF_INNER_INTERVAL,
    CONF_CLIMATE_MIN_TEMP, CONF_CLIMATE_MAX_TEMP,
}

# --- Defaults ---
CONF_DISTURBANCE_ALPHA = "disturbance_alpha"
CONF_DISTURBANCE_ESTIMATE = "disturbance_estimate"

DEFAULT_RC_R = 0.05
DEFAULT_RC_C = 500.0
DEFAULT_RC_UA = 20.0
DEFAULT_RC_G = 1.5
DEFAULT_PID_KP = 1.0
DEFAULT_PID_KI = 0.1
DEFAULT_PID_KD = 0.0
DEFAULT_SUPPLY_MIN = 20.0
DEFAULT_SUPPLY_MAX = 70.0
DEFAULT_OUTPUT_MIN = 0.0
DEFAULT_OUTPUT_MAX = 30.0
DEFAULT_PREDICTION_HORIZON = 2
DEFAULT_OUTER_INTERVAL = 5
DEFAULT_INNER_INTERVAL = 30
DEFAULT_CLIMATE_MIN_TEMP = 15.0
DEFAULT_CLIMATE_MAX_TEMP = 28.0
DEFAULT_TARGET_TEMPERATURE = 21.0
DEFAULT_HVAC_MODE = "heat"
DEFAULT_DRY_RUN = True
DEFAULT_DISTURBANCE_ALPHA = 0.95
DEFAULT_DISTURBANCE_ESTIMATE = 0.0
# Hard cap on the observer's target shift (°C); ±1.5 was still too much if it wound up
DISTURBANCE_MAX = 0.5
# Observer leak time constant (h): d_hat decays toward 0 so it only carries recent error.
# Not offset-free any more: at α 0.95 / 5 min ticks it removes ~65 % of a steady offset.
DISTURBANCE_LEAK_TAU_H = 3.0
DEFAULT_PUMP_OFF_THRESHOLD = 100.0  # above normal output range → feature disabled by default
DEFAULT_MIN_PUMP_ON_DURATION = 20  # minutes
DEFAULT_BOOST_TRIGGER_SPAN = 0.3  # °C below target that triggers a preventive boost

# --- Rooms (config subentries, shadow mode) ---
SUBENTRY_TYPE_ROOM = "room"
# Room subentry data: name (title), sensor, and the RC keys CONF_RC_R/C/UA/G
CONF_ROOM_SENSOR = "room_sensor"
# Room state inputs (number entities, restored on restart; never reload)
ROOM_INPUT_TARGET = "target"
ROOM_INPUT_IMPORTANCE = "importance"
ROOM_INPUT_RAMP = "importance_ramp_time"
DEFAULT_ROOM_TARGET = 22.0
DEFAULT_ROOM_IMPORTANCE = 1.0
DEFAULT_IMPORTANCE_RAMP_MIN = 45

# --- Path MPC (shadow mode) inputs (number entities, restored on restart; never reload) ---
PATH_INPUT_BAND = "path_comfort_band"
PATH_INPUT_HORIZON = "path_horizon"
DEFAULT_PATH_BAND = 0.2
DEFAULT_PATH_HORIZON = 6

# --- Status ---
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"
STATUS_FAULT = "fault"
STATUS_STARTING = "starting"

# --- Open-Meteo ---
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&hourly=shortwave_radiation"
    "&timezone=UTC"
    "&forecast_days=2"
)

# --- Outdoor source labels ---
OUTDOOR_SOURCE_LAST_KNOWN = "last_known"
OUTDOOR_SOURCE_UNAVAILABLE = "unavailable"
