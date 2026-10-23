"""Mertik Maxitrol fireplace integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .const import CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR, DOMAIN
from .coordinator import MertikDataCoordinator
from .mertik import Mertik

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PLATFORMS = ["climate", "light", "number", "select", "sensor", "switch"]

type MertikConfigEntry = ConfigEntry[MertikDataCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: MertikConfigEntry) -> bool:
    """Set up the Mertik component."""
    try:
        mertik = await Mertik.async_connect(entry.data[CONF_HOST])
    except Exception as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
        ) from err

    coordinator = MertikDataCoordinator(hass, mertik, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _sync_handset_temperature_visibility(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    return True


async def _async_options_updated(hass: HomeAssistant, entry: MertikConfigEntry) -> None:
    """Options changed: re-check which temperature sensor is in use."""
    _sync_handset_temperature_visibility(hass, entry)


def _sync_handset_temperature_visibility(
    hass: HomeAssistant, entry: MertikConfigEntry
) -> None:
    """Hide the Handset Temperature sensor when another sensor is selected.

    Uses the entity registry's hidden flag, so the entity is kept (and can be
    shown again) but drops off dashboards. A hide or unhide done by the user
    is left alone.
    """
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, entry.entry_id + "-AmbientTemperature"
    )
    if entity_id is None:
        return
    reg_entry = registry.async_get(entity_id)
    if reg_entry is None or reg_entry.hidden_by == er.RegistryEntryHider.USER:
        return
    external = entry.options.get(
        CONF_TEMP_SENSOR, entry.data.get(CONF_TEMP_SENSOR, DEFAULT_TEMP_SENSOR)
    )
    hidden_by = er.RegistryEntryHider.INTEGRATION if external else None
    if reg_entry.hidden_by != hidden_by:
        registry.async_update_entity(entity_id, hidden_by=hidden_by)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up Mertik integration from YAML (unused)."""
    return True


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: MertikConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow the user to remove the device via the HA UI."""
    return True


async def async_unload_entry(hass: HomeAssistant, entry: MertikConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.mertik.close()
    return unloaded
