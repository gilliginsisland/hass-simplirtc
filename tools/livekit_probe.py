"""Exercise the HA offer/answer path against a real SimpliSafe LiveKit camera.

No media is saved. Only timing, signaling state, and decoded frame counts are
printed. Credentials are read from files and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import time

from aiohttp import ClientSession
from aiortc import (
    RTCBundlePolicy, RTCConfiguration, RTCIceServer, RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.sdp import candidate_from_sdp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# A local macOS test can decode WebRTC without HA's optional snapshot library.
logging.getLogger("homeassistant.components.camera.img_util").setLevel(logging.CRITICAL)
from homeassistant.components.camera import WebRTCAnswer, WebRTCCandidate, WebRTCError
from custom_components.simplirtc.livekit import LiveKitSession
from simplipy.api import API


async def connection_details(args: argparse.Namespace) -> dict[str, str]:
    if args.details_file:
        data = json.loads(args.details_file.read_text())
        return data.get("liveKitDetails", data)
    async with ClientSession() as http:
        api = await API.async_from_refresh_token(
            args.refresh_token_file.read_text().strip(), session=http,
        )
        systems = await api.async_get_systems()
        for system in systems.values():
            for camera in system.cameras.values():
                if args.camera and camera.name.casefold() != args.camera.casefold():
                    continue
                if camera.camera_settings.get("admin", {}).get("webRTCProvider") != "mist":
                    continue
                data = await api.async_request(
                    "get", f"cameras/{camera.serial}/{system.system_id}/live-view",
                    url_base="https://app-hub.prd.aser.simplisafe.com/v2",
                    headers={"Host": "app-hub.prd.aser.simplisafe.com"},
                )
                return data["liveKitDetails"]
    raise RuntimeError("No matching LiveKit camera")


async def probe(args: argparse.Namespace) -> None:
    details = await connection_details(args)
    started = time.monotonic()
    ready = asyncio.get_running_loop().create_future()
    messages = asyncio.Queue()
    counts: dict[str, int] = {}
    consumers: list[asyncio.Task] = []
    answers = 0
    last_frame: dict[str, float] = {}

    def report(*values) -> None:
        print(f"{time.monotonic() - started:.2f}s", *values, flush=True)

    session = LiveKitSession(
        livekit_url=details["liveKitURL"], user_token=details["userToken"],
        send_ice_servers=lambda servers: ready.set_result(servers),
    )
    def closed() -> None:
        if not ready.done():
            ready.set_exception(RuntimeError("Session closed before ICE configuration"))
        messages.put_nowait(WebRTCError(code="closed", message="Session closed"))

    session.start(closed)
    peer = None
    try:
        async with asyncio.timeout(args.duration):
            servers = await ready
            peer = RTCPeerConnection(RTCConfiguration(
                iceServers=[RTCIceServer(
                    urls=list(server.urls), username=server.username or None,
                    credential=server.credential or None,
                ) for server in servers],
                bundlePolicy=RTCBundlePolicy.MAX_BUNDLE,
            ))
            for kind in ("video", "audio"):
                peer.addTransceiver(kind, direction="recvonly")

            @peer.on("track")
            def track_received(track):
                report("track", track.kind)
                async def consume():
                    while True:
                        await track.recv()
                        counts[track.kind] = counts.get(track.kind, 0) + 1
                        last_frame[track.kind] = time.monotonic()
                        if counts[track.kind] == 1:
                            report("first decoded frame", track.kind)
                consumers.append(asyncio.create_task(consume()))

            @peer.on("connectionstatechange")
            def state_changed():
                report("connection", peer.connectionState)

            await peer.setLocalDescription(await peer.createOffer())
            report("sending frontend offer")
            await session.handle_offer(peer.localDescription.sdp, messages.put_nowait)
            while True:
                message = await messages.get()
                if isinstance(message, WebRTCError):
                    raise RuntimeError(message.code)
                if isinstance(message, WebRTCAnswer):
                    answers += 1
                    report("answer", answers, "signaling state", peer.signalingState)
                    await peer.setRemoteDescription(RTCSessionDescription(
                        type="answer", sdp=message.answer,
                    ))
                elif isinstance(message, WebRTCCandidate) and message.candidate.candidate:
                    candidate = candidate_from_sdp(
                        message.candidate.candidate.removeprefix("candidate:"),
                    )
                    candidate.sdpMid = message.candidate.sdp_mid
                    candidate.sdpMLineIndex = message.candidate.sdp_m_line_index
                    await peer.addIceCandidate(candidate)
    except TimeoutError:
        report("test duration reached")
    finally:
        session.close()
        await session.wait_closed()
        for consumer in consumers:
            consumer.cancel()
        await asyncio.gather(*consumers, return_exceptions=True)
        if peer:
            await peer.close()
        report("answers", answers, "decoded frames", counts)
        ages = {kind: round(time.monotonic() - stamp, 2) for kind, stamp in last_frame.items()}
        report("seconds since last frame", ages)
    if (answers != 1 or not all(counts.get(kind, 0) for kind in ("audio", "video"))
            or any(age > 5 for age in ages.values())):
        raise RuntimeError("Expected one answer and continuing decoded audio and video")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    credentials = parser.add_mutually_exclusive_group(required=True)
    credentials.add_argument("--refresh-token-file", type=Path)
    credentials.add_argument("--details-file", type=Path)
    parser.add_argument("--camera", help="Camera name (default: first LiveKit camera)")
    parser.add_argument("--duration", type=float, default=35)
    args = parser.parse_args()
    # HTTP exception messages can include camera IDs; report only the error type.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(probe(args))
    except Exception as error:
        print(f"Probe failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
