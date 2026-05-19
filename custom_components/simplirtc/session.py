"""Shared WebRTC session contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

from webrtc_models import RTCIceCandidateInit


class Session(ABC):
	"""Common interface for active camera WebRTC sessions."""

	@abstractmethod
	async def stream(self, offer_sdp: str) -> None:
		"""Start streaming for a Home Assistant SDP offer."""

	@abstractmethod
	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		"""Send a Home Assistant ICE candidate to the upstream session."""

	@abstractmethod
	async def close(self) -> None:
		"""Close the session and release all resources."""
