"""Minimal RTP sender support required by this integration."""

from __future__ import annotations

import uuid
from typing import Any, Optional, Union

from .codecs import get_capabilities
from .exceptions import InvalidStateError
from .mediastreams import MediaStreamTrack
from .rtcdtlstransport import RTCDtlsTransport
from .rtcrtpparameters import (
    RTCRtpCapabilities,
    RTCRtpSendParameters,
)
from .rtp import AnyRtcpPacket
from .stats import RTCStatsReport
from .utils import random32


class RTCRtpSender:
    """RTP sender metadata plus raw RTP output registration."""

    def __init__(
        self,
        trackOrKind: Union[MediaStreamTrack, str],
        transport: RTCDtlsTransport,
        *,
        raw_rtp_router: Any | None = None,
        raw_rtp_peer_id: str | None = None,
    ) -> None:
        if transport.state == "closed":
            raise InvalidStateError

        if isinstance(trackOrKind, MediaStreamTrack):
            self.__kind = trackOrKind.kind
            self.replaceTrack(trackOrKind)
        else:
            self.__kind = trackOrKind
            self.replaceTrack(None)

        self._ssrc = random32()
        self._rtx_ssrc = random32()
        self._stream_id = str(uuid.uuid4())
        self._enabled = True
        self.__raw_rtp_router = raw_rtp_router
        self.__raw_rtp_peer_id = raw_rtp_peer_id
        self.__started = False
        self.__transport = transport

    @property
    def kind(self) -> str:
        return self.__kind

    @property
    def track(self) -> MediaStreamTrack | None:
        """Return the media track attached to this sender, if any."""
        return self.__track

    @property
    def transport(self) -> RTCDtlsTransport:
        """Return the DTLS transport used by this sender."""
        return self.__transport

    @classmethod
    def getCapabilities(self, kind: str) -> RTCRtpCapabilities:
        """Return the configured RTP capabilities for this media kind."""
        return get_capabilities(kind)

    async def getStats(self) -> RTCStatsReport:
        """Return transport stats for API compatibility."""
        stats = RTCStatsReport()
        stats.update(self.transport._get_stats())
        return stats

    def replaceTrack(self, track: Optional[MediaStreamTrack]) -> None:
        self.__track = track
        self._track_id = track.id if track is not None else str(uuid.uuid4())

    def setTransport(self, transport: RTCDtlsTransport) -> None:
        self.__transport = transport

    async def send(self, parameters: RTCRtpSendParameters) -> None:
        """Register a negotiated raw RTP output.

        This vendored sender intentionally does not implement media encoding.
        The integration only creates sending transceivers for raw RTP forwarding.
        """
        if self.__started:
            return
        if self.__raw_rtp_router is None or self.__raw_rtp_peer_id is None:
            raise RuntimeError("Vendored RTCRtpSender only supports raw RTP output")
        self.__raw_rtp_router.register_output(
            peer_id=self.__raw_rtp_peer_id,
            sender=self,
            parameters=parameters,
        )
        self.__started = True

    async def stop(self) -> None:
        """Unregister this raw RTP output."""
        if not self.__started:
            return
        if self.__raw_rtp_router is not None and self.__raw_rtp_peer_id is not None:
            self.__raw_rtp_router.unregister_output(
                peer_id=self.__raw_rtp_peer_id,
                kind=self.kind,
            )
        self.__started = False

    async def _handle_rtcp_packet(self, packet: AnyRtcpPacket) -> None:
        return None
