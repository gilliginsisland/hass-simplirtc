"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import time
from typing import Any, TypeVar, override

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
	WebRTCAnswer,
	WebRTCCandidate,
	WebRTCSendMessage,
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from webrtc_models import RTCIceCandidateInit

from .const import (
	DOMAIN,
	ATTR_CONFIG_ENTRY_ID,
)
from .kinesis import KinesisSession

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"
_StreamResponseT = TypeVar("_StreamResponseT")


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry,
	async_add_entities: AddEntitiesCallback,
) -> None:
	"""Set up a SimpliSafe Camera."""

	entry_id: str = entry.data[ATTR_CONFIG_ENTRY_ID]

	_LOGGER.info("Setting up SimpliSafe Camera for entry: %s", entry_id)

	simplisafe_entry: ConfigEntry[SimpliSafe] | None
	if not (simplisafe_entry := hass.config_entries.async_get_entry(entry_id)):
		_LOGGER.warning("Missing SimpliSafe entry: %s", entry_id)
		return
	simplisafe = simplisafe_entry.runtime_data

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

	async def _create_stream(self, response_type: type[_StreamResponseT]) -> _StreamResponseT:
		path = f"cameras/{self._device.serial}/{self._system.system_id}/live-view"
		return TypeAdapter(response_type).validate_python(
			await self._simplisafe._api.async_request("get", path, url_base=WEBRTC_URL_BASE)  # pyright: ignore[reportPrivateUsage]
		)


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

			live_view = await self._create_stream(LiveKitResponse)
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
		self._sessions: dict[str, asyncio.Task[KinesisSession]] = {}

	@override
	async def async_handle_async_webrtc_offer(
		self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
	) -> None:
		"""Handle a Kinesis WebRTC offer."""

		self._sessions[session_id] = session_future = self.hass.async_create_task(
			self._create_webrtc_session(session_id, send_message)
		)
		try:
			session = await session_future
			if self._sessions.get(session_id) is session_future:
				session.start(offer_sdp)
		except Exception:
			self._sessions.pop(session_id, None)
			raise

	@override
	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a Kinesis WebRTC candidate."""

		if not (session_future := self._sessions.get(session_id)):
			_LOGGER.debug("Ignoring WebRTC candidate for closed session %s", session_id)
			return
		try:
			session = await session_future
		except Exception as err:
			_LOGGER.debug("Ignoring WebRTC candidate for failed session %s: %s", session_id, err)
			return
		await session.send_candidate(
			candidate.candidate,
			sdp_mid=candidate.sdp_mid,
			sdp_m_line_index=candidate.sdp_m_line_index,
		)

	@override
	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a Kinesis WebRTC session."""

		if not (session_future := self._sessions.pop(session_id, None)):
			return

		async def close_session() -> None:
			if not session_future.done():
				session_future.cancel()
			try:
				session = await session_future
			except asyncio.CancelledError:
				return
			except Exception as err:
				_LOGGER.debug(
					"WebRTC session %s ended before startup completed: %s",
					session_id,
					err,
				)
				return
			session.close()

		self.hass.async_create_task(close_session())

	async def _create_webrtc_session(
		self, session_id: str, send_message: WebRTCSendMessage
	) -> KinesisSession:
		live_view = await self._create_stream(KenisisResponse)

		def send_candidate(
			candidate: str,
			sdp_mid: str | None,
			sdp_m_line_index: int | None,
		) -> None:
			send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
				candidate=candidate,
				sdp_mid=sdp_mid,
				sdp_m_line_index=sdp_m_line_index,
			)))

		return KinesisSession(
			session_id=session_id,
			channel_endpoint=live_view.signedChannelEndpoint,
			client_id=live_view.clientId,
			send_answer=lambda answer_sdp: send_message(WebRTCAnswer(answer=answer_sdp)),
			send_candidate=send_candidate,
		)
