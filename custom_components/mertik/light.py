"""Light entity for Mertik Maxitrol fireplace.

On/off is reported by the fireplace in every status packet (bit 0x04 of the
status word), so the entity follows the light however it is switched: HA, the
handset, the MyFire app, or the fire going out. While the light is on, the
reported level byte also gives its brightness, so dimming from the MyFire app
is followed too (to the nearest of the nine levels).

On HA startup the light is shown as off, with its brightness restored from
the previous session; the next status packet corrects the state if the light
is actually on. Nothing is sent to the device at startup: commands only
follow a user action.

The fire acts as a trigger for the light in both directions:
- switched on from HA: the light is switched on once a flame is seen
  (option "light_with_fire"), as the handset does;
- extinguished (by any means): the device turns the light off itself, so the
  entity is set to off without sending anything. Brightness is kept so the
  light comes back at the same level.
The light can always be switched on and off on its own, fire on or off.

For a few seconds after HA sends a light command the reported state is
ignored, so a status packet sent just before the fireplace acts does not flip
the entity back. The light byte in the status packet is not used: it keeps a
brightness code after the light is switched off.
"""

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import MertikConfigEntry
from .const import LEVEL_BRIGHTNESS, level_from_brightness
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

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-Light"
        self._is_on = False
        self._brightness = DEFAULT_BRIGHTNESS

    async def async_added_to_hass(self) -> None:
        """Start as off and restore the last brightness.

        No command is sent to the fireplace here. If the light is actually on,
        the next status packet reports it and the entity follows.
        """
        await super().async_added_to_hass()
        self._is_on = False
        last_state = await self.async_get_last_state()
        if (
            last_state is not None
            and last_state.attributes.get(ATTR_BRIGHTNESS) is not None
        ):
            self._brightness = last_state.attributes[ATTR_BRIGHTNESS]

    async def _restore_light(self) -> None:
        """Send light on at the current brightness (light-with-fire)."""
        await self.hass.async_add_executor_job(self._dataservice.light_on)
        await self.hass.async_add_executor_job(
            self._dataservice.set_light_brightness, self._brightness
        )
        # _is_on stays True; brightness unchanged
        self.async_write_ha_state()

    async def _refresh_level(self) -> None:
        await self.hass.async_add_executor_job(
            self._dataservice.set_light_brightness, self._brightness
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Follow the light as reported by the fireplace."""
        reported = self._dataservice.light_reported
        if reported is not None:
            # Switched by HA, the handset or the app, or off with the fire.
            self._is_on = reported
        level = self._dataservice.light_level_reported
        if level is not None and level_from_brightness(self._brightness) != level:
            # Dimmed elsewhere (the MyFire app): follow, to that level's middle.
            self._brightness = LEVEL_BRIGHTNESS[level - 1]
        if self._dataservice.consume_light_refresh_due():
            # The fire has finished shutting down: send HA's brightness so the
            # fire's level and HA's are back in step (does not switch it on).
            self.hass.async_create_task(self._refresh_level())
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
        # Always send light_on: a brightness command alone does not switch
        # the light on.
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
