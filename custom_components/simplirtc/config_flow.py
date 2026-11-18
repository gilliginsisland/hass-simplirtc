"""UI setup for the global SimpliRTC device watcher."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class CloudConfigFlow(ConfigFlow, domain=DOMAIN):
	"""Enable live views for every configured SimpliSafe account."""

	VERSION = 1
	MINOR_VERSION = 2

	async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
		"""Allow one global entry, with no account selection or credentials."""
		if self._async_current_entries():
			return self.async_abort(reason="single_instance_allowed")
		await self.async_set_unique_id(DOMAIN)
		self._abort_if_unique_id_configured()
		if user_input is not None:
			return self.async_create_entry(title="SimpliRTC", data={})
		return self.async_show_form(step_id="user")

	async def async_step_import(self, user_input: dict[str, Any]) -> ConfigFlowResult:
		"""Import the original YAML enablement without requiring user input."""
		return await self.async_step_user({})
