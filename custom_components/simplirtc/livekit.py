"""Support for LiveKit WebRTC streams."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
import json
import logging
import time
from typing import Any, override
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)
from webrtc_models import RTCIceCandidateInit
from aiortc import (
	RTCConfiguration,
	RTCIceCandidate,
	RTCIceServer,
	RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcdatachannel import RTCDataChannel
from aiortc.sdp import candidate_from_sdp

from .protobufs import livekit_models_pb2 as livekit_models
from .protobufs.livekit_rtc_pb2 import (
	JoinResponse,
	LeaveRequest,
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


@dataclass(frozen=True, slots=True)
class LiveKitTrackMetadata:
	"""LiveKit metadata for a remotely published media track."""

	participant_sid: str
	track_sid: str
	kind: str
	mime_type: str
	codecs: tuple[str, ...]
	width: int
	height: int


LiveKitJoinCallback = Callable[[JoinResponse], Coroutine[Any, Any, None]]
LiveKitOfferCallback = Callable[[SessionDescription], Coroutine[Any, Any, RTCSessionDescription]]
LiveKitTracksCallback = Callable[[tuple[LiveKitTrackMetadata, ...]], Coroutine[Any, Any, None]]
LiveKitTrickleCallback = Callable[[TrickleRequest], Coroutine[Any, Any, None]]
LiveKitCloseCallback = Callable[[], Coroutine[Any, Any, None]]
SendAnswer = Callable[[str], None]


class LiveKitEngine:
	"""LiveKit websocket signaling engine."""

	def __init__(
		self,
		*,
		session_id: str,
		livekit_url: str,
		user_token: str,
		on_join: LiveKitJoinCallback,
		on_offer: LiveKitOfferCallback,
		on_tracks: LiveKitTracksCallback,
		on_trickle: LiveKitTrickleCallback,
		on_close: LiveKitCloseCallback,
	) -> None:
		self._session: ClientSession | None = None
		self._ws: ClientWebSocketResponse | None = None
		self._reader_task: asyncio.Task[None] | None = None
		self._ping_task: asyncio.Task[None] | None = None
		self._callback_tasks: set[asyncio.Task[None]] = set()
		self._offer_lock = asyncio.Lock()
		self._join_ready = asyncio.Event()
		self._closed = False
		self._logger = _LOGGER.getChild(f"engine.{session_id}")

		self._on_join_callback = on_join
		self._on_offer_callback = on_offer
		self._on_tracks_callback = on_tracks
		self._on_trickle_callback = on_trickle
		self._on_close_callback = on_close

		self._livekit_leave_received = False

		self._ws_endpoint = f"{livekit_url.rstrip('/')}/rtc?{urlencode({
			'access_token': user_token,
			'sdk': _LIVEKIT_SDK,
			'version': _LIVEKIT_SDK_VERSION,
			'protocol': _LIVEKIT_PROTOCOL_VERSION,
			'auto_subscribe': '1',
		})}"

	async def start(self) -> None:
		"""Start LiveKit signaling."""
		try:
			self._session = session = ClientSession()
			self._ws = await session.ws_connect(self._ws_endpoint)
			self._reader_task = asyncio.create_task(self._read())
		except Exception as err:
			self._logger.error("Error in LiveKit engine setup: %s", err)
			await self.close()
			raise

	async def _read(self) -> None:
		try:
			assert self._ws is not None, "WebSocket connection not established"
			async for msg in self._ws:
				if msg.type != WSMsgType.BINARY:
					raise RuntimeError(f"LiveKit sent non-binary signaling message type={msg.type}")

				try:
					response = SignalResponse.FromString(msg.data)
				except Exception as err:
					raise RuntimeError("Error parsing LiveKit SignalResponse") from err

				kind = response.WhichOneof("message")
				if kind == "join":
					await self._on_join(response.join)
				elif kind == "offer":
					self._start_callback_task(self._on_offer(response.offer))
				elif kind == "trickle":
					self._start_callback_task(self._on_livekit_trickle(response.trickle))
				elif kind == "update":
					tracks = self._track_metadata_from_participants(response.update.participants, source="update")
					if tracks:
						await self._on_tracks_callback(tracks)
				elif kind == "leave":
					self._livekit_leave_received = True
					self._logger.debug(
						"LiveKit requested session leave: reason=%s action=%s can_reconnect=%s",
						response.leave.reason,
						response.leave.action,
						response.leave.can_reconnect,
					)
					break
				elif kind == "pong_resp":
					self._logger.debug("LiveKit pong timestamp=%s", response.pong_resp.timestamp)
				else:
					self._logger.debug("LiveKit signaling message kind=%s", kind)
		except asyncio.CancelledError:
			raise
		except Exception as err:
			self._logger.error("Error in LiveKit WebSocket read loop: %s", err)
		finally:
			self._reader_task = None
			await self.close()
			await self._on_close_callback()

	async def _on_join(self, join: JoinResponse) -> None:
		tracks = self._track_metadata_from_participants(join.other_participants, source="join")
		self._logger.debug(
			"LiveKit join accepted: room=%s participant=%s subscriber_primary=%s fast_publish=%s "
			"server_version=%s server_region=%s",
			join.room.name or join.room.sid,
			join.participant.identity or join.participant.sid,
			join.subscriber_primary,
			join.fast_publish,
			join.server_version or join.server_info.version,
			join.server_region or join.server_info.region,
		)

		if tracks:
			await self._on_tracks_callback(tracks)
		await self._on_join_callback(join)
		if join.ping_interval <= 0:
			raise RuntimeError(f"LiveKit join had invalid ping_interval={join.ping_interval}")
		self._ping_task = asyncio.create_task(self._ping_loop(join.ping_interval))
		self._join_ready.set()

	async def _on_offer(self, offer: SessionDescription) -> None:
		async with self._offer_lock:
			if self._closed:
				return

			self._logger.debug(
				"Received LiveKit offer id=%s type=%s",
				offer.id,
				offer.type,
			)
			answer = await self._on_offer_callback(offer)
			await self._send_livekit_answer(offer.id, answer)

	async def _send_livekit_answer(self, offer_id: int, answer: RTCSessionDescription) -> None:
		if not self._ws:
			raise RuntimeError(f"Cannot send LiveKit SDP answer id={offer_id} because WebSocket is closed")

		request = SignalRequest(answer=SessionDescription(
			type=answer.type,
			sdp=answer.sdp,
			id=offer_id,
		))
		self._logger.debug("Sending LiveKit answer id=%s", offer_id)
		await self._ws.send_bytes(request.SerializeToString())

	async def _send_livekit_leave(self) -> None:
		if self._livekit_leave_received or self._ws is None or self._ws.closed:
			return

		request = SignalRequest(leave=LeaveRequest(
			can_reconnect=False,
			reason=livekit_models.CLIENT_INITIATED,
			action=LeaveRequest.DISCONNECT,
		))
		try:
			await self._ws.send_bytes(request.SerializeToString())
		except Exception as err:
			self._logger.debug("Failed to send LiveKit leave request during close: %s", err)
		else:
			self._logger.debug("Sent LiveKit leave request")

	async def _on_livekit_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.SUBSCRIBER:
			raise RuntimeError(f"LiveKit ICE candidate had unexpected target={trickle.target}")
		await self._join_ready.wait()
		if self._closed:
			return
		await self._on_trickle_callback(trickle)

	def _start_callback_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
		task = asyncio.create_task(coroutine)
		self._callback_tasks.add(task)
		task.add_done_callback(self._callback_tasks.discard)
		task.add_done_callback(self._log_callback_task_error)

	def _log_callback_task_error(self, task: asyncio.Task[None]) -> None:
		if task.cancelled():
			return
		try:
			task.result()
		except Exception as err:
			self._logger.error("Error in LiveKit signaling callback: %s", err)
			if not self._closed:
				asyncio.create_task(self.close())

	def _track_metadata_from_participants(
		self,
		participants: Iterable[livekit_models.ParticipantInfo],
		*,
		source: str,
	) -> tuple[LiveKitTrackMetadata, ...]:
		tracks: list[LiveKitTrackMetadata] = []
		for participant in participants:
			for track in participant.tracks:
				if track.type == livekit_models.AUDIO:
					kind = "audio"
				elif track.type == livekit_models.VIDEO:
					kind = "video"
				else:
					raise RuntimeError(f"LiveKit track {track.sid} from {source} has unsupported type={track.type}")

				if not track.sid:
					raise RuntimeError(f"LiveKit {kind} track from {source} is missing sid")
				if not track.mime_type:
					raise RuntimeError(f"LiveKit {kind} track {track.sid} from {source} is missing mime_type")
				codecs = tuple(codec.mime_type for codec in track.codecs)
				if not codecs or any(not codec for codec in codecs):
					raise RuntimeError(f"LiveKit {kind} track {track.sid} from {source} is missing codec metadata")

				tracks.append(LiveKitTrackMetadata(
					participant_sid=participant.sid,
					track_sid=track.sid,
					kind=kind,
					mime_type=track.mime_type,
					codecs=codecs,
					width=track.width,
					height=track.height,
				))
		if tracks:
			self._logger.debug(
				"Received LiveKit participant tracks from %s: tracks=%s",
				source,
				tracks,
			)
		return tuple(tracks)

	async def _ping_loop(self, interval_seconds: int) -> None:
		assert self._ws is not None
		while True:
			await asyncio.sleep(interval_seconds)
			if self._closed or self._ws.closed:
				return
			request = SignalRequest(ping_req=Ping(timestamp=int(time.time() * 1000)))
			await self._ws.send_bytes(request.SerializeToString())

	async def close(self) -> None:
		"""Close this LiveKit engine."""
		if self._closed:
			return
		self._closed = True
		await self._send_livekit_leave()

		current_task = asyncio.current_task()
		for task in (self._reader_task, self._ping_task):
			if task and task is not current_task:
				task.cancel()
		for task in tuple(self._callback_tasks):
			if task is not current_task:
				task.cancel()
		for task in (self._reader_task, self._ping_task):
			if task and task is not current_task:
				try:
					await task
				except asyncio.CancelledError:
					pass
		for task in tuple(self._callback_tasks):
			if task is not current_task:
				try:
					await task
				except asyncio.CancelledError:
					pass
				except Exception:
					pass

		if self._ws:
			await self._ws.close()
			self._ws = None
		if self._session:
			await self._session.close()
			self._session = None
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
		self._session_id = session_id
		self._send_answer = send_answer
		self._closed = False
		self._logger = _LOGGER.getChild(f"session.{session_id}")
		self._expected_livekit_track_kinds: set[str] = set()
		self._peer_offer_ready = asyncio.Event()
		self._livekit_pc_ready = asyncio.Event()
		self._livekit_codec_events_by_kind = {
			"audio": asyncio.Event(),
			"video": asyncio.Event(),
		}
		self._livekit_remote_description_ready = asyncio.Event()
		self._livekit_media_answer_ready = asyncio.Event()
		self._peer_answer_ready = asyncio.Event()
		self._livekit_codecs_by_kind: dict[str, tuple[str, ...]] = {}
		self._rtp_bridge = RawRtpBridge()
		self._livekit_pc: RawRtpPeerConnection | None = None
		self._peer_pc = RawRtpPeerConnection(bridge=self._rtp_bridge, side="peer")

		@self._peer_pc.on("track")
		def on_track(track: MediaStreamTrack) -> None:
			self._logger.debug("Peer incoming track ignored kind=%s id=%s", track.kind, track.id)

		@self._peer_pc.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			state = self._peer_pc.connectionState
			self._logger.debug("Peer connectionState=%s", state)
			if state in {"failed", "closed"}:
				await self.close()

		@self._peer_pc.on("iceconnectionstatechange")
		async def on_iceconnectionstatechange() -> None:
			state = self._peer_pc.iceConnectionState
			self._logger.debug("Peer iceConnectionState=%s", state)
			if state == "failed":
				await self.close()

		self._engine = LiveKitEngine(
			session_id=session_id,
			livekit_url=livekit_url,
			user_token=user_token,
			on_join=self._on_livekit_join,
			on_offer=self._answer_livekit_offer,
			on_tracks=self._on_livekit_tracks,
			on_trickle=self._add_livekit_candidate,
			on_close=self.close,
		)

	@classmethod
	async def create(
		cls,
		*,
		session_id: str,
		send_answer: SendAnswer,
		livekit_url: str,
		user_token: str,
		offer_sdp: str,
	) -> LiveKitSession:
		session = cls(
			session_id=session_id,
			send_answer=send_answer,
			livekit_url=livekit_url,
			user_token=user_token,
		)
		try:
			await session.stream(offer_sdp)
			return session
		except (asyncio.CancelledError, Exception):
			await session.close()
			raise

	@override
	async def stream(self, offer_sdp: str) -> None:
		"""Answer a peer offer using ready LiveKit media."""
		await self._peer_pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
		self._peer_offer_ready.set()
		await self._engine.start()
		await self._livekit_media_answer_ready.wait()
		if self._closed:
			raise RuntimeError("LiveKit session closed before media offer was answered")
		await self._answer_peer_offer()

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Handle an ICE candidate from the peer."""
		if self._closed:
			self._logger.debug("Ignoring peer ICE candidate for closed LiveKit session")
			return

		if not candidate.candidate:
			self._logger.debug("Peer end-of-candidates")
			await self._peer_pc.addIceCandidate(None)
			return

		try:
			aiortc_candidate = _candidate_from_init(candidate.candidate)
			aiortc_candidate.sdpMid = candidate.sdp_mid
			aiortc_candidate.sdpMLineIndex = candidate.sdp_m_line_index
		except Exception as err:
			self._logger.error("Failed to parse peer ICE candidate: %s", err)
			return

		self._logger.debug(
			"Adding peer ICE candidate mid=%s index=%s type=%s",
			aiortc_candidate.sdpMid,
			aiortc_candidate.sdpMLineIndex,
			aiortc_candidate.type,
		)
		await self._peer_pc.addIceCandidate(aiortc_candidate)

	async def _on_livekit_join(self, join: JoinResponse) -> None:
		pc = RawRtpPeerConnection(
			bridge=self._rtp_bridge,
			side="livekit",
			configuration=RTCConfiguration(
				iceServers=[
					RTCIceServer(
						urls=list(server.urls),
						username=server.username or None,
						credential=server.credential or None,
					)
					for server in join.ice_servers
				]
			),
		)
		self._livekit_pc = pc
		self._livekit_pc_ready.set()

		@pc.on("track")
		def on_track(track: MediaStreamTrack) -> None:
			self._logger.debug("LiveKit track will be routed kind=%s id=%s", track.kind, track.id)

		@pc.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			state = pc.connectionState
			self._logger.debug("LiveKit connectionState=%s", state)
			if state in {"failed", "closed"}:
				await self.close()

		@pc.on("iceconnectionstatechange")
		async def on_iceconnectionstatechange() -> None:
			state = pc.iceConnectionState
			self._logger.debug("LiveKit iceConnectionState=%s", state)
			if state == "failed":
				await self.close()

		@pc.on("datachannel")
		def on_datachannel(channel: RTCDataChannel) -> None:
			self._logger.debug(
				"LiveKit datachannel accepted label=%s id=%s ordered=%s",
				channel.label,
				channel.id,
				channel.ordered,
			)

			@channel.on("message")
			def on_message(message: bytes | str) -> None:
				size = len(message) if isinstance(message, (bytes, bytearray, str)) else None
				self._logger.debug(
					"LiveKit datachannel message dropped label=%s type=%s size=%s",
					channel.label,
					type(message).__name__,
					size,
				)

	async def _on_livekit_tracks(self, tracks: tuple[LiveKitTrackMetadata, ...]) -> None:
		for track in tracks:
			codecs = tuple(dict.fromkeys(
				codec.lower()
				for codec in (*track.codecs, track.mime_type)
				if codec
			))
			if not codecs:
				raise RuntimeError(f"LiveKit {track.kind} track {track.track_sid} has no codecs")

			if track.kind in self._livekit_codecs_by_kind:
				if self._livekit_codecs_by_kind[track.kind] != codecs:
					raise RuntimeError(
						f"LiveKit {track.kind} track codec metadata changed "
						f"from {self._livekit_codecs_by_kind[track.kind]} to {codecs}"
					)
				continue

			self._livekit_codecs_by_kind[track.kind] = codecs
			self._livekit_codec_events_by_kind[track.kind].set()

	async def _answer_livekit_offer(self, offer: SessionDescription) -> RTCSessionDescription:
		await self._peer_offer_ready.wait()
		await self._livekit_pc_ready.wait()
		if self._closed:
			raise RuntimeError("LiveKit session closed before offer could be answered")
		livekit_pc = self._livekit_pc
		if livekit_pc is None:
			raise RuntimeError("LiveKit PC milestone fired without a peer connection")

		await livekit_pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
		self._livekit_remote_description_ready.set()
		offer_track_kinds = {
			kind for kind in ("audio", "video")
			if livekit_pc.remote_supported_codecs(kind)
		}
		routed_track_kinds = {
			kind for kind in offer_track_kinds
			if self._peer_pc.remote_supported_codecs(kind)
		}
		if offer_track_kinds and not routed_track_kinds:
			raise RuntimeError("LiveKit media offer has no audio or video compatible with the peer offer")
		self._expected_livekit_track_kinds.update(routed_track_kinds)
		if routed_track_kinds:
			for kind in routed_track_kinds:
				await self._livekit_codec_events_by_kind[kind].wait()
				if self._closed:
					raise RuntimeError("LiveKit session closed before track metadata was ready")

		for kind in offer_track_kinds:
			if kind not in routed_track_kinds:
				livekit_pc.set_answer_direction(kind, "inactive")
				continue

			metadata_codecs = self._livekit_codecs_by_kind[kind]
			supported_codecs = self._peer_pc.remote_supported_codecs(kind)
			codecs_for_published_track = [
				codec for metadata_codec in metadata_codecs
				for codec in supported_codecs
				if codec.mimeType.lower() == metadata_codec
			]
			if not codecs_for_published_track:
				raise RuntimeError(
					f"LiveKit {kind} track codecs {metadata_codecs} are not supported by the peer offer"
				)
			try:
				livekit_pc.constrain_answer_codecs(kind, codecs_for_published_track)
			except RuntimeError as err:
				raise RuntimeError(
					f"LiveKit {kind} offer does not contain published track codecs {metadata_codecs}"
				) from err

		await livekit_pc.setLocalDescription()
		if routed_track_kinds:
			self._livekit_media_answer_ready.set()
			await self._peer_answer_ready.wait()
			if self._closed:
				raise RuntimeError("LiveKit session closed before peer answer was ready")
		assert livekit_pc.localDescription is not None
		return livekit_pc.localDescription

	async def _add_livekit_candidate(self, trickle: TrickleRequest) -> None:
		await self._livekit_pc_ready.wait()
		await self._livekit_remote_description_ready.wait()
		if self._closed:
			return
		livekit_pc = self._livekit_pc
		if livekit_pc is None:
			raise RuntimeError("LiveKit PC milestone fired without a peer connection")
		if trickle.final and not trickle.candidateInit:
			self._logger.debug("LiveKit end-of-candidates")
			await livekit_pc.addIceCandidate(None)
			return
		if not trickle.candidateInit:
			raise RuntimeError("LiveKit sent a non-final ICE candidate without candidateInit")

		init = json.loads(trickle.candidateInit)
		candidate_init = init["candidate"]
		sdp_mid = init["sdpMid"]
		sdp_m_line_index = init["sdpMLineIndex"]
		if not isinstance(candidate_init, str) or not candidate_init:
			raise RuntimeError(f"LiveKit ICE candidate has invalid candidate field: {init}")
		if not isinstance(sdp_mid, str) or not sdp_mid:
			raise RuntimeError(f"LiveKit ICE candidate has invalid sdpMid field: {init}")
		if not isinstance(sdp_m_line_index, int):
			raise RuntimeError(f"LiveKit ICE candidate has invalid sdpMLineIndex field: {init}")
		candidate = _candidate_from_init(candidate_init)
		candidate.sdpMid = sdp_mid
		candidate.sdpMLineIndex = sdp_m_line_index
		self._logger.debug(
			"Adding LiveKit ICE candidate mid=%s index=%s type=%s",
			candidate.sdpMid,
			candidate.sdpMLineIndex,
			candidate.type,
		)
		await livekit_pc.addIceCandidate(candidate)

	async def _answer_peer_offer(self) -> None:
		await self._livekit_pc_ready.wait()
		if self._closed:
			raise RuntimeError("LiveKit session closed before peer offer could be answered")
		livekit_pc = self._livekit_pc
		if livekit_pc is None:
			raise RuntimeError("LiveKit PC milestone fired without a peer connection")
		self._peer_pc.constrain_answer_codecs_from(
			livekit_pc,
			kinds=tuple(kind for kind in ("audio", "video") if kind in self._expected_livekit_track_kinds),
		)
		for kind in ("audio", "video"):
			if not self._peer_pc.remote_supported_codecs(kind):
				continue
			direction = "sendonly" if kind in self._expected_livekit_track_kinds else "inactive"
			self._peer_pc.set_answer_direction(kind, direction)
		await self._peer_pc.setLocalDescription()
		assert self._peer_pc.localDescription is not None
		self._send_answer(self._peer_pc.localDescription.sdp)
		self._peer_answer_ready.set()

	@override
	async def close(self) -> None:
		"""Close this peer and LiveKit WebRTC session."""
		if self._closed:
			return
		self._closed = True
		try:
			await self._engine.close()
		finally:
			self._peer_offer_ready.set()
			self._livekit_pc_ready.set()
			for event in self._livekit_codec_events_by_kind.values():
				event.set()
			self._livekit_remote_description_ready.set()
			self._livekit_media_answer_ready.set()
			self._peer_answer_ready.set()
			if self._livekit_pc is not None:
				await self._livekit_pc.close()
				self._livekit_pc = None
			await self._peer_pc.close()
		self._logger.debug("Closed LiveKit peer session")


def _candidate_from_init(candidate: str) -> RTCIceCandidate:
	"""Parse RTCIceCandidateInit.candidate text for aiortc."""
	return candidate_from_sdp(candidate.removeprefix("candidate:"))
