import asyncio

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import (
    SOURCE_SYSTEM,
    ConfigEntry,
    ConfigEntryState,
    ConfigEntryChange,
    SIGNAL_CONFIG_ENTRY_CHANGED,
)
from homeassistant.helpers import discovery_flow
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN, SIMPLISAFE_DOMAIN


PLATFORMS = [
    Platform.CAMERA,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Your controller/hub specific code."""

    @callback
    def async_config_entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != SIMPLISAFE_DOMAIN:
            return

        match change:
            case ConfigEntryChange.ADDED | ConfigEntryChange.UPDATED:
                if entry.state == ConfigEntryState.LOADED:
                    discovery_flow.async_create_flow(
                        hass, DOMAIN, context={"source": SOURCE_SYSTEM}, data={ATTR_CONFIG_ENTRY_ID: entry.entry_id}
                    )
            case ConfigEntryChange.REMOVED:
                # If the entry is removed, we need to unload the platforms
                hass.async_create_task(_async_remove_config_entries(hass, entry.entry_id))


    async_dispatcher_connect(
        hass,
        SIGNAL_CONFIG_ENTRY_CHANGED,
        async_config_entry_changed,
    )

    for entry in hass.config_entries.async_loaded_entries(SIMPLISAFE_DOMAIN):
        async_config_entry_changed(ConfigEntryChange.ADDED, entry)

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up SimpliSafe from a config entry."""

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def _async_remove_config_entries(hass: HomeAssistant, entry_id: str):
    await asyncio.gather(*(
        hass.config_entries.async_remove(entry.entry_id)
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.data.get(ATTR_CONFIG_ENTRY_ID) == entry_id
    ))
