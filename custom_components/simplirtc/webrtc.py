"""WebRTC setup helpers for SimpliRTC."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
import logging
from typing import Any, Generic, Self, TypeVar

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
	"""Buffer messages until a scoped sender is available."""

	def __init__(self) -> None:
		self._pending: asyncio.Queue[_MessageT] = asyncio.Queue()
		self._sending = False

	async def add(self, message: _MessageT) -> None:
		"""Enqueue without doing network I/O in a Home Assistant callback."""
		self._pending.put_nowait(message)

	@asynccontextmanager
	async def sending(self, sender: Callable[[_MessageT], Awaitable[None]]) -> AsyncIterator[None]:
		"""Own the sender task for exactly the surrounding connection scope."""
		if self._sending:
			raise RuntimeError("A sender is already running")
		async def send_loop() -> None:
			while True:
				await sender(await self._pending.get())

		async with asyncio.TaskGroup() as tasks:
			task = tasks.create_task(send_loop(), name="simplirtc-signaling-sender")
			self._sending = True
			try:
				yield
			finally:
				self._sending = False
				task.cancel()

	def clear(self) -> None:
		"""Drop messages after the session owner has finished."""
		while not self._pending.empty():
			self._pending.get_nowait()

	@property
	def flushed(self) -> bool:
		"""Return whether this connection has a sender."""
		return self._sending


class SimpliSafeWebRTCSession(ABC, Generic[_MessageT]):
	"""Own a provider's connection scopes across independent HA requests."""

	def __init__(self, *, logger: logging.Logger, name: str) -> None:
		self._logger = logger
		self._name = name
		self._task: asyncio.Task[None] | None = None
		self._on_close: Callable[[], None] | None = None
		self._closed = asyncio.Event()
		self._close_requested = False
		self._offer_received = asyncio.Event()
		self._send_message: WebRTCSendMessage | None = None
		self._request_queue = MessageQueue[_MessageT]()

	async def __aenter__(self) -> Self:
		self.start(lambda: None)
		return self

	async def __aexit__(self, *exc: Any) -> None:
		self.close()
		await self.wait_closed()

	@property
	def closing(self) -> bool:
		"""Return whether shutdown was requested or the owner has exited."""
		return self._close_requested or (self._task is not None and self._task.done())

	def start(
		self, on_close: Callable[[], None], *,
		create_task: Callable[..., asyncio.Task[None]] = asyncio.create_task,
		unused_timeout: float | None = None,
	) -> None:
		"""Start one lifetime owner, optionally tied to HA's config entry."""
		if self._task is not None or self._close_requested:
			raise RuntimeError("Session has already been started or closed")
		self._on_close = on_close
		owner = self._run(unused_timeout)
		try:
			self._task = create_task(owner, name=self._name)
		except BaseException:
			owner.close()
			self._close_requested = True
			self._closed.set()
			self._on_close = None
			raise
		# A done callback also runs when canceled before the coroutine first starts.
		self._task.add_done_callback(self._finished)

	async def _run(self, unused_timeout: float | None) -> None:
		try:
			async with asyncio.TaskGroup() as tasks:
				expiration = None
				if unused_timeout is not None:
					expiration = tasks.create_task(
						self._expire_unused(unused_timeout), name="simplirtc-unused-session",
					)
				try:
					await self._read()
				finally:
					if expiration is not None:
						expiration.cancel()
		except Exception:
			self._logger.exception("Signaling session failed")

	async def _expire_unused(self, timeout: float) -> None:
		try:
			async with asyncio.timeout(timeout):
				await self._offer_received.wait()
		except TimeoutError:
			self.close()

	def _finished(self, _task: asyncio.Task[None]) -> None:
		"""Publish closure only after every owned context has exited."""
		self._close_requested = True
		self._request_queue.clear()
		self._send_message = None
		self._closed.set()
		if on_close := self._on_close:
			self._on_close = None
			on_close()

	def _check_open(self) -> None:
		if self._task is None or self.closing:
			raise RuntimeError("Session is not running")

	def _accept_offer(self, send_message: WebRTCSendMessage) -> None:
		self._check_open()
		self._send_message = send_message
		self._offer_received.set()

	def close(self) -> None:
		"""Request shutdown; repeated requests cannot interrupt cleanup."""
		if self._close_requested:
			return
		self._close_requested = True
		if self._task is not None:
			if not self._task.cancelling():
				self._task.cancel()
		else:
			self._closed.set()

	async def wait_closed(self) -> None:
		"""Wait for cleanup without propagating caller cancellation to the owner."""
		await self._closed.wait()

	@abstractmethod
	async def _read(self) -> None:
		"""Run the provider's connection contexts and receive loop."""

	@abstractmethod
	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		"""Queue a browser offer for this session."""

	@abstractmethod
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Queue a browser ICE candidate for this session."""


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
