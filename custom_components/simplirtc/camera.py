"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import logging
import time
from typing import Any, TypeVar, override

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
	WebRTCClientConfiguration,
	WebRTCSendMessage,
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from webrtc_models import RTCIceCandidateInit, RTCIceServer

from .kinesis import KinesisSession
from .livekit import LiveKitSession, fetch_ice_servers
from .protobufs.livekit_rtc_pb2 import (
	ICEServer,
	SessionDescription,
	SignalTarget,
	TrickleRequest,
)

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"
_StreamResponseT = TypeVar("_StreamResponseT")


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry[SimpliSafe],
	async_add_entities: AddEntitiesCallback,
) -> None:
	"""Set up a SimpliSafe Camera."""
	simplisafe = entry.runtime_data

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

	async def _create_stream(self, response_type: type[_StreamResponseT]) -> _StreamResponseT:
		path = f"cameras/{self._device.serial}/{self._system.system_id}/live-view"
		return TypeAdapter(response_type).validate_python(
			await self._simplisafe._api.async_request("get", path, url_base=WEBRTC_URL_BASE)  # pyright: ignore[reportPrivateUsage]
		)

	@override
	async def async_camera_image(
		self,
		width: int | None = None,
		height: int | None = None,
	) -> bytes | None:
		"""Return a camera image."""
		_ = width, height
		return None


class SimpliSafeLiveKitCamera(SimpliSafeCamera):
	"""An implementation of a Simplisafe camera."""

	def __init__(self, simplisafe: SimpliSafe, system: SystemV3, device: Camera) -> None:
		super().__init__(simplisafe, system, device)
		self._livekit_url: str = ""
		self._livekit_token: str = ""
		self._livekit_ice_servers: list[ICEServer] = []
		self._livekit_client_configuration = WebRTCClientConfiguration()
		self._cache_expiration: float = 0
		self._lock = asyncio.Lock()
		self._ice_server_task: asyncio.Task[None] | None = None
		self._sessions: dict[str, LiveKitSession] = {}

	@override
	async def async_internal_added_to_hass(self) -> None:
		"""Run when entity is added to hass."""
		await super().async_internal_added_to_hass()
		self._async_fetch_initial_livekit_ice_servers()

	@override
	async def async_will_remove_from_hass(self) -> None:
		"""Run when entity will be removed from hass."""
		if task := self._ice_server_task:
			self._ice_server_task = None
			task.cancel()
		await super().async_will_remove_from_hass()

	@override
	@callback
	def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
		"""Return cached LiveKit ICE servers for the browser peer connection."""
		return self._livekit_client_configuration

	@callback
	def _async_fetch_initial_livekit_ice_servers(self) -> None:
		"""Start the initial LiveKit ICE server fetch if one is not already running."""
		if self._ice_server_task and not self._ice_server_task.done():
			return
		self._ice_server_task = self.hass.async_create_task(
			self._fetch_initial_livekit_ice_servers(),
			f"simplirtc-fetch-livekit-ice-{self.entity_id}",
		)

	async def _fetch_initial_livekit_ice_servers(self) -> None:
		"""Fetch initial LiveKit ICE servers for the sync client config hook."""
		try:
			livekit_url, user_token = await self._live_view()
			self._async_update_livekit_ice_servers(
				await fetch_ice_servers(livekit_url, user_token)
			)
		except Exception as err:
			_LOGGER.debug("Failed to refresh LiveKit ICE servers for %s: %s", self.entity_id, err)
		finally:
			if self._ice_server_task is asyncio.current_task():
				self._ice_server_task = None

	@callback
	def _async_update_livekit_ice_servers(
		self,
		ice_servers: list[ICEServer],
	) -> None:
		"""Store LiveKit ICE servers when the server reports a new list."""
		if self._livekit_ice_servers == ice_servers:
			return
		_LOGGER.debug(
			"Updating LiveKit ICE servers for %s: count=%s",
			self.entity_id,
			len(ice_servers),
		)
		self._livekit_ice_servers = ice_servers
		config = WebRTCClientConfiguration()
		for ice_server in ice_servers:
			config.configuration.ice_servers.append(RTCIceServer(
				urls=list(ice_server.urls),
				username=ice_server.username or None,
				credential=ice_server.credential or None,
			))
		self._livekit_client_configuration = config

	@override
	async def async_handle_async_webrtc_offer(
		self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
	) -> None:
		"""Handle a browser WebRTC offer through LiveKit signaling."""
		livekit_url, user_token = await self._live_view()

		def send_answer(answer: SessionDescription) -> None:
			send_message(WebRTCAnswer(answer=answer.sdp))

		def send_candidate(trickle: TrickleRequest) -> None:
			if trickle.final and not trickle.candidateInit:
				send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
					candidate="",
					sdp_mid=None,
					sdp_m_line_index=None,
				)))
				return
			if not trickle.candidateInit:
				return

			try:
				candidate_init = json.loads(trickle.candidateInit)
			except ValueError as err:
				_LOGGER.warning("Dropping invalid LiveKit ICE candidate JSON: %s", err)
				return

			candidate = candidate_init.get("candidate")
			if not isinstance(candidate, str):
				_LOGGER.warning("Dropping LiveKit ICE candidate without candidate field")
				return
			sdp_mid = candidate_init.get("sdpMid")
			sdp_m_line_index = candidate_init.get("sdpMLineIndex")
			send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
				candidate=candidate,
				sdp_mid=sdp_mid if isinstance(sdp_mid, str) else None,
				sdp_m_line_index=(
					sdp_m_line_index if isinstance(sdp_m_line_index, int) else None
				),
			)))

		def on_close() -> None:
			if self._sessions.get(session_id) is session:
				self._sessions.pop(session_id, None)

		session = LiveKitSession(
			session_id=session_id,
			livekit_url=livekit_url,
			user_token=user_token,
			offer_sdp=offer_sdp,
			send_answer=send_answer,
			send_candidate=send_candidate,
			on_close=on_close,
			on_ice_servers=self._async_update_livekit_ice_servers,
		)
		self._sessions[session_id] = session
		try:
			await session.start()
		except BaseException:
			on_close()
			raise

	@override
	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a browser WebRTC candidate for LiveKit."""
		if not (session := self._sessions.get(session_id)):
			_LOGGER.debug("Ignoring WebRTC candidate for closed session %s", session_id)
			return

		# LiveKit rejects the browser's final null ICE event if it is serialized
		# as an empty candidateInit, and the session works without forwarding it.
		if not candidate.candidate:
			return

		candidate_init: dict[str, str | int] = {"candidate": candidate.candidate}
		if candidate.sdp_mid is not None:
			candidate_init["sdpMid"] = candidate.sdp_mid
		if candidate.sdp_m_line_index is not None:
			candidate_init["sdpMLineIndex"] = candidate.sdp_m_line_index

		await session.send_candidate(TrickleRequest(
			candidateInit=json.dumps(candidate_init, separators=(",", ":")),
			target=SignalTarget.PUBLISHER,
		))

	@override
	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a LiveKit signaling session."""
		if session := self._sessions.pop(session_id, None):
			session.close()

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
