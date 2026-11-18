"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import TypeVar, override

from pydantic import Field, TypeAdapter
from pydantic.dataclasses import dataclass
from simplipy.device.camera import Camera
from simplipy.system.v3 import SystemV3
from simplipy.websocket import (
	EVENT_CAMERA_MOTION_DETECTED,
	WebsocketEvent,
)
from webrtc_models import (
	RTCIceCandidateInit,
	RTCIceServer,
)

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.components.camera import (
	Camera as CameraEntity,
	CameraEntityCapabilities,
	CameraEntityFeature,
	CameraEntityDescription,
	WebRTCClientConfiguration,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.const import DOMAIN as SIMPLISAFE_DOMAIN
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
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


def _camera_web_rtc_provider(camera: Camera) -> str | None:
	"""Read the camera's advertised live-view backend."""
	if not isinstance(settings := camera.camera_settings.get("admin"), Mapping):
		return None
	if provider := settings.get("webRTCProvider"):
		return str(provider)
	return None


async def async_setup_entry(
	hass: HomeAssistant,
	entry: ConfigEntry,
	async_add_entities: AddEntitiesCallback,
) -> None:
	"""Follow registered camera devices across all core SimpliSafe accounts."""
	registry = dr.async_get(hass)
	entities: dict[str, SimpliSafeCamera] = {}
	changes: asyncio.Queue[str] = asyncio.Queue()

	def accepting_changes() -> bool:
		return entry.state in {ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.LOADED}

	def source_account(device: dr.DeviceEntry) -> ConfigEntry[SimpliSafe] | None:
		parent = hass.config_entries.async_get_entry(device.config_entry_id)
		if parent is None or parent.domain != SIMPLISAFE_DOMAIN or parent.disabled_by:
			return None
		# Core registers devices while setting up its entity platforms, after
		# assigning runtime_data. Do not wait for its LOADED notification.
		if parent.state not in {ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.LOADED}:
			return None
		return parent if hasattr(parent, "runtime_data") else None

	@callback
	def device_changed(event: Event[dr.EventDeviceRegistryUpdatedData]) -> None:
		if not accepting_changes():
			return
		device_id = event.data["device_id"]
		device = registry.async_get(device_id)
		if device_id in entities or (device is not None and source_account(device) is not None):
			changes.put_nowait(device_id)

	async def reconcile(device_id: str) -> None:
		parent = registry.async_get(device_id)
		match = None
		if parent is not None and (account := source_account(parent)) is not None:
			for system in account.runtime_data.systems.values():
				if not isinstance(system, SystemV3):
					continue
				for camera in system.cameras.values():
					if (SIMPLISAFE_DOMAIN, camera.serial) in parent.identifiers:
						if _camera_web_rtc_provider(camera) in {"mist", "kvs"}:
							match = (account.runtime_data, system, camera)
						break
				if match is not None:
					break

		existing = entities.get(device_id)
		if existing is not None and (
			match is None
			or existing._device.serial != match[2].serial
			or existing._simplisafe is not match[0]
		):
			if entity := entities.pop(device_id, None):
				# Addition may still be in flight when a removal arrives. The event
				# also completes if HA aborts addition (for example, a disabled entity).
				await entity._platform_ready.wait()
				if entity._added_to_hass:
					await entity.async_remove()
		if match is None:
			return
		if device_id not in entities and accepting_changes():
			simplisafe, system, camera = match
			entity = entities[device_id] = SimpliSafeCamera(
				simplisafe, system, camera, entry, parent,
			)
			async_add_entities([entity])

	async def watch_devices() -> None:
		# Subscribe before the initial scan so late registrations cannot be missed.
		unsubscribe = hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, device_changed)
		try:
			for device in registry.devices:
				changes.put_nowait(device.id)
			while True:
				device_id = await changes.get()
				if not accepting_changes():
					return
				try:
					await reconcile(device_id)
				except Exception:
					_LOGGER.exception("Updating a live-view entity after a device change failed")
		finally:
			unsubscribe()

	entry.async_create_background_task(hass, watch_devices(), "simplirtc-camera-devices")


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
	links: dict[str, EventMediaLink] = Field(default_factory=dict, alias="_links")


@dataclass(kw_only=True, slots=True)
class EventHistoryEvent:
	eventTimestamp: int | float | None = None
	video: dict[str, EventVideo] | None = None


@dataclass(kw_only=True, slots=True)
class EventHistoryResponse:
	events: list[EventHistoryEvent]


class SimpliSafeCamera(  # pyright: ignore[reportUnsafeMultipleInheritance]
	WebRTCClientConfigurationMixin,
	SimpliSafeEntity,
	CameraEntity,
):
	"""An implementation of a Simplisafe camera."""

	_attr_camera_entity_capabilities = CameraEntityCapabilities(two_way_audio=True)

	def __init__(
		self,
		simplisafe: SimpliSafe,
		system: SystemV3,
		device: Camera,
		entry: ConfigEntry,
		parent_device: dr.DeviceEntry,
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

		# Like Switch as X, link to the source integration's existing device.
		self._attr_device_info = None
		self.device_entry = parent_device
		self._attr_unique_id = f"{super().unique_id}-camera"
		self._attr_supported_features |= CameraEntityFeature.STREAM
		self._device: Camera
		self._sessions: dict[str, SimpliSafeWebRTCSession] = {}
		self._entry = entry
		self._unused_sessions: dict[SimpliSafeWebRTCSession, None] = {}
		self._provider_sessions: set[SimpliSafeWebRTCSession] = set()
		self._removing = False
		self._added_to_hass = False
		self._platform_ready = asyncio.Event()
		self.async_on_remove(self._platform_ready.set)
		self._snapshot_template_url: str | None = None

	@override
	async def async_camera_image(
		self,
		width: int | None = None,
		height: int | None = None,
	) -> bytes | None:
		"""Return the latest snapshot from this camera's event history."""
		_ = height
		if self._snapshot_template_url is None:
			await self._async_refresh_snapshot_template_url()

		if snapshot_template_url := self._snapshot_template_url:
			return await self._simplisafe._api.async_media(  # pyright: ignore[reportPrivateUsage]
				snapshot_template_url.replace(
					"{&width}", f"&width={width}" if width is not None else ""
				)
			)

		return None

	async def _async_refresh_snapshot_template_url(self) -> None:
		"""Find this camera's newest snapshot across all returned history events."""
		history = TypeAdapter(EventHistoryResponse).validate_python(
			await self._simplisafe._api.async_request(  # pyright: ignore[reportPrivateUsage]
				"get",
				f"subscriptions/{self._system.system_id}/events?numEvents=50",
			)
		)

		newest_snapshot: EventMediaLink | None = None
		newest_timestamp = 0
		camera_serials = self._camera_event_serials()
		for event in history.events:
			if (timestamp := event.eventTimestamp or 0) <= newest_timestamp:
				continue
			for camera_id, video in (event.video or {}).items():
				if camera_id not in camera_serials:
					continue
				if snapshot := video.links.get("snapshot/jpg"):
					newest_snapshot, newest_timestamp = snapshot, timestamp
					break

		if newest_snapshot:
			self._snapshot_template_url = newest_snapshot.href

	@override
	@callback
	def async_update_from_websocket_event(self, event: WebsocketEvent) -> None:
		"""Cache the snapshot template from a camera motion event."""
		if event.media_urls and (url := event.media_urls.get("image_url")):
			self._snapshot_template_url = url

	@override
	@callback
	def _handle_websocket_update(self, event: WebsocketEvent) -> None:
		"""Ignore camera motion events for other cameras."""
		if (
			event.event_type == EVENT_CAMERA_MOTION_DETECTED
			and event.sensor_serial not in self._camera_event_serials()
		):
			return
		super()._handle_websocket_update(event)

	@callback
	def _camera_event_serials(self) -> set[str]:
		"""Return camera identifiers that may appear in events."""
		serials = {self._device.serial}
		if isinstance(camera_data := self._system.camera_data.get(self._device.serial), Mapping):
			for key in ("uuid", "serial"):
				if isinstance(serial := camera_data.get(key), str) and serial:
					serials.add(serial)
		return serials

	async def _create_stream(self, response_type: type[_StreamResponseT]) -> _StreamResponseT:
		return TypeAdapter(response_type).validate_python(
			await self._simplisafe._api.async_request(  # pyright: ignore[reportPrivateUsage]
				"get", f"cameras/{self._device.serial}/{self._system.system_id}/live-view",
				url_base=WEBRTC_URL_BASE,
				headers={"Host": "app-hub.prd.aser.simplisafe.com"},
			)
		)

	@property
	def _web_rtc_provider(self) -> str | None:
		return _camera_web_rtc_provider(self._device)

	@property
	def extra_state_attributes(self) -> dict[str, str | None]:
		"""Return camera-specific state attributes."""
		return {"webrtc_provider": self._web_rtc_provider}

	@override
	async def async_added_to_hass(self) -> None:
		"""Complete device attachment before processing queued removal events."""
		await super().async_added_to_hass()
		self._added_to_hass = True
		self._platform_ready.set()

	@override
	async def async_will_remove_from_hass(self) -> None:
		"""Stop accepting sessions and await all their connection scopes."""
		self._removing = True
		self._added_to_hass = False
		sessions = tuple(self._provider_sessions)
		for session in sessions:
			session.close()
		await asyncio.gather(*(session.wait_closed() for session in sessions))
		await super().async_will_remove_from_hass()

	async def async_prepare_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
		"""Start a scoped provider session and wait for its ICE configuration."""
		if self._removing:
			raise HomeAssistantError("Camera is being removed")
		ice_servers: list[KinesisIceServer | LiveKitIceServer] = []
		ice_servers_ready: asyncio.Future[None] = self.hass.loop.create_future()

		def send_ice_servers(servers: list[LiveKitIceServer]) -> None:
			ice_servers.extend(servers)
			if not ice_servers_ready.done():
				ice_servers_ready.set_result(None)

		match self._web_rtc_provider:
			case "mist":
				live_view = await self._create_stream(LiveKitResponse)
				session = LiveKitSession(
					livekit_url=live_view.liveKitDetails.liveKitURL,
					user_token=live_view.liveKitDetails.userToken,
					send_ice_servers=send_ice_servers,
				)
			case "kvs":
				live_view = await self._create_stream(KenisisResponse)
				ice_servers.extend(live_view.iceServers)
				ice_servers_ready.set_result(None)
				session = KinesisSession(
					channel_endpoint=live_view.signedChannelEndpoint,
					client_id=live_view.clientId,
				)
			case _ as provider:
				raise HomeAssistantError(
					f"Camera {self.name} has unknown webrtc provider {provider!r}"
				)

		# Removal can happen while the live-view API request is in flight.
		if self._removing:
			raise HomeAssistantError("Camera is being removed")

		def on_close() -> None:
			self._session_closed(session)
			if not ice_servers_ready.done():
				ice_servers_ready.set_exception(HomeAssistantError(
					f"Session closed before ICE configuration for {self.entity_id}"
				))

		self._provider_sessions.add(session)
		try:
			session.start(
				on_close,
				create_task=lambda coro, *, name: self._entry.async_create_background_task(
					self.hass, coro, name, eager_start=False,
				),
				unused_timeout=UNUSED_WEBRTC_SESSION_TTL,
			)
			async with asyncio.timeout(15):
				await ice_servers_ready
		except (Exception, asyncio.CancelledError):
			session.close()
			await session.wait_closed()
			self._provider_sessions.discard(session)
			raise

		if session not in self._provider_sessions or session.closing:
			raise HomeAssistantError("Session closed while preparing configuration")
		self._unused_sessions[session] = None
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
			if (session := next(iter(self._unused_sessions), None)) is None:
				raise HomeAssistantError(f"No prepared WebRTC session for {self.entity_id}")

			self._unused_sessions.pop(session)
			_LOGGER.debug("Using pooled WebRTC session for %s", self.entity_id)
			self._sessions[session_id] = session
		else:
			_LOGGER.debug("Renegotiating WebRTC session for %s", self.entity_id)
		try:
			await session.handle_offer(offer_sdp, send_message)
		except (Exception, asyncio.CancelledError):
			session.close()
			await session.wait_closed()
			raise

	@override
	async def async_on_webrtc_candidate(
		self, session_id: str, candidate: RTCIceCandidateInit
	) -> None:
		"""Handle a WebRTC candidate."""
		if (session := self._sessions.get(session_id)) and not session.closing:
			await session.send_candidate(candidate)
		else:
			_LOGGER.debug("Ignoring WebRTC candidate for closed session %s", session_id)

	@override
	@callback
	def close_webrtc_session(self, session_id: str) -> None:
		"""Close a WebRTC session."""
		if session := self._sessions.get(session_id):
			session.close()

	@callback
	def _session_closed(self, session: SimpliSafeWebRTCSession) -> None:
		"""Remove routing entries only after the provider has finished cleanup."""
		self._provider_sessions.discard(session)
		self._unused_sessions.pop(session, None)
		for session_id, active in tuple(self._sessions.items()):
			if active is session:
				del self._sessions[session_id]
