"""WebRTC setup helpers for SimpliRTC."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from functools import wraps
import logging
from typing import Any, Generic, TypeVar

from homeassistant.components import websocket_api
from homeassistant.components.camera import (
	WebRTCClientConfiguration,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from homeassistant.components.camera.helper import get_camera_from_entity_id
from homeassistant.core import HomeAssistant, callback
from webrtc_models import RTCIceCandidateInit

_LOGGER = logging.getLogger(__name__)
_MessageT = TypeVar("_MessageT")

_webrtc_client_configuration: ContextVar[WebRTCClientConfiguration | None] = ContextVar(
	"webrtc_client_configuration", default=None
)


class WebRTCClientConfigurationMixin:
	"""Mixin for returning request-local WebRTC client configuration."""

	@callback
	def _async_get_webrtc_client_configuration(self) -> WebRTCClientConfiguration:
		"""Return the request-local provider config for the browser peer connection."""
		return _webrtc_client_configuration.get() or WebRTCClientConfiguration()


class MessageQueue(Generic[_MessageT]):
	"""Queue messages until signaling is ready to send them."""

	def __init__(self) -> None:
		self._pending: list[_MessageT] = []
		self._sender: Callable[[_MessageT], Awaitable[None]] | None = None

	async def add(self, message: _MessageT) -> None:
		"""Add a message or send it immediately when a sender is available."""
		if self._sender is None:
			self._pending.append(message)
			return
		await self._sender(message)

	async def flush(self, sender: Callable[[_MessageT], Awaitable[None]]) -> None:
		"""Send queued messages and use sender for future messages."""
		self._sender = sender
		pending = self._pending
		self._pending = []
		for message in pending:
			await sender(message)

	def clear(self) -> None:
		"""Drop queued messages and reset the sender."""
		self._pending.clear()
		self._sender = None

	@property
	def flushed(self) -> bool:
		"""Return whether messages are sent immediately."""
		return self._sender is not None


class SimpliSafeWebRTCSession(ABC):
	"""A provider-specific WebRTC session."""

	@abstractmethod
	def start(self, on_close: Callable[[], None]) -> None:
		"""Start provider signaling before the browser offer arrives."""

	@abstractmethod
	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		"""Handle a browser offer for this session."""

	@abstractmethod
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Send a browser ICE candidate to the provider session."""

	@abstractmethod
	def close(self) -> None:
		"""Close the session."""


async def async_register_webrtc_client_config_handler(
	hass: HomeAssistant, _component: str
) -> None:
	"""Replace a registered Home Assistant websocket handler with a wrapped one."""
	command = "camera/webrtc/get_client_config"

	if (
		handlers := hass.data.get(websocket_api.DOMAIN)
	) is None or command not in handlers:
		_LOGGER.warning("Cannot wrap missing Home Assistant websocket command %s", command)
		return

	handler, schema = handlers[command]
	if not (async_handler := getattr(handler, "__wrapped__", None)):
		_LOGGER.warning(
			"Cannot wrap Home Assistant websocket command %s without async handler",
			command,
		)
		return

	@websocket_api.async_response  # pyright: ignore[reportPrivateImportUsage]
	@wraps(async_handler)
	async def wrapped(
		hass: HomeAssistant,
		connection: websocket_api.ActiveConnection,  # pyright: ignore[reportPrivateImportUsage]
		msg: dict[str, Any],
	) -> None:
		config: WebRTCClientConfiguration | None = None
		camera = get_camera_from_entity_id(hass, msg["entity_id"])
		if async_prepare_config := getattr(
			camera, "async_prepare_webrtc_client_configuration", None
		):
			config = await async_prepare_config()

		token = _webrtc_client_configuration.set(config)
		try:
			await async_handler(hass, connection, msg)
		finally:
			_webrtc_client_configuration.reset(token)

	websocket_api.async_register_command(hass, command, wrapped, schema)

	_LOGGER.debug("Wrapped Home Assistant WebRTC client config websocket handler")
