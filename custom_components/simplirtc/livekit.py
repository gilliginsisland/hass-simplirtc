"""Support for livekit webrtc streams."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
)

from homeassistant.components.camera import (
	WebRTCAnswer,
	WebRTCCandidate,
	WebRTCSendMessage,
	RTCIceCandidateInit,
)

_LOGGER = logging.getLogger(__name__)


class LiveKitSession:
	def __init__(
		self,
		session_id: str,
		send_message: WebRTCSendMessage,
		livekit_url: str,
		user_token: str,
	) -> None:
		self._session_id = session_id
		self._send_message = send_message
		self._session: ClientSession | None = None
		self._ws: ClientWebSocketResponse | None = None
		self._ready_event = asyncio.Event()
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._ws_endpoint = f'{livekit_url}/rtc?access_token={user_token}&sdk=js&version=2.13.4&protocol=16'

	async def stream(self, offer_sdp: str) -> None:
		try:
			self._session = session = ClientSession()
			self._ws = ws = await session.ws_connect(self._ws_endpoint)
			self._logger.debug("-> %s", offer_sdp)

			self._reader_task = asyncio.create_task(self._read())
		except:
			await self.close()

	async def _read(self) -> None:
		try:
			assert self._ws is not None, "WebSocket connection not established"
			async for msg in self._ws:
				self._logger.debug("<- %s", msg.data)

				if msg.data == "":
					continue  # Ignore empty messages

		finally:
			self._reader_task = None
			await self.close()

	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		await self._ready_event.wait()

		assert self._ws, "WebSocket not available"

		self._logger.debug("-> %s", candidate)

	async def close(self) -> None:
		if self._reader_task:
			self._reader_task.cancel()
			try:
				await self._reader_task
			except asyncio.CancelledError:
				pass
		if self._ws:
			await self._ws.close()
			self._ws = None
		if self._session:
			await self._session.close()
			self._session = None
