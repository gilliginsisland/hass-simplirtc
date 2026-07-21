"""Support for kinesis webrtc streams."""

from __future__ import annotations

from abc import ABC
import asyncio
import base64
from collections.abc import Callable
from dataclasses import asdict
import json
import logging
from secrets import token_hex
from typing import Any, override

from aiohttp import ClientSession
from homeassistant.components.camera import (
	WebRTCAnswer,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCCandidate,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass
from webrtc_models import RTCIceCandidateInit

from .webrtc import MessageQueue, SimpliSafeWebRTCSession

_LOGGER = logging.getLogger(__name__)

SessionClosedCallback = Callable[[], None]


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


class KinesisSession(SimpliSafeWebRTCSession):
	def __init__(
		self,
		*,
		channel_endpoint: str,
		client_id: str,
	) -> None:
		self._channel_endpoint = channel_endpoint
		self._client_id = client_id
		self._id = token_hex(4)
		self._request_queue = MessageQueue[KinesisRequest]()
		self._logger = _LOGGER.getChild(f"session.{self._id}")
		self._message_count = 0

		self._send_message: WebRTCSendMessage | None = None
		self._on_close: SessionClosedCallback | None = None
		self._reader_task: asyncio.Task[None] | None = None

	@override
	def start(self, on_close: SessionClosedCallback) -> None:
		"""Start Kinesis signaling in the background."""
		if self._reader_task is not None:
			raise RuntimeError(f"Kinesis session {self._id} already started")

		self._on_close = on_close

		async def reader_task() -> None:
			try:
				await self._read()
			except Exception as err:
				self._logger.error("Error in Kinesis session: %s", err)
			finally:
				self._reader_task = None
				self.close()

		self._reader_task = asyncio.create_task(
			reader_task(), name=f"simplirtc-kinesis-{self._id}"
		)

	@override
	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		"""Handle a browser offer on the running Kinesis session."""
		if self._reader_task is None:
			raise RuntimeError(f"Kinesis session {self._id} was not started")

		self._send_message = send_message

		offer_msg = KinesisRequest(
			action="SDP_OFFER",
			recipientClientId=self._client_id,
			correlationId=f"{self._id}.{self._next_correlation_id()}",
		)
		offer_msg.payload = {"type": "offer", "sdp": offer_sdp}
		await self._request_queue.add(offer_msg)

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		candidate_msg = KinesisRequest(
			action="ICE_CANDIDATE",
			recipientClientId=self._client_id,
			correlationId=f"{self._id}.{self._next_correlation_id()}",
		)
		candidate_msg.payload = {
			"candidate": candidate.candidate,
			"sdpMid": candidate.sdp_mid,
			"sdpMLineIndex": candidate.sdp_m_line_index,
			"usernameFragment": None,
		}
		await self._request_queue.add(candidate_msg)

	@override
	def close(self) -> None:
		if (task := self._reader_task) is not None:
			self._reader_task = None
			task.cancel()
		self._request_queue.clear()
		if on_close := self._on_close:
			self._on_close = None
			on_close()

	def _next_correlation_id(self) -> int:
		self._message_count += 1
		return self._message_count

	async def _read(self) -> None:
		async with (
			ClientSession() as session,
			session.ws_connect(self._channel_endpoint) as ws,
		):
			async def send_request(request: KinesisRequest) -> None:
				self._logger.debug("-> %s", request)
				await ws.send_json(asdict(request))

			await self._request_queue.flush(send_request)
			async for msg in ws:
				self._logger.debug("<- %s", msg.data)

				if msg.data == "":
					continue  # Ignore empty messages

				try:
					parsed = TypeAdapter(KinesisResponse).validate_json(msg.data)
				except Exception:
					self._logger.exception("failed to parse message")
					continue

				payload = parsed.payload
				if not (send_message := self._send_message):
					continue
				match parsed.messageType:
					case "SDP_ANSWER":
						send_message(WebRTCAnswer(answer=payload["sdp"]))
					case "ICE_CANDIDATE":
						send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
							candidate=payload.get("candidate", ""),
							sdp_mid=payload.get("sdpMid"),
							sdp_m_line_index=payload.get("sdpMLineIndex"),
						)))
					case _:
						continue
