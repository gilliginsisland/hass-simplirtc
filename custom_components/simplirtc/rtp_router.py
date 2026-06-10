"""Raw RTP router peer connection helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import logging
from typing import (
	Any,
	Literal,
	cast,
	override,
)

from aiortc import (
	RTCBundlePolicy,
	RTCCertificate,
	RTCConfiguration,
	RTCDtlsTransport,
	RTCIceGatherer,
	RTCIceTransport,
	RTCPeerConnection,
	RTCRtpReceiver,
	RTCRtpSender,
	RTCRtpTransceiver,
	RTCSessionDescription,
)
from aiortc.codecs import is_rtx
from aiortc.mediastreams import MediaStreamTrack
from aiortc.rtcpeerconnection import is_codec_compatible
from aiortc.rtcrtpparameters import (
	RTCRtpCodecParameters,
	RTCRtpReceiveParameters,
	RTCRtpSendParameters,
)
from aiortc.sdp import SessionDescription as SdpSessionDescription
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

Side = Literal["producer", "consumer"]
PeerId = str

_LOGGER = logging.getLogger(__name__)
_RTP_SUBSCRIPTION_QUEUE_SIZE = 90


@dataclass(slots=True)
class RtpInput:
	"""Negotiated RTP stream expected from a remote endpoint."""

	side: Side
	peer_id: PeerId
	kind: str
	mid: str
	transport: RawRtpDtlsTransport
	parameters: RTCRtpReceiveParameters
	header_extensions: HeaderExtensionsMap
	local_rtcp_ssrc: int | None
	primary_ssrc: int | None
	rtx_ssrc: int | None
	subscriptions: dict[PeerId, RtpSubscription] = field(default_factory=dict, init=False)

	def codec_for_payload_type(self, payload_type: int) -> RTCRtpCodecParameters | None:
		return next((codec for codec in self.parameters.codecs if codec.payloadType == payload_type), None)

	def note_packet_ssrc(self, packet: RtpPacket) -> None:
		if not (codec := self.codec_for_payload_type(packet.payload_type)):
			return
		if is_rtx(codec):
			if self.rtx_ssrc is None:
				self.rtx_ssrc = packet.ssrc
			return
		if self.primary_ssrc is None:
			self.primary_ssrc = packet.ssrc

	def is_rtx_ssrc(self, ssrc: int) -> bool:
		return self.rtx_ssrc == ssrc

	def subscribe(self, output: RtpOutput) -> None:
		"""Subscribe a consumer output to this producer input."""
		if subscription := self.subscriptions.pop(output.peer_id, None):
			subscription.close()
		self.subscriptions[output.peer_id] = RtpSubscription(self, output)

	def unsubscribe(self, peer_id: PeerId) -> None:
		"""Remove a consumer output subscription."""
		if subscription := self.subscriptions.pop(peer_id, None):
			subscription.close()

	def close(self) -> None:
		"""Close all subscriptions for this input."""
		for subscription in self.subscriptions.values():
			subscription.close()
		self.subscriptions.clear()

	def matches_rtp(self, peer_id: PeerId, data: bytes, packet: RtpPacket) -> bool:
		"""Return whether an RTP packet belongs to this input."""
		if self.peer_id != peer_id:
			return False
		if packet.ssrc in (self.primary_ssrc, self.rtx_ssrc):
			return True
		packet_with_extensions = RtpPacket.parse(data, self.header_extensions)
		if packet_with_extensions.extensions.mid == self.mid:
			return True
		return self.codec_for_payload_type(packet.payload_type) is not None

	def forward_rtp(self, data: bytes) -> None:
		"""Forward one producer RTP packet to subscribed outputs."""
		packet = RtpPacket.parse(data, self.header_extensions)
		self.note_packet_ssrc(packet)
		if not (source_codec := self.codec_for_payload_type(packet.payload_type)):
			return
		for subscription in tuple(self.subscriptions.values()):
			if rewritten := subscription.rewrite_rtp(data, source_codec):
				subscription.enqueue_rtp(rewritten)

	def rewrite_rtcp_to_subscribers(
		self,
		peer_id: PeerId,
		packet: AnyRtcpPacket,
	) -> tuple[tuple[RawRtpDtlsTransport, AnyRtcpPacket], ...]:
		"""Rewrite producer RTCP for all subscribers."""
		if self.peer_id != peer_id:
			return ()
		routes = []
		for subscription in tuple(self.subscriptions.values()):
			if rewritten := subscription.rewrite_rtcp_from_input(packet):
				routes.append((subscription.output.transport, rewritten))
		return tuple(routes)

	def rewrite_rtcp_from_subscriber(
		self,
		peer_id: PeerId,
		packet: AnyRtcpPacket,
	) -> tuple[tuple[RawRtpDtlsTransport, AnyRtcpPacket], ...]:
		"""Rewrite subscriber RTCP feedback for this producer input."""
		if not (subscription := self.subscriptions.get(peer_id)):
			return ()
		if not (rewritten := subscription.rewrite_rtcp_from_output(packet)):
			return ()
		return ((self.transport, rewritten),)


@dataclass(slots=True)
class RtpOutput:
	"""Negotiated RTP stream we advertise to a remote endpoint."""

	side: Side
	peer_id: PeerId
	kind: str
	mid: str
	transport: RawRtpDtlsTransport
	sender: RawRtpSender
	parameters: RTCRtpSendParameters
	header_extensions: HeaderExtensionsMap

	@property
	def primary_ssrc(self) -> int:
		return self.sender._ssrc

	@property
	def rtx_ssrc(self) -> int:
		return self.sender._rtx_ssrc

	def codec_for_source(self, source_codec: RTCRtpCodecParameters) -> RTCRtpCodecParameters | None:
		return next(
			(
				codec for codec in self.parameters.codecs
				if not is_rtx(codec) and is_codec_compatible(codec, source_codec)
			),
			None,
		)

	def rtx_codec_for_base(self, base_codec: RTCRtpCodecParameters) -> RTCRtpCodecParameters | None:
		if (base_payload_type := base_codec.payloadType) is None:
			raise RuntimeError(f"Negotiated codec {base_codec.mimeType} is missing a payload type")
		return next(
			(
				codec for codec in self.parameters.codecs
				if is_rtx(codec) and codec.parameters.get("apt") == base_payload_type
			),
			None,
		)


@dataclass(slots=True)
class RtpSubscription:
	"""Mapping between one producer input and one consumer output."""

	input: RtpInput
	output: RtpOutput
	_queue: asyncio.Queue[bytes] = field(init=False, repr=False)
	_send_task: asyncio.Task[None] = field(init=False, repr=False)
	_dropped_packets: int = field(default=0, init=False, repr=False)

	def __post_init__(self) -> None:
		self._queue = asyncio.Queue(maxsize=_RTP_SUBSCRIPTION_QUEUE_SIZE)
		self._send_task = asyncio.create_task(self._send_rtp())
		self._send_task.add_done_callback(self._log_send_error)

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
		if not (target_codec := self.output.codec_for_source(source_codec)):
			return None
		packet = RtpPacket.parse(data, self.input.header_extensions)
		if is_rtx(source_codec):
			if not (target_rtx_codec := self.output.rtx_codec_for_base(target_codec)):
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
			self._rewrite_receiver_reports_from_input(rewritten.reports)
			return rewritten

		if isinstance(rewritten, RtcpRrPacket):
			if not self._rewrite_receiver_reports_from_input(rewritten.reports):
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
			self._rewrite_receiver_reports_from_output(rewritten.reports)
			return rewritten

		if isinstance(rewritten, RtcpRrPacket):
			if not self._rewrite_receiver_reports_from_output(rewritten.reports):
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

	def _rewrite_receiver_reports_from_input(self, reports: list[RtcpReceiverInfo]) -> bool:
		return self._rewrite_receiver_reports(reports, self._input_to_output_ssrc)

	def _rewrite_receiver_reports_from_output(self, reports: list[RtcpReceiverInfo]) -> bool:
		return self._rewrite_receiver_reports(reports, self._output_to_input_ssrc)

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


class RawRtpProducer:
	"""Raw RTP media produced by one producer PC and subscribed to by consumers."""

	def __init__(self) -> None:
		self._inputs_by_kind: dict[str, RtpInput] = {}
		self._outputs_by_kind: dict[str, dict[PeerId, RtpOutput]] = {}

	def register_input(
		self,
		*,
		side: Side,
		peer_id: PeerId,
		kind: str,
		receiver: RawRtpReceiver,
		parameters: RTCRtpReceiveParameters,
	) -> None:
		if side != "producer":
			return
		if existing_input := self._inputs_by_kind.get(kind):
			existing_input.close()
		rtp_input = self._inputs_by_kind[kind] = self._create_input(
			side=side,
			peer_id=peer_id,
			kind=kind,
			receiver=receiver,
			parameters=parameters,
		)
		for output in self._outputs_by_kind.get(kind, {}).values():
			rtp_input.subscribe(output)

	def unregister_input(self, *, side: Side, peer_id: PeerId, kind: str) -> None:
		if (
			side == "producer"
			and (rtp_input := self._inputs_by_kind.get(kind))
			and rtp_input.peer_id == peer_id
		):
			rtp_input.close()
			del self._inputs_by_kind[kind]

	def register_output(
		self,
		*,
		side: Side,
		peer_id: PeerId,
		kind: str,
		transport: RawRtpDtlsTransport,
		sender: RawRtpSender,
		parameters: RTCRtpSendParameters,
	) -> None:
		if side != "consumer":
			return
		output = self._create_output(
			side=side,
			peer_id=peer_id,
			kind=kind,
			transport=transport,
			sender=sender,
			parameters=parameters,
		)
		self._outputs_by_kind.setdefault(kind, {})[peer_id] = output
		if rtp_input := self._inputs_by_kind.get(kind):
			rtp_input.subscribe(output)

	def unregister_output(self, *, side: Side, peer_id: PeerId, kind: str) -> None:
		if side != "consumer":
			return
		if outputs := self._outputs_by_kind.get(kind):
			outputs.pop(peer_id, None)
			if not outputs:
				del self._outputs_by_kind[kind]
		if rtp_input := self._inputs_by_kind.get(kind):
			rtp_input.unsubscribe(peer_id)

	async def forward_rtp(self, side: Side, peer_id: PeerId, data: bytes) -> None:
		if side != "producer" or not (rtp_input := self._input_for_rtp(peer_id, data)):
			return
		rtp_input.forward_rtp(data)

	async def forward_rtcp(self, side: Side, peer_id: PeerId, data: bytes) -> None:
		try:
			packets = RtcpPacket.parse(data)
		except ValueError as err:
			_LOGGER.debug("Dropping unparsable RTCP from %s: %s", side, err)
			return

		by_transport: dict[RawRtpDtlsTransport, list[AnyRtcpPacket]] = {}
		for packet in packets:
			for rtp_input in tuple(self._inputs_by_kind.values()):
				routes = (
					rtp_input.rewrite_rtcp_to_subscribers(peer_id, packet)
					if side == "producer"
					else rtp_input.rewrite_rtcp_from_subscriber(peer_id, packet)
				)
				for transport, rewritten in routes:
					by_transport.setdefault(transport, []).append(rewritten)

		for transport, rewritten_packets in by_transport.items():
			await transport._send_rtp(b"".join(bytes(packet) for packet in rewritten_packets))

	def _input_for_rtp(self, peer_id: PeerId, data: bytes) -> RtpInput | None:
		packet = RtpPacket.parse(data)
		matches = (
			rtp_input for rtp_input in self._inputs_by_kind.values()
			if rtp_input.matches_rtp(peer_id, data, packet)
		)
		if not (rtp_input := next(matches, None)):
			return None
		return None if next(matches, None) else rtp_input

	def _create_input(
		self,
		*,
		side: Side,
		peer_id: PeerId,
		kind: str,
		receiver: RawRtpReceiver,
		parameters: RTCRtpReceiveParameters,
	) -> RtpInput:
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(parameters)
		primary_ssrc: int | None = None
		rtx_ssrc: int | None = None
		if parameters.encodings:
			encoding = parameters.encodings[0]
			primary_ssrc = encoding.ssrc
			if rtx := encoding.rtx:
				rtx_ssrc = rtx.ssrc
		transport = receiver.transport
		if not isinstance(transport, RawRtpDtlsTransport):
			raise RuntimeError(f"Raw RTP input for {side} {kind} has non-raw transport")
		return RtpInput(
			side=side,
			peer_id=peer_id,
			kind=kind,
			mid=parameters.muxId,
			transport=transport,
			parameters=parameters,
			header_extensions=header_extensions,
			local_rtcp_ssrc=receiver.local_rtcp_ssrc,
			primary_ssrc=primary_ssrc,
			rtx_ssrc=rtx_ssrc,
		)

	def _create_output(
		self,
		*,
		side: Side,
		peer_id: PeerId,
		kind: str,
		transport: RawRtpDtlsTransport,
		sender: RawRtpSender,
		parameters: RTCRtpSendParameters,
	) -> RtpOutput:
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(parameters)
		return RtpOutput(
			side=side,
			peer_id=peer_id,
			kind=kind,
			mid=parameters.muxId,
			transport=transport,
			sender=sender,
			parameters=parameters,
			header_extensions=header_extensions,
		)


class RawRtpDtlsTransport(RTCDtlsTransport):
	"""DTLS transport that routes decrypted media through a raw RTP producer."""

	def __init__(
		self,
		transport: RTCIceTransport,
		certificates: list[RTCCertificate],
		*,
		producer: RawRtpProducer,
		side: Side,
		peer_id: PeerId,
	) -> None:
		super().__init__(transport, certificates)
		self._raw_producer = producer
		self._raw_side: Side = side
		self._raw_peer_id = peer_id

	@override
	async def _handle_rtp_data(self, data: bytes, arrival_time_ms: int) -> None:
		await self._raw_producer.forward_rtp(self._raw_side, self._raw_peer_id, data)

	@override
	async def _handle_rtcp_data(self, data: bytes) -> None:
		await self._raw_producer.forward_rtcp(self._raw_side, self._raw_peer_id, data)


class RawRtpSender(RTCRtpSender):
	"""RTP sender that only registers negotiated outbound RTP state."""

	def __init__(
		self,
		trackOrKind: MediaStreamTrack | str,
		transport: RawRtpDtlsTransport,
		*,
		producer: RawRtpProducer,
		side: Side,
		peer_id: PeerId,
	) -> None:
		super().__init__(trackOrKind, transport)
		self._raw_producer = producer
		self._raw_side: Side = side
		self._raw_peer_id = peer_id
		self._raw_started = False

	@override
	async def send(self, parameters: RTCRtpSendParameters) -> None:
		if self._raw_started:
			return
		transport = self.transport
		if not isinstance(transport, RawRtpDtlsTransport):
			raise RuntimeError(f"Raw RTP output for {self._raw_side} {self.kind} has non-raw transport")
		self._raw_producer.register_output(
			side=self._raw_side,
			peer_id=self._raw_peer_id,
			kind=self.kind,
			transport=transport,
			sender=self,
			parameters=parameters,
		)
		self._raw_started = True

	@override
	async def stop(self) -> None:
		if self._raw_started:
			self._raw_producer.unregister_output(side=self._raw_side, peer_id=self._raw_peer_id, kind=self.kind)
			self._raw_started = False

	@override
	async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
		return None


class RawRtpReceiver(RTCRtpReceiver):
	"""RTP receiver that only registers negotiated inbound RTP state."""

	def __init__(
		self,
		kind: str,
		transport: RawRtpDtlsTransport,
		*,
		producer: RawRtpProducer,
		side: Side,
		peer_id: PeerId,
	) -> None:
		super().__init__(kind, transport)
		self._raw_producer = producer
		self._raw_side: Side = side
		self._raw_peer_id = peer_id
		self._raw_kind = kind
		self._raw_started = False
		self.local_rtcp_ssrc: int | None = None

	@override
	async def receive(self, parameters: RTCRtpReceiveParameters) -> None:
		if self._raw_started:
			return
		self._raw_producer.register_input(
			side=self._raw_side,
			peer_id=self._raw_peer_id,
			kind=self._raw_kind,
			receiver=self,
			parameters=parameters,
		)
		self._raw_started = True

	@override
	async def stop(self) -> None:
		if self._raw_started:
			self._raw_producer.unregister_input(side=self._raw_side, peer_id=self._raw_peer_id, kind=self._raw_kind)
			self._raw_started = False

	@override
	def _handle_disconnect(self) -> None:
		return None

	@override
	async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
		return None

	@override
	async def _handle_rtp_packet(self, packet: RtpPacket, arrival_time_ms: int) -> None:
		return None

	@override
	def _set_rtcp_ssrc(self, ssrc: int) -> None:
		self.local_rtcp_ssrc = ssrc
		super()._set_rtcp_ssrc(ssrc)


class RawRtpPeerConnection(RTCPeerConnection):
	"""Peer connection that negotiates normally and routes raw RTP."""

	def __init__(
		self,
		*,
		producer: RawRtpProducer,
		side: Side,
		peer_id: PeerId | None = None,
		configuration: RTCConfiguration | None = None,
	) -> None:
		if not configuration:
			configuration = RTCConfiguration()
		super().__init__(configuration=configuration)
		self._raw_producer = producer
		self._raw_side: Side = side
		self._raw_peer_id = peer_id or side
		self._raw_configuration = configuration
		self._remote_media_kinds: tuple[str, ...] = ()
		self._remote_supported_codecs_by_kind: dict[str, list[RTCRtpCodecParameters]] = {}

	@property
	def peer_id(self) -> PeerId:
		"""Return this peer connection's raw RTP routing ID."""
		return self._raw_peer_id

	@property
	def rtc_configuration(self) -> RTCConfiguration:
		"""Return the mutable RTC configuration used by this peer connection."""
		return self._raw_configuration

	@override
	async def setRemoteDescription(self, sessionDescription: RTCSessionDescription) -> None:
		description = SdpSessionDescription.parse(sessionDescription.sdp)
		await super().setRemoteDescription(sessionDescription)
		self._remote_media_kinds = tuple(dict.fromkeys(
			media.kind for media in description.media
			if media.port
		))
		self._remote_supported_codecs_by_kind = {}
		for transceiver in self.getTransceivers():
			if transceiver._codecs:
				self._remote_supported_codecs_by_kind.setdefault(transceiver.kind, []).extend(
					transceiver._codecs
				)

	def remote_media_kinds(self) -> tuple[str, ...]:
		"""Return media kinds from the already-applied remote description."""
		return self._remote_media_kinds

	def remote_supported_codecs(self, kind: str) -> list[RTCRtpCodecParameters]:
		"""Return codecs accepted from the already-applied remote description."""
		return list(self._remote_supported_codecs_by_kind.get(kind, ()))

	def set_answer_direction(self, kind: str, direction: str) -> None:
		"""Set the local answer direction for a negotiated media section."""
		if not (transceiver := next(
			(item for item in self.getTransceivers() if item.kind == kind),
			None,
			)):
			raise RuntimeError(f"Remote offer has no {kind} media section to set direction")
		transceiver.direction = direction

	def _RTCPeerConnection__createDtlsTransport(self) -> RawRtpDtlsTransport:
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
				iceServers=pc._RTCPeerConnection__configuration.iceServers,
				local_username=parameters.usernameFragment,
				local_password=parameters.password,
			)
		else:
			ice_gatherer = RTCIceGatherer(iceServers=pc._RTCPeerConnection__configuration.iceServers)

		ice_gatherer.on("statechange", pc._RTCPeerConnection__updateIceGatheringState)
		ice_transport = RTCIceTransport(ice_gatherer)
		ice_transport.on("statechange", pc._RTCPeerConnection__updateIceConnectionState)
		ice_transport.on("statechange", pc._RTCPeerConnection__updateConnectionState)
		pc._RTCPeerConnection__iceTransports.add(ice_transport)

		dtls_transport = RawRtpDtlsTransport(
			ice_transport,
			pc._RTCPeerConnection__certificates,
			producer=self._raw_producer,
			side=self._raw_side,
			peer_id=self._raw_peer_id,
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
		kind: str,
		sender_track: MediaStreamTrack | None = None,
	) -> RTCRtpTransceiver:
		pc = cast(Any, self)
		dtls_transport = None
		bundled = False
		transceivers = pc._RTCPeerConnection__transceivers
		if pc._RTCPeerConnection__configuration.bundlePolicy == RTCBundlePolicy.MAX_BUNDLE:
			if transceivers:
				dtls_transport = transceivers[0].receiver.transport
				bundled = True
			elif pc._RTCPeerConnection__sctp:
				dtls_transport = pc._RTCPeerConnection__sctp.transport
				bundled = True
		elif pc._RTCPeerConnection__configuration.bundlePolicy == RTCBundlePolicy.BALANCED:
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
			producer=self._raw_producer,
			side=self._raw_side,
			peer_id=self._raw_peer_id,
		)
		receiver = RawRtpReceiver(
			kind,
			dtls_transport,
			producer=self._raw_producer,
			side=self._raw_side,
			peer_id=self._raw_peer_id,
		)
		transceiver = RTCRtpTransceiver(
			direction=direction,
			kind=kind,
			sender=sender,
			receiver=receiver,
		)
		transceiver.receiver._set_rtcp_ssrc(transceiver.sender._ssrc)
		transceiver.sender._stream_id = pc._RTCPeerConnection__stream_id
		transceiver._bundled = bundled
		transceivers.append(transceiver)
		return transceiver
