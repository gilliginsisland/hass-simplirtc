"""Integration providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import (
	Platform,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import (
	ConfigEntry,
	ConfigEntryState,
	ConfigEntryChange,
	SOURCE_SYSTEM,
	SIGNAL_CONFIG_ENTRY_CHANGED,
)
from homeassistant.helpers import (
	discovery_flow,
	config_validation as cv,
)
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType
from homeassistant.components.simplisafe import (
	DOMAIN as SIMPLISAFE_DOMAIN,
	SimpliSafe,
)

from .const import (
	DOMAIN,
	ATTR_CONFIG_ENTRY_ID,
)
from .web import SimpliRTCStreamInfoView

PLATFORMS = [
	Platform.CAMERA,
	Platform.EVENT,
]

CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
	"""Set up the Simplirtc component."""
	if DOMAIN not in config:
		return True

	hass.http.register_view(SimpliRTCStreamInfoView(hass))

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

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry[SimpliSafe]) -> bool:
	"""Set up SimpliSafe from a config entry."""
	entry_id: str = entry.data[ATTR_CONFIG_ENTRY_ID]

	simplisafe_entry: ConfigEntry[SimpliSafe] | None = (
		hass.config_entries.async_get_entry(entry_id)
	)
	if simplisafe_entry is None:
		_LOGGER.debug("Skipping setup for missing SimpliSafe entry: %s", entry_id)
		return False

	entry.runtime_data = simplisafe_entry.runtime_data
	await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
	return True

async def _async_remove_config_entries(hass: HomeAssistant, entry_id: str):
	await asyncio.gather(*(
		hass.config_entries.async_remove(entry.entry_id)
		for entry in hass.config_entries.async_entries(DOMAIN)
		if entry.data.get(ATTR_CONFIG_ENTRY_ID) == entry_id
	))
