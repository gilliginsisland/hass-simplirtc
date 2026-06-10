"""Live-media SFU built on raw RTP peer connections."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import logging
from typing import Any
import uuid

from aiortc import (
	RTCConfiguration,
	RTCIceCandidate,
	RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

from .rtp_router import (
	RawRtpPeerConnection,
	RawRtpProducer,
)

_LOGGER = logging.getLogger(__name__)

ProducerTeardown = Callable[[], None]
SetupProducer = Callable[[RawRtpPeerConnection], Coroutine[Any, Any, ProducerTeardown]]


def ice_candidate_from_sdp(
	candidate: str,
	*,
	sdp_mid: str | None = None,
	sdp_m_line_index: int | None = None,
) -> RTCIceCandidate | None:
	"""Parse an SDP ICE candidate string for aiortc."""
	if not candidate:
		return None
	aiortc_candidate = candidate_from_sdp(candidate.removeprefix("candidate:"))
	aiortc_candidate.sdpMid = sdp_mid
	aiortc_candidate.sdpMLineIndex = sdp_m_line_index
	return aiortc_candidate


class RawRtpSfu:
	"""Fan out one raw RTP producer peer connection to consumer peer connections."""

	def __init__(
		self,
		*,
		setup_producer_pc: SetupProducer,
		idle_timeout: float = 30,
	) -> None:
		self._setup_producer_pc = setup_producer_pc
		self._idle_timeout = idle_timeout
		self._logger = _LOGGER.getChild("sfu")
		self._rtp_producer = RawRtpProducer()
		self._rtc_configuration = RTCConfiguration()
		self._close_lock = asyncio.Lock()
		self._consumers: dict[str, RawRtpPeerConnection] = {}
		self._pending_consumer_candidates: dict[str, list[RTCIceCandidate | None]] = {}
		self._producer_pc: RawRtpPeerConnection | None = None
		self._producer_setup_task: asyncio.Task[None] | None = None
		self._producer_teardown: ProducerTeardown | None = None
		self._idle_task: asyncio.Task[None] | None = None

	async def create_session(
		self,
		offer_sdp: str,
		*,
		peer_id: str,
	) -> str:
		"""Create a consumer session and return its SDP answer."""
		self.close_session(peer_id)
		self._consumers[peer_id] = consumer = self._create_consumer(peer_id)
		try:
			return await self._answer_offer(consumer, offer_sdp)
		except BaseException:
			self.close_consumer(consumer)
			raise

	def _create_consumer(self, peer_id: str) -> RawRtpPeerConnection:
		"""Create a consumer peer connection."""
		if self._idle_task:
			self._idle_task.cancel()
			self._idle_task = None

		consumer = RawRtpPeerConnection(
			producer=self._rtp_producer,
			side="consumer",
			peer_id=peer_id,
			configuration=self._rtc_configuration,
		)

		@consumer.on("connectionstatechange")
		async def on_consumer_connectionstatechange() -> None:
			state = consumer.connectionState
			self._logger.debug("Consumer %s connectionState=%s", peer_id, state)
			if state in {"failed", "closed"}:
				self.close_consumer(consumer)

		return consumer

	async def add_candidate(
		self,
		peer_id: str,
		candidate: str,
		*,
		sdp_mid: str | None = None,
		sdp_m_line_index: int | None = None,
	) -> None:
		"""Add a consumer ICE candidate by peer ID."""
		if not (consumer := self._consumers.get(peer_id)):
			self._logger.debug("Ignoring ICE candidate for closed consumer %s", peer_id)
			return
		aiortc_candidate = ice_candidate_from_sdp(
			candidate,
			sdp_mid=sdp_mid,
			sdp_m_line_index=sdp_m_line_index,
		)
		if not consumer.remoteDescription:
			self._pending_consumer_candidates.setdefault(peer_id, []).append(aiortc_candidate)
			return
		await consumer.addIceCandidate(aiortc_candidate)

	async def _answer_offer(
		self,
		consumer: RawRtpPeerConnection,
		offer_sdp: str,
	) -> str:
		"""Answer a consumer peer connection offer."""
		await self._ensure_producer()
		await consumer.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
		for candidate in self._pending_consumer_candidates.pop(consumer.peer_id, ()):
			await consumer.addIceCandidate(candidate)
		if not (consumer_media_kinds := consumer.remote_media_kinds()):
			raise RuntimeError("Consumer offer has no media sections")
		if unsupported := tuple(
			kind for kind in consumer_media_kinds
			if not consumer.remote_supported_codecs(kind)
		):
			raise RuntimeError(
				f"Consumer offer contains unsupported media types: {', '.join(unsupported)}"
			)

		for kind in consumer_media_kinds:
			consumer.set_answer_direction(kind, "sendonly")
		await consumer.setLocalDescription()
		if not (local_description := consumer.localDescription):
			raise RuntimeError("Consumer peer connection did not create an SDP answer")

		return local_description.sdp

	def close_session(self, peer_id: str) -> None:
		"""Close a consumer session by peer ID."""
		if consumer := self._consumers.get(peer_id):
			self.close_consumer(consumer)

	def close_consumer(self, consumer: RawRtpPeerConnection) -> None:
		"""Close a consumer session if it is still the current session for its peer ID."""
		if self._consumers.get(consumer.peer_id) is not consumer:
			return
		del self._consumers[consumer.peer_id]
		self._pending_consumer_candidates.pop(consumer.peer_id, None)

		def _log_close_error(task: asyncio.Task[None]) -> None:
			if task.cancelled():
				return
			try:
				task.result()
			except BaseException as err:
				self._logger.error(
					"Error closing raw RTP SFU consumer %s: %s",
					consumer.peer_id,
					err,
				)

		close_task = asyncio.create_task(consumer.close())
		close_task.add_done_callback(_log_close_error)

		if self._consumers or not self._producer_pc or self._idle_task:
			return

		self._idle_task = idle_task = asyncio.create_task(self._close_when_idle())

		def _log_idle_close_error(task: asyncio.Task[None]) -> None:
			if task.cancelled():
				return
			try:
				task.result()
			except BaseException as err:
				self._logger.error("Error closing idle raw RTP SFU producer: %s", err)

		idle_task.add_done_callback(_log_idle_close_error)

	async def _ensure_producer(self) -> None:
		if self._producer_pc and self._producer_teardown:
			return
		if not (producer_task := self._producer_setup_task):
			producer_task = self._producer_setup_task = asyncio.create_task(self._setup_producer())
		return await asyncio.shield(producer_task)

	async def _setup_producer(self) -> None:
		"""Start a producer peer connection and keep its teardown callback."""
		producer_peer_id = f"producer-{uuid.uuid4()}"
		self._producer_pc = producer_pc = RawRtpPeerConnection(
			producer=self._rtp_producer,
			side="producer",
			peer_id=producer_peer_id,
			configuration=self._rtc_configuration,
		)

		@producer_pc.on("connectionstatechange")
		async def on_producer_connectionstatechange() -> None:
			state = producer_pc.connectionState
			self._logger.debug("Producer connectionState=%s", state)
			if self._producer_pc is not producer_pc or state not in {"failed", "closed"}:
				return
			task = asyncio.create_task(self._close_failed_producer(producer_pc))

			def log_task_error(task: asyncio.Task[None]) -> None:
				if task.cancelled():
					return
				try:
					task.result()
				except BaseException as err:
					self._logger.error("Error closing failed raw RTP SFU producer: %s", err)

			task.add_done_callback(log_task_error)

		try:
			producer_teardown = await self._setup_producer_pc(producer_pc)
		except BaseException:
			if self._producer_pc is producer_pc:
				self._producer_pc = None
			await producer_pc.close()
			raise
		finally:
			if self._producer_setup_task is asyncio.current_task():
				self._producer_setup_task = None

		if self._producer_pc is producer_pc:
			self._producer_teardown = producer_teardown
			return
		producer_teardown()
		await producer_pc.close()

	async def _close_failed_producer(self, producer_pc: RawRtpPeerConnection) -> None:
		"""Close current consumers when the producer connection stops."""
		if self._producer_pc is not producer_pc:
			return
		await self._close_producer()
		for consumer in tuple(self._consumers.values()):
			self.close_consumer(consumer)

	async def close(self) -> None:
		"""Close the producer and all consumers."""
		async with self._close_lock:
			consumers = []
			for peer_id, consumer in tuple(self._consumers.items()):
				if self._consumers.get(peer_id) is consumer:
					del self._consumers[peer_id]
					self._pending_consumer_candidates.pop(peer_id, None)
					consumers.append(consumer)
			results = await asyncio.gather(
				*(consumer.close() for consumer in consumers),
				return_exceptions=True,
			)
			results += await self._close_producer()
			if errors := tuple(
				result for result in results
				if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
			):
				raise BaseExceptionGroup("Error closing raw RTP SFU", errors)
			self._logger.debug("Closed raw RTP SFU")

	async def _close_when_idle(self) -> None:
		await asyncio.sleep(self._idle_timeout)
		if not self._consumers:
			await self._close_producer()

	async def _close_producer(self) -> tuple[BaseException | None, ...]:
		"""Close the current producer and return cleanup results."""
		if (idle_task := self._idle_task) and idle_task is not asyncio.current_task():
			idle_task.cancel()
		self._idle_task = None

		producer_pc, self._producer_pc = self._producer_pc, None
		producer_task, self._producer_setup_task = self._producer_setup_task, None
		producer_teardown, self._producer_teardown = self._producer_teardown, None
		results: list[BaseException | None] = []

		if producer_task:
			if not producer_task.done():
				producer_task.cancel()
			try:
				await producer_task
			except BaseException as err:
				results.append(err)

		if producer_teardown:
			try:
				producer_teardown()
			except BaseException as err:
				results.append(err)
		if producer_pc:
			try:
				await producer_pc.close()
			except BaseException as err:
				results.append(err)
		return tuple(results)
