"""Climate entity -- thermostat setpoint display and thermostatic control logic."""

import logging
from datetime import datetime
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
from homeassistant.util import dt as dt_util

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
    CONF_HYSTERESIS,
    DEFAULT_HYSTERESIS,
    CONF_MIN_REIGNITION,
    DEFAULT_MIN_REIGNITION,
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


# Heating modes in order of increasing heat output.
_HEAT_ORDER = [MODE_STANDBY, MODE_LOW, MODE_MEDIUM, MODE_FULL]


def choose_thermostatic_mode(
    gap: float,
    low: float,
    full: float,
    hysteresis: float,
    current: str | None,
) -> str:
    """Pick the heating mode for gap = setpoint - room temperature.

    With hysteresis h the bands are:

        gap <= -h                  Standby (pilot only)
        h <= gap <= low - h        Low Heat
        low + h <= gap <= full - h Medium Heat
        gap >= full + h            Full Heat

    Inside the 2h-wide dead zone around each boundary (0, low, full) the
    current mode is kept if it is one of the two modes either side of that
    boundary; a mode further away is moved to the nearer of the two. With no
    current mode (just switched on) the plain boundary decides.
    """
    h = hysteresis
    if gap <= -h:
        return MODE_STANDBY
    if h <= gap <= low - h:
        return MODE_LOW
    if low + h <= gap <= full - h:
        return MODE_MEDIUM
    if gap >= full + h:
        return MODE_FULL

    # Dead zone: find the boundary we are near and the modes either side.
    if gap < h:
        boundary, below, above = 0.0, MODE_STANDBY, MODE_LOW
    elif gap < low + h:
        boundary, below, above = low, MODE_LOW, MODE_MEDIUM
    else:
        boundary, below, above = full, MODE_MEDIUM, MODE_FULL

    if current in (below, above):
        return current
    if current is None:
        return above if gap > boundary else below
    # Current mode is further away: move to the nearer neighbour.
    if _HEAT_ORDER.index(current) > _HEAT_ORDER.index(above):
        return above
    return below


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
        # Mode the thermostat last put the fire in (kept across setpoint
        # changes so hysteresis knows where it is; cleared when the fire is
        # switched off or Thermostatic is left).
        self._thermo_mode: str | None = None
        # When the thermostat last dropped the burner to pilot; re-ignition is
        # held off until the minimum re-ignition time has passed.
        self._standby_since: datetime | None = None
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
    def _hysteresis(self) -> float:
        return float(self._get_option(CONF_HYSTERESIS, DEFAULT_HYSTERESIS))

    @property
    def _min_reignition_seconds(self) -> float:
        return 60.0 * float(
            self._get_option(CONF_MIN_REIGNITION, DEFAULT_MIN_REIGNITION)
        )

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
        if self._dataservice._in_standby:
            # Thermostatic standby: pilot only. The device's flame byte does not
            # distinguish pilot from burner, so use our own standby state.
            return HVACAction.IDLE
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
        # Re-check now against the new setpoint. Do not clear
        # _last_applied_mode: a command is only sent if the mode actually
        # changes (re-sending the same mode makes the fire beep).
        self._run_thermostatic_logic()
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
        # While a heating mode is waiting for ignition to finish, the
        # coordinator applies it (off the event loop) once the burner has
        # settled; skip the temperature comparison until then -- flame height
        # commands are ignored during ignition.
        if self._dataservice.has_pending_mode:
            return

        if self._dataservice.heating_mode != MODE_THERMO:
            self._last_applied_mode = None  # reset when leaving thermo mode
            self._thermo_mode = None
            self._standby_since = None
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
                    self._thermo_mode = MODE_STANDBY

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

        target_mode = choose_thermostatic_mode(
            diff,
            self._low_thresh,
            self._high_thresh,
            self._hysteresis,
            self._thermo_mode,
        )

        # Minimum time between re-ignitions: once the thermostat has dropped
        # the burner to pilot, a full (spark) ignition is held off until the
        # time has passed. While the pilot is still lit the burner relights
        # from it with no ignition cycle, so there is nothing to protect and
        # the burner relights at once.
        if (
            target_mode != MODE_STANDBY
            and self._thermo_mode == MODE_STANDBY
            and self._standby_since is not None
            and not self._dataservice.is_pilot_on
        ):
            waited = (dt_util.utcnow() - self._standby_since).total_seconds()
            if waited < self._min_reignition_seconds:
                _LOGGER.debug(
                    "Thermostatic: %s wanted, re-ignition held for %.0fs more",
                    target_mode,
                    self._min_reignition_seconds - waited,
                )
                target_mode = MODE_STANDBY

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
            self._thermo_mode = None
            self._standby_since = None
            return

        # Only act if the required mode has changed -- prevents sending the
        # same command every 10 seconds when the temperature is stable.
        if target_mode == self._last_applied_mode:
            return

        self._last_applied_mode = target_mode
        # Start the re-ignition timer only when a lit burner drops to pilot,
        # not when the fire is first armed in an already-warm room.
        if target_mode == MODE_STANDBY and self._thermo_mode in (
            MODE_LOW,
            MODE_MEDIUM,
            MODE_FULL,
        ):
            self._standby_since = dt_util.utcnow()
        self._thermo_mode = target_mode

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
