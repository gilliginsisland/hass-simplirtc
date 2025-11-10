from __future__ import annotations
from typing import cast

from aiohttp import web
from pydantic import TypeAdapter

from homeassistant.core import HomeAssistant
from homeassistant.components.camera import DOMAIN as CAMERA_DOMAIN
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .camera import LiveViewResponse, SimpliSafeCamera

class SimpliRTCStreamInfoView(HomeAssistantView):
	"""View to handle SimpliRTC stream info requests."""

	url = "/api/simplirtc_proxy/{entity_id}"
	name = f"api:{DOMAIN}:simplirtc"
	requires_auth = False

	def __init__(self, hass: HomeAssistant) -> None:
		self.hass = hass

	async def get(self, request: web.Request, entity_id: str) -> web.Response:
		"""Handle GET request for stream info."""

		entity_registry = er.async_get(self.hass)
		if not (camera := cast(SimpliSafeCamera, entity_registry.async_get_entity_id(CAMERA_DOMAIN, DOMAIN, entity_id))):
			return web.Response(status=404, text=f"Entity {entity_id} not found")

		try:
			stream_info = await camera._create_stream()
			return web.Response(
				body=TypeAdapter(LiveViewResponse).dump_json(stream_info),
				content_type="application/json",
			)
		except Exception as e:
			return web.Response(status=500, text=f"Error fetching stream info: {str(e)}")
