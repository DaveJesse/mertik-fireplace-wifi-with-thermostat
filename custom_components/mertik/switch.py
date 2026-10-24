"""Switch entity for Mertik Maxitrol fireplace (on/off)."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MertikConfigEntry
from .coordinator import MertikDataCoordinator
from .entity import MertikEntity

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MertikConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Mertik switch entities."""
    dataservice = entry.runtime_data
    async_add_entities(
        [
            MertikOnOffSwitchEntity(dataservice, entry.entry_id, entry.title),
        ]
    )


class MertikOnOffSwitchEntity(MertikEntity, SwitchEntity):
    _attr_name = None
    _attr_icon = "mdi:fireplace"

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-OnOff"

    @property
    def is_on(self) -> bool:
        return bool(self._dataservice.is_on)

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._dataservice.check_ignition_allowed()
        await self.hass.async_add_executor_job(self._dataservice.turn_on)
        self._dataservice.async_set_updated_data(None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.async_add_executor_job(self._dataservice.turn_off)
        self._dataservice.async_set_updated_data(None)

