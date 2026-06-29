"""Support for LiveKit WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import (
	AsyncGenerator,
	AsyncIterator,
	Awaitable,
	Callable,
)
from contextlib import asynccontextmanager
import gzip
import json
import logging
import time
from typing import Any, Literal, TypeAlias
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)

from .protobufs.livekit_models_pb2 import (
	AUDIO,
	CAMERA,
	ClientInfo,
	MICROPHONE,
	VIDEO,
)
from .protobufs.livekit_rtc_pb2 import (
	ConnectionSettings,
	ICEServer,
	JoinRequest,
	JoinResponse,
	LeaveRequest,
	MediaSectionsRequirement,
	ParticipantUpdate,
	Ping,
	Pong,
	SessionDescription,
	SignalRequest,
	SignalResponse,
	SignalTarget,
	SubscriptionResponse,
	TrickleRequest,
	WrappedJoinRequest,
)

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = 16
_LIVEKIT_SDK_VERSION = "simplirtc"

SendAnswer = Callable[[SessionDescription], None]
SendCandidate = Callable[[TrickleRequest], None]
CandidateSender = Callable[[TrickleRequest], Awaitable[None]]
SessionClosedCallback = Callable[[], None]
OnIceServers = Callable[[list[ICEServer]], None]
LiveKitSignalMessage: TypeAlias = (
	tuple[Literal["join"], JoinResponse]
	| tuple[Literal["answer"], SessionDescription]
	| tuple[Literal["offer"], SessionDescription]
	| tuple[Literal["trickle"], TrickleRequest]
	| tuple[Literal["update"], ParticipantUpdate]
	| tuple[Literal["media_sections_requirement"], MediaSectionsRequirement]
	| tuple[Literal["leave"], LeaveRequest]
	| tuple[Literal["subscription_response"], SubscriptionResponse]
	| tuple[Literal["pong_resp"], Pong]
	| tuple[Literal["unhandled"], SignalResponse]
)


async def fetch_ice_servers(livekit_url: str, user_token: str) -> list[ICEServer]:
	"""Fetch the ICE servers LiveKit returns in its initial join response."""
	logger = _LOGGER.getChild("ice")
	async with LiveKitSignalConnection.connect(
		livekit_url,
		token=user_token,
		session_id="ice-config",
		auto_subscribe=False,
		logger=logger,
	) as signal:
		async for message in signal.responses():
			match message:
				case ("join", join):
					await signal.send(SignalRequest(leave=LeaveRequest()))
					return list(join.ice_servers)
				case ("leave", leave):
					raise RuntimeError(
						f"LiveKit left before join response: reason={leave.reason}"
					)
				case _:
					continue

	raise RuntimeError("LiveKit closed before sending a join response")


class CandidateQueue:
	"""Queue ICE candidates until signaling is ready to send them."""

	def __init__(self) -> None:
		self._pending: list[TrickleRequest] = []
		self._sender: CandidateSender | None = None

	async def add(self, candidate: TrickleRequest) -> None:
		"""Add a candidate or send it immediately after flush."""
		if self._sender is None:
			self._pending.append(candidate)
			return
		await self._sender(candidate)

	async def flush(self, sender: CandidateSender) -> None:
		"""Send queued candidates and use sender for future additions."""
		self._sender = sender
		pending = self._pending
		self._pending = []
		for candidate in pending:
			await sender(candidate)


class LiveKitSignalConnection:
	"""A wrapped LiveKit websocket connection that emits protobuf responses."""

	def __init__(
		self,
		*,
		session_id: str,
		ws: ClientWebSocketResponse,
		logger: logging.Logger,
	) -> None:
		self.session_id = session_id
		self._ws = ws
		self._logger = logger
		self._ping_task: asyncio.Task[None] | None = None

	@classmethod
	@asynccontextmanager
	async def connect(
		cls,
		url: str,
		*,
		token: str,
		session_id: str,
		auto_subscribe: bool,
		logger: logging.Logger,
		offer_sdp: str | None = None,
	) -> AsyncGenerator[LiveKitSignalConnection]:
		"""Connect to LiveKit signaling for a wrapped join request."""
		join_request = JoinRequest(
			client_info=ClientInfo(
				sdk=ClientInfo.JS,
				version=_LIVEKIT_SDK_VERSION,
				protocol=_LIVEKIT_PROTOCOL_VERSION,
			),
			connection_settings=ConnectionSettings(auto_subscribe=auto_subscribe),
		)
		if offer_sdp is not None:
			join_request.publisher_offer.type = "offer"
			join_request.publisher_offer.sdp = offer_sdp
		wrapped_join_request = WrappedJoinRequest(
			compression=WrappedJoinRequest.GZIP,
			join_request=gzip.compress(join_request.SerializeToString()),
		)
		logger.debug(
			"LiveKit wrapped join request session=%s: %s",
			session_id,
			join_request,
		)
		async with (
			ClientSession() as http_session,
			http_session.ws_connect(
				f"{url.rstrip('/')}/rtc?{urlencode({
					'join_request': base64.urlsafe_b64encode(
						wrapped_join_request.SerializeToString()
					).decode(),
				})}",
				headers={"Authorization": f"Bearer {token}"},
			) as ws,
			cls(session_id=session_id, ws=ws, logger=logger) as signal,
		):
			yield signal

	async def __aenter__(self) -> LiveKitSignalConnection:
		return self

	async def __aexit__(self, *_exc_info: object) -> None:
		self.close()

	def close(self) -> None:
		"""Stop background ping for this websocket session."""
		if ping_task := self._ping_task:
			self._ping_task = None
			ping_task.cancel()

	async def responses(self) -> AsyncIterator[LiveKitSignalMessage]:
		"""Yield parsed LiveKit SignalResponse messages."""

		async for msg in self._ws:
			if msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
				break
			if msg.type != WSMsgType.BINARY:
				raise RuntimeError(
					f"LiveKit sent non-binary signaling message type={msg.type}"
				)

			try:
				response = SignalResponse.FromString(msg.data)
			except Exception as err:
				self._logger.error("Error parsing LiveKit SignalResponse: %s", err)
				continue

			kind = response.WhichOneof("message")
			self._logger.debug(
				"LiveKit websocket receive session=%s kind=%s: %s",
				self.session_id,
				kind,
				response,
			)
			match kind:
				case "join":
					self._start_ping(response.join.ping_interval)
					yield kind, response.join
				case "answer":
					yield kind, response.answer
				case "offer":
					yield kind, response.offer
				case "trickle":
					yield kind, response.trickle
				case "update":
					yield kind, response.update
				case "media_sections_requirement":
					yield kind, response.media_sections_requirement
				case "leave":
					yield kind, response.leave
				case "subscription_response":
					yield kind, response.subscription_response
				case "pong_resp":
					yield kind, response.pong_resp
				case _:
					yield "unhandled", response

	async def send(self, request: SignalRequest) -> None:
		"""Send a protobuf request on this websocket."""
		if self._ws.closed:
			return
		kind = request.WhichOneof("message")
		self._logger.debug(
			"LiveKit websocket send session=%s kind=%s: %s",
			self.session_id,
			kind,
			request,
		)
		await self._ws.send_bytes(request.SerializeToString())

	def _start_ping(self, interval_seconds: int) -> None:
		if interval_seconds <= 0:
			raise RuntimeError(
				f"LiveKit join had invalid ping_interval={interval_seconds}"
			)
		if self._ping_task is not None:
			return
		self._ping_task = asyncio.create_task(
			self._ping_loop(interval_seconds),
			name=f"simplirtc-livekit-ping-{self.session_id}",
		)
		self._ping_task.add_done_callback(self._log_ping_task_error)

	async def _ping_loop(self, interval_seconds: int) -> None:
		while not self._ws.closed:
			await asyncio.sleep(interval_seconds)
			await self.send(
				SignalRequest(ping_req=Ping(timestamp=int(time.time() * 1000)))
			)

	def _log_ping_task_error(self, task: asyncio.Task[None]) -> None:
		if self._ping_task is task:
			self._ping_task = None
		if task.cancelled():
			return
		try:
			task.result()
		except Exception as err:
			self._logger.error("LiveKit session %s ping failed: %s", self.session_id, err)


class _LiveKitAiortcWarmupSession:
	"""Warm a LiveKit room with a local aiortc peer connection when needed."""

	def __init__(
		self,
		*,
		session_id: str,
	) -> None:
		self.session_id = session_id
		self._logger = _LOGGER.getChild("warmup")
		self._consume_tasks: list[asyncio.Task[None]] = []
		self._peer_connection: Any | None = None
		self._remote_candidates: list[Any] = []
		self._offer_in_flight = False
		self._renegotiation_pending = False
		self.used_peer_connection = False

	async def run(
		self,
		join: JoinResponse,
		responses: AsyncIterator[LiveKitSignalMessage],
		signal: LiveKitSignalConnection,
	) -> bool:
		"""Run warmup until LiveKit answers an audio/video offer."""
		try:
			await self._start(join, signal)

			async for message in responses:
				match message:
					case ("answer", answer):
						if await self._on_answer(answer, signal):
							await signal.send(SignalRequest(leave=LeaveRequest()))
							return True
					case ("trickle", trickle):
						await self._on_trickle(trickle)
					case ("media_sections_requirement", media_sections_requirement):
						await self._on_media_sections_requirement(
							media_sections_requirement,
							signal,
						)
					case ("leave", leave):
						self._logger.debug(
							"LiveKit aiortc warmup requested leave: reason=%s action=%s can_reconnect=%s",
							leave.reason,
							leave.action,
							leave.can_reconnect,
						)
					case ("offer", _):
						self._logger.warning(
							"Ignoring LiveKit offer during local-offer aiortc warmup"
						)
					case ("subscription_response", subscription_response):
						self._logger.debug(
							"LiveKit aiortc warmup subscription response: track=%s error=%s",
							subscription_response.track_sid,
							subscription_response.err,
						)
					case ("pong_resp", pong):
						self._logger.debug(
							"LiveKit aiortc warmup pong timestamp=%s",
							pong.timestamp,
						)
					case ("unhandled", kind):
						self._logger.debug(
							"LiveKit aiortc warmup message kind=%s",
							kind,
						)
					case (kind, _):
						self._logger.debug(
							"LiveKit aiortc warmup message kind=%s",
							kind,
						)
			return False
		finally:
			await self._close_peer_connection()

	async def _start(
		self,
		join: JoinResponse,
		signal: LiveKitSignalConnection,
	) -> None:
		self._create_peer_connection(join.ice_servers)
		await self._send_peer_offer(signal)

	def _create_peer_connection(self, ice_servers) -> None:
		from .vendor.aiortc import (
			RTCBundlePolicy,
			RTCConfiguration,
			RTCIceServer,
			RTCPeerConnection,
		)

		config = RTCConfiguration(
			iceServers=[
				RTCIceServer(
					urls=list(ice_server.urls),
					username=ice_server.username or None,
					credential=ice_server.credential or None,
				)
				for ice_server in ice_servers
			],
			bundlePolicy=RTCBundlePolicy.MAX_BUNDLE,
		)
		pc = RTCPeerConnection(configuration=config)
		self._peer_connection = pc
		self.used_peer_connection = True

		pc.addTransceiver("audio", direction="recvonly")
		pc.addTransceiver("video", direction="recvonly")

		@pc.on("connectionstatechange")
		def on_connectionstatechange() -> None:
			self._logger.debug(
				"LiveKit aiortc warmup peer connection state=%s",
				pc.connectionState,
			)

		@pc.on("iceconnectionstatechange")
		def on_iceconnectionstatechange() -> None:
			self._logger.debug(
				"LiveKit aiortc warmup peer ice state=%s",
				pc.iceConnectionState,
			)

		@pc.on("track")
		def on_track(track) -> None:
			self._logger.debug(
				"LiveKit aiortc warmup received track kind=%s id=%s",
				track.kind,
				track.id,
			)
			task = asyncio.create_task(
				self._consume_track(track),
				name=f"simplirtc-livekit-warmup-track-{self.session_id}-{track.kind}",
			)
			self._consume_tasks.append(task)

	async def _send_peer_offer(self, signal: LiveKitSignalConnection) -> None:
		if not (pc := self._peer_connection):
			return
		if self._offer_in_flight:
			self._renegotiation_pending = True
			return
		if pc.signalingState != "stable":
			self._logger.debug(
				"Deferring LiveKit aiortc warmup offer in signaling state=%s",
				pc.signalingState,
			)
			self._renegotiation_pending = True
			return

		offer = await pc.createOffer()
		await pc.setLocalDescription(offer)
		self._offer_in_flight = True
		self._logger.debug(
			"LiveKit aiortc warmup local offer:\n%s",
			pc.localDescription.sdp,
		)
		await signal.send(
			SignalRequest(
				offer=SessionDescription(
					type=pc.localDescription.type,
					sdp=pc.localDescription.sdp,
				)
			)
		)

	async def _on_answer(
		self,
		answer: SessionDescription,
		signal: LiveKitSignalConnection,
	) -> bool:
		if not (pc := self._peer_connection):
			self._logger.debug("Dropping LiveKit answer before aiortc peer exists")
			return False

		from .vendor.aiortc import RTCSessionDescription

		await pc.setRemoteDescription(
			RTCSessionDescription(sdp=answer.sdp, type=answer.type)
		)
		self._offer_in_flight = False
		for candidate in self._remote_candidates:
			await pc.addIceCandidate(candidate)
		self._remote_candidates = []

		if self._renegotiation_pending:
			self._renegotiation_pending = False
			await self._send_peer_offer(signal)
			return False
		return True

	async def _on_trickle(self, trickle: TrickleRequest) -> None:
		if not (pc := self._peer_connection):
			return
		if trickle.target != SignalTarget.PUBLISHER:
			self._logger.debug(
				"Dropping LiveKit aiortc warmup ICE candidate for target=%s",
				trickle.target,
			)
			return

		if trickle.final and not trickle.candidateInit:
			await pc.addIceCandidate(None)
			return
		if not trickle.candidateInit:
			return

		try:
			init = json.loads(trickle.candidateInit)
		except ValueError as err:
			self._logger.warning(
				"Dropping invalid LiveKit aiortc warmup ICE candidate JSON: %s",
				err,
			)
			return

		candidate_sdp = init.get("candidate")
		if not isinstance(candidate_sdp, str):
			self._logger.warning(
				"Dropping LiveKit aiortc warmup ICE candidate without candidate field"
			)
			return

		from .vendor.aiortc.sdp import candidate_from_sdp

		candidate = candidate_from_sdp(candidate_sdp.removeprefix("candidate:"))
		sdp_mid = init.get("sdpMid")
		sdp_m_line_index = init.get("sdpMLineIndex")
		candidate.sdpMid = sdp_mid if isinstance(sdp_mid, str) else None
		candidate.sdpMLineIndex = (
			sdp_m_line_index if isinstance(sdp_m_line_index, int) else None
		)
		if pc.remoteDescription:
			await pc.addIceCandidate(candidate)
			return
		self._remote_candidates.append(candidate)

	async def _on_media_sections_requirement(
		self,
		requirement: MediaSectionsRequirement,
		signal: LiveKitSignalConnection,
	) -> None:
		self._logger.warning(
			"LiveKit aiortc warmup media sections requirement audio=%s video=%s",
			requirement.num_audios,
			requirement.num_videos,
		)
		await self._send_peer_offer(signal)

	async def _consume_track(self, track) -> None:
		while True:
			try:
				await track.recv()
			except Exception as err:
				self._logger.debug(
					"LiveKit aiortc warmup track ended kind=%s id=%s error=%s",
					track.kind,
					track.id,
					err,
				)
				return

	async def _close_peer_connection(self) -> None:
		for task in self._consume_tasks:
			task.cancel()
		await asyncio.gather(*self._consume_tasks, return_exceptions=True)
		self._consume_tasks = []
		if pc := self._peer_connection:
			self._peer_connection = None
			await pc.close()


class LiveKitSession:
	"""A browser-offer LiveKit signaling session for Home Assistant."""

	def __init__(
		self,
		*,
		session_id: str,
		livekit_url: str,
		user_token: str,
		offer_sdp: str,
		send_answer: SendAnswer,
		send_candidate: SendCandidate,
		on_close: SessionClosedCallback | None = None,
		on_ice_servers: OnIceServers | None = None,
	) -> None:
		self.session_id = session_id
		self._livekit_url = livekit_url
		self._user_token = user_token
		self._offer_sdp = offer_sdp
		self._send_answer = send_answer
		self._send_candidate = send_candidate
		self._on_close = on_close
		self._on_ice_servers = on_ice_servers
		self._logger = _LOGGER.getChild(f"session.{session_id}")

		self._reader_task: asyncio.Task[None] | None = None
		self._candidate_queue = CandidateQueue()
		self._answer_sent = False

	async def start(self) -> None:
		"""Start LiveKit signaling."""
		if self._reader_task is not None:
			raise RuntimeError(f"LiveKit session {self.session_id} already started")

		self._reader_task = task = asyncio.create_task(
			self._read(),
			name=f"simplirtc-livekit-{self.session_id}",
		)
		task.add_done_callback(self._log_task_error)

	async def send_candidate(
		self,
		candidate: TrickleRequest,
	) -> None:
		"""Forward a browser ICE candidate to LiveKit."""
		await self._candidate_queue.add(candidate)

	def close(self) -> None:
		"""Close this LiveKit signaling session."""
		if reader_task := self._reader_task:
			self._reader_task = None
			reader_task.cancel()

	def _log_task_error(self, task: asyncio.Task[None]) -> None:
		if self._reader_task is task:
			self._reader_task = None
		if task.cancelled():
			return
		try:
			task.result()
		except Exception as err:
			self._logger.error("LiveKit session %s failed: %s", self.session_id, err)

	async def _read(self) -> None:
		try:
			await self._run_browser_session()
		finally:
			self._reader_task = None
			if self._on_close:
				self._on_close()

	async def _run_browser_session(self) -> None:
		async with LiveKitSignalConnection.connect(
			self._livekit_url,
			token=self._user_token,
			session_id=self.session_id,
			auto_subscribe=True,
			logger=self._logger,
		) as signal:
			responses = signal.responses()
			async for message in responses:
				match message:
					case ("join", join):
						if (
							(on_ice_servers := self._on_ice_servers)
							and (ice_servers := list(join.ice_servers))
						):
							on_ice_servers(ice_servers)
						has_audio = False
						has_video = False
						for participant in join.other_participants:
							for track in participant.tracks:
								if not track.sid:
									continue
								if track.type == AUDIO and track.source == MICROPHONE:
									has_audio = True
								elif track.type == VIDEO and track.source == CAMERA:
									has_video = True

						if has_audio and has_video:
							self._logger.info(
								"LiveKit join already has camera audio/video; sending browser offer"
							)
							await signal.send(
								SignalRequest(
									offer=SessionDescription(type="offer", sdp=self._offer_sdp)
								)
							)
							await self._continue_browser_session(responses, signal)
							return

						self._logger.info(
							"LiveKit warming up before sending browser offer"
						)
						await self._warm_up_with_aiortc(
							join,
							responses,
							signal,
						)
						break
					case ("leave", leave):
						raise RuntimeError(
							f"LiveKit left before join response: reason={leave.reason}"
						)
					case ("unhandled", kind):
						raise RuntimeError(f"LiveKit sent {kind} before join response")
					case (kind, _):
						raise RuntimeError(f"LiveKit sent {kind} before join response")
			else:
				raise RuntimeError("LiveKit websocket closed before join response")

		await self._run_browser_offer_join()

	async def _run_browser_offer_join(self) -> None:
		async with LiveKitSignalConnection.connect(
			self._livekit_url,
			token=self._user_token,
			session_id=self.session_id,
			auto_subscribe=True,
			logger=self._logger,
			offer_sdp=self._offer_sdp,
		) as signal:
			responses = signal.responses()
			async for message in responses:
				match message:
					case ("join", join):
						_log_join(self._logger, "LiveKit", join)
						if (
							(on_ice_servers := self._on_ice_servers)
							and (ice_servers := list(join.ice_servers))
						):
							on_ice_servers(ice_servers)
						await self._continue_browser_session(responses, signal)
						return
					case ("leave", leave):
						raise RuntimeError(
							f"LiveKit left before join response: reason={leave.reason}"
						)
					case ("unhandled", kind):
						raise RuntimeError(f"LiveKit sent {kind} before join response")
					case (kind, _):
						raise RuntimeError(f"LiveKit sent {kind} before join response")

			raise RuntimeError("LiveKit websocket closed before join response")

	async def _warm_up_with_aiortc(
		self,
		join: JoinResponse,
		responses: AsyncIterator[LiveKitSignalMessage],
		signal: LiveKitSignalConnection,
	) -> None:
		warmup = _LiveKitAiortcWarmupSession(session_id=self.session_id)
		try:
			warmed_up = await warmup.run(join, responses, signal)
		except Exception:
			self._logger.exception(
				"LiveKit aiortc warmup failed; continuing with browser offer"
			)
		else:
			self._logger.info(
				"LiveKit aiortc warmup complete=%s used_peer_connection=%s",
				warmed_up,
				warmup.used_peer_connection,
			)

	async def _continue_browser_session(
		self,
		responses: AsyncIterator[LiveKitSignalMessage],
		signal: LiveKitSignalConnection,
	) -> None:
		async for message in responses:
			match message:
				case ("join", _):
					self._logger.warning("Ignoring unexpected LiveKit join after browser offer")
				case ("answer", answer):
					await self._on_answer(answer, signal)
				case ("trickle", trickle):
					self._on_trickle(trickle)
				case ("media_sections_requirement", media_sections_requirement):
					self._on_media_sections_requirement(
						media_sections_requirement
					)
				case ("leave", leave):
					self._logger.debug(
						"LiveKit requested session leave: reason=%s action=%s can_reconnect=%s",
						leave.reason,
						leave.action,
						leave.can_reconnect,
					)
				case ("subscription_response", subscription_response):
					self._logger.debug(
						"LiveKit subscription response: track=%s error=%s",
						subscription_response.track_sid,
						subscription_response.err,
					)
					if subscription_response.err:
						self._logger.warning(
							"LiveKit subscription failed: track=%s error=%s",
							subscription_response.track_sid,
							subscription_response.err,
						)
				case ("offer", _):
					self._logger.warning(
						"Ignoring unexpected LiveKit offer in browser-offer session"
					)
				case ("pong_resp", pong):
					self._logger.debug(
						"LiveKit pong timestamp=%s",
						pong.timestamp,
					)
				case ("unhandled", kind):
					self._logger.debug("LiveKit signaling message kind=%s", kind)
				case (kind, _):
					self._logger.debug("LiveKit signaling message kind=%s", kind)

	async def _on_answer(
		self,
		answer: SessionDescription,
		signal: LiveKitSignalConnection,
	) -> None:
		await self._candidate_queue.flush(
			lambda candidate: self._send_livekit_candidate(signal, candidate)
		)
		if self._answer_sent:
			self._logger.debug("Ignoring LiveKit answer after answer was already sent")
			return
		self._send_answer(answer)
		self._answer_sent = True

	def _on_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.PUBLISHER:
			self._logger.debug("Dropping LiveKit ICE candidate for target=%s", trickle.target)
			return

		self._send_candidate(trickle)

	def _on_media_sections_requirement(
		self,
		requirement: MediaSectionsRequirement,
	) -> None:
		if requirement.num_audios or requirement.num_videos:
			self._logger.warning(
				"LiveKit requested extra media sections audio=%s video=%s; Home Assistant cannot apply renegotiation",
				requirement.num_audios,
				requirement.num_videos,
			)

	async def _send_livekit_candidate(
		self,
		signal: LiveKitSignalConnection,
		candidate: TrickleRequest,
	) -> None:
		await signal.send(SignalRequest(trickle=candidate))
