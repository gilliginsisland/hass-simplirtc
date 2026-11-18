"""Support for kinesis webrtc streams."""

from __future__ import annotations

import base64
from dataclasses import asdict
import json
import logging
from secrets import token_hex
from typing import Any, override

from aiohttp import ClientSession, ClientWebSocketResponse
from homeassistant.components.camera import (
	WebRTCAnswer,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCCandidate,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass
from webrtc_models import RTCIceCandidateInit

from .webrtc import SimpliSafeWebRTCSession

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True, slots=True, config={"extra": "ignore"})
class KinesisMessage:
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


class KinesisSession(SimpliSafeWebRTCSession[KinesisRequest]):
	def __init__(
		self,
		*,
		channel_endpoint: str,
		client_id: str,
	) -> None:
		self._channel_endpoint = channel_endpoint
		self._client_id = client_id
		self._id = token_hex(4)
		super().__init__(logger=_LOGGER.getChild(f"session.{self._id}"), name=f"simplirtc-kinesis-{self._id}")
		self._message_count = 0

	@override
	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		"""Handle a browser offer on the running Kinesis session."""
		self._accept_offer(send_message)

		offer_msg = KinesisRequest(
			action="SDP_OFFER",
			recipientClientId=self._client_id,
			correlationId=f"{self._id}.{self._next_correlation_id()}",
		)
		offer_msg.payload = {"type": "offer", "sdp": offer_sdp}
		self._request_queue.put_nowait(offer_msg)

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		self._check_open()
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
		self._request_queue.put_nowait(candidate_msg)

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

			async with self._request_queue.sending(send_request):
				await self._receive(ws)

	async def _receive(self, ws: ClientWebSocketResponse) -> None:
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
