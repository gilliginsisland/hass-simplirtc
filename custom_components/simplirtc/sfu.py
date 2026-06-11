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
	RawRtpRouter,
)

_LOGGER = logging.getLogger(__name__)

SetupProducer = Callable[[RawRtpPeerConnection], Coroutine[Any, Any, None]]


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
		self._rtp_router = RawRtpRouter()
		self._rtc_configuration = RTCConfiguration()
		self._consumers: dict[str, RawRtpPeerConnection] = {}
		self._producer_pc: RawRtpPeerConnection | None = None
		self._producer_setup_task: asyncio.Task[None] | None = None
		self._producer_close_task: asyncio.Task[None] | None = None
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
			if producer_close_task := self._producer_close_task:
				await asyncio.shield(producer_close_task)
				self._producer_close_task = None

			await self._ensure_producer()

			await consumer.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
			await consumer.add_pending_remote_candidates()
			await consumer.setLocalDescription()
			if not (local_description := consumer.localDescription):
				raise RuntimeError("Consumer peer connection did not create an SDP answer")
			return local_description.sdp
		except BaseException:
			self.close_session(peer_id)
			raise

	def _create_consumer(self, peer_id: str) -> RawRtpPeerConnection:
		"""Create a consumer peer connection."""
		if self._idle_task:
			self._idle_task.cancel()
			self._idle_task = None

		consumer = RawRtpPeerConnection(
			configuration=self._rtc_configuration,
		)
		self._rtp_router.addOutput(
			consumer,
			peer_id=peer_id,
		)

		@consumer.on("connectionstatechange")
		async def on_consumer_connectionstatechange() -> None:
			state = consumer.connectionState
			self._logger.debug("Consumer %s connectionState=%s", peer_id, state)
			if state in {"failed", "closed"}:
				self.close_session(consumer.peer_id)

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
		aiortc_candidate = ice_candidate_from_sdp(
			candidate,
			sdp_mid=sdp_mid,
			sdp_m_line_index=sdp_m_line_index,
		)

		if consumer := self._consumers.get(peer_id):
			await consumer.addIceCandidate(aiortc_candidate)
			return

		self._logger.debug("Ignoring ICE candidate for closed consumer %s", peer_id)

	def close_session(self, peer_id: str) -> None:
		"""Close a consumer session by peer ID."""
		if consumer := self._consumers.pop(peer_id, None):
			asyncio.create_task(consumer.close())

		if (
			not self._consumers
			and (self._producer_pc or self._producer_setup_task)
			and not self._idle_task
			and not self._producer_close_task
		):
			self._idle_task = asyncio.create_task(self._close_when_idle())

	async def _ensure_producer(self) -> None:
		if self._producer_pc:
			return
		if not (producer_task := self._producer_setup_task):
			producer_task = self._producer_setup_task = asyncio.create_task(self._setup_producer())
		return await asyncio.shield(producer_task)

	async def _setup_producer(self) -> None:
		"""Start a producer peer connection and keep its teardown callback."""
		producer_peer_id = f"producer-{uuid.uuid4()}"
		producer_pc = RawRtpPeerConnection(
			configuration=self._rtc_configuration,
		)
		self._rtp_router.addInput(
			producer_pc,
			peer_id=producer_peer_id,
		)
		setup_producer_pc_task: asyncio.Task[None] | None = None

		@producer_pc.on("connectionstatechange")
		async def on_producer_connectionstatechange() -> None:
			nonlocal setup_producer_pc_task
			state = producer_pc.connectionState
			self._logger.debug("Producer connectionState=%s", state)
			if state not in {"failed", "closed"}:
				return
			if self._producer_pc is producer_pc:
				self.close()
				return
			if task := setup_producer_pc_task:
				setup_producer_pc_task = None
				task.cancel()

		setup_producer_pc_task = asyncio.create_task(self._setup_producer_pc(producer_pc))

		try:
			await setup_producer_pc_task
		except BaseException:
			setup_producer_pc_task = None
			await producer_pc.close()
			raise
		finally:
			self._producer_setup_task = None

		self._producer_pc = producer_pc

	async def _close_when_idle(self) -> None:
		await asyncio.sleep(self._idle_timeout)
		if not self._consumers:
			self.close()
		self._idle_task = None

	async def _close_producer(self) -> None:
		"""Close the current producer."""

		if consumers := self._consumers:
			for peer_id, consumer in tuple(consumers.items()):
				if consumer.remoteDescription:
					self.close_session(peer_id)

		tasks: list[asyncio.Task[Any]] = []

		if idle_task := self._idle_task:
			self._idle_task = None
			idle_task.cancel()
			tasks.append(idle_task)

		if producer_task := self._producer_setup_task:
			self._producer_setup_task = None
			producer_task.cancel()
			tasks.append(producer_task)

		if producer_pc := self._producer_pc:
			self._producer_pc = None
			tasks.append(asyncio.create_task(producer_pc.close()))

		await asyncio.gather(*tasks, return_exceptions=True)

	def close(self) -> None:
		"""Schedule active producer and consumer shutdown."""
		if not self._producer_close_task:
			self._producer_close_task = asyncio.create_task(self._close_producer())
