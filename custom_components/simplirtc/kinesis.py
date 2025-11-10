"""Support for kinesis webrtc streams."""

from __future__ import annotations

from abc import ABC
from typing import Any
import asyncio
import json
import base64
import logging
from dataclasses import asdict

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
)
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass

from homeassistant.components.camera import (
	WebRTCAnswer,
	WebRTCCandidate,
	WebRTCSendMessage,
	RTCIceCandidateInit,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True, slots=True, config={"extra": "ignore"})
class KinesisMessage(ABC):
	messagePayload: str | None = None

	@property
	def payload(self) -> Any:
		if not self.messagePayload:
			return None
		return json.loads(base64.b64decode(self.messagePayload).decode())

	@payload.setter
	def payload(self, value: Any) -> None:
		self.messagePayload = base64.b64encode(json.dumps(value).encode()).decode()


@dataclass(kw_only=True, slots=True, config={"extra": "ignore"})
class KinesisRequest(KinesisMessage):
	action: str
	recipientClientId: str
	correlationId: str


@dataclass(kw_only=True, slots=True, config={"extra": "ignore"})
class KinesisResponse(KinesisMessage):
	messageType: str
	statusResponse: Any = None


class KinesisSession:
	def __init__(
		self,
		session_id: str,
		channel_endpoint: str,
		client_id: str,
		send_message: WebRTCSendMessage,
	) -> None:
		self._session_id = session_id
		self._channel_endpoint = channel_endpoint
		self._client_id = client_id
		self._send_message = send_message
		self._session: ClientSession | None = None
		self._ws: ClientWebSocketResponse | None = None
		self._ready_event = asyncio.Event()
		self._reader_task: asyncio.Task | None = None
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._message_count = 0

	def _next_correlation_id(self) -> int:
		self._message_count += 1
		return self._message_count

	async def stream(self, offer_sdp: str) -> None:
		(offer_msg := KinesisRequest(
			action="SDP_OFFER",
			recipientClientId=self._client_id,
			correlationId=f'{self._session_id}.{self._next_correlation_id()}',
		)).payload={"type": "offer", "sdp": offer_sdp}

		try:
			self._session = session = ClientSession()
			self._ws = ws = await session.ws_connect(self._channel_endpoint)
			self._logger.debug("-> %s", offer_msg)

			await ws.send_json(asdict(offer_msg))
			self._reader_task = asyncio.create_task(self._read())
		except:
			await self.close()
		finally:
			self._ready_event.set()

	async def _read(self) -> None:
		try:
			assert self._ws is not None, "WebSocket connection not established"
			async for msg in self._ws:
				self._logger.debug("<- %s", msg.data)

				if msg.data == "":
					continue  # Ignore empty messages

				try:
					parsed = TypeAdapter(KinesisResponse).validate_json(msg.data)
				except Exception:
					self._logger.exception("failed to parse message")
					continue

				payload = parsed.payload
				match parsed.messageType:
					case "SDP_ANSWER":
						self._send_message(WebRTCAnswer(payload["sdp"]))
					case "ICE_CANDIDATE":
						candidate = RTCIceCandidateInit(
							candidate=payload.get("candidate"),
							sdp_mid=payload.get("sdpMid"),
							sdp_m_line_index=payload.get("sdpMLineIndex"),
						)
						self._send_message(WebRTCCandidate(candidate))

		finally:
			self._reader_task = None
			await self.close()

	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		await self._ready_event.wait()

		assert self._ws, "WebSocket not available"

		(candidate_msg := KinesisRequest(
			action="ICE_CANDIDATE",
			recipientClientId=self._client_id,
			correlationId=f'{self._session_id}.{self._next_correlation_id()}',
		)).payload={
			"candidate": candidate.candidate,
			"sdpMid": candidate.sdp_mid,
			"sdpMLineIndex": candidate.sdp_m_line_index,
			"usernameFragment": None,
		}

		self._logger.debug("-> %s", candidate_msg)
		await self._ws.send_json(asdict(candidate_msg))

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
