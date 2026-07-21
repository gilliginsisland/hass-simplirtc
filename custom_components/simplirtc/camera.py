"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import functools
import logging
from typing import TypeVar, override

from pydantic import Field, TypeAdapter
from pydantic.dataclasses import dataclass
from simplipy.device.camera import Camera
from simplipy.system.v3 import SystemV3
from simplipy.websocket import (
	EVENT_CAMERA_MOTION_DETECTED,
)
from webrtc_models import (
	RTCIceCandidateInit,
	RTCIceServer,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.camera import (
	Camera as CameraEntity,
	CameraEntityFeature,
	CameraEntityDescription,
	WebRTCClientConfiguration,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .kinesis import KinesisSession
from .livekit import LiveKitSession
from .protobufs.livekit_rtc_pb2 import ICEServer as LiveKitIceServer
from .webrtc import (
	WebRTCClientConfigurationMixin,
	SimpliSafeWebRTCSession,
)

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE = "https://app-hub.prd.aser.simplisafe.com/v2"
UNUSED_WEBRTC_SESSION_TTL = 30
_StreamResponseT = TypeVar("_StreamResponseT")


async def async_setup_entry(
	hass: HomeAssistant,  # pyright: ignore[reportUnusedParameter]
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
			cameras.append(SimpliSafeCamera(simplisafe, system, camera))

	async_add_entities(cameras)


@dataclass(kw_only=True, slots=True)
class KinesisIceServer:
	urls: list[str]
	username: str | None = None
	credential: str | None = None


@dataclass(kw_only=True, slots=True)
class KenisisResponse:
	signedChannelEndpoint: str
	clientId: str
	iceServers: list[KinesisIceServer]


@dataclass(kw_only=True, slots=True)
class LiveKitResponse:
	liveKitDetails: LiveKitDetails


@dataclass(kw_only=True, slots=True)
class LiveKitDetails:
	liveKitURL: str
	userToken: str


@dataclass(kw_only=True, slots=True)
class EventMediaLink:
	href: str


@dataclass(kw_only=True, slots=True)
class EventVideo:
	links: dict[str, EventMediaLink] = Field(alias="_links")


@dataclass(kw_only=True, slots=True)
class EventHistoryEvent:
	eventTimestamp: int | float | None = None
	sensorSerial: str | None = None
	video: dict[str, EventVideo] | None = None
	videoStartedBy: str | None = None


@dataclass(kw_only=True, slots=True)
class EventHistoryResponse:
	events: list[EventHistoryEvent]


class SimpliSafeCamera(  # pyright: ignore[reportUnsafeMultipleInheritance]
	WebRTCClientConfigurationMixin,
	SimpliSafeEntity,
	CameraEntity,
):
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
		self._sessions: dict[str, SimpliSafeWebRTCSession] = {}
		self._unused_session_expirations: dict[
			SimpliSafeWebRTCSession, asyncio.TimerHandle
		] = {}

	@override
	async def async_camera_image(
		self,
		width: int | None = None,
		height: int | None = None,
	) -> bytes | None:
		"""Return the latest snapshot from this camera's event history."""
		_ = height
		history = TypeAdapter(EventHistoryResponse).validate_python(
			await self._simplisafe._api.async_request(  # pyright: ignore[reportPrivateUsage]
				"get",
				f"subscriptions/{self._system.system_id}/events?numEvents=50",
			)
		)

		camera_serials = {self._device.serial}
		if isinstance(camera_data := self._system.camera_data.get(self._device.serial), Mapping):
			for key in ("uuid", "serial"):
				if isinstance(serial := camera_data.get(key), str):
					camera_serials.add(serial)

		newest_video: EventVideo | None = None
		newest_timestamp = 0
		for event in history.events:
			if event.sensorSerial not in camera_serials:
				continue
			if event.video is None or event.videoStartedBy is None:
				continue
			if not (video := event.video.get(event.videoStartedBy)):
				continue
			if (timestamp := event.eventTimestamp or 0) <= newest_timestamp:
				continue
			newest_video, newest_timestamp = video, timestamp

		if newest_video and (snapshot := newest_video.links.get("snapshot/jpg")):
			return await self._simplisafe._api.async_media(  # pyright: ignore[reportPrivateUsage]
				snapshot.href.replace(
					"{&width}", f"&width={width}" if width is not None else ""
				)
			)

		return None

	async def _create_stream(self, response_type: type[_StreamResponseT]) -> _StreamResponseT:
		return TypeAdapter(response_type).validate_python(
			await self._simplisafe._api.async_request(  # pyright: ignore[reportPrivateUsage]
				"get", f"cameras/{self._device.serial}/{self._system.system_id}/live-view",
				url_base=WEBRTC_URL_BASE,
			)
		)

	@property
	def _web_rtc_provider(self) -> str | None:
		if not isinstance(settings := self._device.camera_settings.get("admin"), Mapping):
			return None
		if provider := settings.get("webRTCProvider"):  # pyright: ignore[reportUnknownMemberType]
			return str(provider)

	async def async_prepare_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
		"""Create an unused provider session and return its client configuration."""
		ice_servers: list[KinesisIceServer | LiveKitIceServer] = []
		match self._web_rtc_provider:
			case "mist":
				live_view = await self._create_stream(LiveKitResponse)
				ice_servers_ready: asyncio.Future[None] = self.hass.loop.create_future()

				def send_ice_servers(servers: list[LiveKitIceServer]) -> None:
					ice_servers.extend(servers)
					if not ice_servers_ready.done():
						ice_servers_ready.set_result(None)

				def close_before_ice_servers() -> None:
					if not ice_servers_ready.done():
						ice_servers_ready.set_exception(HomeAssistantError(
							f"LiveKit session closed before ICE servers were received for {self.entity_id}"
						))

				session = LiveKitSession(
					livekit_url=live_view.liveKitDetails.liveKitURL,
					user_token=live_view.liveKitDetails.userToken,
					send_ice_servers=send_ice_servers,
				)

				def on_close() -> None:
					close_before_ice_servers()
					self._expire(session)

				session.start(on_close)
				try:
					await ice_servers_ready
				except asyncio.CancelledError:
					session.close()
					raise
			case "kvs":
				live_view = await self._create_stream(KenisisResponse)
				ice_servers.extend(live_view.iceServers)
				session = KinesisSession(
					channel_endpoint=live_view.signedChannelEndpoint,
					client_id=live_view.clientId,
				)
				session.start(functools.partial(self._expire, session))
			case _ as provider:
				raise HomeAssistantError(
					f"Camera {self.name} has unknown webrtc provider {provider!r}"
				)

		self._pool_unused_webrtc_session(session)

		config = WebRTCClientConfiguration()
		config.configuration.ice_servers.extend(
			RTCIceServer(
				urls=list(ice_server.urls),
				username=ice_server.username or None,
				credential=ice_server.credential or None,
			)
			for ice_server in ice_servers
		)

		return config

	@override
	async def async_handle_async_webrtc_offer(
		self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
	) -> None:
		"""Handle a WebRTC offer."""
		if not (session := self._sessions.get(session_id)):
			if (session := next(iter(self._unused_session_expirations), None)) is None:
				raise HomeAssistantError(f"No prepared WebRTC session for {self.entity_id}")

			expiration = self._unused_session_expirations.pop(session)
			expiration.cancel()
			_LOGGER.debug("Using pooled WebRTC session for %s", self.entity_id)
			self._sessions[session_id] = session
		else:
			_LOGGER.debug("Renegotiating WebRTC session for %s", self.entity_id)
		try:
			await session.handle_offer(offer_sdp, send_message)
		except Exception:
			self._sessions.pop(session_id, None)
			session.close()
			raise

	@override
	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a WebRTC candidate."""
		if (session := self._sessions.get(session_id)):
			await session.send_candidate(candidate)
		else:
			_LOGGER.debug("Ignoring WebRTC candidate for closed session %s", session_id)

	@override
	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a WebRTC session."""
		if (session := self._sessions.pop(session_id, None)):
			session.close()

	@callback
	def _pool_unused_webrtc_session(
		self,
		session: SimpliSafeWebRTCSession,
	) -> None:
		"""Pool a session prepared by a client config request."""
		self._unused_session_expirations[session] = self.hass.loop.call_later(
			UNUSED_WEBRTC_SESSION_TTL, self._expire, session,
		)

	@callback
	def _expire(self, session: SimpliSafeWebRTCSession) -> None:
		"""Close an unused prepared session if the offer never arrives."""
		if self._unused_session_expirations.pop(session, None) is not None:
			session.close()
