"""Support for LiveKit WebRTC streams."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
import logging
import time
from typing import override
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
	RTCRtpReceiver,
	RTCRtpSender,
	RTCSessionDescription,
)
from aiortc.contrib.media import MediaRelay
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcdatachannel import RTCDataChannel
from aiortc.rtp import (
	AnyRtcpPacket,
	RTCP_PSFB_FIR,
	RTCP_PSFB_PLI,
	RtcpPsfbPacket,
)
from aiortc.sdp import SessionDescription as ParsedSessionDescription
from aiortc.sdp import candidate_from_sdp
from google.protobuf.internal.enum_type_wrapper import EnumTypeWrapper

from .packet_peer import PacketPeerConnection, PacketStreamTrack
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
	UpdateSubscription,
)
from .session import Session

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = "16"
_LIVEKIT_SDK = "python"
_LIVEKIT_SDK_VERSION_FALLBACK = "1.0.17"
_OPTIONAL_TRACK_READY_TIMEOUT = 1.0
_RTP_MEDIA_KINDS = {"audio", "video"}
_TRACK_POLL_INTERVAL = 0.05


@dataclass(frozen=True, slots=True)
class LiveKitTrackMetadata:
	"""LiveKit metadata for a remotely published media track."""

	participant_sid: str
	track_sid: str
	kind: str
	source: str
	mime_type: str
	codecs: tuple[str, ...]
	width: int
	height: int


LiveKitJoinCallback = Callable[[JoinResponse], Awaitable[None]]
LiveKitOfferCallback = Callable[[SessionDescription], Awaitable[RTCSessionDescription]]
LiveKitTracksCallback = Callable[[tuple[LiveKitTrackMetadata, ...]], Awaitable[None]]
LiveKitTrickleCallback = Callable[[TrickleRequest], Awaitable[None]]
LiveKitCloseCallback = Callable[[], Awaitable[None]]
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
		self._pending_livekit_trickle: list[TrickleRequest] = []
		self._closed = False
		self._join_handled = False
		self._logger = _LOGGER.getChild(f"engine.{session_id}")

		self._on_join_callback = on_join
		self._on_offer_callback = on_offer
		self._on_tracks_callback = on_tracks
		self._on_trickle_callback = on_trickle
		self._on_close_callback = on_close

		self._track_sids_by_participant: dict[str, set[str]] = {}
		self._subscribed_track_sids: set[str] = set()
		self._livekit_bootstrap_answered = False
		self._livekit_leave_received = False
		self._livekit_offer_count = 0
		self._livekit_media_offer_count = 0

		self._ws_endpoint = f"{livekit_url.rstrip('/')}/rtc?{urlencode({
			'access_token': user_token,
			'sdk': _LIVEKIT_SDK,
			'version': _livekit_sdk_version(),
			'protocol': _LIVEKIT_PROTOCOL_VERSION,
			'auto_subscribe': '0',
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
					self._logger.debug("LiveKit non-binary signaling message type=%s", msg.type)
					continue

				try:
					response = SignalResponse.FromString(msg.data)
				except Exception as err:
					self._logger.error("Error parsing LiveKit SignalResponse: %s", err)
					continue

				kind = response.WhichOneof("message")
				if kind == "join":
					await self._on_join(response.join)
				elif kind == "offer":
					await self._on_offer(response.offer)
				elif kind == "trickle":
					await self._on_livekit_trickle(response.trickle)
				elif kind == "update":
					tracks = self._remember_participants(response.update.participants, source="update")
					if tracks:
						await self._on_tracks_callback(tracks)
					await self._maybe_send_manual_subscription()
				elif kind == "leave":
					self._livekit_leave_received = True
					self._logger.debug(
						"LiveKit requested session leave: reason=%s action=%s can_reconnect=%s",
						_enum_name(livekit_models.DisconnectReason, response.leave.reason),
						_enum_name(LeaveRequest.Action, response.leave.action),
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
		tracks = self._remember_participants(join.other_participants, source="join")
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
		self._join_handled = True
		self._ping_task = asyncio.create_task(self._ping_loop(join.ping_interval or 5))
		pending, self._pending_livekit_trickle = self._pending_livekit_trickle, []
		for trickle in pending:
			await self._on_trickle_callback(trickle)

	async def _on_offer(self, offer: SessionDescription) -> None:
		if self._closed:
			return

		self._livekit_offer_count += 1
		parsed_offer = ParsedSessionDescription.parse(offer.sdp)
		has_media = any(
			media.kind in _RTP_MEDIA_KINDS and media.port != 0
			for media in parsed_offer.media
		)
		if has_media:
			self._livekit_media_offer_count += 1

		self._logger.debug(
			"Received LiveKit offer id=%s has_rtp=%s type=%s media=%s",
			offer.id,
			has_media,
			offer.type,
			_sdp_media_summary(parsed_offer),
		)
		answer = await self._on_offer_callback(offer)
		await self._send_livekit_answer(offer.id, answer)

		if not has_media:
			self._livekit_bootstrap_answered = True
			await self._maybe_send_manual_subscription()

	async def _send_livekit_answer(self, offer_id: int, answer: RTCSessionDescription) -> None:
		if not self._ws:
			self._logger.debug("Cannot send LiveKit SDP answer id=%s because WebSocket is closed", offer_id)
			return

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
			self._logger.debug("Ignoring LiveKit ICE candidate for target=%s", trickle.target)
			return
		if not self._join_handled:
			self._pending_livekit_trickle.append(trickle)
			return
		await self._on_trickle_callback(trickle)

	def _remember_participants(
		self,
		participants: Iterable[livekit_models.ParticipantInfo],
		*,
		source: str,
	) -> tuple[LiveKitTrackMetadata, ...]:
		participant_seen = False
		tracks: list[LiveKitTrackMetadata] = []
		for participant in participants:
			participant_seen = True
			track_sids: set[str] = set()
			for track in participant.tracks:
				if not track.sid:
					continue
				if track.type == livekit_models.AUDIO:
					kind = "audio"
				elif track.type == livekit_models.VIDEO:
					kind = "video"
				else:
					continue

				track_sids.add(track.sid)
				tracks.append(LiveKitTrackMetadata(
					participant_sid=participant.sid,
					track_sid=track.sid,
					kind=kind,
					source=_enum_name(livekit_models.TrackSource, track.source),
					mime_type=track.mime_type,
					codecs=tuple(codec.mime_type for codec in track.codecs if codec.mime_type),
					width=track.width,
					height=track.height,
				))
			if track_sids:
				self._track_sids_by_participant.setdefault(participant.sid, set()).update(track_sids)
		if participant_seen:
			self._logger.debug(
				"Received LiveKit participant tracks from %s: tracks=%s remembered=%s",
				source,
				tracks,
				{sid: sorted(track_sids) for sid, track_sids in self._track_sids_by_participant.items()},
			)
		return tuple(tracks)

	async def _maybe_send_manual_subscription(self) -> None:
		if not self._livekit_bootstrap_answered:
			return
		if not self._track_sids_by_participant:
			self._logger.debug("Manual LiveKit subscribe waiting for published track SIDs")
			return

		assert self._ws is not None
		all_track_sids = {sid for sids in self._track_sids_by_participant.values() for sid in sids}
		new_track_sids = sorted(all_track_sids - self._subscribed_track_sids)
		if not new_track_sids:
			return

		new_track_sid_set = set(new_track_sids)
		participant_tracks = [
			livekit_models.ParticipantTracks(
				participant_sid=participant_sid,
				track_sids=sorted(track_sids & new_track_sid_set),
			)
			for participant_sid, track_sids in self._track_sids_by_participant.items()
			if track_sids & new_track_sid_set
		]
		request = SignalRequest(subscription=UpdateSubscription(
			track_sids=new_track_sids,
			subscribe=True,
			participant_tracks=participant_tracks,
		))
		self._subscribed_track_sids.update(new_track_sids)
		self._logger.debug("Sending manual LiveKit subscription track_sids=%s", new_track_sids)
		await self._ws.send_bytes(request.SerializeToString())

	async def _ping_loop(self, interval_seconds: int) -> None:
		assert self._ws is not None
		while True:
			await asyncio.sleep(max(1, interval_seconds))
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
		for task in (self._reader_task, self._ping_task):
			if task and task is not current_task:
				try:
					await task
				except asyncio.CancelledError:
					pass

		if self._ws:
			await self._ws.close()
			self._ws = None
		if self._session:
			await self._session.close()
			self._session = None
		self._logger.debug(
			"Closed LiveKit engine offers=%s media_offers=%s manual_subscriptions=%s",
			self._livekit_offer_count,
			self._livekit_media_offer_count,
			sorted(self._subscribed_track_sids),
		)


class LiveKitSession(Session):
	"""Native LiveKit-to-Home Assistant WebRTC session."""

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
		self._media_relay = MediaRelay()
		self._livekit_tracks_by_kind: dict[str, PacketStreamTrack] = {}
		self._livekit_codecs_by_kind: dict[str, tuple[str, ...]] = {}
		self._hass_relay_tracks_by_kind: dict[str, MediaStreamTrack] = {}
		self._hass_senders_by_kind: dict[str, RTCRtpSender] = {}
		self._hass_outgoing_track_kinds: set[str] = set()
		self._livekit_datachannels: set[str] = set()
		self._livekit_pc: PacketPeerConnection | None = None
		self._hass_pc = PacketPeerConnection(
			session_id=session_id,
			logger=self._logger.getChild("hass_pc"),
		)

		@self._hass_pc.on("track")
		def on_track(track: MediaStreamTrack) -> None:
			self._logger.debug("Home Assistant incoming track ignored kind=%s id=%s", track.kind, track.id)

		@self._hass_pc.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			state = self._hass_pc.connectionState
			self._logger.debug("Home Assistant connectionState=%s", state)
			if state in {"failed", "closed"}:
				await self.close()

		@self._hass_pc.on("iceconnectionstatechange")
		async def on_iceconnectionstatechange() -> None:
			state = self._hass_pc.iceConnectionState
			self._logger.debug("Home Assistant iceConnectionState=%s", state)
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
		"""Answer a Home Assistant offer using ready LiveKit media."""
		await self._hass_pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
		await self._engine.start()
		await self._wait_for_livekit_track("video")
		if await self._wait_for_optional_livekit_track("audio", _OPTIONAL_TRACK_READY_TIMEOUT) is None:
			self._logger.debug("LiveKit audio track was not ready before initial Home Assistant answer")
		await self._answer_hass_offer()

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Handle an ICE candidate from the Home Assistant peer."""
		if self._closed:
			self._logger.debug("Ignoring Home Assistant ICE candidate for closed LiveKit session")
			return

		if not candidate.candidate:
			self._logger.debug("Home Assistant end-of-candidates")
			await self._hass_pc.addIceCandidate(None)
			return

		try:
			aiortc_candidate = _candidate_from_init(candidate.candidate)
			aiortc_candidate.sdpMid = candidate.sdp_mid
			aiortc_candidate.sdpMLineIndex = candidate.sdp_m_line_index
		except Exception as err:
			self._logger.error("Failed to parse Home Assistant ICE candidate: %s", err)
			return

		self._logger.debug(
			"Adding Home Assistant ICE candidate mid=%s index=%s type=%s",
			aiortc_candidate.sdpMid,
			aiortc_candidate.sdpMLineIndex,
			aiortc_candidate.type,
		)
		await self._hass_pc.addIceCandidate(aiortc_candidate)

	async def _on_livekit_join(self, join: JoinResponse) -> None:
		pc = PacketPeerConnection(
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
			session_id=self._session_id,
			logger=self._logger.getChild("livekit_pc"),
		)
		self._livekit_pc = pc

		@pc.on("track")
		def on_track(track: MediaStreamTrack) -> None:
			self._add_livekit_track(track)

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
			self._livekit_datachannels.add(channel.label)
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
			if track.kind in self._livekit_codecs_by_kind:
				continue

			codecs = tuple(dict.fromkeys(
				codec.lower()
				for codec in ((*track.codecs, track.mime_type) if track.mime_type else track.codecs)
				if codec
			))
			if codecs:
				self._livekit_codecs_by_kind[track.kind] = codecs

	async def _answer_livekit_offer(self, offer: SessionDescription) -> RTCSessionDescription:
		if self._livekit_pc is None:
			raise RuntimeError("LiveKit offer arrived before join created the peer connection")
		await self._livekit_pc.setRemoteDescription(RTCSessionDescription(sdp=offer.sdp, type=offer.type))
		for kind in ("audio", "video"):
			if not self._livekit_pc.remote_supported_codecs(kind):
				continue

			metadata_codecs = self._livekit_codecs_by_kind.get(kind)
			if metadata_codecs is None:
				raise RuntimeError(f"LiveKit {kind} offer arrived before track metadata")

			supported_codecs = self._hass_pc.answer_codecs(kind) or self._hass_pc.remote_supported_codecs(kind)
			codecs_for_published_track = [
				codec for metadata_codec in metadata_codecs
				for codec in supported_codecs
				if codec.mimeType.lower() == metadata_codec
			]
			if not codecs_for_published_track:
				raise RuntimeError(
					f"LiveKit {kind} track codecs {metadata_codecs} are not supported by the browser offer"
				)
			try:
				self._livekit_pc.constrain_answer_codecs_to_supported(kind, codecs_for_published_track)
			except RuntimeError as err:
				raise RuntimeError(
					f"LiveKit {kind} offer does not contain published track codecs {metadata_codecs}"
				) from err

		answer = await self._livekit_pc.createAnswer()
		await self._livekit_pc.setLocalDescription(answer)
		assert self._livekit_pc.localDescription is not None
		return self._livekit_pc.localDescription

	async def _add_livekit_candidate(self, trickle: TrickleRequest) -> None:
		if self._livekit_pc is None:
			raise RuntimeError("LiveKit ICE candidate arrived before join created the peer connection")
		if trickle.final and not trickle.candidateInit:
			self._logger.debug("LiveKit end-of-candidates")
			await self._livekit_pc.addIceCandidate(None)
			return
		if not trickle.candidateInit:
			return

		init = json.loads(trickle.candidateInit)
		candidate = _candidate_from_init(str(init.get("candidate", "")))
		candidate.sdpMid = init.get("sdpMid")
		candidate.sdpMLineIndex = init.get("sdpMLineIndex")
		self._logger.debug(
			"Adding LiveKit ICE candidate mid=%s index=%s type=%s",
			candidate.sdpMid,
			candidate.sdpMLineIndex,
			candidate.type,
		)
		await self._livekit_pc.addIceCandidate(candidate)

	async def _answer_hass_offer(self) -> None:
		for kind, incoming_track in self._livekit_tracks_by_kind.items():
			if not incoming_track.ready or kind in self._hass_outgoing_track_kinds:
				continue
			if kind not in self._hass_relay_tracks_by_kind:
				self._hass_relay_tracks_by_kind[kind] = self._media_relay.subscribe(
					incoming_track,
					buffered=False,
				)
			sender = self._hass_pc.addTrack(self._hass_relay_tracks_by_kind[kind])
			self._hass_senders_by_kind[kind] = sender
			self._hass_outgoing_track_kinds.add(kind)
			if kind == "video":
				self._install_keyframe_forwarding(sender, incoming_track.receiver)
			self._logger.debug("Added LiveKit %s packet stream track to Home Assistant PC", kind)

		if self._livekit_pc is None:
			raise RuntimeError("Cannot answer Home Assistant offer before LiveKit join")
		self._hass_pc.constrain_answer_codecs_from(self._livekit_pc)
		answer = await self._hass_pc.createAnswer()
		await self._hass_pc.setLocalDescription(answer)
		assert self._hass_pc.localDescription is not None
		self._send_answer(self._hass_pc.localDescription.sdp)

	def _add_livekit_track(self, track: MediaStreamTrack) -> None:
		if track.kind not in {"audio", "video"}:
			return
		if not isinstance(track, PacketStreamTrack):
			raise RuntimeError(f"LiveKit {track.kind} track does not support packet streams")
		if track.kind in self._livekit_tracks_by_kind:
			self._logger.debug("Ignoring duplicate LiveKit %s track id=%s", track.kind, track.id)
			return

		self._livekit_tracks_by_kind[track.kind] = track
		self._logger.debug("Prepared LiveKit %s packet stream track", track.kind)

	async def _wait_for_livekit_track(self, kind: str) -> PacketStreamTrack:
		while not self._closed:
			if (track := self._livekit_tracks_by_kind.get(kind)) is not None:
				if not track.ready:
					raise RuntimeError(f"LiveKit {track.kind} packet stream ended before it was ready")
				return track
			await asyncio.sleep(_TRACK_POLL_INTERVAL)
		raise RuntimeError(f"LiveKit session closed before {kind} media was ready")

	async def _wait_for_optional_livekit_track(
		self,
		kind: str,
		timeout: float,
	) -> PacketStreamTrack | None:
		deadline = asyncio.get_running_loop().time() + timeout
		while not self._closed and asyncio.get_running_loop().time() < deadline:
			track = self._livekit_tracks_by_kind.get(kind)
			if track is not None and track.ready:
				return track
			await asyncio.sleep(_TRACK_POLL_INTERVAL)
		return None

	def _install_keyframe_forwarding(
		self,
		sender: RTCRtpSender,
		incoming_receiver: RTCRtpReceiver,
	) -> None:
		original_handler = sender._handle_rtcp_packet

		async def handle_rtcp(packet: AnyRtcpPacket) -> None:
			if isinstance(packet, RtcpPsfbPacket) and packet.fmt in (RTCP_PSFB_FIR, RTCP_PSFB_PLI):
				sources = incoming_receiver.getSynchronizationSources()
				if not sources:
					self._logger.debug(
						"Home Assistant requested video keyframe before incoming media SSRC was known"
					)
				for source in sources:
					self._logger.debug("Forwarding Home Assistant keyframe request to LiveKit ssrc=%s", source.source)
					await incoming_receiver._send_rtcp_pli(source.source)
			await original_handler(packet)

		sender._handle_rtcp_packet = handle_rtcp

	@override
	async def close(self) -> None:
		"""Close this Home Assistant and LiveKit WebRTC session."""
		if self._closed:
			return
		self._closed = True
		try:
			await self._engine.close()
		finally:
			for track in self._livekit_tracks_by_kind.values():
				track.end_from_source()
			if self._livekit_pc is not None:
				await self._livekit_pc.close()
				self._livekit_pc = None
			await self._hass_pc.close()
		self._logger.debug(
			"Closed LiveKit Home Assistant session tracks=%s datachannels=%s",
			sorted(self._hass_outgoing_track_kinds),
			sorted(self._livekit_datachannels),
		)


def _sdp_media_summary(parsed_sdp: ParsedSessionDescription) -> list[dict[str, object]]:
	"""Return a redacted media summary for LiveKit SDP debug logs."""
	summary: list[dict[str, object]] = []
	for media in parsed_sdp.media:
		entry: dict[str, object] = {
			"mid": media.rtp.muxId,
			"kind": media.kind,
			"port": media.port,
			"direction": media.direction,
		}
		if media.kind in {"audio", "video"}:
			entry["codecs"] = [
				{
					"mimeType": codec.mimeType,
					"payloadType": codec.payloadType,
					"parameters": codec.parameters,
				}
				for codec in media.rtp.codecs
			]
		elif media.kind == "application":
			entry["sctp_port"] = media.sctp_port
		summary.append(entry)
	return summary


def _candidate_from_init(candidate: str) -> RTCIceCandidate:
	"""Parse RTCIceCandidateInit.candidate text for aiortc."""
	return candidate_from_sdp(candidate.removeprefix("candidate:"))


def _livekit_sdk_version() -> str:
	try:
		return version("livekit")
	except PackageNotFoundError:
		return _LIVEKIT_SDK_VERSION_FALLBACK


def _enum_name(enum_type: EnumTypeWrapper, value: int) -> str:
	try:
		return enum_type.Name(value)
	except ValueError:
		return str(value)
