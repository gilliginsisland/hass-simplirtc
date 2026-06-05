"""Shared WebRTC session contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import Any

from webrtc_models import RTCIceCandidateInit

_LOGGER = logging.getLogger(__name__)


class Session(ABC):
	"""Common interface for active camera WebRTC sessions."""

	def __init__(self) -> None:
		self._closed = False
		self._close_task: asyncio.Task[None] | None = None
		self._tasks: set[asyncio.Task[Any]] = set()

	def start(self, offer_sdp: str) -> None:
		"""Start streaming in the background."""
		self._tasks.add(asyncio.create_task(self._run_stream(offer_sdp)))

	async def _run_stream(self, offer_sdp: str) -> None:
		try:
			await self._stream(offer_sdp)
		except (asyncio.CancelledError, Exception):
			self.close()
			raise

	def _wait(self, event: asyncio.Event) -> asyncio.Task[bool]:
		"""Track a wait task so session close can cancel it."""
		if self._closed:
			raise asyncio.CancelledError

		task = asyncio.create_task(event.wait())
		self._tasks.add(task)
		task.add_done_callback(self._tasks.discard)
		return task

	def close(self) -> None:
		"""Schedule session cleanup."""
		self._closed = True
		if self._close_task:
			return

		async def close_session() -> None:
			tasks = tuple(self._tasks)
			self._tasks.clear()
			for task in tasks:
				task.cancel()
			results = await asyncio.gather(*tasks, return_exceptions=True)
			errors = [
				result for result in results
				if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
			]
			try:
				await self._close()
			except BaseException as err:
				if not isinstance(err, asyncio.CancelledError):
					errors.append(err)
			if errors:
				raise BaseExceptionGroup("Error closing WebRTC session", errors)

		self._close_task = asyncio.create_task(close_session())
		self._close_task.add_done_callback(self._log_close_error)

	def _log_close_error(self, task: asyncio.Task[None]) -> None:
		if task.cancelled():
			return
		try:
			task.result()
		except BaseException as err:
			_LOGGER.error("Error closing WebRTC session: %s", err)

	@abstractmethod
	async def _stream(self, offer_sdp: str) -> None:
		"""Start streaming for a Home Assistant SDP offer."""

	@abstractmethod
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Send a Home Assistant ICE candidate to the upstream session."""

	@abstractmethod
	async def _close(self) -> None:
		"""Close the session and release all resources."""
