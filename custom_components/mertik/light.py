"""Light entity for Mertik Maxitrol fireplace.

Light on/off and brightness are tracked locally (fully optimistic).
The status packet is unreliable for light state.

On HA startup the on/off state and brightness are restored from the previous
session via RestoreEntity. Nothing is sent to the device at startup: commands
only follow a user action.

The fire acts as a trigger for the light in both directions:
- switched on from HA: the light is switched on once a flame is seen
  (option "light_with_fire"), as the handset does;
- extinguished (by any means): the device turns the light off itself, so the
  entity is set to off without sending anything. Brightness is kept so the
  light comes back at the same level.
The light can always be switched on and off on its own, fire on or off.
"""

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import MertikConfigEntry
from .coordinator import MertikDataCoordinator
from .entity import MertikEntity

PARALLEL_UPDATES = 1

DEFAULT_BRIGHTNESS = 128


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MertikConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Mertik light entity."""
    dataservice = entry.runtime_data
    async_add_entities(
        [
            MertikLightEntity(dataservice, entry.entry_id, entry.title),
        ]
    )


class MertikLightEntity(MertikEntity, LightEntity, RestoreEntity):
    _attr_translation_key = "light"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}
    _attr_assumed_state = True

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-Light"
        self._is_on = False
        self._brightness = DEFAULT_BRIGHTNESS

    async def async_added_to_hass(self) -> None:
        """Restore the last known on/off state and brightness.

        The device does not report the light state, so this is an assumed
        state. No command is sent to the fireplace here.
        """
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == STATE_ON
            if last_state.attributes.get(ATTR_BRIGHTNESS) is not None:
                self._brightness = last_state.attributes[ATTR_BRIGHTNESS]

    async def _restore_light(self) -> None:
        """Send light on at the current brightness (light-with-fire)."""
        await self.hass.async_add_executor_job(self._dataservice.light_on)
        await self.hass.async_add_executor_job(
            self._dataservice.set_light_brightness, self._brightness
        )
        # _is_on stays True; brightness unchanged
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Follow the fire: light on with the fire, off when it goes out."""
        if self._dataservice.consume_fire_turned_off():
            # The device switches the light off with the fire; just reflect it.
            self._is_on = False
        if self._dataservice.consume_light_on_due():
            # Fire has just been switched on and lit: light on, as the
            # handset does (option "light_with_fire").
            self._is_on = True
            self.hass.async_create_task(self._restore_light())
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        return self._is_on

    @property
    def brightness(self) -> int:
        return self._brightness

    async def async_turn_on(self, **kwargs: Any) -> None:
        # The light state is assumed (restored, never reported by the device),
        # so always send light_on: the light may be off even if we think it is
        # on, and a brightness command alone does not switch it on.
        await self.hass.async_add_executor_job(self._dataservice.light_on)
        if ATTR_BRIGHTNESS in kwargs:
            self._brightness = kwargs[ATTR_BRIGHTNESS]
            await self.hass.async_add_executor_job(
                self._dataservice.set_light_brightness, self._brightness
            )
        self._is_on = True
        self.async_write_ha_state()
        self._dataservice.async_set_updated_data(None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._is_on = False
        self.async_write_ha_state()
        await self.hass.async_add_executor_job(self._dataservice.light_off)
        self._dataservice.async_set_updated_data(None)
