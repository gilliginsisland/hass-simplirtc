"""Support for LiveKit WebRTC streams."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
import json
import logging
import time
from typing import (
	Any,
	Literal,
	overload,
	override,
)
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)
from aiortc import (
	RTCConfiguration,
	RTCIceCandidate,
	RTCIceServer,
	RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp
from webrtc_models import RTCIceCandidateInit

from .protobufs.livekit_rtc_pb2 import (
	JoinResponse,
	Ping,
	SessionDescription,
	SignalRequest,
	SignalResponse,
	SignalTarget,
	TrickleRequest,
)
from .rtp_router import RawRtpBridge, RawRtpPeerConnection
from .session import Session

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = "16"
_LIVEKIT_SDK = "python"
_LIVEKIT_SDK_VERSION = "1.0.17"


LiveKitJoinCallback = Callable[[JoinResponse], Coroutine[Any, Any, None]]
LiveKitOfferCallback = Callable[[SessionDescription], Coroutine[Any, Any, RTCSessionDescription]]
LiveKitTrickleCallback = Callable[[TrickleRequest], Coroutine[Any, Any, None]]
LiveKitCloseCallback = Callable[[], None]
LiveKitEvent = Literal["join", "offer", "trickle", "close"]
SendAnswer = Callable[[str], None]


class LiveKitEngine:
	"""LiveKit websocket signaling engine."""

	def __init__(
		self,
		*,
		session_id: str,
		livekit_url: str,
		user_token: str,
	) -> None:
		self._session = ClientSession()
		self._ws: ClientWebSocketResponse | None = None
		self._tasks: set[asyncio.Task[None]] = set()
		self._logger = _LOGGER.getChild(f"engine.{session_id}")

		self._on_join_callback: LiveKitJoinCallback | None = None
		self._on_offer_callback: LiveKitOfferCallback | None = None
		self._on_trickle_callback: LiveKitTrickleCallback | None = None
		self._on_close_callback: LiveKitCloseCallback | None = None

		self._ws_endpoint = f"{livekit_url.rstrip('/')}/rtc?{urlencode({
			'access_token': user_token,
			'sdk': _LIVEKIT_SDK,
			'version': _LIVEKIT_SDK_VERSION,
			'protocol': _LIVEKIT_PROTOCOL_VERSION,
			'auto_subscribe': '1',
		})}"

	@overload
	def on(self, event: Literal["join"]) -> Callable[[LiveKitJoinCallback], LiveKitJoinCallback]: ...

	@overload
	def on(self, event: Literal["offer"]) -> Callable[[LiveKitOfferCallback], LiveKitOfferCallback]: ...

	@overload
	def on(self, event: Literal["trickle"]) -> Callable[[LiveKitTrickleCallback], LiveKitTrickleCallback]: ...

	@overload
	def on(self, event: Literal["close"]) -> Callable[[LiveKitCloseCallback], LiveKitCloseCallback]: ...

	def on(self, event: LiveKitEvent) -> Callable[[Any], Any]:
		"""Register a LiveKit signaling event handler."""
		def decorator(callback: Any) -> Any:
			match event:
				case "join":
					self._on_join_callback = callback
				case "offer":
					self._on_offer_callback = callback
				case "trickle":
					self._on_trickle_callback = callback
				case "close":
					self._on_close_callback = callback
			return callback

		return decorator

	async def start(self) -> None:
		"""Start LiveKit signaling."""
		try:
			self._ws = await self._session.ws_connect(self._ws_endpoint)
			self._start_task(self._read())
		except Exception as err:
			self._logger.error("Error in LiveKit engine setup: %s", err)
			await self.close()
			raise

	async def _read(self) -> None:
		try:
			assert self._ws, "WebSocket connection not established"
			async for msg in self._ws:
				if msg.type != WSMsgType.BINARY:
					raise RuntimeError(f"LiveKit sent non-binary signaling message type={msg.type}")

				try:
					response = SignalResponse.FromString(msg.data)
				except Exception as err:
					self._logger.error("Error parsing LiveKit SignalResponse: %s", err)
					continue

				match kind := response.WhichOneof("message"):
					case "join":
						await self._on_join(response.join)
					case "offer":
						await self._on_offer(response.offer)
					case "trickle":
						await self._on_livekit_trickle(response.trickle)
					case "update":
						self._logger.debug("LiveKit participant update ignored")
					case "leave":
						self._logger.debug(
							"LiveKit requested session leave: reason=%s action=%s can_reconnect=%s",
							response.leave.reason,
							response.leave.action,
							response.leave.can_reconnect,
						)
						break
					case "pong_resp":
						self._logger.debug("LiveKit pong timestamp=%s", response.pong_resp.timestamp)
					case _:
						self._logger.debug("LiveKit signaling message kind=%s", kind)
		except asyncio.CancelledError:
			raise
		except Exception as err:
			self._logger.error("Error in LiveKit WebSocket read loop: %s", err)
		finally:
			if callback := self._on_close_callback:
				callback()

	async def _on_join(self, join: JoinResponse) -> None:
		self._logger.debug(
			"LiveKit join accepted: room=%s participant=%s subscriber_primary=%s fast_publish=%s server_version=%s server_region=%s",
			join.room.name or join.room.sid,
			join.participant.identity or join.participant.sid,
			join.subscriber_primary,
			join.fast_publish,
			join.server_version or join.server_info.version,
			join.server_region or join.server_info.region,
		)

		if not (callback := self._on_join_callback):
			return
		await callback(join)
		if join.ping_interval <= 0:
			raise RuntimeError(f"LiveKit join had invalid ping_interval={join.ping_interval}")
		self._start_task(self._ping_loop(join.ping_interval))

	async def _on_offer(self, offer: SessionDescription) -> None:
		self._logger.debug(
			"Received LiveKit offer id=%s type=%s",
			offer.id,
			offer.type,
		)
		if not (callback := self._on_offer_callback):
			return
		answer = await callback(offer)

		assert self._ws, f"Cannot send LiveKit SDP answer id={offer.id} because WebSocket is closed"

		request = SignalRequest(answer=SessionDescription(
			type=answer.type,
			sdp=answer.sdp,
			id=offer.id,
		))
		self._logger.debug("Sending LiveKit answer id=%s", offer.id)
		await self._ws.send_bytes(request.SerializeToString())

	async def _on_livekit_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.SUBSCRIBER:
			self._logger.debug("Dropping LiveKit ICE candidate for target=%s", trickle.target)
			return
		if not (callback := self._on_trickle_callback):
			return
		await callback(trickle)

	def _start_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
		task = asyncio.create_task(coroutine)
		self._tasks.add(task)

	async def _ping_loop(self, interval_seconds: int) -> None:
		while True:
			await asyncio.sleep(interval_seconds)
			if not self._ws or self._ws.closed:
				return
			request = SignalRequest(ping_req=Ping(timestamp=int(time.time() * 1000)))
			await self._ws.send_bytes(request.SerializeToString())

	async def close(self) -> None:
		"""Close this LiveKit engine."""
		tasks = tuple(self._tasks)
		self._tasks.clear()
		ws, self._ws = self._ws, None
		for task in tasks:
			task.cancel()

		results = await asyncio.gather(
			*tasks,
			*(closer.close() for closer in (ws, self._session) if closer),
			return_exceptions=True,
		)
		if errors := tuple(
			result for result in results
			if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
		):
			raise BaseExceptionGroup("Error closing LiveKit engine", errors)
		self._logger.debug("Closed LiveKit engine")


class LiveKitSession(Session):
	"""Native LiveKit WebRTC proxy session."""

	def __init__(
		self,
		*,
		session_id: str,
		send_answer: SendAnswer,
		livekit_url: str,
		user_token: str,
	) -> None:
		super().__init__()
		self._session_id = session_id
		self._send_answer = send_answer
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._consumer_media_kinds: tuple[str, ...] = ()
		self._rtc_configuration = RTCConfiguration()
		self._rtc_configuration_ready = asyncio.Event()
		self._consumer_remote_description_ready = asyncio.Event()
		self._producer_remote_description_ready = asyncio.Event()
		self._producer_ready = asyncio.Event()
		self._rtp_bridge = RawRtpBridge()
		self._consumer_pc = RawRtpPeerConnection(
			bridge=self._rtp_bridge,
			side="consumer",
			configuration=self._rtc_configuration,
		)
		self._producer_pc = RawRtpPeerConnection(
			bridge=self._rtp_bridge,
			side="producer",
			configuration=self._rtc_configuration,
		)

		@self._consumer_pc.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			state = self._consumer_pc.connectionState
			self._logger.debug("Consumer connectionState=%s", state)
			if state in {"failed", "closed"}:
				self.close()

		@self._consumer_pc.on("iceconnectionstatechange")
		async def on_iceconnectionstatechange() -> None:
			state = self._consumer_pc.iceConnectionState
			self._logger.debug("Consumer iceConnectionState=%s", state)
			if state == "failed":
				self.close()

		@self._producer_pc.on("connectionstatechange")
		async def on_producer_connectionstatechange() -> None:
			state = self._producer_pc.connectionState
			self._logger.debug("Producer connectionState=%s", state)
			if state in {"failed", "closed"}:
				self.close()

		@self._producer_pc.on("iceconnectionstatechange")
		async def on_producer_iceconnectionstatechange() -> None:
			state = self._producer_pc.iceConnectionState
			self._logger.debug("Producer iceConnectionState=%s", state)
			if state == "failed":
				self.close()

		self._engine = LiveKitEngine(
			session_id=session_id,
			livekit_url=livekit_url,
			user_token=user_token,
		)

		@self._engine.on("join")
		async def on_livekit_join(join: JoinResponse) -> None:
			self._rtc_configuration.iceServers = [
				RTCIceServer(
					urls=list(server.urls),
					username=server.username or None,
					credential=server.credential or None,
				)
				for server in join.ice_servers
			]
			self._rtc_configuration_ready.set()

		@self._engine.on("offer")
		async def answer_producer_offer(offer: SessionDescription) -> RTCSessionDescription:
			await self._wait(self._rtc_configuration_ready)
			await self._wait(self._consumer_remote_description_ready)

			await self._producer_pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
			self._producer_remote_description_ready.set()

			for kind in self._producer_pc.remote_media_kinds():
				if consumer_codecs := self._consumer_pc.remote_supported_codecs(kind):
					self._producer_pc.constrain_answer_codecs(kind, consumer_codecs)

			await self._producer_pc.setLocalDescription()
			assert self._producer_pc.localDescription
			if all(
				kind in self._producer_pc.remote_media_kinds()
				for kind in self._consumer_media_kinds
			):
				self._producer_ready.set()
			return self._producer_pc.localDescription

		@self._engine.on("trickle")
		async def add_producer_candidate(trickle: TrickleRequest) -> None:
			await self._wait(self._producer_remote_description_ready)
			if trickle.final and not trickle.candidateInit:
				self._logger.debug("Producer end-of-candidates")
				await self._producer_pc.addIceCandidate(None)
				return
			if not trickle.candidateInit:
				raise RuntimeError("Producer sent a non-final ICE candidate without candidateInit")

			init = json.loads(trickle.candidateInit)
			if not isinstance(candidate_init := init["candidate"], str) or not candidate_init:
				raise RuntimeError(f"Producer ICE candidate has invalid candidate field: {init}")
			if not isinstance(sdp_mid := init["sdpMid"], str) or not sdp_mid:
				raise RuntimeError(f"Producer ICE candidate has invalid sdpMid field: {init}")
			if not isinstance(sdp_m_line_index := init["sdpMLineIndex"], int):
				raise RuntimeError(f"Producer ICE candidate has invalid sdpMLineIndex field: {init}")
			candidate = _candidate_from_init(candidate_init)
			candidate.sdpMid = sdp_mid
			candidate.sdpMLineIndex = sdp_m_line_index
			self._logger.debug(
				"Adding producer ICE candidate mid=%s index=%s type=%s",
				candidate.sdpMid,
				candidate.sdpMLineIndex,
				candidate.type,
			)
			await self._producer_pc.addIceCandidate(candidate)

		@self._engine.on("close")
		def on_livekit_close() -> None:
			self.close()

	@override
	async def _stream(self, offer_sdp: str) -> None:
		"""Answer a consumer offer using ready LiveKit media."""
		await self._engine.start()
		await self._wait(self._rtc_configuration_ready)
		await self._consumer_pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
		if not (consumer_media_kinds := self._consumer_pc.remote_media_kinds()):
			raise RuntimeError("Consumer offer has no media sections")
		self._consumer_media_kinds = consumer_media_kinds
		if unsupported := tuple(
			kind for kind in self._consumer_media_kinds
			if not self._consumer_pc.remote_supported_codecs(kind)
		):
			raise RuntimeError(
				f"Consumer offer contains unsupported media types: {', '.join(unsupported)}"
			)
		self._consumer_remote_description_ready.set()
		await self._wait(self._producer_ready)
		for kind in self._consumer_media_kinds:
			self._consumer_pc.set_answer_direction(kind, "sendonly")
		await self._consumer_pc.setLocalDescription()
		assert self._consumer_pc.localDescription
		self._send_answer(self._consumer_pc.localDescription.sdp)

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Handle an ICE candidate from the consumer."""
		await self._wait(self._consumer_remote_description_ready)
		if not candidate.candidate:
			self._logger.debug("Consumer end-of-candidates")
			await self._consumer_pc.addIceCandidate(None)
			return

		try:
			aiortc_candidate = _candidate_from_init(candidate.candidate)
			aiortc_candidate.sdpMid = candidate.sdp_mid
			aiortc_candidate.sdpMLineIndex = candidate.sdp_m_line_index
		except Exception as err:
			self._logger.error("Failed to parse consumer ICE candidate: %s", err)
			return

		self._logger.debug(
			"Adding consumer ICE candidate mid=%s index=%s type=%s",
			aiortc_candidate.sdpMid,
			aiortc_candidate.sdpMLineIndex,
			aiortc_candidate.type,
		)
		await self._consumer_pc.addIceCandidate(aiortc_candidate)

	@override
	async def _close(self) -> None:
		"""Close this producer and consumer WebRTC session."""
		results = await asyncio.gather(
			self._engine.close(),
			self._producer_pc.close(),
			self._consumer_pc.close(),
			return_exceptions=True,
		)
		if errors := tuple(
			result for result in results
			if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
		):
			raise BaseExceptionGroup("Error closing LiveKit proxy session", errors)
		self._logger.debug("Closed LiveKit proxy session")


def _candidate_from_init(candidate: str) -> RTCIceCandidate:
	"""Parse RTCIceCandidateInit.candidate text for aiortc."""
	return candidate_from_sdp(candidate.removeprefix("candidate:"))
