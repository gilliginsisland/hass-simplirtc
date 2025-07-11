import asyncio
import logging

from homeassistant.const import CONF_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigEntryChange,
    SIGNAL_CONFIG_ENTRY_CHANGED,
)
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, SIMPLISAFE_DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Your controller/hub specific code."""

    async def async_setup_config_entry(entry: ConfigEntry):
        """Set up SimpliSafe from a config entry."""
        _LOGGER.info("Setting up SimpliSafe Camera for entry: %s", entry)
        await async_load_platform(hass, Platform.CAMERA, DOMAIN, {CONF_ENTITY_ID: entry.entry_id}, config)

    @callback
    def async_config_entry_changed(change: ConfigEntryChange, entry: ConfigEntry) -> None:
        if entry.domain != SIMPLISAFE_DOMAIN:
            return

        if entry.state == ConfigEntryState.LOADED:
            hass.create_task(
                async_setup_config_entry(entry)
            )

    async_dispatcher_connect(
        hass,
        SIGNAL_CONFIG_ENTRY_CHANGED,
        async_config_entry_changed,
    )

    await asyncio.gather(*[
        async_setup_config_entry(entry)
        for entry in hass.config_entries.async_loaded_entries(SIMPLISAFE_DOMAIN)
    ])

    return True
