from datetime import datetime, timedelta
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import homeassistant.helpers.issue_registry as ir
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    FLAME_MAX,
    FLAME_MIN,
    MODE_FULL,
    MODE_LOW,
    MODE_MEDIUM,
    MODE_STANDBY,
    MODE_THERMO,
)
from .mertik import Mertik

_LOGGER = logging.getLogger(__name__)
OPTIMISTIC_ON_SECONDS = 20
OPTIMISTIC_OFF_SECONDS = 20
# A fire that has been switched off only counts as relit at the fireplace once
# the device has reported a flame continuously for this long. A single status
# packet with a flipped flame bit has been seen (0B -> 8B -> 0B within 50 ms).
RELIGHT_CONFIRM_SECONDS = 15
# Flame step and aux setting each heating mode puts the fire at.
MODE_TARGETS: dict[str, tuple[int, bool]] = {
    MODE_FULL: (FLAME_MAX, True),
    MODE_MEDIUM: (FLAME_MAX, False),
    MODE_LOW: (FLAME_MIN, False),
}
# After a heating mode is sent, check this much later that the fireplace
# reports it, and send it once more if not (seen 30 Oct 22:23: a standby
# acknowledged, but the valve stayed at Low).
MODE_CHECK_SECONDS = 25
# Sends of one heating mode in all (first + resends) before raising an alert.
# Seen 30 Oct: two "down to pilot" commands acknowledged but not acted on,
# each while the valve still read 82 after a Low command.
MODE_SEND_ATTEMPTS = 3
# After a shutdown, the fire must be reported fully out this long before it
# may be lit again.
OUT_BEFORE_IGNITION_SECONDS = 15
# Pending "mode" after the fire is lit at the fireplace in a manual mode:
# once settled, show the mode the fire is at; nothing is sent.
MODE_ADOPT = "adopt"


class MertikDataCoordinator(DataUpdateCoordinator[None]):
    def __init__(
        self, hass: HomeAssistant, mertik: Mertik, entry: ConfigEntry | None = None
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Mertik",
            update_interval=timedelta(seconds=10),
        )
        self.mertik = mertik
        self._optimistic_on_until: datetime | None = None
        self._optimistic_off_until: datetime | None = None
        self._prev_is_on: bool = False
        # Light-with-fire: armed when the user switches the fire on, fired
        # (light_on_due) on the first update where a flame is seen.
        self._light_with_next_flame: bool = False
        # Switch-off latch. Set when the fire is switched off (by HA, or by the
        # handset/app as reported by the device's "shutting down" flag); while
        # set the fire is OFF regardless of what the flame byte shows -- the
        # flame takes ~30 s to die and must not be mistaken for a fire that is
        # on. Cleared only by an HA switch-on, or by the device reporting a new
        # flame after the fire has been seen fully out (lit from the handset
        # or the MyFire app).
        # Starts as "off": the first status decides (see _update_switch_off_latch).
        self._switched_off: bool = True
        self._seen_out_since_off: bool = True
        self._first_status_seen: bool = False
        # time.monotonic() when a new flame was first reported while switched
        # off; cleared if any later update reports the fire out.
        self._relight_since: float | None = None
        # Set when HA switches the fire off: time and status count at that
        # moment, so switch-on can be refused until the device has reported.
        self._off_at: float | None = None
        self._status_count_at_off: int = 0
        self.light_on_due: bool = False
        self.fire_just_turned_off: bool = (
            False  # set True for one cycle when fire turns off
        )
        self._in_standby: bool = False  # True when thermostatic standby is active
        # (mode, check due at, times sent) -- see _mode_check_due.
        self._mode_check: tuple[str, datetime, int] | None = None
        self._retrying = False
        # Shutdown (mertik.last_shutdown_at) for which the light level has
        # been re-sent, and whether a re-send is waiting for the light entity.
        self._light_refreshed_for: float | None = None
        self.light_refresh_due = False
        # Set when the fire is switched on while already lit in Thermostatic:
        # the thermostat forgets its last mode and re-sends what is needed.
        self.thermo_recheck = False
        self._pending_mode: str | None = None  # mode to apply once ignition completes
        self._pending_mode_since: datetime | None = (
            None  # when _pending_mode was set (for timeout)
        )
        self._heating_mode: str | None = (
            None  # current mode set by the Heating Mode select entity
        )
        self._was_igniting: bool = False  # tracks igniting falling edge
        self._flame_on_since: datetime | None = (
            None  # timestamp when flame first lit after ignite
        )
        self._settle_seconds: int = 35  # seconds to wait after flame_on before aux_off
        self._ignition_timeout_seconds: int = (
            120  # abandon pending mode after this many seconds
        )
        self._is_light_on: bool = False
        self._light_brightness: int = 0

    # ---- On/off state ----------------------------------------------------
    # Use flame_on (flame byte > threshold) as the primary "is fire running"
    # indicator. on_flag ("FF") is only set when ignite_fireplace() is used,
    # not when set_flame_height() starts the fire.

    @property
    def is_on(self) -> bool:
        """Fire is running -- based on flame byte, standby state, or optimistic timer.

        _in_standby (pilot lit, thermostatic armed) takes priority over the
        optimistic-off timer so the Fireplace switch stays on while in standby.
        """
        if self._switched_off:
            return False
        if self._in_standby:
            return True
        now = dt_util.utcnow()
        if self._optimistic_off_until and now < self._optimistic_off_until:
            return False
        if self.mertik.is_flame_on or self.mertik.is_igniting:
            return True
        if self._optimistic_on_until and now < self._optimistic_on_until:
            return True
        return False

    def mark_optimistic_on(self) -> None:
        self._optimistic_off_until = None
        self._optimistic_on_until = dt_util.utcnow() + timedelta(
            seconds=OPTIMISTIC_ON_SECONDS
        )

    def mark_optimistic_off(self) -> None:
        self._optimistic_on_until = None
        self._optimistic_off_until = dt_util.utcnow() + timedelta(
            seconds=OPTIMISTIC_OFF_SECONDS
        )

    @property
    def heating_mode(self) -> str | None:
        return self._heating_mode

    def set_heating_mode(self, mode: str) -> None:
        self._heating_mode = mode

    def turn_on(self) -> None:
        """Switch the fire on in the current heating mode (blocking I/O).

        Thermostatic: light the pilot and let the thermostat decide whether to
        ignite the burner. Any other mode: ignite and apply that mode.
        """
        from .const import CONF_LIGHT_WITH_FIRE, DEFAULT_LIGHT_WITH_FIRE, MODE_THERMO

        if self.ignition_blocked():
            # Safety net; switch-on requests are normally refused earlier by
            # async_check_ignition_allowed() with a message to the user.
            _LOGGER.warning("Switch-on ignored: the fire is still shutting down")
            return
        self._switched_off = False
        self._light_with_next_flame = bool(
            self.config_entry.options.get(
                CONF_LIGHT_WITH_FIRE,
                self.config_entry.data.get(
                    CONF_LIGHT_WITH_FIRE, DEFAULT_LIGHT_WITH_FIRE
                ),
            )
        )
        mode = self._heating_mode
        if self.is_on and self.mertik.is_pilot_on and not self.mertik.is_shutting_down:
            # Already lit (e.g. an automation switching on a running fire):
            # do not ignite again. Re-apply the mode instead; for Thermostatic
            # the thermostat re-checks and sends what is needed.
            _LOGGER.debug("Switch-on while the fire is lit: no ignition")
            self._light_with_next_flame = False
            if mode == MODE_THERMO:
                self.thermo_recheck = True
            elif mode:
                self.apply_heating_mode(mode)
            return
        if mode == MODE_THERMO:
            self.arm_thermostatic()
        else:
            # mark_optimistic_on first so apply_heating_mode's is_on guard passes.
            self.mark_optimistic_on()
            self.apply_heating_mode(mode)

    def ignition_blocked(self) -> bool:
        """True while the fire is still going out after being switched off.

        After switching off, the receiver runs a shutdown sequence (about
        15-30 s) during which the MyFire app reports "Ignition is not allowed".
        Commanding it then was seen to make the WiFi box lose the receiver
        (F43). Blocked while any of these hold:
        - the device reports "shutting down" (status bit 7);
        - the fire is off but a flame is still burning (flame byte top bit),
          i.e. it is still going out;
        - HA switched it off less than a minute ago and no status packet has
          arrived since (the device may not have reported the shutdown yet).
        """
        mertik = self.mertik
        if mertik.is_shutting_down:
            return True
        if not self.is_on and mertik.is_pilot_on:
            return True
        # Just after a shutdown, the receiver ignores an ignite for a few
        # seconds after it first reports the fire out (31 Oct 12:12:27: ignite
        # acknowledged, nothing happened; 35 s later it lit).
        out_for = mertik.out_after_shutdown_seconds
        if out_for is not None and out_for < OUT_BEFORE_IGNITION_SECONDS:
            return True
        return (
            self._off_at is not None
            and time.monotonic() - self._off_at < 60
            and mertik.status_count == self._status_count_at_off
        )

    async def async_check_ignition_allowed(self) -> None:
        """Get a fresh status, then refuse a switch-on during shutdown."""
        await self.hass.async_add_executor_job(self.mertik.refresh_status)
        if self.ignition_blocked():
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ignition_not_allowed",
            )

    def turn_off(self) -> None:
        """Switch the fire fully off, pilot included (blocking I/O)."""
        self._light_with_next_flame = False
        self._switched_off = True
        self._seen_out_since_off = False
        self._relight_since = None
        self._status_count_at_off = self.mertik.status_count
        self._off_at = time.monotonic()
        self.guard_flame_off()
        self.mark_optimistic_off()

    def select_heating_mode(self, mode: str) -> None:
        """Choose a heating mode and apply it to the fire (blocking I/O).

        Standby: pilot only. Thermostatic: nothing is sent here; the thermostat
        drives the fire on its next update. Other modes are applied directly
        (if the fire is off they are remembered for the next switch-on).
        """
        from .const import MODE_STANDBY, MODE_THERMO

        self._heating_mode = mode
        if mode == MODE_STANDBY:
            self.standby()
            self.mark_optimistic_off()
        elif mode != MODE_THERMO:
            self.apply_heating_mode(mode)

    def ignite_fireplace(self) -> None:
        self.mertik.ignite_fireplace()

    def guard_flame_off(self) -> None:
        self._optimistic_on_until = None
        self._optimistic_off_until = None
        self.mertik.guard_flame_off()
        self._in_standby = False
        self._pending_mode = None  # discard any in-flight ignition/mode sequence
        self._pending_mode_since = None
        self._flame_on_since = None
        # Signal light entity that fire turned off (device kills light too)
        self.fire_just_turned_off = True
        self._prev_is_on = False

    def standby(self) -> None:
        """Pilot flame only -- main burners off but ignition source stays lit.
        Used by thermostatic Off so re-ignition is fast when heat is needed.
        Does NOT set fire_just_turned_off because the device keeps the light
        on in standby mode (only guard_flame_off kills the light).
        """
        # CMD_STANDBY turns a burning fire down to pilot. It is never used to
        # light anything (on a fully-off fire it does not light the pilot), so
        # it is only sent when the fire is on AND the device reports a flame.
        if not self.is_on:
            _LOGGER.debug("standby() skipped: fire is not on")
            return
        if not self.mertik.is_pilot_on:
            _LOGGER.debug("standby() skipped: no flame to turn down")
            return
        self._optimistic_on_until = None
        self._optimistic_off_until = None
        self._in_standby = True
        self.mertik.standBy()
        self._expect_mode(MODE_STANDBY)

    # ---- Check that a heating mode took effect -------------------------

    def _expect_mode(self, mode: str) -> None:
        """Check, MODE_CHECK_SECONDS from now, that the fire reached mode."""
        sends = self._mode_check[2] + 1 if (self._mode_check and self._retrying) else 1
        self._mode_check = (
            mode,
            dt_util.utcnow() + timedelta(seconds=MODE_CHECK_SECONDS),
            sends,
        )

    def _mode_reached(self, mode: str) -> bool:
        m = self.mertik
        if mode == MODE_STANDBY:
            return not m.is_burner_lit
        want_step, want_aux = MODE_TARGETS[mode]
        return (
            m.is_burner_lit
            and m.get_flame_height() == want_step
            and m.aux_setting == want_aux
        )

    def _set_mode_issue(self, mode: str | None) -> None:
        """Raise (mode) or clear (None) the "fire not following" alert."""
        issue_id = f"mode_not_followed_{self.config_entry.entry_id}"
        if mode is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="mode_not_followed",
            translation_placeholders={"name": self.config_entry.title, "mode": mode},
        )

    def _mode_check_due(self) -> str | None:
        """Mode to send again because the fire did not reach it, or None.

        Runs in the event loop, no I/O. Sends a mode up to MODE_SEND_ATTEMPTS
        times in all; if the fire still does not follow, raises an alert.
        """
        if self._mode_check is None:
            return None
        mode, due, sends = self._mode_check
        if dt_util.utcnow() < due:
            return None
        if self._switched_off or not self.mertik.is_pilot_on or self._pending_mode:
            self._mode_check = None
            self._set_mode_issue(None)
            return None
        if not self.mertik.state_settled:
            return None
        if self._mode_reached(mode):
            self._mode_check = None
            self._set_mode_issue(None)
            return None
        if sends >= MODE_SEND_ATTEMPTS:
            _LOGGER.warning(
                "%s sent %d times but the fireplace reports output %s, aux %s",
                mode,
                sends,
                self.mertik.burner_output,
                self.mertik.aux_setting,
            )
            self._mode_check = None
            self._set_mode_issue(mode)
            return None
        _LOGGER.info(
            "%s not reached (output %s, aux %s): sending again",
            mode,
            self.mertik.burner_output,
            self.mertik.aux_setting,
        )
        return mode

    def resend_mode(self, mode: str) -> None:
        """Send a heating mode again (from the executor)."""
        self._retrying = True
        try:
            if mode == MODE_STANDBY:
                self.standby()
            else:
                self.apply_heating_mode(mode)
        finally:
            self._retrying = False

    def arm_thermostatic(self) -> None:
        """Switch on in Thermostatic mode (only from an explicit switch-on).

        Ignites the fire. Once the burner has lit and settled, the thermostat
        chooses the mode: a heat mode if the room needs heat, otherwise down
        to pilot (standby). CMD_STANDBY alone does not light a cold fire.
        """
        self._in_standby = False
        self.mark_optimistic_on()
        self._pending_mode = MODE_THERMO
        self._pending_mode_since = dt_util.utcnow()
        self.mertik.ignite_fireplace()

    @property
    def is_aux_on(self) -> bool:
        # In thermostatic standby only the pilot burns. The fireplace keeps
        # its aux setting (bit 0x08) on pilot, but the rear burner is not lit.
        if self._in_standby:
            return False
        return self.mertik.is_aux_on  # gated on the burner output in mertik.py

    def aux_on(self) -> None:
        self.mertik.aux_on()

    def aux_off(self) -> None:
        self.mertik.aux_off()

    def get_flame_height(self) -> int:
        return self.mertik.get_flame_height()

    def set_flame_height(self, flame_height: int) -> None:
        self.mertik.set_flame_height(flame_height)

    @property
    def fault_code(self) -> int:
        return self.mertik.fault_code

    def consume_fire_turned_off(self) -> bool:
        """Return True once after the fire has gone out (light went off too)."""
        off, self.fire_just_turned_off = self.fire_just_turned_off, False
        return off

    def consume_light_refresh_due(self) -> bool:
        """True once after each completed shutdown (see _async_update_data)."""
        due, self.light_refresh_due = self.light_refresh_due, False
        return due

    def consume_light_on_due(self) -> bool:
        """Return True once when the light should come on with the fire."""
        due, self.light_on_due = self.light_on_due, False
        return due

    @property
    def is_pilot_on(self) -> bool:
        """A flame (pilot or burner) is burning, from the status packet."""
        return self.mertik.is_pilot_on

    @property
    def not_responding(self) -> bool:
        """True while the WiFi box is not replying at all."""
        return self.mertik.not_responding

    def consume_light_event(self) -> bool | None:
        """Light switched on (True) or off (False) since last asked, or None."""
        return self.mertik.take_light_event()

    @property
    def flame_level(self) -> str:
        """Flame in plain words: off, pilot, low (steps 1-6) or high (7-12)."""
        m = self.mertik
        if not m.is_flame_on:
            return "off"
        step = m.get_flame_height()
        if step == 0:
            return "pilot"
        return "low" if step <= 6 else "high"

    @property
    def light_level_reported(self) -> int | None:
        """Light level 1-9 as reported while the light is on, or None."""
        return self.mertik.light_level_reported

    @property
    def light_reported(self) -> bool | None:
        """Light on/off as reported by the fireplace (None = not known now)."""
        return self.mertik.light_reported

    @property
    def receiver_lost(self) -> bool:
        """True while the WiFi box reports no link to the fireplace (F43)."""
        return self.mertik.receiver_lost

    @property
    def is_handset_connected(self) -> bool:
        return self.mertik.is_handset_connected

    @property
    def ambient_temperature(self) -> float | None:
        return self.mertik.ambient_temperature

    @property
    def is_light_on(self) -> bool:
        return self._is_light_on

    def light_on(self) -> None:
        self._is_light_on = True
        self.mertik.light_on()

    def light_off(self) -> None:
        self._is_light_on = False
        self.mertik.light_off()

    def set_light_brightness(self, brightness: int) -> None:
        self._light_brightness = brightness
        self.mertik.set_light_brightness(brightness)

    @property
    def light_brightness(self) -> int:
        return self._light_brightness

    def apply_heating_mode(self, mode: str | None) -> None:
        """Apply a named heating mode to the physical fireplace.

        Standby is handled first -- it must never trigger ignition regardless
        of the current fire state.

        For heat modes, two paths:
        1. Fire not physically on (is_flame_on=False, is_igniting=False):
           a. User switched off (is_on=False, _in_standby=False): block, do nothing.
           b. Optimistically on (user pressed On) or in thermostatic standby:
              ignite and defer via _pending_mode. In standby the pilot may have
              gone out, so always ignite to be safe -- sending flame-height commands
              to an extinguished device clears _in_standby and makes is_on go False
              (the 'turns itself off' symptom).
        2. Fire physically on (is_flame_on or is_igniting): apply mode directly.
        """
        from .const import MODE_STANDBY, MODE_FULL, MODE_MEDIUM, MODE_LOW

        # Standby never ignites -- handle it unconditionally before ignition logic.
        if mode == MODE_STANDBY:
            self._pending_mode = None
            self._pending_mode_since = None
            self.standby()
            return

        physically_on = self.mertik.is_flame_on or self.mertik.is_igniting

        if not physically_on:
            # Block if user explicitly switched off -- not optimistically on or standby.
            if not self.is_on and not self._in_standby:
                _LOGGER.debug(
                    "apply_heating_mode: fire is off by user, not igniting for %s", mode
                )
                return
            # Either optimistically on (user pressed On) or in thermostatic standby.
            # mark_optimistic_on clears any pending opt-off timer and bridges the gap
            # between ignition and the next poll confirming is_flame_on=True.
            self._pending_mode = mode
            self._pending_mode_since = dt_util.utcnow()
            self._in_standby = False
            self.mark_optimistic_on()
            self.mertik.ignite_fireplace()
            return

        # Fire is physically on (flame_on or igniting): apply mode directly.
        self._in_standby = False
        self._pending_mode = None
        self._pending_mode_since = None
        target = MODE_TARGETS.get(mode)
        if target is None:
            return
        want_step, want_aux = target
        # Skip a command that would change nothing (each one beeps): only
        # when the fireplace's latest status, taken after the last command,
        # shows the burner lit at that step / with that aux setting.
        known = self.mertik.state_settled and self.mertik.is_burner_lit
        send_step = not (known and self.mertik.get_flame_height() == want_step)
        send_aux = not (known and self.mertik.aux_setting == want_aux)
        if not (send_step or send_aux):
            _LOGGER.debug("%s: fire already there, nothing sent", mode)
        if mode == MODE_FULL:
            # Aux first: an aux-on sent 2 s after a flame-up was answered
            # "no receiver" (31 Oct 11:44, 11:58, 12:53) while the valve moved.
            if send_aux:
                self.mertik.aux_on()
            if send_step:
                self.mertik.set_flame_height(want_step)
        else:
            if send_aux:
                self.mertik.aux_off()
            if send_step:
                self.mertik.set_flame_height(want_step)
        self._expect_mode(mode)

    @property
    def has_pending_mode(self) -> bool:
        """True while a heating mode is waiting for ignition to finish."""
        return self._pending_mode is not None

    def _pending_mode_due(self) -> str | None:
        """Advance the ignition/settle state machine; no device I/O.

        Called once per poll from _async_update_data. Returns the deferred
        heating mode when it is time to apply it (the caller sends it from
        the executor), otherwise None.

        After the burner lights, we wait _settle_seconds before sending
        aux_off / set_flame_height. The device firmware ignores these
        commands if sent too soon after ignition (ACK is received but
        the physical state does not change). 35 seconds is conservative
        but reliable based on observed device behaviour.
        """
        if not self._pending_mode:
            return None
        # Abandon pending mode if ignition has taken too long (e.g. failed ignition
        # leaves is_igniting stuck True indefinitely via flame_byte bit 4).
        if self._pending_mode_since is not None:
            elapsed = (dt_util.utcnow() - self._pending_mode_since).total_seconds()
            if elapsed > self._ignition_timeout_seconds:
                _LOGGER.warning(
                    "Ignition timeout after %.0fs — abandoning pending mode %s",
                    elapsed,
                    self._pending_mode,
                )
                self._pending_mode = None
                self._pending_mode_since = None
                self._flame_on_since = None
                return None
        # Still igniting -- wait
        if self.mertik.is_igniting:
            _LOGGER.debug(
                "Waiting for ignition to complete before applying %s",
                self._pending_mode,
            )
            self._flame_on_since = None
            return None
        # Igniting bit just dropped False -- flame_on may lag by one poll cycle.
        if not self.mertik.is_flame_on:
            _LOGGER.debug("Igniting cleared but flame_on not yet set -- waiting")
            return None
        # Burner is lit -- start the settle timer if not already started
        if self._flame_on_since is None:
            self._flame_on_since = dt_util.utcnow()
            _LOGGER.info(
                "Burner lit, waiting %ds before applying %s",
                self._settle_seconds,
                self._pending_mode,
            )
            return None
        # Check if enough time has passed since the burner lit
        elapsed = (dt_util.utcnow() - self._flame_on_since).total_seconds()
        if elapsed < self._settle_seconds:
            _LOGGER.debug(
                "Settling: %.0fs / %ds before applying %s",
                elapsed,
                self._settle_seconds,
                self._pending_mode,
            )
            return None
        # Settle period complete -- apply the deferred mode
        _LOGGER.info(
            "Settled (%.0fs), applying deferred mode %s", elapsed, self._pending_mode
        )
        mode = self._pending_mode
        self._pending_mode = None
        self._pending_mode_since = None
        self._flame_on_since = None
        return mode

    def _update_switch_off_latch(self) -> None:
        """Keep the switch-off latch in step with what the device reports."""
        mertik = self.mertik
        if mertik.is_shutting_down and not self._switched_off:
            # Switched off at the fireplace (handset or MyFire app).
            _LOGGER.info("Fire switched off at the fireplace")
            self._switched_off = True
            self._seen_out_since_off = False
            self._relight_since = None
            self._in_standby = False
            self._pending_mode = None
            self._pending_mode_since = None
        flame = mertik.is_pilot_on or mertik.is_igniting
        if not self._first_status_seen:
            # Startup: on only if the very first status shows a flame.
            self._first_status_seen = True
            if flame and not mertik.is_shutting_down:
                self._switched_off = False
            return
        if not self._switched_off:
            return
        if not flame and not mertik.is_shutting_down:
            self._seen_out_since_off = True
            self._relight_since = None
        elif flame and self._seen_out_since_off and not mertik.is_shutting_down:
            # Fully out, now reporting a flame: lit from the handset or the
            # app -- but only once it has been reported for long enough, so a
            # single glitched status packet cannot count as a relight.
            now = time.monotonic()
            if self._relight_since is None:
                self._relight_since = now
                _LOGGER.debug("Flame reported on a switched-off fire; confirming")
            elif now - self._relight_since >= RELIGHT_CONFIRM_SECONDS:
                _LOGGER.info("Fire lit at the fireplace (handset or MyFire app)")
                self._switched_off = False
                self._relight_since = None
                self._adopt_fireplace_ignition()
        else:
            self._relight_since = None

    def _adopt_fireplace_ignition(self) -> None:
        """Reflect a fire lit with the handset or the MyFire app.

        Nothing is sent while it lights and settles (commands then are
        ignored or answered "no receiver": 31 Oct 12:34:42). Once settled, in
        Thermostatic the thermostat takes over; in a manual mode the Heating
        Mode shows what the fire is doing (the app lights at its last
        setting, the handset at Full). Nothing is sent to the fire.
        """
        from .mertik import IGNITION_FLAME_HEIGHT

        self.mertik.flameHeight = IGNITION_FLAME_HEIGHT
        self._pending_mode = MODE_THERMO if self._heating_mode == MODE_THERMO else MODE_ADOPT
        self._pending_mode_since = dt_util.utcnow()
        # The flame has already burned RELIGHT_CONFIRM_SECONDS.
        self._flame_on_since = dt_util.utcnow() - timedelta(
            seconds=RELIGHT_CONFIRM_SECONDS
        )

    def _mode_from_report(self) -> str:
        """Heating mode matching what the fireplace reports."""
        m = self.mertik
        if not m.is_burner_lit:
            return MODE_STANDBY
        if m.aux_setting:
            return MODE_FULL
        return MODE_MEDIUM if m.get_flame_height() >= 7 else MODE_LOW

    async def _async_update_data(self) -> None:
        try:
            await self.hass.async_add_executor_job(self.mertik.refresh_status)
            self._update_switch_off_latch()
            # Apply a heating mode deferred until the burner has lit and
            # settled. Sent from the executor: it is blocking socket I/O.
            # MODE_THERMO means "lit for Thermostatic": nothing to send, the
            # thermostat chooses the mode on this update.
            if (mode := self._pending_mode_due()) == MODE_ADOPT:
                self._heating_mode = self._mode_from_report()
                _LOGGER.info("Lit at the fireplace: showing %s", self._heating_mode)
            elif mode is not None and mode != MODE_THERMO:
                await self.hass.async_add_executor_job(self.apply_heating_mode, mode)
            elif (mode := self._mode_check_due()) is not None:
                await self.hass.async_add_executor_job(self.resend_mode, mode)
            ir.async_delete_issue(self.hass, DOMAIN, "cannot_connect")
            issue_id = f"receiver_lost_{self.config_entry.entry_id}"
            if self.mertik.receiver_lost:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    issue_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="receiver_lost",
                    translation_placeholders={"name": self.config_entry.title},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            silent_id = f"not_responding_{self.config_entry.entry_id}"
            if self.mertik.not_responding:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    silent_id,
                    is_fixable=False,
                    severity=ir.IssueSeverity.ERROR,
                    translation_key="not_responding",
                    translation_placeholders={"name": self.config_entry.title},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, silent_id)
            # After a shutdown has completed, re-send the light level so the
            # fire's stored brightness matches HA's (it does not switch the
            # light on).
            shutdown_at = self.mertik.last_shutdown_at
            out_for = self.mertik.out_after_shutdown_seconds
            if (
                shutdown_at is not None
                and shutdown_at != self._light_refreshed_for
                and out_for is not None
                and out_for >= OUT_BEFORE_IGNITION_SECONDS
            ):
                self._light_refreshed_for = shutdown_at
                self.light_refresh_due = True
            # Light-with-fire: once a flame (pilot or burner) is seen after the
            # user switched the fire on, ask the light entity to switch on.
            if self._light_with_next_flame and (
                self.mertik.is_flame_on or self.mertik.is_pilot_on
            ):
                self._light_with_next_flame = False
                self.light_on_due = True
            # Detect when fire turns off so light entity can reset its state.
            # The device physically turns the light off when fire is extinguished.
            current_on = self.is_on
            self.fire_just_turned_off = self._prev_is_on and not current_on
            self._prev_is_on = current_on
        except Exception as err:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "cannot_connect",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="cannot_connect",
                translation_placeholders={"host": self.mertik.ip},
            )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
            ) from err
