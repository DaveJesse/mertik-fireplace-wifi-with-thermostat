"""Climate entity -- thermostat setpoint display and thermostatic control logic."""

import logging
from typing import Any, TypeVar

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import MertikConfigEntry
from .coordinator import MertikDataCoordinator
from .const import (
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
    MIN_TARGET_TEMP,
    MAX_TARGET_TEMP,
    MODE_STANDBY,
    MODE_FULL,
    MODE_MEDIUM,
    MODE_LOW,
    MODE_THERMO,
    MODE_TO_PRESET,
    PRESET_TO_MODE,
)
from .entity import MertikEntity

_T = TypeVar("_T")

PARALLEL_UPDATES = 1

_LOGGER = logging.getLogger(__name__)

MIN_TEMP = MIN_TARGET_TEMP
MAX_TEMP_C = MAX_TARGET_TEMP


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MertikConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Mertik climate entity."""
    dataservice = entry.runtime_data
    async_add_entities(
        [
            MertikClimateEntity(dataservice, entry.entry_id, entry.title, entry),
        ]
    )


class MertikClimateEntity(MertikEntity, ClimateEntity, RestoreEntity):
    """The fireplace as a thermostat: the whole control panel in one entity.

    - Heat / Off switches the fire on and off.
    - Presets choose the heating mode (Low, Medium, Full, Thermostatic); they
      are shared with the Heating Mode select entity.
    - The target temperature is used in Thermostatic mode, where this entity
      also runs the control loop on every coordinator update.
    """

    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP_C
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_preset_modes = list(PRESET_TO_MODE)
    # Target temperature stays supported in every mode so automations that
    # set it never fail; it only takes effect in Thermostatic.
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(
        self,
        dataservice: MertikDataCoordinator,
        entry_id: str,
        device_name: str,
        entry: Any,
    ) -> None:
        """Initialize thermostat entity."""
        super().__init__(dataservice, entry_id, device_name)
        self._entry = entry
        self._attr_unique_id = entry_id + "-Thermostat"
        self._target_temp = self._default_target
        self._last_applied_mode: str | None = (
            None  # prevent re-sending same command every poll
        )
        # Previous fire on/off state, used to detect switch-on (off -> on) so
        # the setpoint can be reset to the configured default.
        self._was_on: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Restore target temperature from last known state."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.attributes.get(ATTR_TEMPERATURE) is not None:
            self._target_temp = float(last.attributes[ATTR_TEMPERATURE])
        # Restore the heating mode from the preset if nothing else has (e.g.
        # the Heating Mode select entity is disabled).
        if (
            self._dataservice.heating_mode is None
            and last is not None
            and last.attributes.get("preset_mode") in PRESET_TO_MODE
        ):
            self._dataservice.set_heating_mode(
                PRESET_TO_MODE[last.attributes["preset_mode"]]
            )
        # Treat the state at startup as the baseline: a restart with the fire
        # already running keeps the remembered setpoint.
        self._was_on = self._dataservice.is_on

    # ---- Config helpers --------------------------------------------------

    def _get_option(self, key: str, default: _T) -> _T:
        """Return option value, falling back to data then the supplied default."""
        return self._entry.options.get(key, self._entry.data.get(key, default))

    @property
    def _low_thresh(self) -> float:
        return float(self._get_option(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD))

    @property
    def _high_thresh(self) -> float:
        return float(self._get_option(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD))

    @property
    def _temp_sensor_entity_id(self) -> str:
        return self._get_option(CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR)

    @property
    def _default_target(self) -> float:
        """Setpoint applied each time the fire is switched on."""
        return float(self._get_option(CONF_DEFAULT_TARGET, DEFAULT_TARGET_TEMP))

    @property
    def target_temperature_step(self) -> float:
        """Return the setpoint increment configured by the user."""
        return float(self._get_option(CONF_TEMP_STEP, DEFAULT_TEMP_STEP))

    # ---- Temperature reading --------------------------------------------

    def _get_current_temperature(self) -> float | None:
        sensor_id = self._temp_sensor_entity_id
        if sensor_id:
            state = self.hass.states.get(sensor_id)
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    return float(state.state)
                except ValueError:
                    _LOGGER.warning(
                        "Temp sensor %s: non-numeric value %s", sensor_id, state.state
                    )
            # External sensor configured but unavailable — return None rather than
            # falling back to the device value (user explicitly chose the sensor).
            return None
        # No external sensor — fall back to device-reported ambient temperature.
        # When the handset has a fault (e.g. F44: out of range / low battery),
        # the device cannot measure room temperature; the reported value is
        # meaningless. Return None so thermostatic control can react safely.
        if not self._dataservice.is_handset_connected:
            return None
        t = self._dataservice.ambient_temperature
        return t if t else None

    # ---- State properties -----------------------------------------------

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature from sensor or device handset."""
        return self._get_current_temperature()

    @property
    def target_temperature(self) -> float:
        """Return the current setpoint temperature."""
        return self._target_temp

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.HEAT if self._dataservice.is_on else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        """Heating when the burner is lit, idle on pilot only, else off."""
        mertik = self._dataservice.mertik
        if mertik.is_flame_on or mertik.is_igniting:
            return HVACAction.HEATING
        if self._dataservice.is_on:
            return HVACAction.IDLE
        return HVACAction.OFF

    @property
    def preset_mode(self) -> str | None:
        """Current heating mode as a preset (None for Standby / not set)."""
        return MODE_TO_PRESET.get(self._dataservice.heating_mode or "")

    # ---- Commands -------------------------------------------------------

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        step = self.target_temperature_step
        snapped = round(round(float(temp) / step) * step, 2)
        self._target_temp = max(MIN_TEMP, min(MAX_TEMP_C, snapped))
        self._last_applied_mode = None  # force re-evaluation on next poll
        self.async_write_ha_state()
        _LOGGER.debug("Thermostat setpoint -> %.1f C", self._target_temp)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode == HVACMode.OFF:
            await self.async_turn_off()
        elif hvac_mode == HVACMode.HEAT:
            await self.async_turn_on()

    async def async_turn_on(self) -> None:
        """Switch the fire on in the current heating mode."""
        if self._dataservice.is_on:
            return
        await self.hass.async_add_executor_job(self._dataservice.turn_on)
        self._dataservice.async_set_updated_data(None)

    async def async_turn_off(self) -> None:
        """Switch the fire off, pilot included."""
        await self.hass.async_add_executor_job(self._dataservice.turn_off)
        self._dataservice.async_set_updated_data(None)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Choose the heating mode (shared with the Heating Mode select)."""
        mode = PRESET_TO_MODE[preset_mode]
        self._last_applied_mode = None  # re-evaluate if Thermostatic
        await self.hass.async_add_executor_job(
            self._dataservice.select_heating_mode, mode
        )
        self._dataservice.async_set_updated_data(None)

    # ---- Thermostatic logic (runs on every coordinator poll) ------------

    def _handle_coordinator_update(self) -> None:
        self._apply_default_on_switch_on()
        self._run_thermostatic_logic()
        super()._handle_coordinator_update()

    def _apply_default_on_switch_on(self) -> None:
        """Reset the setpoint to the configured default when the fire turns on.

        Covers every way of switching on (HA switch, handset, MyFire app),
        because it watches the fire's on/off state rather than the switch.
        """
        is_on = self._dataservice.is_on
        if self._was_on is False and is_on:
            self._target_temp = self._default_target
            self._last_applied_mode = None
            _LOGGER.debug(
                "Fire switched on: setpoint reset to default %.1f C",
                self._target_temp,
            )
        self._was_on = is_on

    def _run_thermostatic_logic(self) -> None:
        # If we are waiting for an ignition to complete, let the
        # coordinator handle it this cycle and skip the temperature
        # comparison -- flame height commands are ignored during ignition.
        if self._dataservice.check_pending_mode():
            return

        if self._dataservice.heating_mode != MODE_THERMO:
            self._last_applied_mode = None  # reset when leaving thermo mode
            return

        current = self._get_current_temperature()
        if current is None:
            # Handset fault with no external sensor: the device cannot read room
            # temperature. Force standby rather than leaving burners at any heat level.
            if (
                not self._temp_sensor_entity_id
                and not self._dataservice.is_handset_connected
            ):
                _LOGGER.warning(
                    "Thermostatic: handset fault (F%d), no external sensor — forcing standby",
                    self._dataservice.fault_code,
                )
                if self._last_applied_mode != MODE_STANDBY:
                    self._last_applied_mode = MODE_STANDBY

                    async def _do_standby_fault() -> None:
                        await self.hass.async_add_executor_job(
                            self._dataservice.standby
                        )

                    self.hass.async_create_task(_do_standby_fault())
            else:
                _LOGGER.debug("Thermostatic: no temperature reading, skipping")
            return

        diff = self._target_temp - current
        sensor_id = self._temp_sensor_entity_id or "Mertik handset"

        if diff <= 0:
            target_mode = MODE_STANDBY  # room at/above setpoint -> standby
        elif diff < self._low_thresh:
            target_mode = MODE_LOW
        elif diff < self._high_thresh:
            target_mode = MODE_MEDIUM
        else:
            target_mode = MODE_FULL

        _LOGGER.debug(
            "Thermostatic: sensor=%s current=%.1fC setpoint=%.1fC diff=%.1fC -> %s",
            sensor_id,
            current,
            self._target_temp,
            diff,
            target_mode if target_mode else "Standby",
        )

        # Fire switched off (and not in thermostatic standby): forget the last
        # mode applied so that the next switch-on re-evaluates and ignites.
        # This must come before the "unchanged mode" check below; otherwise a
        # mode remembered from before the fire was turned off (e.g. Full Heat)
        # matches the new demand and ignition is never sent -- the fire then
        # sits at pilot only after switch-on.
        if not self._dataservice.is_on and not self._dataservice._in_standby:
            self._last_applied_mode = None
            return

        # Only act if the required mode has changed -- prevents sending the
        # same command every 10 seconds when the temperature is stable.
        if target_mode == self._last_applied_mode:
            return

        self._last_applied_mode = target_mode

        if target_mode == MODE_STANDBY:
            if self._dataservice.is_on:
                _LOGGER.info(
                    "Thermostatic: standby (%.1fC >= %.1fC setpoint)",
                    current,
                    self._target_temp,
                )

                # Do NOT call mark_optimistic_off here -- _in_standby keeps is_on
                # True so the Fireplace switch stays on while in thermostatic standby.
                async def _do_standby() -> None:
                    await self.hass.async_add_executor_job(self._dataservice.standby)

                self.hass.async_create_task(_do_standby())
        else:
            _LOGGER.info(
                "Thermostatic: applying %s (diff=%.1fC, sensor=%s)",
                target_mode,
                diff,
                sensor_id,
            )

            async def _do_apply(mode: str = target_mode) -> None:
                await self.hass.async_add_executor_job(
                    self._dataservice.apply_heating_mode, mode
                )

            self.hass.async_create_task(_do_apply())
