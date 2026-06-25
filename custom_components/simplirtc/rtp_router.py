"""Raw RTP router peer connection helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import logging

from .vendor.aiortc import (
	RTCConfiguration,
	RTCDtlsTransport,
	RTCPeerConnection,
	RTCRtpSender,
)
from .vendor.aiortc.codecs import is_rtx
from .vendor.aiortc.rtcpeerconnection import is_codec_compatible
from .vendor.aiortc.rtcrtpparameters import (
	RTCRtpCodecParameters,
	RTCRtpReceiveParameters,
	RTCRtpSendParameters,
)
from .vendor.aiortc.rtp import (
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
	transport: RTCDtlsTransport
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
	sender: RTCRtpSender
	parameters: RTCRtpSendParameters
	kind: MediaKind = field(init=False)
	mid: str = field(init=False)
	transport: RTCDtlsTransport = field(init=False)
	header_extensions: HeaderExtensionsMap = field(init=False)

	def __post_init__(self) -> None:
		transport = self.sender.transport
		if not isinstance(transport, RTCDtlsTransport):
			raise RuntimeError(f"Raw RTP output for {self.sender.kind} has unexpected transport")
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
	) -> tuple[tuple[RTCDtlsTransport, AnyRtcpPacket], ...]:
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
	) -> tuple[tuple[RTCDtlsTransport, AnyRtcpPacket], ...]:
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
		peer_connection.setRawRtpRouter(self, peer_id=peer_id, is_output=False)

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
		peer_connection.setRawRtpRouter(self, peer_id=peer_id, is_output=True)

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

	def register_input(
		self,
		*,
		peer_id: PeerId,
		kind: MediaKind,
		transport: RTCDtlsTransport,
		local_rtcp_ssrc: int | None,
		parameters: RTCRtpReceiveParameters,
	) -> None:
		rtp_input = RtpInput(
			peer_id=peer_id,
			kind=kind,
			transport=transport,
			local_rtcp_ssrc=local_rtcp_ssrc,
			parameters=parameters,
		)
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

	def register_output(
		self,
		*,
		peer_id: PeerId,
		sender: RTCRtpSender,
		parameters: RTCRtpSendParameters,
	) -> None:
		output = RtpOutput(
			peer_id=peer_id,
			sender=sender,
			parameters=parameters,
		)
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

		by_transport: dict[RTCDtlsTransport, list[AnyRtcpPacket]] = {}
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

		by_transport: dict[RTCDtlsTransport, list[AnyRtcpPacket]] = {}
		for packet in packets:
			for track in tuple(self._tracks_by_kind.values()):
				for transport, rewritten in track.rewrite_rtcp_from_output(peer_id, packet):
					by_transport.setdefault(transport, []).append(rewritten)

		for transport, rewritten_packets in by_transport.items():
			await transport._send_rtp(b"".join(bytes(packet) for packet in rewritten_packets))

RawRtpPeerConnection = RTCPeerConnection
