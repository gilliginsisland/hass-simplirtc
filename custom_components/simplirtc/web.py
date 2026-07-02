from __future__ import annotations

import aiohttp
from aiohttp import web

from homeassistant.core import HomeAssistant
from homeassistant.components.camera import DATA_COMPONENT
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .camera import SimpliSafeGo2rtcCamera, SimpliSafeLiveKitCamera


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


class SimpliRTCFlvProxyView(HomeAssistantView):
	"""Proxy the authenticated SimpliSafe FLV stream for go2rtc.

	go2rtc reads this endpoint out-of-process and this view injects the current
	SimpliSafe bearer token, so the raw FLV never touches HA's in-process stream
	worker. Access is gated by the per-camera proxy token.
	"""

	url = "/api/simplirtc_flv/{entity_id}"
	name = f"api:{DOMAIN}:flv"
	requires_auth = False

	def __init__(self, hass: HomeAssistant) -> None:
		self.hass = hass

	async def get(self, request: web.Request, entity_id: str) -> web.StreamResponse:
		"""Stream the authenticated FLV back to the caller (go2rtc)."""

		if not isinstance(
			camera := self.hass.data[DATA_COMPONENT].get_entity(entity_id),
			SimpliSafeGo2rtcCamera,
		):
			return web.Response(status=404, text=f"Entity {entity_id} is not a SimpliRTC FLV camera")

		if request.query.get("sig") != camera.proxy_token:
			return web.Response(status=401, text="Invalid signature")

		if not (token := camera.access_token):
			return web.Response(status=503, text="No SimpliSafe access token available")

		session = async_get_clientsession(self.hass)
		try:
			upstream = await session.get(
				camera.flv_url(),
				headers={"Authorization": f"Bearer {token}"},
			)
		except aiohttp.ClientError as err:
			return web.Response(status=502, text=f"Upstream error: {err}")

		if upstream.status != 200:
			upstream.close()
			return web.Response(status=upstream.status, text="Upstream returned an error")

		response = web.StreamResponse(status=200, headers={"Content-Type": "video/x-flv"})
		await response.prepare(request)
		try:
			async for chunk in upstream.content.iter_chunked(65536):
				await response.write(chunk)
		except (ConnectionResetError, aiohttp.ClientError):
			pass
		finally:
			upstream.close()
		return response
