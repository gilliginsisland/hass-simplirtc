"""Support for LiveKit WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import (
	AsyncGenerator,
	AsyncIterator,
	Awaitable,
	Callable,
	Iterable,
)
from contextlib import asynccontextmanager
import gzip
import json
import logging
import time
from typing import Literal, TypeAlias
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)

from .vendor.aiortc import (
	RTCBundlePolicy,
	RTCConfiguration,
	RTCIceServer,
	RTCPeerConnection,
	RTCSessionDescription,
)
from .vendor.aiortc.sdp import candidate_from_sdp
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
	SubscriptionPermissionUpdate,
	SubscriptionResponse,
	TrickleRequest,
	WrappedJoinRequest,
)

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = 16
_LIVEKIT_SDK_VERSION = "simplirtc"
_CAMERA_TRACK_KIND_BY_TYPE_SOURCE = {
	(AUDIO, MICROPHONE): "audio",
	(VIDEO, CAMERA): "video",
}

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
	| tuple[Literal["subscription_permission_update"], SubscriptionPermissionUpdate]
	| tuple[Literal["subscription_response"], SubscriptionResponse]
	| tuple[Literal["pong_resp"], Pong]
	| tuple[Literal["response"], SignalResponse]
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
				case "subscription_permission_update":
					yield kind, response.subscription_permission_update
				case "subscription_response":
					yield kind, response.subscription_response
				case "pong_resp":
					yield kind, response.pong_resp
				case _:
					self._logger.debug(
						"Unhandled LiveKit signaling response: %s",
						response,
					)
					yield "response", response

	async def send(self, request: SignalRequest) -> None:
		"""Send a protobuf request on this websocket."""
		if self._ws.closed:
			return
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
			self._logger.error(
				"LiveKit session %s ping failed: %s",
				self.session_id,
				err,
			)


class WarmupSession:
	"""Warm a LiveKit room with a local aiortc peer connection when needed."""

	def __init__(
		self,
		signal: LiveKitSignalConnection,
		*,
		ice_servers: Iterable[ICEServer],
	) -> None:
		self._signal = signal
		self._logger = _LOGGER.getChild("warmup")

		self._peer_connection = RTCPeerConnection(configuration=RTCConfiguration(
			iceServers=[
				RTCIceServer(
					urls=list(ice_server.urls),
					username=ice_server.username or None,
					credential=ice_server.credential or None,
				)
				for ice_server in ice_servers
			],
			bundlePolicy=RTCBundlePolicy.MAX_BUNDLE,
		))
		self._peer_connection.addTransceiver("audio", direction="recvonly")
		self._peer_connection.addTransceiver("video", direction="recvonly")

		self._waiting_for_track_permissions = set(
			_CAMERA_TRACK_KIND_BY_TYPE_SOURCE.values()
		)
		self._track_sid_by_kind: dict[str, str] = {}
		self._allowed_track_sids: set[str] = set()

	async def run(
		self,
		responses: AsyncIterator[LiveKitSignalMessage],
	) -> None:
		"""Run warmup until camera tracks are published and allowed."""
		try:
			await self._send_peer_offer()
			await self._read_until_camera_tracks_allowed(responses)
		finally:
			await self._peer_connection.close()

	async def _read_until_camera_tracks_allowed(
		self,
		responses: AsyncIterator[LiveKitSignalMessage],
	) -> None:
		"""Read warmup signaling until camera tracks are allowed or the socket closes."""
		async for message in responses:
			match message:
				case ("answer", answer):
					await self._on_answer(answer)
				case ("trickle", trickle):
					await self._on_trickle(trickle)
				case ("media_sections_requirement", _):
					await self._send_peer_offer()
				case ("update", update):
					self._on_participant_update(update)
				case ("subscription_permission_update", subscription_permission_update):
					self._on_subscription_permission_update(
						subscription_permission_update
					)
				case ("subscription_response", subscription_response):
					if subscription_response.err:
						self._logger.warning(
							"LiveKit aiortc warmup subscription failed: track=%s error=%s",
							subscription_response.track_sid,
							subscription_response.err,
						)
				case (_, _):
					self._logger.debug(f"Unhandled LiveKit aiortc warmup message kind={message[0]}")

			if not self._waiting_for_track_permissions:
				return

		raise RuntimeError(f"LiveKit aiortc warmup websocket closed before camera track permissions were allowed: waiting_for={sorted(self._waiting_for_track_permissions)}")

	async def _send_peer_offer(self) -> None:
		await self._peer_connection.setLocalDescription()
		description = self._peer_connection.localDescription
		await self._signal.send(
			SignalRequest(offer=SessionDescription(type=description.type, sdp=description.sdp))
		)

	async def _on_answer(self, answer: SessionDescription) -> None:
		await self._peer_connection.setRemoteDescription(
			RTCSessionDescription(sdp=answer.sdp, type=answer.type)
		)

	async def _on_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.PUBLISHER:
			return

		if not trickle.candidateInit:
			if trickle.final:
				await self._peer_connection.addIceCandidate(None)
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

		candidate = candidate_from_sdp(candidate_sdp.removeprefix("candidate:"))
		sdp_mid = init.get("sdpMid")
		sdp_m_line_index = init.get("sdpMLineIndex")
		candidate.sdpMid = sdp_mid if isinstance(sdp_mid, str) else None
		candidate.sdpMLineIndex = (
			sdp_m_line_index if isinstance(sdp_m_line_index, int) else None
		)
		await self._peer_connection.addIceCandidate(candidate)

	def _on_participant_update(self, update: ParticipantUpdate) -> None:
		for participant in update.participants:
			if not participant.is_publisher:
				continue
			for track in participant.tracks:
				if not track.sid:
					continue
				if track_kind := _CAMERA_TRACK_KIND_BY_TYPE_SOURCE.get(
					(track.type, track.source)
				):
					self._track_sid_by_kind[track_kind] = track.sid
					if track.sid in self._allowed_track_sids:
						self._waiting_for_track_permissions.discard(track_kind)
					else:
						self._waiting_for_track_permissions.add(track_kind)

	def _on_subscription_permission_update(
		self,
		update: SubscriptionPermissionUpdate,
	) -> None:
		if update.allowed:
			self._allowed_track_sids.add(update.track_sid)
			if track_kind := self._track_kind_for_sid(update.track_sid):
				self._waiting_for_track_permissions.discard(track_kind)
		else:
			self._allowed_track_sids.discard(update.track_sid)
			if track_kind := self._track_kind_for_sid(update.track_sid):
				self._waiting_for_track_permissions.add(track_kind)

	def _track_kind_for_sid(self, track_sid: str) -> str | None:
		for track_kind, current_track_sid in self._track_sid_by_kind.items():
			if current_track_sid == track_sid:
				return track_kind
		return None


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
						has_audio = any(
							track.sid
							and track.type == AUDIO
							and track.source == MICROPHONE
							for participant in join.other_participants
							for track in participant.tracks
						)
						has_video = any(
							track.sid
							and track.type == VIDEO
							and track.source == CAMERA
							for participant in join.other_participants
							for track in participant.tracks
						)

						if has_audio and has_video:
							await signal.send(SignalRequest(offer=SessionDescription(type="offer", sdp=self._offer_sdp)))
							await self._continue_browser_session(responses, signal)
							return

						try:
							warmup = WarmupSession(signal, ice_servers=join.ice_servers)
							await warmup.run(responses)
						except Exception:
							self._logger.exception("LiveKit aiortc warmup failed; continuing with browser offer")

						break
					case ("leave", leave):
						raise RuntimeError(f"LiveKit left before join response: reason={leave.reason}")
					case (_, _):
						raise RuntimeError(f"LiveKit sent {message[0]} before join response")
			else:
				raise RuntimeError("LiveKit websocket closed before join response")

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
						if (
							(on_ice_servers := self._on_ice_servers)
							and (ice_servers := list(join.ice_servers))
						):
							on_ice_servers(ice_servers)
						await self._continue_browser_session(responses, signal)
						return
					case ("leave", leave):
						raise RuntimeError(f"LiveKit left before join response: reason={leave.reason}")
					case (_, _):
						raise RuntimeError(f"LiveKit sent {message[0]} before join response")

			raise RuntimeError("LiveKit websocket closed before join response")

	async def _continue_browser_session(
		self,
		responses: AsyncIterator[LiveKitSignalMessage],
		signal: LiveKitSignalConnection,
	) -> None:
		async for message in responses:
			match message:
				case ("join", _):
					self._logger.warning(
						"Ignoring unexpected LiveKit join after browser offer"
					)
				case ("answer", answer):
					await self._on_answer(answer, signal)
				case ("trickle", trickle):
					self._on_trickle(trickle)
				case ("media_sections_requirement", media_sections_requirement):
					self._on_media_sections_requirement(media_sections_requirement)
				case ("leave", leave):
					self._logger.warning(
						"LiveKit requested session leave: reason=%s action=%s can_reconnect=%s",
						leave.reason,
						leave.action,
						leave.can_reconnect,
					)
				case ("subscription_permission_update", _):
					pass
				case ("subscription_response", subscription_response):
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
				case ("pong_resp", _):
					pass
				case (_, _):
					self._logger.debug(f"Unhandled LiveKit signaling message kind={message[0]}")

	async def _on_answer(self, answer: SessionDescription, signal: LiveKitSignalConnection) -> None:
		await self._candidate_queue.flush(
			lambda candidate: signal.send(SignalRequest(trickle=candidate))
		)
		self._send_answer(answer)

	def _on_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.PUBLISHER:
			return

		self._send_candidate(trickle)

	def _on_media_sections_requirement(
		self,
		requirement: MediaSectionsRequirement,
	) -> None:
		if requirement.num_audios or requirement.num_videos:
			self._logger.warning(f"LiveKit requested extra media sections audio={requirement.num_audios} video={requirement.num_videos}; Home Assistant cannot apply renegotiation")
