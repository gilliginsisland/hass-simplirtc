"""WebRTC snapshot support for Home Assistant cameras."""

from __future__ import annotations

import asyncio
from io import BytesIO
from typing import Any, cast
from uuid import uuid4

from aiortc import (
	RTCIceCandidate as AiortcIceCandidate,
	RTCPeerConnection,
	RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.sdp import candidate_from_sdp
from av.video.frame import VideoFrame
from PIL.Image import Image
from webrtc_models import RTCIceCandidate, RTCIceCandidateInit

DEFAULT_SNAPSHOT_TIMEOUT = 15


def _candidate_from_init(
	candidate: RTCIceCandidate | RTCIceCandidateInit,
) -> AiortcIceCandidate | None:
	if not candidate.candidate:
		return None
	aiortc_candidate = candidate_from_sdp(candidate.candidate.removeprefix("candidate:"))
	if isinstance(candidate, RTCIceCandidateInit):
		aiortc_candidate.sdpMid = candidate.sdp_mid
		aiortc_candidate.sdpMLineIndex = candidate.sdp_m_line_index
	return aiortc_candidate


def _frame_to_jpeg(frame: VideoFrame) -> bytes:
	image = cast(Image, frame.to_image())
	if image.mode != "RGB":
		image = image.convert("RGB")
	output = BytesIO()
	image.save(output, format="JPEG")
	return output.getvalue()


class Snapshotter:
	"""One-shot WebRTC snapshot capture."""

	def __init__(self) -> None:
		self._pc = RTCPeerConnection()
		self.session_id = f"snapshot-{uuid4().hex}"
		self._frame_future: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
		self._remote_candidates: list[RTCIceCandidate | RTCIceCandidateInit] = []
		self._remote_description_ready = asyncio.Event()
		self._closed = False
		self._pc.on("track", self._on_track)

	async def make_offer(self) -> str:
		"""Create a video-only WebRTC offer for snapshot capture."""
		self._pc.addTransceiver("video", direction="recvonly")
		await self._pc.setLocalDescription()
		if self._pc.localDescription is None:
			raise RuntimeError("Snapshot peer connection did not create an SDP offer")
		return self._pc.localDescription.sdp

	async def wait_for_image(self) -> bytes:
		"""Wait for the first decoded video frame as JPEG bytes."""
		return await self._frame_future

	async def close(self) -> None:
		"""Close the temporary snapshot peer connection."""
		self._closed = True
		await self._pc.close()

	def _fail_frame_on_task_error(self, task: asyncio.Task[Any]) -> None:
		if task.cancelled() or self._frame_future.done():
			return
		try:
			task.result()
		except Exception as err:
			self._frame_future.set_exception(err)

	async def _add_remote_candidate(self, candidate: RTCIceCandidate | RTCIceCandidateInit) -> None:
		if self._closed:
			return
		if not self._remote_description_ready.is_set():
			self._remote_candidates.append(candidate)
			return
		await self._pc.addIceCandidate(_candidate_from_init(candidate))

	async def _set_remote_answer(self, answer_sdp: str) -> None:
		if self._closed:
			return
		await self._pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
		self._remote_description_ready.set()
		for candidate in self._remote_candidates:
			await self._pc.addIceCandidate(_candidate_from_init(candidate))
		self._remote_candidates.clear()

	def send_answer(self, answer_sdp: str) -> None:
		"""Apply a WebRTC answer from the upstream camera session."""
		task = asyncio.create_task(self._set_remote_answer(answer_sdp))
		task.add_done_callback(self._fail_frame_on_task_error)

	def send_candidate(self, candidate: RTCIceCandidate | RTCIceCandidateInit) -> None:
		"""Apply a WebRTC candidate from the upstream camera session."""
		task = asyncio.create_task(self._add_remote_candidate(candidate))
		task.add_done_callback(self._fail_frame_on_task_error)

	async def _read_video_frame(self, track: MediaStreamTrack) -> None:
		try:
			while not self._frame_future.done():
				frame = await track.recv()
				if isinstance(frame, VideoFrame):
					image = await asyncio.to_thread(_frame_to_jpeg, frame)
					if not self._frame_future.done():
						self._frame_future.set_result(image)
					return
		except MediaStreamError as err:
			if not self._frame_future.done():
				self._frame_future.set_exception(err)
		except Exception as err:
			if not self._frame_future.done():
				self._frame_future.set_exception(err)

	def _on_track(self, track: MediaStreamTrack) -> None:
		if track.kind == "video":
			asyncio.create_task(self._read_video_frame(track))
