"""Web-app client token for SimpliSafe live view.

Since about September 2026 SimpliSafe answers the v2 ``live-view`` endpoint with 404 for tokens issued to the iOS-app
client that Home Assistant's SimpliSafe integration uses, while the same request with a token from the SimpliSafe
*web app* client succeeds. This module keeps a second, separate token family for the web-app client in
``/config/.storage/simplirtc_webapp_token.json`` (created once from a browser login) and refreshes it here. Auth0
rotates refresh tokens, so every refresh writes the new one back before it is used again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_LOGGER = logging.getLogger(__name__)
TOKEN_URL = "https://auth.simplisafe.com/oauth/token"
ORIGIN = "https://webapp.simplisafe.com"
_LOCK = asyncio.Lock()


def _path(hass: HomeAssistant) -> str:
	return hass.config.path(".storage", "simplirtc_webapp_token.json")


def available(hass: HomeAssistant) -> bool:
	return os.path.exists(_path(hass))


def _read(path: str) -> dict:
	with open(path, encoding="utf-8") as f:
		return json.load(f)


def _write(path: str, data: dict) -> None:
	tmp = path + ".tmp"
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(data, f)
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)


async def async_access_token(hass: HomeAssistant) -> str:
	"""Return a valid web-app access token, refreshing (and persisting the rotated refresh token) when needed."""
	path = _path(hass)
	async with _LOCK:
		data = await hass.async_add_executor_job(_read, path)
		if data.get("access_token") and data.get("expires_at", 0) > time.time() + 60:
			return data["access_token"]
		session = async_get_clientsession(hass)
		async with session.post(TOKEN_URL, json={"grant_type": "refresh_token", "client_id": data["client_id"],
												 "refresh_token": data["refresh_token"]},
								headers={"Origin": ORIGIN}, timeout=20) as resp:
			body = await resp.json(content_type=None)
			if resp.status != 200:
				raise RuntimeError(f"SimpliSafe web-app token refresh failed: {resp.status} {str(body)[:200]}")
		data["access_token"] = body["access_token"]
		data["expires_at"] = time.time() + int(body.get("expires_in", 3600))
		if body.get("refresh_token"):
			data["refresh_token"] = body["refresh_token"]
		await hass.async_add_executor_job(_write, path, data)
		_LOGGER.debug("Refreshed SimpliSafe web-app token (expires in %ss)", body.get("expires_in"))
		return data["access_token"]


async def async_live_view(hass: HomeAssistant, url: str) -> dict:
	"""GET a live-view session with the web-app token."""
	token = await async_access_token(hass)
	session = async_get_clientsession(hass)
	async with session.get(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json, text/plain, */*"},
						   timeout=20) as resp:
		if resp.status != 200:
			raise RuntimeError(f"SimpliSafe live-view (web-app token) returned {resp.status}")
		return await resp.json(content_type=None)
