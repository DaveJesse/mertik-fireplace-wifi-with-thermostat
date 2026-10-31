"""Constants for the Mertik Maxitrol Fireplace integration."""

DOMAIN = "mertik"
DEFAULT_NAME = "Mertik Maxitrol"

# Config / options entry keys
CONF_LOW_THRESHOLD = "low_threshold"
CONF_HIGH_THRESHOLD = "high_threshold"
CONF_TEMP_SENSOR = "temperature_sensor"

# Default thermostatic thresholds (degrees C below setpoint)
DEFAULT_LOW_THRESHOLD = 1.0  # within 1C below setpoint -> Low Heat
DEFAULT_HIGH_THRESHOLD = 2.0  # more than 2C below setpoint -> Full Heat
DEFAULT_TEMP_SENSOR = ""  # empty = use fireplace handset sensor

# Thermostat setpoint step
CONF_TEMP_STEP = "temperature_step"
DEFAULT_TEMP_STEP = 0.1

# Thermostatic hysteresis (degrees C)
CONF_HYSTERESIS = "hysteresis"
DEFAULT_HYSTERESIS = 0.1

# Switch the fire's light on when the fire is switched on (as the handset does)
CONF_LIGHT_WITH_FIRE = "light_with_fire"
DEFAULT_LIGHT_WITH_FIRE = True

# Default thermostat target temperature, applied each time the fire is switched on
CONF_DEFAULT_TARGET = "default_temperature"
DEFAULT_TARGET_TEMP = 20.0
MIN_TARGET_TEMP = 5.0
MAX_TARGET_TEMP = 36.0

# Heating mode select options
MODE_OFF = "Off"  # kept for compatibility, not shown in UI
MODE_STANDBY = "Standby"  # pilot only; shown by thermostat when room is warm
MODE_FULL = "Full Heat"
MODE_MEDIUM = "Medium Heat"
MODE_LOW = "Low Heat"
MODE_THERMO = "Thermostatic"
HEATING_MODES = [MODE_STANDBY, MODE_FULL, MODE_MEDIUM, MODE_LOW, MODE_THERMO]

# Thermostat (climate) presets: translation key -> heating mode
PRESET_TO_MODE = {
    "low_heat": MODE_LOW,
    "medium_heat": MODE_MEDIUM,
    "full_heat": MODE_FULL,
    "thermostatic": MODE_THERMO,
}
MODE_TO_PRESET = {mode: preset for preset, mode in PRESET_TO_MODE.items()}

# Flame height step range
FLAME_MIN = 1
FLAME_MAX = 12  # table has 12 entries; formula gives 13 for max (0xFF)


# Light levels 1-9: HA brightness for each (the middle of the band the Light
# entity sends as that level: 77, 88 ... EE, FB).
LEVEL_BRIGHTNESS = [1, 33, 65, 96, 128, 160, 191, 223, 255]


def level_from_brightness(brightness: int) -> int:
    """Light level 1-9 the Light entity sends for an HA brightness 1-255."""
    return round(max(0, min(254, brightness - 1)) / 254 * 8) + 1

# Flame Height sensor states (steps 1-6 = low, 7-12 = high).
FLAME_LEVELS = ["off", "pilot", "low", "high"]
