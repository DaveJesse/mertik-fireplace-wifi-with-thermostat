"""Config flow and options flow for Mertik Maxitrol fireplace."""

import logging
import socket
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.components.sensor import SensorDeviceClass

from .const import (
    DOMAIN,
    CONF_LOW_THRESHOLD,
    CONF_HIGH_THRESHOLD,
    CONF_TEMP_SENSOR,
    CONF_TEMP_STEP,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_TEMP_SENSOR,
    DEFAULT_TEMP_STEP,
    CONF_DEFAULT_TARGET,
    DEFAULT_TARGET_TEMP,
    DEFAULT_NAME,
    CONF_LIGHT_WITH_FIRE,
    DEFAULT_LIGHT_WITH_FIRE,
    CONF_HYSTERESIS,
    DEFAULT_HYSTERESIS,
    CONF_MIN_REIGNITION,
    DEFAULT_MIN_REIGNITION,
    MAX_MIN_REIGNITION,
    MIN_TARGET_TEMP,
    MAX_TARGET_TEMP,
)


def _valid_target(value: float) -> bool:
    return MIN_TARGET_TEMP <= value <= MAX_TARGET_TEMP


def _valid_hysteresis(h: float, low: float, full: float) -> bool:
    """Bands must not overlap: 2h has to fit in the Low and Medium bands."""
    return h >= 0 and 2 * h <= low and 2 * h <= full - low

_LOGGER = logging.getLogger(__name__)


def _temp_sensor_options(hass: HomeAssistant) -> dict[str, str]:
    """Return {entity_id: friendly_name} for all temperature sensors."""
    registry = er.async_get(hass)
    options = {"": "Mertik handset (built-in)"}
    for entry in registry.entities.values():
        state = hass.states.get(entry.entity_id)
        if state is None:
            continue
        if state.attributes.get("device_class") == SensorDeviceClass.TEMPERATURE:
            # Skip the Mertik's own sensor to avoid it appearing twice
            if entry.domain == "sensor" and entry.platform == DOMAIN:
                continue
            friendly = state.attributes.get("friendly_name", entry.entity_id)
            options[entry.entity_id] = f"{friendly} ({entry.entity_id})"
    return options


class MertikConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Initial setup: name, host, temperature sensor, thresholds, default setpoint."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            low = user_input.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
            high = user_input.get(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD)

            target = user_input.get(CONF_DEFAULT_TARGET, DEFAULT_TARGET_TEMP)

            if low <= 0 or high <= 0 or low >= high:
                errors["base"] = "invalid_thresholds"
            elif not _valid_target(target):
                errors[CONF_DEFAULT_TARGET] = "invalid_default_temp"
            else:
                await self.async_set_unique_id(host)
                self._abort_if_unique_id_configured()
                can_connect = await self.hass.async_add_executor_job(
                    _test_connection, host
                )
                if not can_connect:
                    errors["base"] = "cannot_connect"
                else:
                    data = dict(user_input)
                    name = data.pop(CONF_NAME, DEFAULT_NAME).strip() or DEFAULT_NAME
                    return self.async_create_entry(title=name, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): str,
                vol.Required(CONF_HOST): str,
                vol.Optional(CONF_TEMP_SENSOR, default=DEFAULT_TEMP_SENSOR): vol.In(
                    _temp_sensor_options(self.hass)
                ),
                vol.Optional(
                    CONF_LOW_THRESHOLD, default=DEFAULT_LOW_THRESHOLD
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_HIGH_THRESHOLD, default=DEFAULT_HIGH_THRESHOLD
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_DEFAULT_TARGET, default=DEFAULT_TARGET_TEMP
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST]
            low = user_input.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)
            high = user_input.get(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD)

            if low <= 0 or high <= 0 or low >= high:
                errors["base"] = "invalid_thresholds"
            else:
                can_connect = await self.hass.async_add_executor_job(
                    _test_connection, host
                )
                if not can_connect:
                    errors["base"] = "cannot_connect"
                else:
                    existing = (
                        self.hass.config_entries.async_entry_for_domain_unique_id(
                            DOMAIN, host
                        )
                    )
                    if existing and existing.entry_id != entry.entry_id:
                        errors["base"] = "already_configured"
                    else:
                        return self.async_update_reload_and_abort(
                            entry,
                            unique_id=host,
                            data_updates=user_input,
                        )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST, default=entry.data.get(CONF_HOST, "")): str,
                vol.Optional(
                    CONF_LOW_THRESHOLD,
                    default=entry.data.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD),
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_HIGH_THRESHOLD,
                    default=entry.data.get(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD),
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler."""
        return MertikOptionsFlow(config_entry)


class MertikOptionsFlow(config_entries.OptionsFlow):
    """Options flow: adjust thresholds and temperature sensor selection.

    Accessible via Settings -> Devices & Services -> Mertik -> Configure.
    Changes take effect immediately without restarting HA.
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        # Current values (prefer options over data for previously set values)
        current_low = self._entry.options.get(
            CONF_LOW_THRESHOLD,
            self._entry.data.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD),
        )
        current_high = self._entry.options.get(
            CONF_HIGH_THRESHOLD,
            self._entry.data.get(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD),
        )
        current_sensor = self._entry.options.get(
            CONF_TEMP_SENSOR,
            self._entry.data.get(CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR),
        )
        current_step = float(
            self._entry.options.get(
                CONF_TEMP_STEP,
                self._entry.data.get(CONF_TEMP_STEP, DEFAULT_TEMP_STEP),
            )
        )

        current_target = float(
            self._entry.options.get(
                CONF_DEFAULT_TARGET,
                self._entry.data.get(CONF_DEFAULT_TARGET, DEFAULT_TARGET_TEMP),
            )
        )

        current_light = bool(
            self._entry.options.get(
                CONF_LIGHT_WITH_FIRE,
                self._entry.data.get(CONF_LIGHT_WITH_FIRE, DEFAULT_LIGHT_WITH_FIRE),
            )
        )

        current_hyst = float(
            self._entry.options.get(
                CONF_HYSTERESIS,
                self._entry.data.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS),
            )
        )
        current_reign = float(
            self._entry.options.get(
                CONF_MIN_REIGNITION,
                self._entry.data.get(CONF_MIN_REIGNITION, DEFAULT_MIN_REIGNITION),
            )
        )

        if user_input is not None:
            low = user_input.get(CONF_LOW_THRESHOLD, current_low)
            high = user_input.get(CONF_HIGH_THRESHOLD, current_high)
            step = user_input.get(CONF_TEMP_STEP, current_step)
            if low <= 0 or high <= 0 or low >= high:
                errors["base"] = "invalid_thresholds"
            elif step <= 0:
                errors[CONF_TEMP_STEP] = "invalid_temp_step"
            elif not _valid_target(
                user_input.get(CONF_DEFAULT_TARGET, current_target)
            ):
                errors[CONF_DEFAULT_TARGET] = "invalid_default_temp"
            elif not _valid_hysteresis(
                user_input.get(CONF_HYSTERESIS, current_hyst), low, high
            ):
                errors[CONF_HYSTERESIS] = "invalid_hysteresis"
            elif not (
                0 <= user_input.get(CONF_MIN_REIGNITION, current_reign)
                <= MAX_MIN_REIGNITION
            ):
                errors[CONF_MIN_REIGNITION] = "invalid_min_reignition"
            else:
                return self.async_create_entry(title="", data=user_input)

        # Build sensor selector list dynamically from entities on the system
        sensor_options = _temp_sensor_options(self.hass)
        # If previously selected sensor no longer exists, fall back gracefully
        if current_sensor not in sensor_options:
            current_sensor = DEFAULT_TEMP_SENSOR

        schema = vol.Schema(
            {
                vol.Optional(CONF_TEMP_SENSOR, default=current_sensor): vol.In(
                    sensor_options
                ),
                vol.Optional(CONF_LOW_THRESHOLD, default=current_low): vol.Coerce(
                    float
                ),
                vol.Optional(CONF_HIGH_THRESHOLD, default=current_high): vol.Coerce(
                    float
                ),
                vol.Optional(CONF_TEMP_STEP, default=current_step): vol.Coerce(float),
                vol.Optional(CONF_DEFAULT_TARGET, default=current_target): vol.Coerce(
                    float
                ),
                vol.Optional(CONF_HYSTERESIS, default=current_hyst): vol.Coerce(
                    float
                ),
                vol.Optional(
                    CONF_MIN_REIGNITION, default=current_reign
                ): vol.Coerce(float),
                vol.Optional(CONF_LIGHT_WITH_FIRE, default=current_light): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "low_desc": "Degrees C below setpoint to switch to Low Heat",
                "high_desc": "Degrees C below setpoint to switch to Full Heat (must be > Low threshold)",
            },
        )


def _test_connection(host: str) -> bool:
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        sock.connect((host, 2000))
        sock.close()
        return True
    except OSError:
        return False
