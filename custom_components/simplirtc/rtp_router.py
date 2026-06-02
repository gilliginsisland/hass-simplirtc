"""Raw RTP router peer connection helpers."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Literal, override

from aiortc import (
	RTCBundlePolicy,
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
from aiortc.rtp import (
	AnyRtcpPacket,
	HeaderExtensionsMap,
	RTCP_PSFB_APP,
	RtcpByePacket,
	RtcpPacket,
	RtcpPsfbPacket,
	RtcpRrPacket,
	RtcpRtpfbPacket,
	RtcpSdesPacket,
	RtcpSrPacket,
	RtpPacket,
	pack_remb_fci,
	unpack_remb_fci,
)

Side = Literal["livekit", "peer"]

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RtpInput:
	"""Negotiated RTP stream expected from a remote peer."""

	side: Side
	kind: str
	mid: str
	transport: RawRtpDtlsTransport
	parameters: RTCRtpReceiveParameters
	header_extensions: HeaderExtensionsMap
	local_rtcp_ssrc: int | None
	primary_ssrc: int | None
	rtx_ssrc: int | None

	def codec_for_payload_type(self, payload_type: int) -> RTCRtpCodecParameters | None:
		for codec in self.parameters.codecs:
			if codec.payloadType == payload_type:
				return codec
		return None

	def note_packet_ssrc(self, packet: RtpPacket) -> None:
		codec = self.codec_for_payload_type(packet.payload_type)
		if codec is None:
			return
		if is_rtx(codec):
			if self.rtx_ssrc is None:
				self.rtx_ssrc = packet.ssrc
			return
		if self.primary_ssrc is None:
			self.primary_ssrc = packet.ssrc

	def is_rtx_ssrc(self, ssrc: int) -> bool:
		return self.rtx_ssrc == ssrc


@dataclass(slots=True)
class RtpOutput:
	"""Negotiated RTP stream we advertise to a remote peer."""

	side: Side
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
		for codec in self.parameters.codecs:
			if not is_rtx(codec) and is_codec_compatible(codec, source_codec):
				return codec
		return None

	def rtx_codec_for_base(self, base_codec: RTCRtpCodecParameters) -> RTCRtpCodecParameters | None:
		for codec in self.parameters.codecs:
			if is_rtx(codec) and codec.parameters.get("apt") == base_codec.payloadType:
				return codec
		return None


class RawRtpBridge:
	"""Route decrypted RTP/RTCP packets between two peer connections."""

	def __init__(self) -> None:
		self._inputs_by_side_kind: dict[tuple[Side, str], RtpInput] = {}
		self._outputs_by_side_kind: dict[tuple[Side, str], RtpOutput] = {}
		self._inputs_by_side_ssrc: dict[tuple[Side, int], RtpInput] = {}
		self._outputs_by_side_ssrc: dict[tuple[Side, int], RtpOutput] = {}

	def register_input(
		self,
		*,
		side: Side,
		kind: str,
		receiver: RawRtpReceiver,
		parameters: RTCRtpReceiveParameters,
	) -> None:
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(parameters)
		primary_ssrc: int | None = None
		rtx_ssrc: int | None = None
		if parameters.encodings:
			primary_ssrc = parameters.encodings[0].ssrc
			if parameters.encodings[0].rtx is not None:
				rtx_ssrc = parameters.encodings[0].rtx.ssrc
		transport = receiver.transport
		if not isinstance(transport, RawRtpDtlsTransport):
			raise RuntimeError(f"Raw RTP input for {side} {kind} has non-raw transport")

		rtp_input = RtpInput(
			side=side,
			kind=kind,
			mid=parameters.muxId,
			transport=transport,
			parameters=parameters,
			header_extensions=header_extensions,
			local_rtcp_ssrc=receiver.local_rtcp_ssrc,
			primary_ssrc=primary_ssrc,
			rtx_ssrc=rtx_ssrc,
		)
		self._inputs_by_side_kind[(side, kind)] = rtp_input
		self._index_input_ssrcs(rtp_input)

	def unregister_input(self, *, side: Side, kind: str) -> None:
		rtp_input = self._inputs_by_side_kind.pop((side, kind), None)
		if rtp_input is None:
			return
		for key, value in list(self._inputs_by_side_ssrc.items()):
			if value is rtp_input:
				self._inputs_by_side_ssrc.pop(key)

	def register_output(
		self,
		*,
		side: Side,
		kind: str,
		transport: RawRtpDtlsTransport,
		sender: RawRtpSender,
		parameters: RTCRtpSendParameters,
	) -> None:
		header_extensions = HeaderExtensionsMap()
		header_extensions.configure(parameters)
		output = RtpOutput(
			side=side,
			kind=kind,
			mid=parameters.muxId,
			transport=transport,
			sender=sender,
			parameters=parameters,
			header_extensions=header_extensions,
		)
		self._outputs_by_side_kind[(side, kind)] = output
		self._outputs_by_side_ssrc[(side, output.primary_ssrc)] = output
		self._outputs_by_side_ssrc[(side, output.rtx_ssrc)] = output

	def unregister_output(self, *, side: Side, kind: str) -> None:
		output = self._outputs_by_side_kind.pop((side, kind), None)
		if output is None:
			return
		for key, value in list(self._outputs_by_side_ssrc.items()):
			if value is output:
				self._outputs_by_side_ssrc.pop(key)

	async def forward_rtp(self, side: Side, data: bytes) -> None:
		rtp_input = self._input_for_rtp(side, data)
		if rtp_input is None:
			return
		packet = RtpPacket.parse(data, rtp_input.header_extensions)
		rtp_input.note_packet_ssrc(packet)
		self._index_input_ssrcs(rtp_input)

		output = self._outputs_by_side_kind.get((self._opposite(side), rtp_input.kind))
		if output is None:
			return

		source_codec = rtp_input.codec_for_payload_type(packet.payload_type)
		if source_codec is None:
			return
		target_codec = output.codec_for_source(source_codec)
		if target_codec is None:
			return

		if is_rtx(source_codec):
			target_rtx_codec = output.rtx_codec_for_base(target_codec)
			if target_rtx_codec is None:
				return
			packet.payload_type = target_rtx_codec.payloadType
			packet.ssrc = output.rtx_ssrc
		else:
			packet.payload_type = target_codec.payloadType
			packet.ssrc = output.primary_ssrc

		packet.extensions.mid = output.mid
		await output.transport._send_rtp(packet.serialize(output.header_extensions))

	async def forward_rtcp(self, side: Side, data: bytes) -> None:
		try:
			packets = RtcpPacket.parse(data)
		except ValueError as err:
			_LOGGER.debug("Dropping unparsable RTCP from %s: %s", side, err)
			return

		by_transport: dict[RawRtpDtlsTransport, list[AnyRtcpPacket]] = {}
		for packet in packets:
			route = self._rewrite_rtcp(side, packet)
			if route is None:
				continue
			transport, rewritten = route
			by_transport.setdefault(transport, []).append(rewritten)

		for transport, rewritten_packets in by_transport.items():
			await transport._send_rtp(b"".join(bytes(packet) for packet in rewritten_packets))

	def _input_for_rtp(self, side: Side, data: bytes) -> RtpInput | None:
		packet = RtpPacket.parse(data)
		rtp_input = self._inputs_by_side_ssrc.get((side, packet.ssrc))
		if rtp_input is not None:
			return rtp_input

		for input_ in self._inputs_by_side_kind.values():
			if input_.side != side:
				continue
			packet_with_extensions = RtpPacket.parse(data, input_.header_extensions)
			if packet_with_extensions.extensions.mid == input_.mid:
				return input_

		# If MID is absent, payload type is only safe when it identifies exactly one input.
		payload_type_input: RtpInput | None = None
		for input_ in self._inputs_by_side_kind.values():
			if input_.side != side or input_.codec_for_payload_type(packet.payload_type) is None:
				continue
			if payload_type_input is not None:
				return None
			payload_type_input = input_
		if payload_type_input is None:
			return None
		return payload_type_input

	def _rewrite_rtcp(
		self,
		side: Side,
		packet: AnyRtcpPacket,
	) -> tuple[RawRtpDtlsTransport, AnyRtcpPacket] | None:
		if isinstance(packet, RtcpSrPacket):
			rtp_input = self._inputs_by_side_ssrc.get((side, packet.ssrc))
			if rtp_input is None:
				return None
			output = self._outputs_by_side_kind.get((self._opposite(side), rtp_input.kind))
			if output is None:
				return None
			packet.ssrc = output.primary_ssrc
			self._rewrite_receiver_reports(packet.reports, side)
			return output.transport, packet

		if isinstance(packet, RtcpRrPacket):
			self._rewrite_receiver_reports(packet.reports, side)
			report_ssrc = packet.reports[0].ssrc if packet.reports else None
			rtcp_ssrc = self._rtcp_ssrc_for_feedback_target(side, report_ssrc)
			if rtcp_ssrc is None:
				return None
			transport = self._transport_for_media_ssrc(self._opposite(side), report_ssrc)
			if transport is None:
				return None
			packet.ssrc = rtcp_ssrc
			return transport, packet

		if isinstance(packet, (RtcpPsfbPacket, RtcpRtpfbPacket)):
			feedback_route = self._rewrite_feedback_packet(side, packet)
			if feedback_route is None:
				return None
			rtcp_ssrc, route_media_ssrc = feedback_route
			transport = self._transport_for_media_ssrc(self._opposite(side), packet.media_ssrc)
			if transport is None and route_media_ssrc != packet.media_ssrc:
				transport = self._transport_for_media_ssrc(self._opposite(side), route_media_ssrc)
			if transport is None:
				return None
			packet.ssrc = rtcp_ssrc
			return transport, packet

		if isinstance(packet, RtcpByePacket):
			sources: list[int] = []
			for source in packet.sources:
				mapped = self._map_media_ssrc_from_side(side, source)
				if mapped is not None:
					sources.append(mapped)
			if not sources:
				return None
			packet.sources = sources
			transport = self._transport_for_media_ssrc(self._opposite(side), sources[0])
			if transport is None:
				return None
			return transport, packet

		if isinstance(packet, RtcpSdesPacket):
			mapped_chunks = []
			for chunk in packet.chunks:
				mapped = self._map_media_ssrc_from_side(side, chunk.ssrc)
				if mapped is not None:
					chunk.ssrc = mapped
					mapped_chunks.append(chunk)
			if not mapped_chunks:
				return None
			packet.chunks = mapped_chunks
			transport = self._transport_for_media_ssrc(self._opposite(side), mapped_chunks[0].ssrc)
			if transport is None:
				return None
			return transport, packet

		return None

	def _rewrite_receiver_reports(self, reports: list[object], side: Side) -> None:
		for report in reports:
			if not hasattr(report, "ssrc"):
				continue
			mapped = self._map_media_ssrc_from_side(side, report.ssrc)
			if mapped is not None:
				report.ssrc = mapped

	def _rewrite_feedback_packet(
		self,
		side: Side,
		packet: RtcpPsfbPacket | RtcpRtpfbPacket,
	) -> tuple[int, int] | None:
		if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_APP:
			try:
				bitrate, ssrcs = unpack_remb_fci(packet.fci)
			except ValueError:
				return None
			mapped_ssrcs = [
				mapped_ssrc
				for ssrc in ssrcs
				if (mapped_ssrc := self._map_media_ssrc_from_side(side, ssrc)) is not None
			]
			if not mapped_ssrcs:
				return None
			packet.fci = pack_remb_fci(bitrate, mapped_ssrcs)
			rtcp_ssrc = self._rtcp_ssrc_for_feedback_target(side, mapped_ssrcs[0])
			if rtcp_ssrc is None:
				return None
			return rtcp_ssrc, mapped_ssrcs[0]

		mapped = self._map_media_ssrc_from_side(side, packet.media_ssrc)
		if mapped is None:
			return None
		packet.media_ssrc = mapped
		rtcp_ssrc = self._rtcp_ssrc_for_feedback_target(side, packet.media_ssrc)
		if rtcp_ssrc is None:
			return None
		return rtcp_ssrc, packet.media_ssrc

	def _map_media_ssrc_from_side(self, side: Side, ssrc: int) -> int | None:
		output = self._outputs_by_side_ssrc.get((side, ssrc))
		if output is not None:
			target_input = self._inputs_by_side_kind.get((self._opposite(side), output.kind))
			if target_input is None:
				return None
			if output.rtx_ssrc == ssrc:
				return target_input.rtx_ssrc
			return target_input.primary_ssrc

		rtp_input = self._inputs_by_side_ssrc.get((side, ssrc))
		if rtp_input is not None:
			target_output = self._outputs_by_side_kind.get((self._opposite(side), rtp_input.kind))
			if target_output is None:
				return None
			if rtp_input.is_rtx_ssrc(ssrc):
				return target_output.rtx_ssrc
			return target_output.primary_ssrc
		return None

	def _rtcp_ssrc_for_feedback_target(self, source_side: Side, media_ssrc: int | None) -> int | None:
		if media_ssrc is None:
			return None
		target_side = self._opposite(source_side)
		rtp_input = self._inputs_by_side_ssrc.get((target_side, media_ssrc))
		if rtp_input is not None:
			return rtp_input.local_rtcp_ssrc
		output = self._outputs_by_side_ssrc.get((target_side, media_ssrc))
		if output is not None:
			return output.primary_ssrc
		return None

	def _transport_for_media_ssrc(self, side: Side, ssrc: int | None) -> RawRtpDtlsTransport | None:
		if ssrc is None:
			return None
		rtp_input = self._inputs_by_side_ssrc.get((side, ssrc))
		if rtp_input is not None:
			return rtp_input.transport
		output = self._outputs_by_side_ssrc.get((side, ssrc))
		if output is not None:
			return output.transport
		return None

	def _index_input_ssrcs(self, rtp_input: RtpInput) -> None:
		if rtp_input.primary_ssrc is not None:
			self._inputs_by_side_ssrc[(rtp_input.side, rtp_input.primary_ssrc)] = rtp_input
		if rtp_input.rtx_ssrc is not None:
			self._inputs_by_side_ssrc[(rtp_input.side, rtp_input.rtx_ssrc)] = rtp_input

	def _opposite(self, side: Side) -> Side:
		if side == "livekit":
			return "peer"
		return "livekit"


class RawRtpDtlsTransport(RTCDtlsTransport):
	"""DTLS transport that routes decrypted media through RawRtpBridge."""

	def __init__(
		self,
		transport: RTCIceTransport,
		certificates: list[object],
		*,
		bridge: RawRtpBridge,
		side: Side,
	) -> None:
		super().__init__(transport, certificates)
		self._raw_bridge = bridge
		self._raw_side = side

	@override
	async def _handle_rtp_data(self, data: bytes, arrival_time_ms: int) -> None:
		await self._raw_bridge.forward_rtp(self._raw_side, data)

	@override
	async def _handle_rtcp_data(self, data: bytes) -> None:
		await self._raw_bridge.forward_rtcp(self._raw_side, data)


class RawRtpSender(RTCRtpSender):
	"""RTP sender that only registers negotiated outbound RTP state."""

	def __init__(
		self,
		trackOrKind: MediaStreamTrack | str,
		transport: RawRtpDtlsTransport,
		*,
		bridge: RawRtpBridge,
		side: Side,
	) -> None:
		super().__init__(trackOrKind, transport)
		self._raw_bridge = bridge
		self._raw_side = side
		self._raw_started = False

	@override
	async def send(self, parameters: RTCRtpSendParameters) -> None:
		if self._raw_started:
			return
		self._raw_bridge.register_output(
			side=self._raw_side,
			kind=self.kind,
			transport=self.transport,
			sender=self,
			parameters=parameters,
		)
		self._raw_started = True

	@override
	async def stop(self) -> None:
		if self._raw_started:
			self._raw_bridge.unregister_output(side=self._raw_side, kind=self.kind)
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
		bridge: RawRtpBridge,
		side: Side,
	) -> None:
		super().__init__(kind, transport)
		self._raw_bridge = bridge
		self._raw_side = side
		self._raw_kind = kind
		self._raw_started = False
		self.local_rtcp_ssrc: int | None = None

	@override
	async def receive(self, parameters: RTCRtpReceiveParameters) -> None:
		if self._raw_started:
			return
		self._raw_bridge.register_input(
			side=self._raw_side,
			kind=self._raw_kind,
			receiver=self,
			parameters=parameters,
		)
		self._raw_started = True

	@override
	async def stop(self) -> None:
		if self._raw_started:
			self._raw_bridge.unregister_input(side=self._raw_side, kind=self._raw_kind)
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
		bridge: RawRtpBridge,
		side: Side,
		configuration: RTCConfiguration | None = None,
	) -> None:
		super().__init__(configuration=configuration)
		self._raw_bridge = bridge
		self._raw_side = side
		self._remote_supported_codecs_by_kind: dict[str, list[RTCRtpCodecParameters]] = {}

	@override
	async def setRemoteDescription(self, sessionDescription: RTCSessionDescription) -> None:
		await super().setRemoteDescription(sessionDescription)
		self._remote_supported_codecs_by_kind = {}
		for transceiver in self.getTransceivers():
			if transceiver._codecs:
				self._remote_supported_codecs_by_kind.setdefault(transceiver.kind, []).extend(
					transceiver._codecs
				)

	def remote_supported_codecs(self, kind: str) -> list[RTCRtpCodecParameters]:
		"""Return codecs accepted from the already-applied remote description."""
		return list(self._remote_supported_codecs_by_kind.get(kind, ()))

	def answer_codecs(self, kind: str) -> list[RTCRtpCodecParameters]:
		"""Return the codecs this peer is currently prepared to answer with."""
		for transceiver in self.getTransceivers():
			if transceiver.kind == kind and transceiver._codecs:
				return list(transceiver._codecs)
		return []

	def constrain_answer_codecs(
		self,
		kind: str,
		supported_codecs: list[RTCRtpCodecParameters],
	) -> list[RTCRtpCodecParameters]:
		"""Constrain this peer's answer codecs to another peer's codec order."""
		if not supported_codecs:
			raise RuntimeError(f"No supported {kind} codecs were provided")

		for transceiver in self.getTransceivers():
			if transceiver.kind != kind or not transceiver._codecs:
				continue
			selected = self._compatible_codecs(transceiver._codecs, supported_codecs)
			if not selected:
				raise RuntimeError(f"No {kind} codec is compatible between the two remote offers")
			transceiver._codecs = selected
			return selected
		raise RuntimeError(f"Peer offer has no {kind} media section to constrain")

	def constrain_answer_codecs_from(
		self,
		other: RawRtpPeerConnection,
		*,
		kinds: tuple[str, ...] = ("audio", "video"),
	) -> None:
		"""Constrain this peer's answer codecs from another peer's remote offer."""
		for kind in kinds:
			supported_codecs = other.answer_codecs(kind)
			if not supported_codecs:
				raise RuntimeError(f"Source peer has no selected {kind} answer codecs")
			self.constrain_answer_codecs(kind, supported_codecs)

	def set_answer_direction(self, kind: str, direction: str) -> None:
		"""Set the local answer direction for a negotiated media section."""
		for transceiver in self.getTransceivers():
			if transceiver.kind == kind:
				transceiver.direction = direction
				return
		raise RuntimeError(f"Peer offer has no {kind} media section to set direction")

	def _compatible_codecs(
		self,
		candidate_codecs: list[RTCRtpCodecParameters],
		supported_codecs: list[RTCRtpCodecParameters],
	) -> list[RTCRtpCodecParameters]:
		selected: list[RTCRtpCodecParameters] = []
		selected_payload_types: set[int] = set()
		selected_base_codecs: list[tuple[RTCRtpCodecParameters, RTCRtpCodecParameters]] = []
		for supported in supported_codecs:
			if is_rtx(supported):
				continue
			for candidate in candidate_codecs:
				if (
					not is_rtx(candidate)
					and candidate.payloadType not in selected_payload_types
					and is_codec_compatible(candidate, supported)
				):
					selected.append(candidate)
					selected_payload_types.add(candidate.payloadType)
					selected_base_codecs.append((candidate, supported))
					break

		for candidate_base, supported_base in selected_base_codecs:
			for candidate in candidate_codecs:
				if (
					is_rtx(candidate)
					and candidate.payloadType not in selected_payload_types
					and candidate.parameters.get("apt") == candidate_base.payloadType
					and self._supports_rtx_for_base(supported_codecs, supported_base)
				):
					selected.append(candidate)
					selected_payload_types.add(candidate.payloadType)
					break
		return selected

	def _supports_rtx_for_base(
		self,
		codecs: list[RTCRtpCodecParameters],
		base_codec: RTCRtpCodecParameters,
	) -> bool:
		for codec in codecs:
			if is_rtx(codec) and codec.parameters.get("apt") == base_codec.payloadType:
				return True
		return False

	@override
	def _RTCPeerConnection__createDtlsTransport(self) -> RawRtpDtlsTransport:
		if len(self._RTCPeerConnection__transceivers) > 0 or self._RTCPeerConnection__sctp:
			if len(self._RTCPeerConnection__transceivers) > 0:
				parameters = self._RTCPeerConnection__transceivers[
					0
				].receiver.transport.transport.iceGatherer.getLocalParameters()
			else:
				parameters = (
					self._RTCPeerConnection__sctp.transport.transport.iceGatherer.getLocalParameters()
				)
			ice_gatherer = RTCIceGatherer(
				iceServers=self._RTCPeerConnection__configuration.iceServers,
				local_username=parameters.usernameFragment,
				local_password=parameters.password,
			)
		else:
			ice_gatherer = RTCIceGatherer(iceServers=self._RTCPeerConnection__configuration.iceServers)

		ice_gatherer.on("statechange", self._RTCPeerConnection__updateIceGatheringState)
		ice_transport = RTCIceTransport(ice_gatherer)
		ice_transport.on("statechange", self._RTCPeerConnection__updateIceConnectionState)
		ice_transport.on("statechange", self._RTCPeerConnection__updateConnectionState)
		self._RTCPeerConnection__iceTransports.add(ice_transport)

		dtls_transport = RawRtpDtlsTransport(
			ice_transport,
			self._RTCPeerConnection__certificates,
			bridge=self._raw_bridge,
			side=self._raw_side,
		)
		dtls_transport.on("statechange", self._RTCPeerConnection__updateConnectionState)
		self._RTCPeerConnection__dtlsTransports.add(dtls_transport)

		self._RTCPeerConnection__updateIceGatheringState()
		self._RTCPeerConnection__updateIceConnectionState()
		self._RTCPeerConnection__updateConnectionState()
		return dtls_transport

	@override
	def _RTCPeerConnection__createTransceiver(
		self,
		direction: str,
		kind: str,
		sender_track: MediaStreamTrack | None = None,
	) -> RTCRtpTransceiver:
		dtls_transport = None
		bundled = False
		transceivers = self._RTCPeerConnection__transceivers
		if self._RTCPeerConnection__configuration.bundlePolicy == RTCBundlePolicy.MAX_BUNDLE:
			if len(transceivers) > 0:
				dtls_transport = transceivers[0].receiver.transport
				bundled = True
			elif self._RTCPeerConnection__sctp:
				dtls_transport = self._RTCPeerConnection__sctp.transport
				bundled = True
		elif self._RTCPeerConnection__configuration.bundlePolicy == RTCBundlePolicy.BALANCED:
			transceiver = next(
				filter(lambda item: item.kind == kind, transceivers),
				None,
			)
			if transceiver:
				dtls_transport = transceiver.receiver.transport
				bundled = True

		if not dtls_transport:
			dtls_transport = self._RTCPeerConnection__createDtlsTransport()
		if not isinstance(dtls_transport, RawRtpDtlsTransport):
			raise RuntimeError("Raw RTP transceiver received a non-raw DTLS transport")

		sender = RawRtpSender(
			sender_track or kind,
			dtls_transport,
			bridge=self._raw_bridge,
			side=self._raw_side,
		)
		receiver = RawRtpReceiver(
			kind,
			dtls_transport,
			bridge=self._raw_bridge,
			side=self._raw_side,
		)
		transceiver = RTCRtpTransceiver(
			direction=direction,
			kind=kind,
			sender=sender,
			receiver=receiver,
		)
		transceiver.receiver._set_rtcp_ssrc(transceiver.sender._ssrc)
		transceiver.sender._stream_id = self._RTCPeerConnection__stream_id
		transceiver._bundled = bundled
		transceivers.append(transceiver)
		return transceiver
