"""Support for LiveKit WebRTC signaling."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import (
	AsyncGenerator,
	AsyncIterator,
	Callable,
	Iterable,
)
from contextlib import asynccontextmanager
import gzip
import json
import logging
from secrets import token_hex
import time
from typing import Literal, TypeAlias, override
from urllib.parse import urlencode

from aiohttp import (
	ClientSession,
	ClientWebSocketResponse,
	WSMsgType,
)
from homeassistant.components.camera import (
	WebRTCAnswer,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCCandidate,  # pyright: ignore[reportPrivateImportUsage]
	WebRTCSendMessage,  # pyright: ignore[reportPrivateImportUsage]
)
from webrtc_models import RTCIceCandidateInit

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
from .webrtc import MessageQueue, SimpliSafeWebRTCSession

_LOGGER = logging.getLogger(__name__)

_LIVEKIT_PROTOCOL_VERSION = 16
_LIVEKIT_SDK_VERSION = "simplirtc"
_CAMERA_TRACK_KIND_BY_TYPE_SOURCE = {
	(AUDIO, MICROPHONE): "audio",
	(VIDEO, CAMERA): "video",
}

SessionClosedCallback = Callable[[], None]
SendIceServers = Callable[[list[ICEServer]], None]
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


class LiveKitSignal:
	"""A wrapped LiveKit websocket signal task."""

	def __init__(
		self,
		*,
		ws: ClientWebSocketResponse,
		logger: logging.Logger,
		task_group: asyncio.TaskGroup,
	) -> None:
		self._ws = ws
		self._logger = logger
		self._task_group = task_group
		self._ping_task: asyncio.Task[None] | None = None

	@classmethod
	@asynccontextmanager
	async def connect(
		cls,
		url: str,
		*,
		token: str,
		auto_subscribe: bool,
		logger: logging.Logger,
		offer_sdp: str | None = None,
	) -> AsyncGenerator[LiveKitSignal, None]:
		"""Connect to LiveKit signaling and yield a running signal."""
		async with (
			asyncio.TaskGroup() as task_group,
			ClientSession() as http_session,
			http_session.ws_connect(
				cls._join_url(
					url,
					auto_subscribe=auto_subscribe,
					offer_sdp=offer_sdp,
				),
				headers={"Authorization": f"Bearer {token}"},
			) as ws,
		):
			yield cls(
				ws=ws,
				logger=logger,
				task_group=task_group,
			)

	def __aiter__(self) -> AsyncIterator[LiveKitSignalMessage]:
		"""Iterate over parsed LiveKit signaling messages."""
		return self.messages()

	@staticmethod
	def _join_url(
		url: str,
		*,
		auto_subscribe: bool,
		offer_sdp: str | None = None,
	) -> str:
		"""Return the wrapped LiveKit join URL."""
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
		return f"{url.rstrip('/')}/rtc?{urlencode({
			'join_request': base64.urlsafe_b64encode(
				wrapped_join_request.SerializeToString()
			).decode(),
		})}"

	async def messages(self) -> AsyncIterator[LiveKitSignalMessage]:
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

			yield self._response_message(response)

	def _response_message(
		self, response: SignalResponse,
	) -> LiveKitSignalMessage:
		match kind := response.WhichOneof("message"):
			case "join":
				self._start_ping(response.join.ping_interval)
				return kind, response.join
			case "answer":
				return kind, response.answer
			case "offer":
				return kind, response.offer
			case "trickle":
				return kind, response.trickle
			case "update":
				return kind, response.update
			case "media_sections_requirement":
				return kind, response.media_sections_requirement
			case "leave":
				return kind, response.leave
			case "subscription_permission_update":
				return kind, response.subscription_permission_update
			case "subscription_response":
				return kind, response.subscription_response
			case "pong_resp":
				return kind, response.pong_resp
			case _:
				self._logger.debug(
					"Unhandled LiveKit signaling response: %s",
					response,
				)
				return "response", response

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
		self._ping_task = self._task_group.create_task(
			self._ping_loop(interval_seconds),
			name="simplirtc-livekit-ping",
		)

	async def _ping_loop(self, interval_seconds: int) -> None:
		try:
			while not self._ws.closed:
				await asyncio.sleep(interval_seconds)
				await self.send(
					SignalRequest(ping_req=Ping(timestamp=int(time.time() * 1000)))
				)
		except asyncio.CancelledError:
			raise
		except Exception as err:
			self._logger.error("LiveKit ping failed: %s", err)
			raise
		finally:
			self._ping_task = None


class WarmupSession:
	"""Warm a LiveKit room with a local aiortc peer connection when needed."""

	def __init__(
		self,
		signal: LiveKitSignal,
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

	async def run(self) -> None:
		"""Run warmup until camera tracks are published and allowed."""
		try:
			await self._send_peer_offer()
			async for message in self._signal:
				match message:
					case ("answer", answer):
						await self._on_answer(answer)
					case ("trickle", trickle):
						await self._on_trickle(trickle)
					case ("media_sections_requirement", _):
						await self._send_peer_offer()
					case ("update", update):
						self._on_participant_update(update)
					case ("subscription_permission_update", update):
						self._on_subscription_permission_update(update)
					case ("subscription_response", response):
						if response.err:
							self._logger.warning(
								"LiveKit aiortc warmup subscription failed: track=%s error=%s",
								response.track_sid,
								response.err,
							)
					case ("pong_resp", _):
						pass
					case (kind, response):
						self._logger.debug(
							"Unhandled LiveKit aiortc warmup message kind=%s response=%s",
							kind,
							response,
						)

				if not self._waiting_for_track_permissions:
					return

			raise RuntimeError(
				"LiveKit aiortc warmup websocket closed before camera track permissions were allowed: waiting_for="
				f"{sorted(self._waiting_for_track_permissions)}"
			)
		finally:
			await self._peer_connection.close()

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

class LiveKitSession(SimpliSafeWebRTCSession):
	"""A browser-offer LiveKit signaling session for Home Assistant."""

	def __init__(
		self,
		*,
		livekit_url: str,
		user_token: str,
		send_ice_servers: SendIceServers,
	) -> None:
		self._livekit_url = livekit_url
		self._user_token = user_token
		self._send_ice_servers = send_ice_servers
		self._id = token_hex(4)
		self._send_message: WebRTCSendMessage | None = None
		self._on_close: SessionClosedCallback | None = None
		self._logger = _LOGGER.getChild(f"session.{self._id}")

		self._offer_sdp: str | None = None
		self._reader_task: asyncio.Task[None] | None = None
		self._request_queue = MessageQueue[SignalRequest]()

	@override
	def start(self, on_close: SessionClosedCallback) -> None:
		"""Start the no-offer LiveKit join used for ICE and optional warmup."""
		if self._reader_task is not None:
			raise RuntimeError(f"LiveKit session {self._id} already started")

		self._on_close = on_close

		async def reader_task() -> None:
			try:
				await self._read()
			except Exception as err:
				self._logger.error("Error in LiveKit session: %s", err)
			finally:
				self._reader_task = None
				self.close()

		self._reader_task = asyncio.create_task(
			reader_task(),
			name=f"simplirtc-livekit-{self._id}",
		)

	@override
	async def handle_offer(self, offer_sdp: str, send_message: WebRTCSendMessage) -> None:
		"""Handle a browser offer with the prepared LiveKit session."""
		if self._reader_task is None:
			raise RuntimeError(f"LiveKit session {self._id} was not started")
		self._send_message = send_message
		self._offer_sdp = offer_sdp
		await self._request_queue.add(SignalRequest(offer=SessionDescription(
			type="offer",
			sdp=offer_sdp,
		)))

	@override
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Forward a browser ICE candidate to LiveKit."""
		# LiveKit rejects the browser's final null ICE event if it is serialized
		# as an empty candidateInit, and the session works without forwarding it.
		if not candidate.candidate:
			return

		candidate_init: dict[str, str | int] = {"candidate": candidate.candidate}
		if candidate.sdp_mid is not None:
			candidate_init["sdpMid"] = candidate.sdp_mid
		if candidate.sdp_m_line_index is not None:
			candidate_init["sdpMLineIndex"] = candidate.sdp_m_line_index

		await self._request_queue.add(SignalRequest(
			trickle=TrickleRequest(
				candidateInit=json.dumps(candidate_init, separators=(",", ":")),
				target=SignalTarget.PUBLISHER,
			),
		))

	@override
	def close(self) -> None:
		"""Stop this LiveKit signaling session."""
		self._request_queue.clear()
		if (reader_task := self._reader_task) is not None:
			self._reader_task = None
			reader_task.cancel()
		if on_close := self._on_close:
			self._on_close = None
			on_close()

	async def _read(self) -> None:
		"""Run initial signaling, optional warmup, and browser signaling."""
		async with LiveKitSignal.connect(
			self._livekit_url,
			token=self._user_token,
			auto_subscribe=True,
			logger=self._logger.getChild("initial"),
		) as signal:
			async for message in signal:
				match message:
					case ("join", join):
						break
					case ("leave", leave):
						raise RuntimeError(
							f"LiveKit left before join response: reason={leave.reason}"
						)
					case (kind, _):
						raise RuntimeError(f"LiveKit sent {kind} before join response")
			else:
				raise RuntimeError("LiveKit websocket closed before join response")

			ice_servers = list(join.ice_servers)
			self._send_ice_servers(ice_servers)

			if all(
				any(
					track.sid
					and (track.type, track.source) == required_track
					for participant in join.other_participants
					for track in participant.tracks
				)
				for required_track in _CAMERA_TRACK_KIND_BY_TYPE_SOURCE
			):
				await self._request_queue.flush(signal.send)
				await self._run_browser_signal(signal)
				return

			try:
				await WarmupSession(signal, ice_servers=ice_servers).run()
			except Exception as err:
				self._logger.warning(
					"LiveKit aiortc warmup failed; continuing with browser offer: %s",
					err,
				)

		async with LiveKitSignal.connect(
			self._livekit_url,
			token=self._user_token,
			auto_subscribe=True,
			logger=self._logger.getChild("browser"),
		) as signal:
			await self._run_browser_signal(signal)

	async def _run_browser_signal(self, signal: LiveKitSignal) -> None:
		"""Handle LiveKit signaling for the browser-owned peer connection."""

		async for message in signal:
			match message:
				case ("join", _):
					await self._request_queue.flush(signal.send)
				case ("answer", answer):
					self._on_answer(answer)
				case ("trickle", trickle):
					self._on_trickle(trickle)
				case ("media_sections_requirement", requirement):
					await self._on_media_sections_requirement(requirement)
				case ("leave", leave):
					self._logger.warning(
						"LiveKit browser signal left: reason=%s",
						leave.reason,
					)
				case ("subscription_response", response):
					if response.err:
						self._logger.warning(
							"LiveKit browser subscription failed: track=%s error=%s",
							response.track_sid,
							response.err,
						)
				case ("pong_resp", _):
					pass
				case (kind, response):
					self._logger.debug(
						"Unhandled LiveKit browser message kind=%s response=%s",
						kind,
						response,
					)

		if not self._request_queue.flushed:
			raise RuntimeError("LiveKit websocket closed before join response")

	def _on_answer(self, answer: SessionDescription) -> None:
		if send_message := self._send_message:
			send_message(WebRTCAnswer(answer=answer.sdp))

	def _on_trickle(self, trickle: TrickleRequest) -> None:
		if trickle.target != SignalTarget.PUBLISHER:
			return

		if trickle.final and not trickle.candidateInit:
			if send_message := self._send_message:
				send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
					candidate="",
					sdp_mid=None,
					sdp_m_line_index=None,
				)))
			return
		if not trickle.candidateInit:
			return

		try:
			candidate_init = json.loads(trickle.candidateInit)
		except ValueError as err:
			self._logger.warning("Dropping invalid LiveKit ICE candidate JSON: %s", err)
			return

		candidate = candidate_init.get("candidate")
		if not isinstance(candidate, str):
			self._logger.warning("Dropping LiveKit ICE candidate without candidate field")
			return
		sdp_mid = candidate_init.get("sdpMid")
		sdp_m_line_index = candidate_init.get("sdpMLineIndex")
		if send_message := self._send_message:
			send_message(WebRTCCandidate(candidate=RTCIceCandidateInit(
				candidate=candidate,
				sdp_mid=sdp_mid if isinstance(sdp_mid, str) else None,
				sdp_m_line_index=(
					sdp_m_line_index if isinstance(sdp_m_line_index, int) else None
				),
			)))

	async def _on_media_sections_requirement(
		self,
		requirement: MediaSectionsRequirement,
	) -> None:
		if requirement.num_audios or requirement.num_videos:
			self._logger.warning(f"LiveKit requested extra media sections audio={requirement.num_audios} video={requirement.num_videos}; Home Assistant cannot apply renegotiation")
		if self._offer_sdp is None:
			self._logger.warning("LiveKit requested media sections before browser offer")
			return
		await self._request_queue.add(SignalRequest(offer=SessionDescription(
			type="offer",
			sdp=self._offer_sdp,
		)))
