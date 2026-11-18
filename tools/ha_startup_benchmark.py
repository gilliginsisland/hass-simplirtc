"""Paste into HA Debug console, then await measure_camera_start(camera, label).

The caller resolves the camera entity in their own HA session. This helper never
prints credentials, SDP, addresses, or media. Times include provider setup, ICE
gathering, signaling, and the first decoded audio/video frames.
"""

import asyncio
import time
import uuid
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp
from homeassistant.components.camera import WebRTCAnswer, WebRTCCandidate, WebRTCError
def peer_summary(peer):
    """Collect ICE state before close clears the candidates and nominated pairs."""
    ice = {t.receiver.transport.transport for t in peer.getTransceivers()}
    return {
        "side": getattr(peer, "_raw_side", "browser"),
        "connection": peer.connectionState,
        "ice": peer.iceConnectionState,
        "candidate_types": sorted({c.type for transport in ice for c in transport.iceGatherer.getLocalCandidates()}),
        "selected_pairs": sorted({
            (pair.local_candidate.type, pair.remote_candidate.type)
            for transport in ice for pair in transport._connection._nominated.values()
        }),
    }


async def measure_camera_start(camera, label, *, observed_peers=(), timeout=50):
    started = time.monotonic()
    elapsed = lambda: round(time.monotonic() - started, 3)
    result = {"label": label}
    pc = None
    session_id = "benchmark-" + uuid.uuid4().hex
    tasks = []
    queue = asyncio.Queue()
    frames = set()
    async def receive(track):
        await track.recv()
        result["first_" + track.kind] = elapsed()
        frames.add(track.kind)
    async def signaling():
        while True:
            message = await queue.get()
            if isinstance(message, WebRTCAnswer):
                await pc.setRemoteDescription(RTCSessionDescription(sdp=message.answer, type="answer"))
                result["answer"] = elapsed()
            elif isinstance(message, WebRTCCandidate):
                init = message.candidate
                if init.candidate:
                    candidate = candidate_from_sdp(init.candidate.removeprefix("candidate:"))
                    candidate.sdpMid = init.sdp_mid
                    candidate.sdpMLineIndex = init.sdp_m_line_index
                    await pc.addIceCandidate(candidate)
            elif isinstance(message, WebRTCError):
                raise RuntimeError(message.code)
    try:
        async with asyncio.timeout(timeout):
            config = await camera.async_prepare_webrtc_client_configuration()
            result["configuration"] = elapsed()
            servers = [RTCIceServer(urls=s.urls, username=s.username, credential=s.credential) for s in config.configuration.ice_servers]
            result["provider_turn"] = any(url.startswith(("turn:", "turns:")) for s in servers for url in ([s.urls] if isinstance(s.urls, str) else s.urls))
            pc = RTCPeerConnection(RTCConfiguration(iceServers=servers))
            @pc.on("track")
            def on_track(track):
                tasks.append(asyncio.create_task(receive(track)))
            for kind in ("audio", "video"):
                pc.addTransceiver(kind, direction="recvonly")
            await pc.setLocalDescription()
            result["offer"] = elapsed()
            tasks.append(asyncio.create_task(signaling()))
            await camera.async_handle_async_webrtc_offer(pc.localDescription.sdp, session_id, queue.put_nowait)
            while frames != {"audio", "video"}:
                for task in tasks:
                    if task.done() and not task.cancelled() and task.exception():
                        raise task.exception()
                await asyncio.sleep(.02)
            result["local_candidate_types"] = sorted({c.type for t in pc.getTransceivers() for c in t.receiver.transport.transport.iceGatherer.getLocalCandidates()})
            result["state"] = pc.connectionState
    except Exception as err:
        result["error"] = type(err).__name__
        result["elapsed"] = elapsed()
    finally:
        result["peers"] = [peer_summary(peer) for peer in [*observed_peers, pc] if peer is not None]
        session = camera._sessions.get(session_id)
        camera.close_webrtc_session(session_id)
        if session is not None:
            await session.wait_closed()
        if pc is not None:
            await pc.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        result["remaining_sessions"] = len(camera._provider_sessions)
    return result
