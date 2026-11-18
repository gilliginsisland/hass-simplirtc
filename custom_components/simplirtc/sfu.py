"""Own one raw RTP producer and its browser-facing consumers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
import logging
from uuid import uuid4

from aiortc import RTCBundlePolicy, RTCConfiguration, RTCSessionDescription

from .rtp_router import RawRtpBridge, RawRtpPeerConnection

_LOGGER = logging.getLogger(__name__)
ProducerConnection = Callable[[RawRtpPeerConnection], AbstractAsyncContextManager[None]]


class RawRtpSfu:
	"""Share an upstream connection until the last viewer has been gone for 30s."""

	def __init__(
		self, connect_producer: ProducerConnection, *, idle_timeout: float = 30,
		create_task: Callable[..., asyncio.Task[None]] = asyncio.create_task,
	) -> None:
		self._connect_producer = connect_producer
		self._create_task = create_task
		self._idle_timeout = idle_timeout
		self._bridge = RawRtpBridge()
		self._consumers: set[RawRtpPeerConnection] = set()
		self._changed = asyncio.Event()
		self._producer_task: asyncio.Task[None] | None = None
		self._ready: asyncio.Future[RTCConfiguration] | None = None
		self._closing = False

	async def _prepare(self) -> tuple[RTCConfiguration, asyncio.Task[None]]:
		if self._closing:
			raise RuntimeError("SFU is closing")
		if self._producer_task is None or self._producer_task.done():
			self._ready = asyncio.get_running_loop().create_future()
			self._producer_task = self._create_task(
				self._run_producer(self._ready), name="simplirtc-sfu-producer",
			)
		assert self._ready is not None
		return await asyncio.shield(self._ready), self._producer_task

	async def _run_producer(self, ready: asyncio.Future[RTCConfiguration]) -> None:
		producer = RawRtpPeerConnection(
			bridge=self._bridge, side="producer", peer_id=f"producer-{uuid4()}",
			configuration=RTCConfiguration(bundlePolicy=RTCBundlePolicy.MAX_BUNDLE),
		)
		try:
			async with self._connect_producer(producer):
				# The provider must configure TURN before yielding. Consumers are only
				# constructed after this point, so neither leg gathers with defaults.
				ready.set_result(producer.rtc_configuration)
				while True:
					self._changed.clear()
					if self._consumers:
						await self._changed.wait()
					else:
						try:
							async with asyncio.timeout(self._idle_timeout):
								await self._changed.wait()
						except TimeoutError:
							return
		except asyncio.CancelledError:
			raise
		except Exception as err:
			if not ready.done():
				ready.set_exception(err)
				# Retrieve even if every waiting viewer was canceled during setup.
				ready.exception()
			_LOGGER.exception("SFU producer failed")
		finally:
			if not ready.done():
				ready.cancel()
			await producer.close()

	@asynccontextmanager
	async def consumer(self, peer_id: str) -> AsyncIterator[RawRtpPeerConnection]:
		"""Keep a producer lease and own a consumer through its complete cleanup."""
		configuration, producer_task = await self._prepare()
		consumer = RawRtpPeerConnection(
			bridge=self._bridge, side="consumer", peer_id=peer_id,
			configuration=configuration,
		)
		self._consumers.add(consumer)
		self._changed.set()
		ended = asyncio.Event()
		def producer_ended(_task: asyncio.Task[None]) -> None:
			ended.set()
		producer_task.add_done_callback(producer_ended)
		@consumer.on("connectionstatechange")
		def consumer_state_changed() -> None:
			if consumer.connectionState in {"failed", "closed"}:
				ended.set()
		# The session owner waits on this event and closes its consumer in finally.
		consumer.ended = ended
		try:
			yield consumer
		finally:
			producer_task.remove_done_callback(producer_ended)
			self._consumers.discard(consumer)
			self._changed.set()
			await consumer.close()

	@staticmethod
	async def answer_offer(consumer: RawRtpPeerConnection, offer_sdp: str) -> str:
		"""Negotiate a viewing-only browser connection independently of LiveKit."""
		await consumer.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
		if not (kinds := consumer.remote_media_kinds()):
			raise ValueError("Consumer offer has no audio or video")
		for kind in kinds:
			if not consumer.remote_supported_codecs(kind):
				raise ValueError(f"Consumer offer has no supported {kind} codec")
			consumer.set_answer_direction(kind, "sendonly")
		await consumer.setLocalDescription()
		assert consumer.localDescription is not None
		return consumer.localDescription.sdp

	async def close(self) -> None:
		"""Stop the producer; session owners await their consumers separately."""
		self._closing = True
		if task := self._producer_task:
			if not task.cancelling():
				task.cancel()
			await asyncio.gather(task, return_exceptions=True)
