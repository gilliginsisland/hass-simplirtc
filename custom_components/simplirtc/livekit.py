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
)
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)
from aiortc import (
	RTCIceServer,
	RTCSessionDescription,
)

from .protobufs.livekit_rtc_pb2 import (
	JoinResponse,
	Ping,
	SessionDescription,
	SignalRequest,
	SignalResponse,
	SignalTarget,
	TrickleRequest,
)
from .rtp_router import RawRtpPeerConnection
from .sfu import (
	ice_candidate_from_sdp,
)

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = "16"
_LIVEKIT_SDK = "python"
_LIVEKIT_SDK_VERSION = "1.0.17"


LiveKitJoinCallback = Callable[[JoinResponse], Coroutine[Any, Any, None]]
LiveKitOfferCallback = Callable[[SessionDescription], Coroutine[Any, Any, RTCSessionDescription]]
LiveKitTrickleCallback = Callable[[TrickleRequest], Coroutine[Any, Any, None]]
LiveKitCloseCallback = Callable[[], None]
LiveKitEvent = Literal["join", "offer", "trickle", "close"]
GetLiveKitConnectionInfo = Callable[[], Coroutine[Any, Any, tuple[str, str]]]


class LiveKitEngine:
	"""LiveKit websocket signaling engine."""

	def __init__(
		self,
		*,
		livekit_url: str,
		user_token: str,
	) -> None:
		self._reader_task: asyncio.Task[None] | None = None
		self._logger = _LOGGER.getChild("engine")

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

	def start(self) -> None:
		"""Start LiveKit signaling."""

		self._reader_task = task = asyncio.create_task(self._read())

		def log_task_error(task: asyncio.Task[None]) -> None:
			if self._reader_task is task:
				self._reader_task = None
			if not task.cancelled():
				try:
					task.result()
				except BaseException as err:
					self._logger.error("Error in LiveKit engine: %s", err)

		def on_close(_: asyncio.Task[None]):
			if callback := self._on_close_callback:
				callback()

		task.add_done_callback(log_task_error)
		task.add_done_callback(on_close)

	async def _read(self) -> None:
		async with (
			ClientSession() as session,
			session.ws_connect(self._ws_endpoint) as ws,
		):
			async with asyncio.TaskGroup() as task_group:
				await self._read_messages(ws, task_group)

	async def _read_messages(
		self,
		ws: ClientWebSocketResponse,
		task_group: asyncio.TaskGroup,
	) -> None:
		async for msg in ws:
			if msg.type != WSMsgType.BINARY:
				raise RuntimeError(f"LiveKit sent non-binary signaling message type={msg.type}")

			try:
				response = SignalResponse.FromString(msg.data)
			except Exception as err:
				self._logger.error("Error parsing LiveKit SignalResponse: %s", err)
				continue

			match kind := response.WhichOneof("message"):
				case "join":
					await self._on_join(response.join, ws, task_group)
				case "offer":
					await self._on_offer(response.offer, ws)
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
		raise asyncio.CancelledError

	async def _on_join(
		self,
		join: JoinResponse,
		ws: ClientWebSocketResponse,
		task_group: asyncio.TaskGroup,
	) -> None:
		self._logger.debug(
			"LiveKit join accepted: room=%s participant=%s subscriber_primary=%s fast_publish=%s server_version=%s server_region=%s",
			join.room.name or join.room.sid,
			join.participant.identity or join.participant.sid,
			join.subscriber_primary,
			join.fast_publish,
			join.server_version or join.server_info.version,
			join.server_region or join.server_info.region,
		)

		if (callback := self._on_join_callback):
			await callback(join)

		if join.ping_interval <= 0:
			raise RuntimeError(f"LiveKit join had invalid ping_interval={join.ping_interval}")

		task_group.create_task(self._ping_loop(ws, join.ping_interval))

	async def _on_offer(
		self,
		offer: SessionDescription,
		ws: ClientWebSocketResponse,
	) -> None:
		self._logger.debug(
			"Received LiveKit offer id=%s type=%s",
			offer.id,
			offer.type,
		)
		if not (callback := self._on_offer_callback):
			return
		answer = await callback(offer)

		request = SignalRequest(answer=SessionDescription(
			type=answer.type,
			sdp=answer.sdp,
			id=offer.id,
		))
		self._logger.debug("Sending LiveKit answer id=%s", offer.id)
		await ws.send_bytes(request.SerializeToString())

	async def _on_livekit_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.SUBSCRIBER:
			self._logger.debug("Dropping LiveKit ICE candidate for target=%s", trickle.target)
			return
		if not (callback := self._on_trickle_callback):
			return
		await callback(trickle)

	async def _ping_loop(
		self,
		ws: ClientWebSocketResponse,
		interval_seconds: int,
	) -> None:
		while True:
			await asyncio.sleep(interval_seconds)
			if ws.closed:
				return
			request = SignalRequest(ping_req=Ping(timestamp=int(time.time() * 1000)))
			await ws.send_bytes(request.SerializeToString())

	def close(self) -> None:
		"""Close this LiveKit engine."""
		if reader_task := self._reader_task:
			self._reader_task = None
			reader_task.cancel()


class LiveKitProducer:
	"""Configure a raw RTP producer peer connection through LiveKit signaling."""

	def __init__(self, *, get_connection_info: GetLiveKitConnectionInfo) -> None:
		self._get_connection_info = get_connection_info
		self._logger = _LOGGER.getChild("producer")

	async def setup(self, producer_pc: RawRtpPeerConnection) -> None:
		"""Start LiveKit signaling and configure the supplied producer PC."""
		livekit_url, user_token = await self._get_connection_info()
		engine = LiveKitEngine(
			livekit_url=livekit_url,
			user_token=user_token,
		)
		rtc_configuration_ready = asyncio.Event()
		producer_remote_description_ready = asyncio.Event()

		@engine.on("join")
		async def on_livekit_join(join: JoinResponse) -> None:
			producer_pc.rtc_configuration.iceServers = [
				RTCIceServer(
					urls=list(server.urls),
					username=server.username or None,
					credential=server.credential or None,
				)
				for server in join.ice_servers
			]
			rtc_configuration_ready.set()

		@engine.on("offer")
		async def answer_producer_offer(offer: SessionDescription) -> RTCSessionDescription:
			await rtc_configuration_ready.wait()
			await producer_pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
			await producer_pc.add_pending_remote_candidates()
			producer_remote_description_ready.set()
			await producer_pc.setLocalDescription()
			if not (local_description := producer_pc.localDescription):
				raise RuntimeError("LiveKit producer PC did not create an SDP answer")
			return local_description

		@engine.on("trickle")
		async def add_producer_candidate(trickle: TrickleRequest) -> None:
			await producer_remote_description_ready.wait()
			if trickle.final and not trickle.candidateInit:
				self._logger.debug("Producer end-of-candidates")
				await producer_pc.addIceCandidate(None)
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
			if not (candidate := ice_candidate_from_sdp(
				candidate_init,
				sdp_mid=sdp_mid,
				sdp_m_line_index=sdp_m_line_index,
			)):
				return
			self._logger.debug(
				"Adding producer ICE candidate mid=%s index=%s type=%s",
				candidate.sdpMid,
				candidate.sdpMLineIndex,
				candidate.type,
			)
			await producer_pc.addIceCandidate(candidate)

		@engine.on("close")
		def on_livekit_close() -> None:
			asyncio.create_task(producer_pc.close())

		@producer_pc.on("connectionstatechange")
		async def on_producer_connectionstatechange() -> None:
			if producer_pc.connectionState in {"failed", "closed"}:
				engine.close()

		engine.start()
		try:
			await rtc_configuration_ready.wait()
		except BaseException:
			engine.close()
			raise
