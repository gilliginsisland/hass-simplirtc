"""Component providing support to the Simplisafe camera."""

from __future__ import annotations

from typing import Any, Awaitable
import logging

from aiohttp import web
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass
from simplipy.device.camera import Camera
from simplipy.system.v3 import SystemV3
from simplipy.websocket import (
    EVENT_CAMERA_MOTION_DETECTED,
    WebsocketEvent,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.camera import (
    Camera as CameraEntity,
    CameraEntityFeature,
    CameraEntityDescription,
    WebRTCAnswer,
    WebRTCCandidate,
    WebRTCSendMessage,
    RTCIceCandidateInit,
)
from homeassistant.components.simplisafe import SimpliSafe
from homeassistant.components.simplisafe.entity import SimpliSafeEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ENTRY_KEY,
)
from .kinesis import (
    KinesisSession,
    KinesisResponse,
)

_LOGGER = logging.getLogger(__name__)
WEBRTC_URL_BASE="https://app-hub.prd.aser.simplisafe.com/v2"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up a SimpliSafe Camera."""

    entry_id = entry.data[ATTR_CONFIG_ENTRY_ID]

    _LOGGER.info("Setting up SimpliSafe Camera for entry: %s", entry_id)

    simplisafe = hass.data[ENTRY_KEY][entry_id]

    cameras: list[SimpliSafeCamera] = []

    for system in simplisafe.systems.values():
        if not isinstance(system, SystemV3):
            _LOGGER.warning("Skipping camera setup for V%d system: %s", system.version, system.system_id)
            continue

        for camera in system.cameras.values():
            cameras.append(SimpliSafeCamera(simplisafe, system, camera))

    async_add_entities(cameras)


@dataclass(kw_only=True, slots=True)
class LiveViewResponse:
    signedChannelEndpoint: str
    clientId: str
    iceServers: list[Any]


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

        self._video_url: str | None = None
        self._attr_unique_id = f"{super().unique_id}-camera"
        self._attr_supported_features |= CameraEntityFeature.STREAM
        self._device: Camera

        self._sessions: dict[str, Awaitable[KinesisSession]] = {}

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of camera image."""
        return None

    async def handle_async_mjpeg_stream(
        self, request: web.Request
    ) -> web.StreamResponse | None:
        """Generate an HTTP MJPEG stream from the camera."""
        return None

    async def async_handle_async_webrtc_offer(
        self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
    ) -> None:
        """Handle a WebRTC offer."""

        self._sessions[session_id] = future = self.hass.loop.create_future()

        def on_message(response: KinesisResponse) -> None:
            payload = response.payload

            if response.messageType == "SDP_ANSWER":
                send_message(WebRTCAnswer(payload["sdp"]))

            elif response.messageType == "ICE_CANDIDATE":
                candidate = RTCIceCandidateInit(
                    candidate=payload.get("candidate"),
                    sdp_mid=payload.get("sdpMid"),
                    sdp_m_line_index=payload.get("sdpMLineIndex"),
                )
                send_message(WebRTCCandidate(candidate))

        try:
            live_view = await self._create_stream()

            session = KinesisSession(
                session_id= session_id,
                channel_endpoint=live_view.signedChannelEndpoint,
                client_id=live_view.clientId,
                message_handler=on_message,
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
        await session.send_candidate(
            candidate.candidate,
            sdp_mid=candidate.sdp_mid,
            sdp_m_line_index=candidate.sdp_m_line_index,
        )

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Close a WebRTC session."""

        if (session := self._sessions.pop(session_id, None)) is None:
            return

        async def close_session() -> None:
            await (await session).close()

        self.hass.async_create_task(close_session())

    async def stream_source(self) -> str | None:
        """Return the source of the stream."""

    async def _create_stream(self) -> LiveViewResponse:
        path = f'cameras/{self._device.serial}/{self._system.system_id}/live-view'
        resp = await self._simplisafe._api.async_request('get', path, url_base=WEBRTC_URL_BASE)
        return TypeAdapter(LiveViewResponse).validate_python(resp)

    @callback
    def async_update_from_websocket_event(self, event: WebsocketEvent) -> None:
        """Update the entity when new data comes from the websocket."""

        _LOGGER.debug('Event recievied: %s', event)
