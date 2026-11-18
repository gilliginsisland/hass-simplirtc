"""Terminate normal LiveKit negotiation while routing camera RTP to HA viewers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
import json
import logging
from urllib.parse import urlencode
from uuid import uuid4

from aiortc import RTCIceCandidate, RTCIceServer, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from homeassistant.components.camera import WebRTCAnswer, WebRTCSendMessage
from webrtc_models import RTCIceCandidateInit

from .livekit import LiveKitSignal, SendIceServers
from .protobufs.livekit_rtc_pb2 import ICEServer, SessionDescription, SignalRequest, SignalTarget
from .rtp_router import RawRtpPeerConnection
from .sfu import RawRtpSfu
from .webrtc import SimpliSafeWebRTCSession

_LOGGER = logging.getLogger(__name__)


def _candidate(init: dict) -> RTCIceCandidate | None:
	if not init.get("candidate"):
		return None
	candidate = candidate_from_sdp(init["candidate"].removeprefix("candidate:"))
	candidate.sdpMid = init.get("sdpMid")
	candidate.sdpMLineIndex = init.get("sdpMLineIndex")
	return candidate


class SubscriberSignal(LiveKitSignal):
	"""Use the normal subscriber flow, in which LiveKit sends every offer."""

	@staticmethod
	def _join_url(url: str, *, auto_subscribe: bool, offer_sdp: str | None = None) -> str:
		return f"{url.rstrip('/')}/rtc?{urlencode({
			'sdk': 'python', 'version': '1.0.17', 'protocol': '16',
			'auto_subscribe': str(int(auto_subscribe)),
		})}"


class LiveKitSfu:
	"""Own one camera's upstream signaling and its raw RTP fan-out."""

	def __init__(
		self, get_connection_info: Callable[[], Awaitable[tuple[str, str]]], *,
		create_task: Callable[..., asyncio.Task[None]] = asyncio.create_task,
	) -> None:
		self._get_connection_info = get_connection_info
		self.ice_servers: list[ICEServer] = []
		self.sfu = RawRtpSfu(self._connect, create_task=create_task)

	@asynccontextmanager
	async def _connect(self, producer: RawRtpPeerConnection) -> AsyncIterator[None]:
		url, token = await self._get_connection_info()
		async with SubscriberSignal.connect(
			url, token=token, auto_subscribe=True, logger=_LOGGER,
		) as signal, asyncio.TaskGroup() as tasks:
			ready = asyncio.Event()
			@producer.on("connectionstatechange")
			def producer_state_changed() -> None:
				if producer.connectionState in {"failed", "closed"}:
					producer.ended.set()
			async def monitor_connection() -> None:
				await producer.ended.wait()
				raise ConnectionError("LiveKit SFU media connection ended")
			monitor = tasks.create_task(monitor_connection(), name="simplirtc-sfu-connection")
			reader = tasks.create_task(self._read(signal, producer, ready), name="simplirtc-sfu-signaling")
			try:
				await ready.wait()
				yield
			finally:
				reader.cancel()
				monitor.cancel()

	async def _read(
		self, signal: LiveKitSignal, producer: RawRtpPeerConnection, ready: asyncio.Event,
	) -> None:
		async for message in signal:
			match message:
				case ("join", join):
					self.ice_servers = list(join.ice_servers)
					if not any(url.startswith(("turn:", "turns:")) for server in self.ice_servers for url in server.urls):
						raise RuntimeError("LiveKit join did not supply SimpliSafe TURN servers")
					producer.rtc_configuration.iceServers = [
						RTCIceServer(urls=list(server.urls), username=server.username or None, credential=server.credential or None)
						for server in self.ice_servers
					]
					ready.set()
				case ("offer", offer):
					if not ready.is_set():
						raise RuntimeError("LiveKit offered media before supplying ICE configuration")
					await producer.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type="offer"))
					await producer.setLocalDescription()
					assert producer.localDescription is not None
					await signal.send(SignalRequest(answer=SessionDescription(
						type="answer", sdp=producer.localDescription.sdp, id=offer.id,
					)))
				case ("trickle", trickle) if trickle.target == SignalTarget.SUBSCRIBER:
					if trickle.candidateInit:
						await producer.addIceCandidate(_candidate(json.loads(trickle.candidateInit)))
					elif trickle.final:
						await producer.addIceCandidate(None)
				case ("leave", _):
					raise RuntimeError("LiveKit ended the SFU session")
		raise RuntimeError("LiveKit SFU websocket closed")

	async def close(self) -> None:
		await self.sfu.close()


class SfuSession(SimpliSafeWebRTCSession[None]):
	"""Adapt independently called HA start/offer/stop methods to a consumer scope."""

	def __init__(self, room: LiveKitSfu, *, send_ice_servers: SendIceServers) -> None:
		super().__init__(logger=_LOGGER, name="simplirtc-sfu-session")
		self._room = room
		self._send_ice_servers = send_ice_servers
		self._consumer: RawRtpPeerConnection | None = None
		self._pending_candidates: list[RTCIceCandidate | None] = []
		self._negotiation_lock = asyncio.Lock()

	async def _read(self) -> None:
		async with self._room.sfu.consumer(str(uuid4())) as consumer:
			self._consumer = consumer
			try:
				self._send_ice_servers(self._room.ice_servers)
				await consumer.ended.wait()
			finally:
				self._consumer = None
				self._pending_candidates.clear()

	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		self._accept_offer(send_message)
		async with self._negotiation_lock:
			if (consumer := self._consumer) is None:
				raise RuntimeError("SFU session is not prepared")
			answer = await self._room.sfu.answer_offer(consumer, offer_sdp)
			for candidate in self._pending_candidates:
				await consumer.addIceCandidate(candidate)
			self._pending_candidates.clear()
			send_message(WebRTCAnswer(answer=answer))

	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		self._check_open()
		parsed = _candidate({
			"candidate": candidate.candidate, "sdpMid": candidate.sdp_mid,
			"sdpMLineIndex": candidate.sdp_m_line_index,
		})
		if (consumer := self._consumer) is not None and consumer.remoteDescription is not None:
			await consumer.addIceCandidate(parsed)
		else:
			self._pending_candidates.append(parsed)
