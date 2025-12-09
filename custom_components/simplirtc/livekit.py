"""Support for livekit webrtc streams."""

from __future__ import annotations

from typing import TypedDict
import asyncio
import json
import logging

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)

from homeassistant.components.camera import (
	WebRTCAnswer,
	WebRTCCandidate,
	WebRTCSendMessage,
	RTCIceCandidateInit,
)

from .protobufs.livekit_rtc_pb2 import (
	SessionDescription,
	SignalRequest,
	SignalResponse,
	SignalTarget,
	TrickleRequest,
)

_LOGGER = logging.getLogger(__name__)


class LiveKitSession:
	def __init__(
		self,
		*,
		session_id: str,
		send_message: WebRTCSendMessage,
		livekit_url: str,
		user_token: str,
	) -> None:
		self._session_id = session_id
		self._send_message = send_message
		self._session: ClientSession | None = None
		self._ws: ClientWebSocketResponse | None = None
		self._local_sdp: str | None = None
		self._ready_event = asyncio.Event()
		self._reader_task: asyncio.Task[None] | None = None
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._ws_endpoint = f'{livekit_url}/rtc?access_token={user_token}&protocol=16'

	async def stream(self, offer_sdp: str) -> None:
		try:
			self._local_sdp = offer_sdp
			self._session = session = ClientSession()
			self._ws = await session.ws_connect(self._ws_endpoint)
			self._reader_task = asyncio.create_task(self._read())
		except Exception as e:
			self._logger.error("Error in stream setup: %s", e)
			await self.close()

	async def _read(self) -> None:
		try:
			assert self._ws is not None, "WebSocket connection not established"
			async for msg in self._ws:
				if msg.type != WSMsgType.BINARY:
					self._logger.debug("<- Text message received: %s", msg.data)
					continue

				try:
					response = SignalResponse.FromString(msg.data)
				except Exception as e:
					self._logger.error("Error parsing SignalResponse: %s", e)
					continue

				self._logger.debug("<- Received SignalResponse: %s", response)
				if response.HasField("offer"):
					asyncio.create_task(self.on_offer(response.offer))
				elif response.HasField("trickle") and response.trickle.target == SignalTarget.SUBSCRIBER:
					asyncio.create_task(self.on_trickle(response.trickle))
		except Exception as e:
			self._logger.error("Error in WebSocket read loop: %s", e)
		finally:
			self._reader_task = None
			# self._ready_event.set()
			await self.close()

	async def on_offer(self, offer: SessionDescription):
		assert self._ws is not None

		self._logger.debug("Received offer SDP id=%s", offer.id)
		if not all(s in offer.sdp for s in ("\nm=audio ","\nm=video ")):
			self._logger.debug("Skipping missing audio or video in offer SDP id=%s", offer.id)
			return

		self._logger.debug("Sending answer for offer SDP id=%s", offer.id)
		self._send_message(WebRTCAnswer(
			answer=offer.sdp.replace("a=setup:actpass", "a=setup:active"),
		))

		if self._local_sdp:
			request = SignalRequest(answer=SessionDescription(
				type="answer",
				sdp=self._local_sdp.replace("a=setup:actpass", "a=setup:passive")
			))
			await self._ws.send_bytes(request.SerializeToString())

		self._ready_event.set()

	async def on_trickle(self, trickle: TrickleRequest):
		self._logger.debug("Received ICE candidate")
		candidate_init: CandidateDict=json.loads(trickle.candidateInit)
		await self._ready_event.wait()
		self._send_message(WebRTCCandidate(
			candidate=RTCIceCandidateInit(
				candidate=candidate_init["candidate"],
				sdp_mid=candidate_init["sdpMid"],
				sdp_m_line_index=candidate_init["sdpMLineIndex"],
				user_fragment=candidate_init["usernameFragment"],
			)
		))

	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		# await self._ready_event.wait()
		assert self._ws, "WebSocket not available"
		await self._ready_event.wait()
		request = SignalRequest(
			trickle=TrickleRequest(
				candidateInit=json.dumps({
					"candidate": candidate.candidate,
					"sdpMid": candidate.sdp_mid,
					"sdpMLineIndex": candidate.sdp_m_line_index,
					"usernameFragment": candidate.user_fragment,
				})
			)
		)
		self._logger.debug("-> Sending ICE candidate: %s", request)
		await self._ws.send_bytes(request.SerializeToString())

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

class CandidateDict(TypedDict):
	candidate: str
	sdpMid: str | None
	sdpMLineIndex: int | None
	usernameFragment: str | None
