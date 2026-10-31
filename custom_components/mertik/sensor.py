"""Sensor entities for Mertik Maxitrol fireplace (temperature, fault code, flame height)."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import MertikConfigEntry
from .const import FLAME_LEVELS
from .coordinator import MertikDataCoordinator
from .entity import MertikEntity

PARALLEL_UPDATES = 0  # read-only coordinator-driven platform

# Maps the raw fault code integer to a translation key.
# Keys must match the state translation keys in strings.json.
FAULT_CODE_MAP: dict[int, str] = {
    2: "f02",
    3: "f03",
    4: "f04",
    6: "f06",
    10: "f10",
    12: "f12",
    13: "f13",
    14: "f14",
    15: "f15",
    16: "f16",
    17: "f17",
    19: "f19",
    26: "f26",
    28: "f28",
    31: "f31",
    41: "f41",
    43: "f43",
    44: "f44",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: MertikConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Mertik sensor entities."""
    dataservice = entry.runtime_data
    async_add_entities(
        [
            MertikAmbientTemperatureSensorEntity(
                dataservice, entry.entry_id, entry.title
            ),
            MertikFaultCodeSensorEntity(
                dataservice, entry.entry_id, entry.title
            ),
            MertikFlameHeightSensorEntity(
                dataservice, entry.entry_id, entry.title
            ),
        ]
    )


class MertikAmbientTemperatureSensorEntity(MertikEntity, SensorEntity):
    _attr_translation_key = "handset_temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-AmbientTemperature"

    @property
    def native_value(self) -> float | None:
        return self._dataservice.ambient_temperature


class MertikFaultCodeSensorEntity(MertikEntity, SensorEntity):
    _attr_translation_key = "fault_code"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # "off" (shown as "No fault") so that dashboards treat it as inactive.
    _attr_options = ["off"] + list(FAULT_CODE_MAP.values())

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-FaultCode"

    @property
    def available(self) -> bool:
        """Stay available during F43 so the fault itself can be shown."""
        return self.coordinator.last_update_success

    @property
    def native_value(self) -> str:
        return FAULT_CODE_MAP.get(self._dataservice.fault_code, "off")


class MertikFlameHeightSensorEntity(MertikEntity, SensorEntity):
    """Flame height in plain words: Off, Pilot, Low or High.

    Read from the burner output the fireplace reports (steps 1-12, see
    PROTOCOL.md): steps 1-6 show as Low, 7-12 as High. Read-only: the
    heating mode sets it.
    """

    _attr_translation_key = "flame_height"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = FLAME_LEVELS

    def __init__(
        self, dataservice: MertikDataCoordinator, entry_id: str, device_name: str
    ) -> None:
        super().__init__(dataservice, entry_id, device_name)
        self._attr_unique_id = entry_id + "-FlameHeightLevel"

    @property
    def native_value(self) -> str:
        return self._dataservice.flame_level
