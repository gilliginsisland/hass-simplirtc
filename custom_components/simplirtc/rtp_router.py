"""Raw RTP router peer connection helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from typing import (
	Any,
	cast,
	override,
)

from aiortc import (
	RTCBundlePolicy,
	RTCCertificate,
	RTCConfiguration,
	RTCDtlsTransport,
	RTCIceCandidate,
	RTCIceGatherer,
	RTCIceTransport,
	RTCPeerConnection,
	RTCRtpReceiver,
	RTCRtpSender,
	RTCRtpTransceiver,
)
from aiortc.codecs import is_rtx
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcpeerconnection import is_codec_compatible
from aiortc.rtcrtpparameters import (
	RTCRtpCodecParameters,
	RTCRtpReceiveParameters,
	RTCRtpSendParameters,
)
from aiortc.rtp import (
	AnyRtcpPacket,
	HeaderExtensionsMap,
	RTCP_PSFB_APP,
	RtcpByePacket,
	RtcpPacket,
	RtcpPsfbPacket,
	RtcpReceiverInfo,
	RtcpRrPacket,
	RtcpRtpfbPacket,
	RtcpSdesPacket,
	RtcpSrPacket,
	RtpPacket,
	pack_remb_fci,
	unpack_remb_fci,
)

PeerId = str
MediaKind = str

_LOGGER = logging.getLogger(__name__)
_RTP_SUBSCRIPTION_QUEUE_SIZE = 90


@dataclass(slots=True)
class RtpInput:
	"""Negotiated RTP stream expected from a remote endpoint."""

	peer_id: PeerId
	kind: MediaKind
	transport: RawRtpDtlsTransport
	local_rtcp_ssrc: int | None
	parameters: RTCRtpReceiveParameters
	header_extensions: HeaderExtensionsMap = field(init=False)
	primary_ssrc: int = field(init=False)
	rtx_ssrc: int | None = field(init=False)

	def __post_init__(self) -> None:
		if not (encodings := self.parameters.encodings):
			raise RuntimeError(f"Raw RTP input for {self.kind} has no SDP SSRC encoding")
		primary_ssrcs = {encoding.ssrc for encoding in encodings}
		if len(primary_ssrcs) != 1:
			raise RuntimeError(
				f"Raw RTP input for {self.kind} has {len(primary_ssrcs)} SDP primary SSRCs; "
				"multiple RTP streams are not supported"
			)
		rtx_ssrcs = {encoding.rtx.ssrc for encoding in encodings if encoding.rtx}
		if len(rtx_ssrcs) > 1:
			raise RuntimeError(
				f"Raw RTP input for {self.kind} has {len(rtx_ssrcs)} SDP RTX SSRCs; "
				"multiple RTX streams are not supported"
			)
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(self.parameters)

		self.header_extensions = header_extensions
		self.primary_ssrc = primary_ssrcs.pop()
		self.rtx_ssrc = rtx_ssrcs.pop() if rtx_ssrcs else None


@dataclass(slots=True)
class RtpOutput:
	"""Negotiated RTP stream we advertise to a remote endpoint."""

	peer_id: PeerId
	sender: RawRtpSender
	parameters: RTCRtpSendParameters
	kind: MediaKind = field(init=False)
	mid: str = field(init=False)
	transport: RawRtpDtlsTransport = field(init=False)
	header_extensions: HeaderExtensionsMap = field(init=False)

	def __post_init__(self) -> None:
		transport = self.sender.transport
		if not isinstance(transport, RawRtpDtlsTransport):
			raise RuntimeError(f"Raw RTP output for {self.sender.kind} has non-raw transport")
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(self.parameters)

		self.kind = self.sender.kind
		self.mid = self.parameters.muxId
		self.transport = transport
		self.header_extensions = header_extensions

	@property
	def primary_ssrc(self) -> int:
		return self.sender._ssrc

	@property
	def rtx_ssrc(self) -> int:
		return self.sender._rtx_ssrc


class RtpSubscription:
	"""Mapping between one producer input and one consumer output."""

	__slots__ = (
		"_dropped_packets",
		"_queue",
		"_send_task",
		"input",
		"output",
	)

	def __init__(self, input_: RtpInput, output: RtpOutput) -> None:
		self.input = input_
		self.output = output
		self._queue = asyncio.Queue(maxsize=_RTP_SUBSCRIPTION_QUEUE_SIZE)
		self._send_task = asyncio.create_task(self._send_rtp())
		self._send_task.add_done_callback(self._log_send_error)
		self._dropped_packets = 0

	def enqueue_rtp(self, packet: bytes) -> None:
		"""Queue RTP for this subscriber, dropping stale packets when overloaded."""
		if self._send_task.done():
			return
		if self._queue.full():
			try:
				self._queue.get_nowait()
			except asyncio.QueueEmpty:
				pass
			self._dropped_packets += 1
			if self._dropped_packets == 1 or self._dropped_packets % _RTP_SUBSCRIPTION_QUEUE_SIZE == 0:
				_LOGGER.debug(
					"Dropped %s stale RTP packets for consumer %s %s",
					self._dropped_packets,
					self.output.peer_id,
					self.output.kind,
				)
		self._queue.put_nowait(packet)

	def close(self) -> None:
		"""Stop this subscription's sender task."""
		self._send_task.cancel()

	async def _send_rtp(self) -> None:
		while True:
			packet = await self._queue.get()
			try:
				await self.output.transport._send_rtp(packet)
			except ConnectionError:
				_LOGGER.debug(
					"Stopping RTP subscription for disconnected consumer %s %s",
					self.output.peer_id,
					self.output.kind,
				)
				return

	def _log_send_error(self, task: asyncio.Task[None]) -> None:
		if task.cancelled():
			return
		try:
			task.result()
		except BaseException as err:
			_LOGGER.error(
				"Error in RTP subscription for consumer %s %s: %s",
				self.output.peer_id,
				self.output.kind,
				err,
			)

	def rewrite_rtp(self, data: bytes, source_codec: RTCRtpCodecParameters) -> bytes | None:
		"""Rewrite producer RTP for this consumer output."""
		if not (target_codec := next(
			(
				codec for codec in self.output.parameters.codecs
				if not is_rtx(codec) and is_codec_compatible(codec, source_codec)
			),
			None,
		)):
			return None
		packet = RtpPacket.parse(data, self.input.header_extensions)
		if is_rtx(source_codec):
			if (base_payload_type := target_codec.payloadType) is None:
				raise RuntimeError(f"Negotiated codec {target_codec.mimeType} is missing a payload type")
			if not (target_rtx_codec := next(
				(
					codec for codec in self.output.parameters.codecs
					if is_rtx(codec) and codec.parameters.get("apt") == base_payload_type
				),
				None,
			)):
				return None
			if (target_payload_type := target_rtx_codec.payloadType) is None:
				raise RuntimeError(
					f"Negotiated codec {target_rtx_codec.mimeType} is missing a payload type"
				)
			packet.payload_type = target_payload_type
			packet.ssrc = self.output.rtx_ssrc
		else:
			if (target_payload_type := target_codec.payloadType) is None:
				raise RuntimeError(f"Negotiated codec {target_codec.mimeType} is missing a payload type")
			packet.payload_type = target_payload_type
			packet.ssrc = self.output.primary_ssrc

		packet.extensions.mid = self.output.mid
		return packet.serialize(self.output.header_extensions)

	def rewrite_rtcp_from_input(self, packet: AnyRtcpPacket) -> AnyRtcpPacket | None:
		"""Rewrite producer RTCP for this consumer output."""
		rewritten = deepcopy(packet)
		if isinstance(rewritten, RtcpSrPacket):
			if (ssrc := self._input_to_output_ssrc(rewritten.ssrc)) is None:
				return None
			rewritten.ssrc = ssrc
			self._rewrite_receiver_reports(rewritten.reports, self._input_to_output_ssrc)
			return rewritten

		if isinstance(rewritten, RtcpRrPacket):
			if not self._rewrite_receiver_reports(rewritten.reports, self._input_to_output_ssrc):
				return None
			rewritten.ssrc = self.output.primary_ssrc
			return rewritten

		if isinstance(rewritten, (RtcpPsfbPacket, RtcpRtpfbPacket)):
			return self._rewrite_feedback_from_input(rewritten)

		if isinstance(rewritten, RtcpByePacket):
			if not (sources := [
				mapped
				for source in rewritten.sources
				if (mapped := self._input_to_output_ssrc(source)) is not None
			]):
				return None
			rewritten.sources = sources
			return rewritten

		if isinstance(rewritten, RtcpSdesPacket):
			mapped_chunks = []
			for chunk in rewritten.chunks:
				if (mapped := self._input_to_output_ssrc(chunk.ssrc)) is not None:
					chunk.ssrc = mapped
					mapped_chunks.append(chunk)
			if not mapped_chunks:
				return None
			rewritten.chunks = mapped_chunks
			return rewritten

		return None

	def rewrite_rtcp_from_output(self, packet: AnyRtcpPacket) -> AnyRtcpPacket | None:
		"""Rewrite consumer RTCP feedback for the producer input."""
		rewritten = deepcopy(packet)
		if isinstance(rewritten, RtcpSrPacket):
			if (ssrc := self._output_to_input_ssrc(rewritten.ssrc)) is None:
				return None
			rewritten.ssrc = ssrc
			self._rewrite_receiver_reports(rewritten.reports, self._output_to_input_ssrc)
			return rewritten

		if isinstance(rewritten, RtcpRrPacket):
			if not self._rewrite_receiver_reports(rewritten.reports, self._output_to_input_ssrc):
				return None
			if self.input.local_rtcp_ssrc is None:
				return None
			rewritten.ssrc = self.input.local_rtcp_ssrc
			return rewritten

		if isinstance(rewritten, (RtcpPsfbPacket, RtcpRtpfbPacket)):
			return self._rewrite_feedback_from_output(rewritten)

		if isinstance(rewritten, RtcpByePacket):
			if not (sources := [
				mapped
				for source in rewritten.sources
				if (mapped := self._output_to_input_ssrc(source)) is not None
			]):
				return None
			rewritten.sources = sources
			return rewritten

		if isinstance(rewritten, RtcpSdesPacket):
			mapped_chunks = []
			for chunk in rewritten.chunks:
				if (mapped := self._output_to_input_ssrc(chunk.ssrc)) is not None:
					chunk.ssrc = mapped
					mapped_chunks.append(chunk)
			if not mapped_chunks:
				return None
			rewritten.chunks = mapped_chunks
			return rewritten

		return None

	def _rewrite_feedback_from_input(
		self,
		packet: RtcpPsfbPacket | RtcpRtpfbPacket,
	) -> RtcpPsfbPacket | RtcpRtpfbPacket | None:
		if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
			try:
				bitrate, ssrcs = unpack_remb_fci(packet.fci)
			except ValueError:
				return None
			if not (mapped_ssrcs := [
				mapped
				for ssrc in ssrcs
				if (mapped := self._input_to_output_ssrc(ssrc)) is not None
			]):
				return None
			packet.fci = pack_remb_fci(bitrate, mapped_ssrcs)
			packet.ssrc = self.output.primary_ssrc
			return packet

		if (mapped := self._input_to_output_ssrc(packet.media_ssrc)) is None:
			return None
		packet.media_ssrc = mapped
		packet.ssrc = self.output.primary_ssrc
		return packet

	def _rewrite_feedback_from_output(
		self,
		packet: RtcpPsfbPacket | RtcpRtpfbPacket,
	) -> RtcpPsfbPacket | RtcpRtpfbPacket | None:
		if self.input.local_rtcp_ssrc is None:
			return None
		if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
			try:
				bitrate, ssrcs = unpack_remb_fci(packet.fci)
			except ValueError:
				return None
			if not (mapped_ssrcs := [
				mapped
				for ssrc in ssrcs
				if (mapped := self._output_to_input_ssrc(ssrc)) is not None
			]):
				return None
			packet.fci = pack_remb_fci(bitrate, mapped_ssrcs)
			packet.ssrc = self.input.local_rtcp_ssrc
			return packet

		if (mapped := self._output_to_input_ssrc(packet.media_ssrc)) is None:
			return None
		packet.media_ssrc = mapped
		packet.ssrc = self.input.local_rtcp_ssrc
		return packet

	def _rewrite_receiver_reports(
		self,
		reports: list[RtcpReceiverInfo],
		map_ssrc: Callable[[int], int | None],
	) -> bool:
		mapped_reports = []
		for report in reports:
			if (mapped := map_ssrc(report.ssrc)) is not None:
				report.ssrc = mapped
				mapped_reports.append(report)
		reports[:] = mapped_reports
		return bool(mapped_reports)

	def _input_to_output_ssrc(self, ssrc: int) -> int | None:
		if self.input.rtx_ssrc == ssrc:
			return self.output.rtx_ssrc
		if self.input.primary_ssrc == ssrc:
			return self.output.primary_ssrc
		return None

	def _output_to_input_ssrc(self, ssrc: int) -> int | None:
		if self.output.rtx_ssrc == ssrc:
			return self.input.rtx_ssrc
		if self.output.primary_ssrc == ssrc:
			return self.input.primary_ssrc
		return None


class RawRtpTrack:
	"""A negotiated raw RTP input that fans out packets to subscribers."""

	__slots__ = (
		"_subscriptions",
		"input",
	)

	def __init__(self, input_: RtpInput) -> None:
		self.input = input_
		self._subscriptions: dict[PeerId, RtpSubscription] = {}

	@property
	def peer_id(self) -> PeerId:
		return self.input.peer_id

	@property
	def kind(self) -> MediaKind:
		return self.input.kind

	@property
	def primary_ssrc(self) -> int:
		return self.input.primary_ssrc

	@property
	def rtx_ssrc(self) -> int | None:
		return self.input.rtx_ssrc

	def subscribe(self, output: RtpOutput) -> None:
		"""Subscribe a consumer output to this raw RTP track."""
		if subscription := self._subscriptions.pop(output.peer_id, None):
			subscription.close()
		self._subscriptions[output.peer_id] = RtpSubscription(self.input, output)

	def unsubscribe(self, peer_id: PeerId) -> None:
		"""Remove one consumer output subscription."""
		if subscription := self._subscriptions.pop(peer_id, None):
			subscription.close()

	def close(self) -> None:
		"""Close all subscriptions for this raw RTP track."""
		for subscription in self._subscriptions.values():
			subscription.close()
		self._subscriptions.clear()

	def forward_rtp(self, data: bytes, payload_type: int) -> None:
		"""Forward one producer RTP packet to subscribed outputs."""
		if not (source_codec := next((
			codec for codec in self.input.parameters.codecs
			if codec.payloadType == payload_type
		), None)):
			return
		for subscription in tuple(self._subscriptions.values()):
			if rewritten := subscription.rewrite_rtp(data, source_codec):
				subscription.enqueue_rtp(rewritten)

	def rewrite_rtcp_from_input(
		self,
		packet: AnyRtcpPacket,
	) -> tuple[tuple[RawRtpDtlsTransport, AnyRtcpPacket], ...]:
		"""Rewrite producer RTCP for all subscribers."""
		routes = []
		for subscription in tuple(self._subscriptions.values()):
			if rewritten := subscription.rewrite_rtcp_from_input(packet):
				routes.append((subscription.output.transport, rewritten))
		return tuple(routes)

	def rewrite_rtcp_from_output(
		self,
		peer_id: PeerId,
		packet: AnyRtcpPacket,
	) -> tuple[tuple[RawRtpDtlsTransport, AnyRtcpPacket], ...]:
		"""Rewrite subscriber RTCP feedback for this input."""
		if not (subscription := self._subscriptions.get(peer_id)):
			return ()
		if not (rewritten := subscription.rewrite_rtcp_from_output(packet)):
			return ()
		return ((self.input.transport, rewritten),)


class RawRtpRouter:
	"""Raw RTP router for negotiated inputs and outputs."""

	def __init__(self) -> None:
		self._tracks_by_kind: dict[MediaKind, RawRtpTrack] = {}
		self._tracks_by_ssrc: dict[tuple[PeerId, int], RawRtpTrack] = {}
		self._outputs_by_peer: dict[PeerId, dict[MediaKind, RtpOutput]] = {}

	def addInput(
		self,
		peer_connection: RawRtpPeerConnection,
		*,
		peer_id: PeerId,
	) -> None:
		"""Register a peer connection as a raw RTP input."""
		peer_connection._set_raw_router(self, peer_id=peer_id, is_output=False)

		@peer_connection.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			if peer_connection.connectionState == "closed":
				self.unregister_peer(peer_connection.peer_id)

	def addOutput(
		self,
		peer_connection: RawRtpPeerConnection,
		*,
		peer_id: PeerId,
	) -> None:
		"""Register a peer connection as a raw RTP output."""
		peer_connection._set_raw_router(self, peer_id=peer_id, is_output=True)

		@peer_connection.on("connectionstatechange")
		async def on_connectionstatechange() -> None:
			if peer_connection.connectionState == "closed":
				self.unregister_peer(peer_connection.peer_id)

	def unregister_peer(self, peer_id: PeerId) -> None:
		"""Remove all negotiated RTP state for a peer."""
		for kind, track in tuple(self._tracks_by_kind.items()):
			if track.peer_id == peer_id:
				self.unregister_input(peer_id=peer_id, kind=kind)
		for kind in tuple(self._outputs_by_peer.get(peer_id, ())):
			self.unregister_output(peer_id=peer_id, kind=kind)

	def register_input(self, rtp_input: RtpInput) -> None:
		if existing_track := self._tracks_by_kind.get(rtp_input.kind):
			self.unregister_input(peer_id=existing_track.peer_id, kind=rtp_input.kind)
		track = RawRtpTrack(rtp_input)
		for ssrc in (track.primary_ssrc, track.rtx_ssrc):
			if ssrc is None:
				continue
			if existing_track := self._tracks_by_ssrc.get((track.peer_id, ssrc)):
				raise RuntimeError(
					f"Raw RTP input {track.peer_id} {track.kind} SSRC {ssrc} "
					f"already belongs to {existing_track.kind}"
				)
			self._tracks_by_ssrc[(track.peer_id, ssrc)] = track
		self._tracks_by_kind[track.kind] = track
		for outputs_by_kind in self._outputs_by_peer.values():
			if output := outputs_by_kind.get(track.kind):
				track.subscribe(output)

	def unregister_input(self, *, peer_id: PeerId, kind: MediaKind) -> None:
		if (
			(track := self._tracks_by_kind.get(kind))
			and track.peer_id == peer_id
		):
			for ssrc in (track.primary_ssrc, track.rtx_ssrc):
				if ssrc is not None and self._tracks_by_ssrc.get((peer_id, ssrc)) is track:
					del self._tracks_by_ssrc[(peer_id, ssrc)]
			track.close()
			del self._tracks_by_kind[kind]

	def register_output(self, output: RtpOutput) -> None:
		self._outputs_by_peer.setdefault(output.peer_id, {})[output.kind] = output
		if track := self._tracks_by_kind.get(output.kind):
			track.subscribe(output)

	def unregister_output(self, *, peer_id: PeerId, kind: MediaKind) -> None:
		if outputs := self._outputs_by_peer.get(peer_id):
			outputs.pop(kind, None)
			if not outputs:
				del self._outputs_by_peer[peer_id]
		if track := self._tracks_by_kind.get(kind):
			track.unsubscribe(peer_id)

	async def forward_input_rtp(self, peer_id: PeerId, data: bytes) -> None:
		"""Forward RTP from the input peer to all subscribed outputs."""
		packet = RtpPacket.parse(data)
		if track := self._tracks_by_ssrc.get((peer_id, packet.ssrc)):
			track.forward_rtp(data, packet.payload_type)

	async def forward_input_rtcp(self, peer_id: PeerId, data: bytes) -> None:
		"""Forward RTCP from the input peer to subscribed outputs."""
		try:
			packets = RtcpPacket.parse(data)
		except ValueError as err:
			_LOGGER.debug("Dropping unparsable input RTCP from %s: %s", peer_id, err)
			return

		by_transport: dict[RawRtpDtlsTransport, list[AnyRtcpPacket]] = {}
		for packet in packets:
			for track in tuple(self._tracks_by_kind.values()):
				if track.peer_id != peer_id:
					continue
				for transport, rewritten in track.rewrite_rtcp_from_input(packet):
					by_transport.setdefault(transport, []).append(rewritten)

		for transport, rewritten_packets in by_transport.items():
			await transport._send_rtp(b"".join(bytes(packet) for packet in rewritten_packets))

	async def forward_output_rtcp(self, peer_id: PeerId, data: bytes) -> None:
		"""Forward RTCP feedback from one output peer back to the input peer."""
		try:
			packets = RtcpPacket.parse(data)
		except ValueError as err:
			_LOGGER.debug("Dropping unparsable output RTCP from %s: %s", peer_id, err)
			return

		by_transport: dict[RawRtpDtlsTransport, list[AnyRtcpPacket]] = {}
		for packet in packets:
			for track in tuple(self._tracks_by_kind.values()):
				for transport, rewritten in track.rewrite_rtcp_from_output(peer_id, packet):
					by_transport.setdefault(transport, []).append(rewritten)

		for transport, rewritten_packets in by_transport.items():
			await transport._send_rtp(b"".join(bytes(packet) for packet in rewritten_packets))

class RawRtpDtlsTransport(RTCDtlsTransport):
	"""DTLS transport that routes decrypted media through a raw RTP router."""

	def __init__(
		self,
		transport: RTCIceTransport,
		certificates: list[RTCCertificate],
		*,
		router: RawRtpRouter,
		peer_id: PeerId,
		is_output: bool,
	) -> None:
		super().__init__(transport, certificates)
		self.__router = router
		self.__peer_id = peer_id
		self.__is_output = is_output

	@override
	async def _handle_rtp_data(self, data: bytes, arrival_time_ms: int) -> None:
		if not self.__is_output:
			await self.__router.forward_input_rtp(self.__peer_id, data)

	@override
	async def _handle_rtcp_data(self, data: bytes) -> None:
		if self.__is_output:
			await self.__router.forward_output_rtcp(self.__peer_id, data)
			return
		await self.__router.forward_input_rtcp(self.__peer_id, data)


class RawRtpSender(RTCRtpSender):
	"""RTP sender that only registers negotiated outbound RTP state."""

	def __init__(
		self,
		trackOrKind: MediaStreamTrack | MediaKind,
		transport: RawRtpDtlsTransport,
		*,
		router: RawRtpRouter,
		peer_id: PeerId,
	) -> None:
		super().__init__(trackOrKind, transport)
		self.__router = router
		self.__peer_id = peer_id
		self.__started = False

	@override
	async def send(self, parameters: RTCRtpSendParameters) -> None:
		if self.__started:
			return
		self.__router.register_output(RtpOutput(
			peer_id=self.__peer_id,
			sender=self,
			parameters=parameters,
		))
		self.__started = True

	@override
	async def stop(self) -> None:
		if self.__started:
			self.__router.unregister_output(peer_id=self.__peer_id, kind=self.kind)
			self.__started = False

	@override
	async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
		return None


class RawRtpReceiver(RTCRtpReceiver):
	"""RTP receiver that only registers negotiated inbound RTP state."""

	def __init__(
		self,
		kind: MediaKind,
		transport: RawRtpDtlsTransport,
		*,
		router: RawRtpRouter,
		peer_id: PeerId,
	) -> None:
		super().__init__(kind, transport)
		self.__router = router
		self.__peer_id = peer_id
		self.__kind = kind
		self.__started = False

	@property
	def local_rtcp_ssrc(self) -> int | None:
		"""Return the local RTCP SSRC configured by aiortc."""
		return cast(int | None, cast(Any, self)._RTCRtpReceiver__rtcp_ssrc)

	@override
	async def receive(self, parameters: RTCRtpReceiveParameters) -> None:
		if self.__started:
			return
		transport = self.transport
		if not isinstance(transport, RawRtpDtlsTransport):
			raise RuntimeError(f"Raw RTP input for {self.__kind} has non-raw transport")
		self.__router.register_input(RtpInput(
			peer_id=self.__peer_id,
			kind=self.__kind,
			transport=transport,
			local_rtcp_ssrc=self.local_rtcp_ssrc,
			parameters=parameters,
		))
		self.__started = True

	@override
	async def stop(self) -> None:
		if self.__started:
			self.__router.unregister_input(peer_id=self.__peer_id, kind=self.__kind)
			self.__started = False

	@override
	def _handle_disconnect(self) -> None:
		return None

	@override
	async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
		return None

	@override
	async def _handle_rtp_packet(self, packet: RtpPacket, arrival_time_ms: int) -> None:
		return None


class RawRtpPeerConnection(RTCPeerConnection):
	"""Peer connection that negotiates normally and routes raw RTP."""

	def __init__(
		self,
		*,
		configuration: RTCConfiguration,
	) -> None:
		super().__init__(configuration=configuration)
		self.__configuration = configuration
		self.__router: RawRtpRouter | None = None
		self.__peer_id: PeerId | None = None
		self.__is_output: bool | None = None
		self._pending_remote_candidates: list[RTCIceCandidate | None] = []

	@property
	def peer_id(self) -> PeerId:
		"""Return this peer connection's raw RTP routing ID."""
		if not (peer_id := self.__peer_id):
			raise RuntimeError("Raw RTP peer has no ID")
		return peer_id

	@property
	def rtc_configuration(self) -> RTCConfiguration:
		"""Return the mutable RTC configuration used by this peer connection."""
		return self.__configuration

	def _set_raw_router(
		self,
		router: RawRtpRouter,
		*,
		peer_id: PeerId,
		is_output: bool,
	) -> None:
		if self.__router:
			raise RuntimeError("Raw RTP router is already attached")
		pc = cast(Any, self)
		if pc._RTCPeerConnection__transceivers or pc._RTCPeerConnection__sctp:
			raise RuntimeError("Raw RTP router must be attached before transceivers are created")
		self.__router = router
		self.__peer_id = peer_id
		self.__is_output = is_output

	@override
	async def addIceCandidate(self, candidate: RTCIceCandidate | None) -> None:
		if not self.remoteDescription:
			self._pending_remote_candidates.append(candidate)
			return
		await super().addIceCandidate(candidate)

	async def add_pending_remote_candidates(self) -> None:
		"""Apply ICE candidates received before the remote description."""
		for candidate in self._pending_remote_candidates:
			await super().addIceCandidate(candidate)
		self._pending_remote_candidates = []

	def __raw_router(self) -> RawRtpRouter:
		if not (router := self.__router):
			raise RuntimeError("Raw RTP router is not attached")
		return router

	def __raw_is_output(self) -> bool:
		if self.__is_output is None:
			raise RuntimeError("Raw RTP router is not attached")
		return self.__is_output

	def _RTCPeerConnection__createDtlsTransport(self) -> RawRtpDtlsTransport:
		router = self.__raw_router()
		pc = cast(Any, self)
		if pc._RTCPeerConnection__transceivers or pc._RTCPeerConnection__sctp:
			if pc._RTCPeerConnection__transceivers:
				parameters = pc._RTCPeerConnection__transceivers[
					0
				].receiver.transport.transport.iceGatherer.getLocalParameters()
			else:
				parameters = (
					pc._RTCPeerConnection__sctp.transport.transport.iceGatherer.getLocalParameters()
				)
			ice_gatherer = RTCIceGatherer(
				iceServers=self.__configuration.iceServers,
				local_username=parameters.usernameFragment,
				local_password=parameters.password,
			)
		else:
			ice_gatherer = RTCIceGatherer(iceServers=self.__configuration.iceServers)

		ice_gatherer.on("statechange", pc._RTCPeerConnection__updateIceGatheringState)
		ice_transport = RTCIceTransport(ice_gatherer)
		ice_transport.on("statechange", pc._RTCPeerConnection__updateIceConnectionState)
		ice_transport.on("statechange", pc._RTCPeerConnection__updateConnectionState)
		pc._RTCPeerConnection__iceTransports.add(ice_transport)

		dtls_transport = RawRtpDtlsTransport(
			ice_transport,
			pc._RTCPeerConnection__certificates,
			router=router,
			peer_id=self.peer_id,
			is_output=self.__raw_is_output(),
		)
		dtls_transport.on("statechange", pc._RTCPeerConnection__updateConnectionState)
		pc._RTCPeerConnection__dtlsTransports.add(dtls_transport)

		pc._RTCPeerConnection__updateIceGatheringState()
		pc._RTCPeerConnection__updateIceConnectionState()
		pc._RTCPeerConnection__updateConnectionState()
		return dtls_transport

	def _RTCPeerConnection__createTransceiver(
		self,
		direction: str,
		kind: MediaKind,
		sender_track: MediaStreamTrack | None = None,
	) -> RTCRtpTransceiver:
		router = self.__raw_router()
		pc = cast(Any, self)
		dtls_transport = None
		bundled = False
		transceivers = pc._RTCPeerConnection__transceivers
		if self.__configuration.bundlePolicy == RTCBundlePolicy.MAX_BUNDLE:
			if transceivers:
				dtls_transport = transceivers[0].receiver.transport
				bundled = True
			elif pc._RTCPeerConnection__sctp:
				dtls_transport = pc._RTCPeerConnection__sctp.transport
				bundled = True
		elif self.__configuration.bundlePolicy == RTCBundlePolicy.BALANCED:
			transceiver = next(
				(item for item in transceivers if item.kind == kind),
				None,
			)
			if transceiver:
				dtls_transport = transceiver.receiver.transport
				bundled = True

		if not dtls_transport:
			dtls_transport = pc._RTCPeerConnection__createDtlsTransport()
		if not isinstance(dtls_transport, RawRtpDtlsTransport):
			raise RuntimeError("Raw RTP transceiver received a non-raw DTLS transport")

		sender = RawRtpSender(
			sender_track or kind,
			dtls_transport,
			router=router,
			peer_id=self.peer_id,
		)
		receiver = RawRtpReceiver(
			kind,
			dtls_transport,
			router=router,
			peer_id=self.peer_id,
		)
		transceiver = RTCRtpTransceiver(
			direction="sendonly" if self.__raw_is_output() else "recvonly",
			kind=kind,
			sender=sender,
			receiver=receiver,
		)
		transceiver.receiver._set_rtcp_ssrc(transceiver.sender._ssrc)
		transceiver.sender._stream_id = pc._RTCPeerConnection__stream_id
		transceiver._bundled = bundled
		transceivers.append(transceiver)
		return transceiver
