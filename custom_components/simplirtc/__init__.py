"""Add live views to devices owned by the built-in SimpliSafe integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry, SOURCE_IMPORT
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
	config_validation as cv,
	device_registry as dr,
	entity_registry as er,
)
from homeassistant.helpers.helper_integration import async_remove_helper_devices
from homeassistant.helpers.typing import ConfigType
from homeassistant.setup import async_when_setup

from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN
from .webrtc import async_register_webrtc_client_config_handler

PLATFORMS = [Platform.CAMERA]
CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
	"""Register WebRTC support and import an existing YAML setup once."""
	async_when_setup(hass, Platform.CAMERA, async_register_webrtc_client_config_handler)
	if DOMAIN in config and not hass.config_entries.async_entries(DOMAIN):
		hass.async_create_task(
			hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_IMPORT}, data={}),
			"simplirtc-import",
		)
	return True


def _migrate_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
	"""Relink devices from the original, per-account integration."""
	if parent_id := entry.data.get(ATTR_CONFIG_ENTRY_ID):
		registry = dr.async_get(hass)
		for device in dr.async_entries_for_config_entry(registry, parent_id):
			async_remove_helper_devices(
				hass, helper_config_entry_id=entry.entry_id, source_device_id=device.id,
			)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	"""Set up one device watcher for every SimpliSafe account."""
	entries = hass.config_entries.async_entries(DOMAIN, include_disabled=False)
	primary = next((item for item in entries if item.unique_id == DOMAIN), entries[0])
	if entry is not primary:
		# The primary entry transfers registry records before removing old entries.
		return True

	registry = er.async_get(hass)
	for previous in hass.config_entries.async_entries(DOMAIN):
		_migrate_devices(hass, previous)
		if previous is primary:
			continue
		for entity in er.async_entries_for_config_entry(registry, previous.entry_id):
			registry.async_update_entity(entity.entity_id, config_entry_id=primary.entry_id)
		await hass.config_entries.async_remove(previous.entry_id)

	hass.config_entries.async_update_entry(
		entry, data={}, title="SimpliRTC", unique_id=DOMAIN, minor_version=2,
	)
	await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
	return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	"""Unload the watcher and await every provider session's cleanup."""
	return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
	"""Accept original entries; setup consolidates them without losing entities."""
	if entry.version > 1:
		return False
	if entry.minor_version < 2:
		hass.config_entries.async_update_entry(entry, minor_version=2)
	return True
