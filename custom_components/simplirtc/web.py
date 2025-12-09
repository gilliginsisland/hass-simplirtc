from __future__ import annotations

from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.components.camera import DATA_COMPONENT
from homeassistant.components.http import HomeAssistantView

from .const import DOMAIN
from .camera import SimpliSafeLiveKitCamera


class SimpliRTCStreamInfoView(HomeAssistantView):
	"""View to handle SimpliRTC stream info requests."""

	url = "/api/simplirtc_proxy/{entity_id}"
	name = f"api:{DOMAIN}:simplirtc"
	requires_auth = False

	def __init__(self, hass: HomeAssistant) -> None:
		self.hass = hass

	async def get(self, request: web.Request, entity_id: str) -> web.Response:
		"""Handle GET request for stream info."""

		if not isinstance(camera := self.hass.data[DATA_COMPONENT].get_entity(entity_id), SimpliSafeLiveKitCamera) :
			return web.Response(status=404, text=f"Entity {entity_id} is not a SimpliSafeLiveKitCamera")

		try:
			url, token = await camera._live_view()
			return web.json_response({"url": url, "token": token})
		except Exception as e:
			return web.Response(status=500, text=f"Error fetching stream info: {str(e)}")
