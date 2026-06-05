"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
import time
from typing import Any, override

from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass
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
	WebRTCMessage,
	WebRTCSendMessage,
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from webrtc_models import RTCIceCandidateInit

from .const import (
	ATTR_CONFIG_ENTRY_ID,
)
from .kinesis import KinesisSession
from .livekit import LiveKitSession
from .session import Session
from .snapshot import DEFAULT_SNAPSHOT_TIMEOUT, Snapshotter

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"


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
			if not isinstance(settings := camera.camera_settings.get("admin"), Mapping):
				_LOGGER.warning("Skipping camera '%s'. Unexpected settings schema.", camera.name)
				continue

			match settings.get("webRTCProvider"):
				case "mist":
					cls = SimpliSafeLiveKitCamera
				case "kvs":
					cls = SimpliSafeKenisisCamera
				case _ as provider:
					_LOGGER.warning("Camera '%s' has unknown webrtc provider '%s'", camera.name, provider)
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
		self._sessions: dict[str, asyncio.Task[Session]] = {}

	async def _create_stream(self) -> dict[str, Any]:
		path = f"cameras/{self._device.serial}/{self._system.system_id}/live-view"
		return await self._simplisafe._api.async_request("get", path, url_base=WEBRTC_URL_BASE)  # pyright: ignore[reportPrivateUsage]

	async def async_handle_async_webrtc_offer(
		self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
	) -> None:
		"""Handle a WebRTC offer."""

		self._sessions[session_id] = task = self.hass.async_create_task(
			self._create_webrtc_session(session_id, send_message)
		)
		try:
			session = await task
			session.start(offer_sdp)
		except (asyncio.CancelledError, Exception):
			self._sessions.pop(session_id, None)
			raise

	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a WebRTC candidate."""

		if not (task := self._sessions.get(session_id)):
			_LOGGER.debug("Ignoring WebRTC candidate for closed session %s", session_id)
			return
		try:
			session = await task
		except asyncio.CancelledError:
			return
		except Exception as err:
			_LOGGER.debug("Ignoring WebRTC candidate for failed session %s: %s", session_id, err)
			return
		try:
			await session.send_candidate(candidate)
		except asyncio.CancelledError:
			return

	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a WebRTC session."""

		if not (task := self._sessions.pop(session_id, None)):
			return

		async def close_session() -> None:
			if not task.done():
				task.cancel()
			try:
				session = await task
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

	@override
	async def async_camera_image(
		self,
		width: int | None = None,
		height: int | None = None,
	) -> bytes | None:
		"""Return a camera image from a temporary WebRTC session."""
		_ = width, height
		snapshotter = Snapshotter()
		try:
			offer_sdp = await snapshotter.make_offer()

			def send_message(message: WebRTCMessage) -> None:
				match message:
					case WebRTCAnswer(answer=answer):
						snapshotter.send_answer(answer)
					case WebRTCCandidate(candidate=candidate):
						snapshotter.send_candidate(candidate)
					case _:
						_LOGGER.debug("Dropping unsupported snapshot WebRTC message type=%s", type(message).__name__)

			async with asyncio.timeout(DEFAULT_SNAPSHOT_TIMEOUT):
				await self.async_handle_async_webrtc_offer(
					offer_sdp,
					snapshotter.session_id,
					send_message,
				)
				return await snapshotter.wait_for_image()
		except TimeoutError:
			_LOGGER.debug("Timed out waiting for WebRTC camera image")
			return None
		finally:
			self.close_webrtc_session(snapshotter.session_id)
			await snapshotter.close()

	async def _create_webrtc_session(
		self, session_id: str, send_message: WebRTCSendMessage
	) -> Session:
		raise NotImplementedError


class SimpliSafeLiveKitCamera(SimpliSafeCamera):
	"""An implementation of a Simplisafe camera."""

	def __init__(self, simplisafe: SimpliSafe, system: SystemV3, device: Camera) -> None:
		super().__init__(simplisafe, system, device)
		self._livekit_url: str = ""
		self._livekit_token: str = ""
		self._cache_expiration: float = 0
		self._lock = asyncio.Lock()

	@override
	async def _create_webrtc_session(
		self, session_id: str, send_message: WebRTCSendMessage
	) -> LiveKitSession:
		livekit_url, livekit_token = await self._live_view()
		return LiveKitSession(
			session_id=session_id,
			send_answer=lambda answer_sdp: send_message(WebRTCAnswer(answer=answer_sdp)),
			livekit_url=livekit_url,
			user_token=livekit_token,
		)

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
				self._cache_expiration = decoded_token["exp"]
			except Exception as err:
				_LOGGER.warning("Failed to decode JWT token for caching: %s", err)
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

	@override
	async def _create_webrtc_session(
		self, session_id: str, send_message: WebRTCSendMessage
	) -> KinesisSession:
		live_view = TypeAdapter(KenisisResponse).validate_python(await self._create_stream())
		return KinesisSession(
			session_id=session_id,
			channel_endpoint=live_view.signedChannelEndpoint,
			client_id=live_view.clientId,
			send_message=send_message,
		)
