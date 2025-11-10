"""Config flow for the simplirtc integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import (
	ConfigFlow,
	ConfigFlowResult,
)

from .const import ATTR_CONFIG_ENTRY_ID, DOMAIN


class CloudConfigFlow(ConfigFlow, domain=DOMAIN):
	"""Handle a config flow for the simplirtc integration."""

	VERSION = 1

	async def async_step_system(
		self, user_input: dict[str, Any] | None = None
	) -> ConfigFlowResult:
		"""Handle the system step."""
		assert user_input is not None, "user_input should not be None"
		entry = self.hass.config_entries.async_get_known_entry(user_input[ATTR_CONFIG_ENTRY_ID])
		return self.async_create_entry(title=entry.title, data=user_input)
