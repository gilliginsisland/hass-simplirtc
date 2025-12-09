"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import time
from typing import Any, Protocol
from collections.abc import Awaitable, Mapping
import asyncio
import logging

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass
from propcache import cached_property
from simplipy.device.camera import Camera
from simplipy.system.v3 import SystemV3
from simplipy.websocket import (
	EVENT_CAMERA_MOTION_DETECTED,
)
import jwt

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.camera import (
	Camera as CameraEntity,
	CameraEntityFeature,
	CameraEntityDescription,
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
	RTCIceCandidateInit,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
	DOMAIN,
	ATTR_CONFIG_ENTRY_ID,
	ENTRY_KEY,
)
from .kinesis import KinesisSession

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE="https://app-hub.prd.aser.simplisafe.com/v2"


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry,
	async_add_entities: AddEntitiesCallback,
) -> None:
	"""Set up a SimpliSafe Camera."""

	entry_id: str = entry.data[ATTR_CONFIG_ENTRY_ID]

	_LOGGER.info("Setting up SimpliSafe Camera for entry: %s", entry_id)

	simplisafe = hass.data[ENTRY_KEY][entry_id]

	cameras: list[SimpliSafeCamera] = []

	for system in simplisafe.systems.values():
		if not isinstance(system, SystemV3):
			_LOGGER.warning("Skipping camera setup for V%d system: %s", system.version, system.system_id)
			continue

		for camera in system.cameras.values():
			if not isinstance(settings := camera.camera_settings.get('admin'), Mapping):
				_LOGGER.warning(f"Skipping camera '{camera.name}'. Unexpected settings schema.")
				continue

			match settings.get('webRTCProvider', None):
				case 'mist':
					cls = SimpliSafeLiveKitCamera
					if not hass.data[DOMAIN]:
						_LOGGER.warning(f"Camera '{camera.name}' requires livekit and no proxy addon is configured")
						continue
				case 'kvs':
					cls = SimpliSafeKenisisCamera
				case _ as provider:
					_LOGGER.warning(f"Camera '{camera.name}' has unknown webrtc provider '{provider}'")
					continue

			cameras.append(cls(simplisafe, system, camera))

	async_add_entities(cameras)


class Session(Protocol):
	async def stream(self, offer_sdp: str) -> None: ...
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None: ...
	async def close(self) -> None: ...


@dataclass(kw_only=True, slots=True)
class KenisisResponse:
	signedChannelEndpoint: str
	clientId: str
	iceServers: list[Any]


@dataclass(kw_only=True, slots=True)
class LiveKitResponse:
	liveKitDetails: LiveKitDetails


@dataclass(kw_only=True, slots=True)
class LiveKitDetails:
	liveKitURL: str
	userToken: str


class SimpliSafeCamera(SimpliSafeEntity, CameraEntity):
	"""An implementation of a Simplisafe camera."""

	def __init__(
		self,
		simplisafe: SimpliSafe,
		system: SystemV3,
		device: Camera,
	) -> None:
		"""Initialize the SimpliSafe camera."""
		super().__init__(
			simplisafe, system, device=device,
			additional_websocket_events=(EVENT_CAMERA_MOTION_DETECTED,)
		)
		self.entity_description = CameraEntityDescription(
			key="live_view",
		)
		CameraEntity.__init__(self)

		self._attr_unique_id = f"{super().unique_id}-camera"
		self._attr_supported_features |= CameraEntityFeature.STREAM
		self._device: Camera

	async def _create_stream(self) -> dict[str, Any]:
		path = f'cameras/{self._device.serial}/{self._system.system_id}/live-view'
		return await self._simplisafe._api.async_request('get', path, url_base=WEBRTC_URL_BASE)  # pyright: ignore[reportPrivateUsage]


class SimpliSafeLiveKitCamera(SimpliSafeCamera):
	"""An implementation of a Simplisafe camera."""

	def __init__(self, simplisafe: SimpliSafe, system: SystemV3, device: Camera) -> None:
		super().__init__(simplisafe, system, device)
		self._livekit_url: str = ""
		self._livekit_token: str = ""
		self._cache_expiration: float = 0
		self._lock = asyncio.Lock()

	@cached_property
	def use_stream_for_stills(self) -> bool:
		"""Whether or not to use stream to generate stills."""
		return True

	async def stream_source(self) -> str | None:
		"""Return the source of the stream."""

		url, token = await self._live_view()
		return f"rtsp://{self.hass.data[DOMAIN]}/{self._device.serial}?url={url}&token={token}"

	async def _live_view(self) -> tuple[str, str]:
		if time.time() < self._cache_expiration:
			return self._livekit_url, self._livekit_token

		async with self._lock:
			if time.time() < self._cache_expiration:
				return self._livekit_url, self._livekit_token

			resp = await self._create_stream()
			live_view = TypeAdapter(LiveKitResponse).validate_python(resp)
			self._livekit_url = live_view.liveKitDetails.liveKitURL
			self._livekit_token = live_view.liveKitDetails.userToken
			try:
				decoded_token = jwt.decode(self._livekit_token, options={"verify_signature": False})
				self._cache_expiration = decoded_token['exp']
			except Exception as e:
				_LOGGER.warning(f"Failed to decode JWT token for caching: {e}")
				self._cache_expiration = 0

		return self._livekit_url, self._livekit_token


class SimpliSafeKenisisCamera(SimpliSafeCamera):
	"""An implementation of a Simplisafe camera."""

	def __init__(
		self,
		simplisafe: SimpliSafe,
		system: SystemV3,
		device: Camera,
	) -> None:
		"""Initialize the SimpliSafe camera."""
		super().__init__(simplisafe, system, device)
		self._sessions: dict[str, Awaitable[KinesisSession]] = {}

	async def async_handle_async_webrtc_offer(
		self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
	) -> None:
		"""Handle a WebRTC offer."""

		self._sessions[session_id] = future = self.hass.loop.create_future()
		try:
			live_view = TypeAdapter(KenisisResponse).validate_python(await self._create_stream())
			session = KinesisSession(
				session_id=session_id,
				channel_endpoint=live_view.signedChannelEndpoint,
				client_id=live_view.clientId,
				send_message=send_message,
			)
			await session.stream(offer_sdp)
		except Exception as e:
			self._sessions.pop(session_id)
			future.set_exception(e)
			raise
		else:
			future.set_result(session)

	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a WebRTC candidate."""

		session = await self._sessions[session_id]
		await session.send_candidate(candidate)

	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a WebRTC session."""

		if (session := self._sessions.pop(session_id, None)) is None:
			return

		async def close_session() -> None:
			await (await session).close()

		self.hass.async_create_task(close_session())
