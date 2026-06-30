"""Support for kinesis webrtc streams."""

from __future__ import annotations

from abc import ABC
import asyncio
import base64
from collections.abc import Callable
from dataclasses import asdict
import json
import logging
from typing import Any

from aiohttp import ClientSession
from pydantic import TypeAdapter
from pydantic.dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

SendAnswer = Callable[[str], None]
SendCandidate = Callable[[str, str | None, int | None], None]


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
		*,
		session_id: str,
		channel_endpoint: str,
		client_id: str,
		send_answer: SendAnswer,
		send_candidate: SendCandidate,
	) -> None:
		self._session_id = session_id
		self._channel_endpoint = channel_endpoint
		self._client_id = client_id
		self._send_answer = send_answer
		self._send_candidate = send_candidate
		self._candidate_queue: asyncio.Queue[KinesisRequest] = asyncio.Queue()
		self._reader_task: asyncio.Task[None] | None = None
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._message_count = 0

	def start(self, offer_sdp: str) -> None:
		"""Start streaming in the background."""
		self._reader_task = task = asyncio.create_task(self._read(offer_sdp))
		def log_task_error(task: asyncio.Task[None]) -> None:
			if self._reader_task is task:
				self._reader_task = None
			if not task.cancelled():
				try:
					task.result()
				except BaseException as err:
					self._logger.error("Error in Kinesis session: %s", err)
		task.add_done_callback(log_task_error)

	async def send_candidate(
		self,
		candidate: str,
		*,
		sdp_mid: str | None = None,
		sdp_m_line_index: int | None = None,
	) -> None:
		candidate_msg = KinesisRequest(
			action="ICE_CANDIDATE",
			recipientClientId=self._client_id,
			correlationId=f"{self._session_id}.{self._next_correlation_id()}",
		)
		candidate_msg.payload = {
			"candidate": candidate,
			"sdpMid": sdp_mid,
			"sdpMLineIndex": sdp_m_line_index,
			"usernameFragment": None,
		}
		await self._candidate_queue.put(candidate_msg)

	def close(self) -> None:
		if self._reader_task:
			self._reader_task.cancel()

	def _next_correlation_id(self) -> int:
		self._message_count += 1
		return self._message_count

	async def _read(self, offer_sdp: str) -> None:
		offer_msg = KinesisRequest(
			action="SDP_OFFER",
			recipientClientId=self._client_id,
			correlationId=f"{self._session_id}.{self._next_correlation_id()}",
		)
		offer_msg.payload = {"type": "offer", "sdp": offer_sdp}

		async with (
			ClientSession() as session,
			session.ws_connect(self._channel_endpoint) as ws,
		):
			self._logger.debug("-> %s", offer_msg)
			await ws.send_json(asdict(offer_msg))

			async def send_kinesis_candidates() -> None:
				while True:
					candidate_msg = await self._candidate_queue.get()
					self._logger.debug("-> %s", candidate_msg)
					await ws.send_json(asdict(candidate_msg))

			async with asyncio.TaskGroup() as task_group:
				task_group.create_task(send_kinesis_candidates())
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
					match parsed.messageType:
						case "SDP_ANSWER":
							self._send_answer(payload["sdp"])
						case "ICE_CANDIDATE":
							self._send_candidate(
								payload.get("candidate", ""),
								payload.get("sdpMid"),
								payload.get("sdpMLineIndex"),
							)
						case _:
							continue
