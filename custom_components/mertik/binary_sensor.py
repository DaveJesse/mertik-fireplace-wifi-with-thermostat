"""Binary sensor for Mertik Maxitrol fireplace: Aux burner state (read-only)."""

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MertikConfigEntry
from .coordinator import MertikDataCoordinator
from .entity import MertikEntity

PARALLEL_UPDATES = 0  # read-only coordinator-driven platform


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MertikConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Mertik binary sensor entities."""
    dataservice = entry.runtime_data
    async_add_entities([MertikAuxBurnerSensor(dataservice, entry.entry_id, entry.title)])


class MertikAuxBurnerSensor(MertikEntity, BinarySensorEntity):
    """Whether the Aux (second) burner is lit.

    The Aux burner is controlled by the heating mode (Full Heat turns it on),
    so this is an indicator only, not a switch.
    """

    _attr_translation_key = "aux"

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-AuxBurner"

    @property
    def is_on(self) -> bool:
        return bool(self._dataservice.is_aux_on)
