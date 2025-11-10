from __future__ import annotations
from typing import cast

from aiohttp import web
from pydantic import TypeAdapter

from homeassistant.core import HomeAssistant
from homeassistant.components.camera import (
	DOMAIN as CAMERA_DOMAIN,
	DATA_COMPONENT as CAMERA_DATA_COMPONENT,
)
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

		if not isinstance(camera := self.hass.data[CAMERA_DATA_COMPONENT].get_entity(entity_id), SimpliSafeCamera) :
			return web.Response(status=404, text=f"Entity {entity_id} is not a SimpliSafeCamera")

		try:
			stream_info = await camera._create_stream()
			return web.Response(
				body=TypeAdapter(LiveViewResponse).dump_json(stream_info),
				content_type="application/json",
			)
		except Exception as e:
			return web.Response(status=500, text=f"Error fetching stream info: {str(e)}")
