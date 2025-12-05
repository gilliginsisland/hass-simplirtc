from homeassistant.components.camera import (
	Camera,
	WebRTCSendMessage,
	RTCIceCandidateInit,
)
from homeassistant.components.camera.webrtc import CameraWebRTCProvider

class Go2RTCSession:
	def __init__(
		self,
		*,
		session_id: str,
		send_message: WebRTCSendMessage,
		camera: Camera,
		provider: CameraWebRTCProvider
	) -> None:
		self._session_id = session_id
		self._send_message = send_message
		self._camera = camera
		self._provider = provider

	async def stream(self, offer_sdp: str) -> None:
		await self._provider.async_handle_async_webrtc_offer(
			self._camera, offer_sdp, self._session_id, self._send_message
		)

	async def send_candidate(self, candidate: RTCIceCandidateInit) -> None:
		await self._provider.async_on_webrtc_candidate(
			self._session_id, candidate
		)

	async def close(self) -> None:
		self._provider.async_close_session(self._session_id)
