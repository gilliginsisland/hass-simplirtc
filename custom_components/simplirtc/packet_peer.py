"""Packet-preserving aiortc peer connection helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import fractions
import logging
import queue
from typing import TypeAlias, override

from av.packet import Packet
from aiortc import (
	RTCBundlePolicy,
	RTCConfiguration,
	RTCDtlsTransport,
	RTCPeerConnection,
	RTCRtpReceiver,
	RTCRtpSender,
	RTCRtpTransceiver,
	RTCSessionDescription,
)
from aiortc.codecs import is_rtx
from aiortc.jitterbuffer import JitterFrame
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack, VIDEO_TIME_BASE
from aiortc.rtcpeerconnection import is_codec_compatible
from aiortc.rtcrtpreceiver import RemoteStreamTrack
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtp import (
	AnyRtcpPacket,
	RtcpSrPacket,
)

_NTP_FRACTIONAL_SCALE = 1 << 32
_RTP_TIMESTAMP_MODULO = 1 << 32
_RTP_TIMESTAMP_HALF_MODULO = 1 << 31

EncodedFrameItem: TypeAlias = tuple[RTCRtpCodecParameters, JitterFrame] | None


@dataclass(slots=True)
class RtpSenderReport:
	ssrc: int
	rtp_timestamp: int
	ntp_seconds: float
	clock_rate: int


class RawRtpTimestampMapper:
	"""Preserve the incoming RTP timestamp for the encoded relay path."""

	def map(self, timestamp: int) -> int:
		return timestamp


class RtpSyncClock:
	"""Map incoming RTP timestamps onto one shared media timeline."""

	def __init__(self, *, logger: logging.Logger) -> None:
		self._logger = logger
		self._epoch_seconds: float | None = None
		self._reports_by_kind: dict[str, RtpSenderReport] = {}
		self._waiting_report_logged: set[str] = set()

	def update_sender_report(
		self,
		*,
		kind: str,
		clock_rate: int,
		packet: RtcpSrPacket,
	) -> None:
		if clock_rate <= 0:
			return

		report = RtpSenderReport(
			ssrc=packet.ssrc,
			rtp_timestamp=packet.sender_info.rtp_timestamp,
			ntp_seconds=(packet.sender_info.ntp_timestamp >> 32)
			+ ((packet.sender_info.ntp_timestamp & 0xFFFFFFFF) / _NTP_FRACTIONAL_SCALE),
			clock_rate=clock_rate,
		)
		self._reports_by_kind[kind] = report
		self._waiting_report_logged.discard(kind)

	def map_timestamp(
		self,
		*,
		kind: str,
		codec: RTCRtpCodecParameters,
		rtp_timestamp: int,
	) -> int | None:
		report = self._reports_by_kind.get(kind)
		if report is None:
			if kind not in self._waiting_report_logged:
				self._logger.debug("Waiting for incoming RTCP sender report before relaying %s media", kind)
				self._waiting_report_logged.add(kind)
			return None

		clock_rate = codec.clockRate or report.clock_rate
		if clock_rate <= 0:
			return None

		source_seconds = report.ntp_seconds + (
			_rtp_timestamp_delta(rtp_timestamp, report.rtp_timestamp) / report.clock_rate
		)
		if self._epoch_seconds is None:
			self._epoch_seconds = source_seconds

		relative_seconds = source_seconds - self._epoch_seconds
		if relative_seconds < 0:
			return None
		return round(relative_seconds * clock_rate)


class PacketRendezvous:
	"""No-buffer source queue replacement for aiortc's remote track."""

	def __init__(self) -> None:
		self.closed = False
		self._waiter: asyncio.Future[Packet | None] | None = None

	async def get(self) -> Packet | None:
		if self.closed:
			return None
		if self._waiter is not None and not self._waiter.done():
			raise RuntimeError("Concurrent packet stream reads are not supported")

		waiter = asyncio.get_running_loop().create_future()
		self._waiter = waiter
		try:
			return await waiter
		finally:
			if self._waiter is waiter:
				self._waiter = None

	async def put(self, item: Packet | None) -> None:
		self.deliver(item)

	def deliver(self, item: Packet | None) -> bool:
		if item is None:
			self.close()
			return True
		if self.closed:
			return False
		if self._waiter is None or self._waiter.done():
			return False

		self._waiter.set_result(item)
		self._waiter = None
		return True

	def close(self) -> None:
		self.closed = True
		if self._waiter is not None and not self._waiter.done():
			self._waiter.set_result(None)
		self._waiter = None


class PacketStreamTrack(RemoteStreamTrack):
	"""A MediaStreamTrack that exposes aiortc receiver encoded frames as packets."""

	def __init__(
		self,
		*,
		kind: str,
		id: str,
		receiver: PacketRtpReceiver,
		session_id: str,
		source_codec: RTCRtpCodecParameters,
		sync_clock: RtpSyncClock,
		logger: logging.Logger,
	) -> None:
		super().__init__(kind=kind, id=id)
		self.session_id = session_id
		self.source_codec = source_codec
		self.receiver = receiver
		self._sync_clock = sync_clock
		self._logger = logger
		self._queue = PacketRendezvous()
		self.packets = 0
		self.bytes = 0
		self.dropped_packets = 0
		self.dropped_unsynced_packets = 0
		self._first_packet_logged = False

	@property
	def ready(self) -> bool:
		return self.readyState == "live" and not self._queue.closed

	def push_encoded(self, codec: RTCRtpCodecParameters, encoded_frame: JitterFrame) -> bool:
		"""Push one encoded frame from aiortc's receiver jitter buffer."""
		if self.readyState != "live":
			return False

		timestamp = self._sync_clock.map_timestamp(
			kind=self.kind,
			codec=codec,
			rtp_timestamp=encoded_frame.timestamp,
		)
		if timestamp is None:
			self.dropped_unsynced_packets += 1
			return False

		packet = Packet(encoded_frame.data)
		packet.pts = timestamp
		packet.dts = timestamp
		packet.time_base = fractions.Fraction(1, codec.clockRate) if codec.clockRate else VIDEO_TIME_BASE
		if not self._queue.deliver(packet):
			self.dropped_packets += 1
			return False

		self.packets += 1
		self.bytes += len(encoded_frame.data)
		if not self._first_packet_logged:
			self._first_packet_logged = True
			self._logger.debug(
				"Packet stream first %s packet: session=%s codec=%s payload_type=%s clock=%s bytes=%s",
				self.kind,
				self.session_id,
				codec.mimeType,
				codec.payloadType,
				codec.clockRate,
				len(encoded_frame.data),
			)
		return True

	def end_from_source(self) -> None:
		"""End this track when the upstream receiver stops."""
		if self.readyState == "live":
			self._queue.close()

	@override
	async def recv(self) -> Packet:
		"""Receive the next encoded packet."""
		if self.readyState != "live":
			raise MediaStreamError

		packet = await self._queue.get()
		if packet is None:
			self.stop()
			raise MediaStreamError
		if not isinstance(packet, Packet):
			self._logger.error("Packet stream received decoded %s item", type(packet).__name__)
			self.stop()
			raise MediaStreamError
		return packet

	@override
	def stop(self) -> None:
		if self.readyState != "ended":
			self._queue.close()
			super().stop()


class PacketStreamQueue(queue.Queue[EncodedFrameItem]):
	"""Forward aiortc receiver encoded frames into a packet stream track."""

	def __init__(
		self,
		*,
		loop: asyncio.AbstractEventLoop,
		track: PacketStreamTrack,
	) -> None:
		super().__init__()
		self._loop = loop
		self._track = track

	@override
	def put(self, item: EncodedFrameItem, block: bool = True, timeout: float | None = None) -> None:
		if item is None:
			self._loop.call_soon_threadsafe(self._track.end_from_source)
			super().put(item, block=block, timeout=timeout)
			return

		codec, encoded_frame = item
		self._loop.call_soon_threadsafe(self._track.push_encoded, codec, encoded_frame)


class PacketRtpReceiver(RTCRtpReceiver):
	"""RTP receiver that can expose encoded media as packets."""

	def __init__(
		self,
		kind: str,
		transport: RTCDtlsTransport,
		*,
		session_id: str,
		sync_clock: RtpSyncClock,
		logger: logging.Logger,
	) -> None:
		self._packet_track: PacketStreamTrack | None = None
		super().__init__(kind, transport)
		self._session_id = session_id
		self._sync_clock = sync_clock
		self._logger = logger
		self.transceiver: RTCRtpTransceiver | None = None

	@property
	def _track(self) -> PacketStreamTrack | None:
		return self._packet_track

	@_track.setter
	def _track(self, track: RemoteStreamTrack | None) -> None:
		if track is None:
			self._packet_track = None
			return

		source_codec = self._source_codec(track.kind)
		packet_track = PacketStreamTrack(
			kind=track.kind,
			id=track.id,
			receiver=self,
			session_id=self._session_id,
			source_codec=source_codec,
			sync_clock=self._sync_clock,
			logger=self._logger,
		)
		self._install_packet_stream(packet_track)
		self._packet_track = packet_track
		self._logger.debug(
			"Created packet stream track kind=%s id=%s source_codec=%s",
			packet_track.kind,
			packet_track.id,
			{
				"mimeType": source_codec.mimeType,
				"payloadType": source_codec.payloadType,
				"clockRate": source_codec.clockRate,
				"parameters": source_codec.parameters,
			},
		)

	def _install_packet_stream(self, track: PacketStreamTrack) -> None:
		"""Install packet delivery before aiortc starts receiving RTP."""
		if getattr(self, "_RTCRtpReceiver__started"):
			raise RuntimeError("Cannot install packet stream after RTP receiver has started")
		setattr(self, "_RTCRtpReceiver__timestamp_mapper", RawRtpTimestampMapper())
		setattr(self, "_RTCRtpReceiver__decoder_queue", PacketStreamQueue(
			loop=asyncio.get_running_loop(),
			track=track,
		))

	def _source_codec(self, kind: str) -> RTCRtpCodecParameters:
		if self.transceiver is None:
			raise RuntimeError(f"Packet receiver has no transceiver for {kind} track")

		fallback_codec: RTCRtpCodecParameters | None = None
		for codec in self.transceiver._codecs:
			if is_rtx(codec):
				continue
			if fallback_codec is None:
				fallback_codec = codec
			if codec.mimeType.lower().startswith(f"{kind}/"):
				return codec
		if fallback_codec is not None:
			return fallback_codec
		raise RuntimeError(f"Source {kind} transceiver has no source RTP codec")

	@override
	async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
		if (
			isinstance(packet, RtcpSrPacket)
			and self._packet_track is not None
			and self._sync_clock is not None
		):
			self._sync_clock.update_sender_report(
				kind=self._packet_track.kind,
				clock_rate=self._packet_track.source_codec.clockRate or 0,
				packet=packet,
			)
		await super()._handle_rtcp_packet(packet)


class PacketPeerConnection(RTCPeerConnection):
	"""Peer connection that creates packet-capable RTP receivers."""

	def __init__(
		self,
		*,
		configuration: RTCConfiguration | None = None,
		session_id: str,
		logger: logging.Logger,
	) -> None:
		super().__init__(configuration=configuration)
		self._packet_session_id = session_id
		self._packet_logger = logger
		self._packet_sync_clock = RtpSyncClock(logger=logger.getChild("rtp_sync"))
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
		return self._constrain_answer_codecs(
			kind,
			supported_codecs,
			preserve_supported_order=True,
		)

	def constrain_answer_codecs_to_supported(
		self,
		kind: str,
		supported_codecs: list[RTCRtpCodecParameters],
	) -> list[RTCRtpCodecParameters]:
		"""Constrain this peer's answer codecs while preserving this peer's offer order."""
		return self._constrain_answer_codecs(
			kind,
			supported_codecs,
			preserve_supported_order=False,
		)

	def _constrain_answer_codecs(
		self,
		kind: str,
		supported_codecs: list[RTCRtpCodecParameters],
		*,
		preserve_supported_order: bool,
	) -> list[RTCRtpCodecParameters]:
		if not supported_codecs:
			return []

		for transceiver in self.getTransceivers():
			if transceiver.kind != kind or not transceiver._codecs:
				continue
			selected = self._compatible_codecs(
				transceiver._codecs,
				supported_codecs,
				preserve_supported_order=preserve_supported_order,
			)
			if not selected:
				raise RuntimeError(f"No {kind} codec is compatible between the two remote offers")
			transceiver._codecs = selected
			return selected
		return []

	def constrain_answer_codecs_from(
		self,
		other: PacketPeerConnection,
		*,
		kinds: tuple[str, ...] = ("audio", "video"),
	) -> dict[str, list[RTCRtpCodecParameters]]:
		"""Constrain this peer's answer codecs from another peer's remote offer."""
		selected_by_kind: dict[str, list[RTCRtpCodecParameters]] = {}
		for kind in kinds:
			supported_codecs = other.answer_codecs(kind) or other.remote_supported_codecs(kind)
			if selected := self.constrain_answer_codecs(kind, supported_codecs):
				selected_by_kind[kind] = selected
		return selected_by_kind

	def _compatible_codecs(
		self,
		candidate_codecs: list[RTCRtpCodecParameters],
		supported_codecs: list[RTCRtpCodecParameters],
		*,
		preserve_supported_order: bool,
	) -> list[RTCRtpCodecParameters]:
		selected: list[RTCRtpCodecParameters] = []
		selected_payload_types: set[int] = set()
		selected_base_codecs: list[tuple[RTCRtpCodecParameters, RTCRtpCodecParameters]] = []
		if preserve_supported_order:
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
		else:
			for candidate in candidate_codecs:
				if is_rtx(candidate) or candidate.payloadType in selected_payload_types:
					continue
				for supported in supported_codecs:
					if not is_rtx(supported) and is_codec_compatible(candidate, supported):
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

		receiver = PacketRtpReceiver(
			kind,
			dtls_transport,
			session_id=self._packet_session_id,
			sync_clock=self._packet_sync_clock,
			logger=self._packet_logger,
		)
		transceiver = RTCRtpTransceiver(
			direction=direction,
			kind=kind,
			sender=RTCRtpSender(sender_track or kind, dtls_transport),
			receiver=receiver,
		)
		receiver.transceiver = transceiver
		transceiver.receiver._set_rtcp_ssrc(transceiver.sender._ssrc)
		transceiver.sender._stream_id = self._RTCPeerConnection__stream_id
		transceiver._bundled = bundled
		transceivers.append(transceiver)
		return transceiver


def _rtp_timestamp_delta(timestamp: int, base: int) -> int:
	delta = (timestamp - base) % _RTP_TIMESTAMP_MODULO
	if delta >= _RTP_TIMESTAMP_HALF_MODULO:
		delta -= _RTP_TIMESTAMP_MODULO
	return delta
