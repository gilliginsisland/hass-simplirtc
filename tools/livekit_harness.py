"""Standalone LiveKit WebRTC harness for the packet bridge modules."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
from pathlib import Path
import sys
import types
from typing import Any
from uuid import uuid4

from aiohttp import ClientSession, web

LOGGER = logging.getLogger("livekit_harness")
REPO_ROOT = Path(__file__).resolve().parents[1]
SIMPLIRTC_PACKAGE_DIR = REPO_ROOT / "custom_components" / "simplirtc"
HARNESS_PACKAGE_NAME = "simplirtc_harness"
DEFAULT_STREAM_INFO_URL = "https://hass.hs.broochian.com/api/simplirtc_proxy/camera.basement_camera"


def simplirtc_module(module_name: str) -> Any:
	if HARNESS_PACKAGE_NAME not in sys.modules:
		package = types.ModuleType(HARNESS_PACKAGE_NAME)
		package.__path__ = [str(SIMPLIRTC_PACKAGE_DIR)]  # type: ignore[attr-defined]
		sys.modules[HARNESS_PACKAGE_NAME] = package
	return importlib.import_module(f"{HARNESS_PACKAGE_NAME}.{module_name}")


async def index(request: web.Request) -> web.Response:
	return web.Response(
		text=HTML.replace("__DEFAULT_STREAM_INFO_URL__", request.app["stream_info_url"]),
		content_type="text/html",
	)


async def create_session(request: web.Request) -> web.Response:
	body = await request.json()
	offer_sdp = str(body["offer"])
	if (livekit_url := body.get("url")) and (user_token := body.get("token")):
		request.app["livekit_offer"] = (str(livekit_url), str(user_token))
	elif not request.app["livekit_offer"]:
		request.app["livekit_offer"] = await fetch_stream_info(
			str(body.get("streamInfoUrl") or request.app["stream_info_url"])
		)

	session_id = uuid4().hex
	request.app["sessions"].add(session_id)

	try:
		answer_sdp = await asyncio.wait_for(
			request.app["sfu"].create_session(offer_sdp, peer_id=session_id),
			timeout=60,
		)
	except Exception:
		request.app["sessions"].discard(session_id)
		request.app["sfu"].close_session(session_id)
		raise

	return web.json_response({"sessionId": session_id, "answer": answer_sdp})


async def add_candidate(request: web.Request) -> web.Response:
	session_id = request.match_info["session_id"]
	if session_id not in request.app["sessions"]:
		return web.Response(status=404, text="Unknown session")

	body = await request.json()
	candidate = body.get("candidate")
	if candidate is None:
		await request.app["sfu"].add_candidate(session_id, "")
	else:
		await request.app["sfu"].add_candidate(
			session_id,
			candidate.get("candidate") or "",
			sdp_mid=candidate.get("sdpMid"),
			sdp_m_line_index=candidate.get("sdpMLineIndex"),
		)
	return web.json_response({"ok": True})


async def close_session(request: web.Request) -> web.Response:
	session_id = request.match_info["session_id"]
	request.app["sessions"].discard(session_id)
	request.app["sfu"].close_session(session_id)
	return web.json_response({"ok": True})


def task_state(task: asyncio.Task[Any] | None) -> dict[str, bool] | None:
	if not task:
		return None
	return {
		"done": task.done(),
		"cancelled": task.cancelled(),
	}


async def debug_state(request: web.Request) -> web.Response:
	sfu = request.app["sfu"]
	router = sfu._rtp_router
	return web.json_response({
		"sessions": sorted(request.app["sessions"]),
		"sfu": {
			"consumers": {
				peer_id: {
					"connectionState": consumer.connectionState,
					"hasRemoteDescription": bool(consumer.remoteDescription),
				}
				for peer_id, consumer in sfu._consumers.items()
			},
			"producer": None if not sfu._producer_pc else {
				"connectionState": sfu._producer_pc.connectionState,
			},
			"producerSetupTask": task_state(sfu._producer_setup_task),
			"producerCloseTask": task_state(sfu._producer_close_task),
			"idleTask": task_state(sfu._idle_task),
		},
		"router": {
			"tracks": {
				kind: {
					"peerId": track.peer_id,
					"primarySsrc": track.primary_ssrc,
					"rtxSsrc": track.rtx_ssrc,
					"subscriptions": sorted(track._subscriptions),
				}
				for kind, track in router._tracks_by_kind.items()
			},
			"outputs": {
				peer_id: sorted(outputs)
				for peer_id, outputs in router._outputs_by_peer.items()
			},
		},
	})


async def fetch_stream_info(stream_info_url: str) -> tuple[str, str]:
	async with ClientSession() as session:
		async with session.get(stream_info_url) as response:
			response.raise_for_status()
			data = await response.json()
	return str(data["url"]), str(data["token"])


async def on_shutdown(app: web.Application) -> None:
	app["sessions"].clear()
	app["sfu"].close()
	if close_task := app["sfu"]._producer_close_task:
		await close_task


def build_app(args: argparse.Namespace) -> web.Application:
	app = web.Application()
	app["stream_info_url"] = args.stream_info_url
	app["livekit_offer"] = None
	app["sessions"] = set()

	async def get_connection_info() -> tuple[str, str]:
		if app["livekit_offer"]:
			return app["livekit_offer"]
		app["livekit_offer"] = await fetch_stream_info(app["stream_info_url"])
		return app["livekit_offer"]

	livekit = simplirtc_module("livekit")
	livekit_producer = livekit.LiveKitProducer(get_connection_info=get_connection_info)
	app["sfu"] = simplirtc_module("sfu").RawRtpSfu(
		setup_producer_pc=livekit_producer.setup,
		idle_timeout=args.idle_timeout,
	)
	app.router.add_get("/", index)
	app.router.add_get("/api/debug/state", debug_state)
	app.router.add_post("/api/session", create_session)
	app.router.add_post("/api/session/{session_id}/candidate", add_candidate)
	app.router.add_delete("/api/session/{session_id}", close_session)
	app.router.add_post("/api/session/{session_id}/close", close_session)
	app.on_shutdown.append(on_shutdown)
	return app


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--host", default="127.0.0.1")
	parser.add_argument("--port", type=int, default=8765)
	parser.add_argument("--stream-info-url", default=DEFAULT_STREAM_INFO_URL)
	parser.add_argument("--log-level", default="DEBUG")
	parser.add_argument("--idle-timeout", type=float, default=30)
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	logging.basicConfig(level=args.log_level.upper())
	logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
	logging.getLogger("aioice").setLevel(logging.WARNING)
	logging.getLogger("aiortc").setLevel(logging.WARNING)
	logging.getLogger("livekit_harness").setLevel(args.log_level.upper())
	logging.getLogger(f"{HARNESS_PACKAGE_NAME}.livekit").setLevel(args.log_level.upper())
	logging.getLogger(f"{HARNESS_PACKAGE_NAME}.rtp_router").setLevel(args.log_level.upper())
	web.run_app(build_app(args), host=args.host, port=args.port)


HTML = """<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8">
	<meta name="viewport" content="width=device-width, initial-scale=1">
	<title>LiveKit Packet Harness</title>
	<style>
		body {
			margin: 0;
			font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
			background: #101418;
			color: #eef3f7;
		}
		main {
			display: grid;
			grid-template-columns: minmax(280px, 420px) minmax(320px, 1fr);
			gap: 20px;
			min-height: 100vh;
			padding: 20px;
			box-sizing: border-box;
		}
		label {
			display: block;
			margin: 0 0 14px;
			font-size: 13px;
			color: #b7c4cf;
		}
		input, textarea, select, button {
			box-sizing: border-box;
			width: 100%;
			margin-top: 6px;
			border: 1px solid #3c4854;
			border-radius: 6px;
			background: #161d23;
			color: #eef3f7;
			font: inherit;
		}
		input, textarea, select {
			padding: 9px 10px;
		}
		textarea {
			min-height: 150px;
			resize: vertical;
			font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
			font-size: 12px;
		}
		button {
			padding: 10px 12px;
			cursor: pointer;
			background: #2e7acb;
			border-color: #2e7acb;
			font-weight: 600;
		}
		button.secondary {
			background: #27313a;
			border-color: #3c4854;
		}
		.video-grid {
			display: grid;
			grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
			gap: 14px;
		}
		.video-card {
			display: grid;
			gap: 8px;
		}
		.video-card h2 {
			margin: 0;
			font-size: 14px;
			font-weight: 600;
			color: #d4dde5;
		}
		.video-card video {
			width: 100%;
			max-height: calc(50vh - 40px);
			background: #000;
			border-radius: 6px;
		}
		pre {
			overflow: auto;
			min-height: 180px;
			max-height: 260px;
			padding: 12px;
			background: #0b0f13;
			border: 1px solid #26313a;
			border-radius: 6px;
			white-space: pre-wrap;
		}
		.actions {
			display: grid;
			grid-template-columns: 1fr 1fr;
			gap: 10px;
		}
		@media (max-width: 850px) {
			main {
				grid-template-columns: 1fr;
			}
		}
	</style>
</head>
<body>
	<main>
		<section>
			<label>Stream info URL
				<input id="streamInfoUrl" value="__DEFAULT_STREAM_INFO_URL__">
			</label>
			<label>LiveKit URL
				<input id="livekitUrl" placeholder="Optional when stream info URL is reachable">
			</label>
			<label>LiveKit token
				<textarea id="token" placeholder="Optional when stream info URL is reachable"></textarea>
			</label>
			<label>Video codec preference
				<select id="videoCodec">
					<option value="auto">Auto</option>
					<option value="h264">H264</option>
					<option value="vp8">VP8</option>
				</select>
			</label>
			<label>Consumer sessions
				<input id="consumerCount" type="number" min="2" value="2">
			</label>
			<div class="actions">
				<button id="connect">Connect</button>
				<button id="close" class="secondary">Close</button>
			</div>
			<pre id="log"></pre>
		</section>
		<section>
			<div id="videos" class="video-grid"></div>
		</section>
	</main>
	<script>
		const logEl = document.getElementById("log");
		const videosEl = document.getElementById("videos");
		let peers = [];

		function log(message) {
			logEl.textContent += `${new Date().toISOString()} ${message}\\n`;
			logEl.scrollTop = logEl.scrollHeight;
		}

		function makeVideo(index) {
			const card = document.createElement("div");
			card.className = "video-card";
			const title = document.createElement("h2");
			title.textContent = `Consumer ${index}`;
			const video = document.createElement("video");
			video.autoplay = true;
			video.muted = true;
			video.playsInline = true;
			video.controls = true;
			card.append(title, video);
			videosEl.append(card);
			return video;
		}

		async function postCandidate(peer, candidate) {
			if (!peer.sessionId) {
				peer.pendingCandidates.push(candidate);
				return;
			}
			await fetch(`/api/session/${peer.sessionId}/candidate`, {
				method: "POST",
				headers: {"content-type": "application/json"},
				body: JSON.stringify({candidate}),
			});
		}

		async function connect() {
			await closeSessions();
			const count = Math.max(2, Number(document.getElementById("consumerCount").value) || 2);
			await Promise.all(Array.from({length: count}, (_, index) => connectPeer(index + 1)));
			window.__simplirtcPCs = peers.map((peer) => peer.pc);
		}

		async function connectPeer(index) {
			const pc = new RTCPeerConnection();
			const peer = {pc, sessionId: null, pendingCandidates: []};
			peers.push(peer);
			const stream = new MediaStream();
			const video = makeVideo(index);
			video.muted = true;
			video.srcObject = stream;

			const videoTransceiver = pc.addTransceiver("video", {direction: "recvonly"});
			const videoCodec = document.getElementById("videoCodec").value;
			if (videoCodec !== "auto") {
				const mimeType = `video/${videoCodec}`;
				const codecs = RTCRtpReceiver.getCapabilities("video").codecs
					.filter((codec) => codec.mimeType.toLowerCase() === mimeType);
				if (!codecs.length) {
					throw new Error(`Browser does not support ${mimeType}`);
				}
				videoTransceiver.setCodecPreferences(codecs);
				log(`preferred ${mimeType}`);
			}
			pc.addTransceiver("audio", {direction: "recvonly"});
			pc.ontrack = (event) => {
				log(`consumer ${index} track ${event.track.kind} ${event.track.id}`);
				stream.addTrack(event.track);
			};
			pc.onicecandidate = (event) => postCandidate(peer, event.candidate);
			pc.onconnectionstatechange = () => log(`consumer ${index} connection ${pc.connectionState}`);
			pc.oniceconnectionstatechange = () => log(`consumer ${index} ice ${pc.iceConnectionState}`);

			const offer = await pc.createOffer();
			await pc.setLocalDescription(offer);
			log(`consumer ${index} created browser offer`);

			const response = await fetch("/api/session", {
				method: "POST",
				headers: {"content-type": "application/json"},
				body: JSON.stringify({
					offer: pc.localDescription.sdp,
					streamInfoUrl: document.getElementById("streamInfoUrl").value,
					url: document.getElementById("livekitUrl").value,
					token: document.getElementById("token").value,
				}),
			});
			if (!response.ok) {
				throw new Error(await response.text());
			}
			const data = await response.json();
			peer.sessionId = data.sessionId;
			await pc.setRemoteDescription({type: "answer", sdp: data.answer});
			log(`consumer ${index} set answer for ${peer.sessionId}`);
			await video.play().catch((error) => log(`play blocked ${error.message}`));

			for (const candidate of peer.pendingCandidates.splice(0)) {
				await postCandidate(peer, candidate);
			}
		}

		async function closeSessions() {
			const closingPeers = peers;
			peers = [];
			window.__simplirtcPCs = [];
			for (const peer of closingPeers) {
				if (peer.sessionId) {
					await fetch(`/api/session/${peer.sessionId}`, {method: "DELETE"}).catch(() => {});
				}
				if (peer.pc) {
					for (const sender of peer.pc.getSenders()) {
						sender.track?.stop();
					}
					for (const receiver of peer.pc.getReceivers()) {
						receiver.track?.stop();
					}
					peer.pc.close();
				}
			}
			videosEl.replaceChildren();
		}

		async function closePeer(index) {
			const peer = peers[index];
			if (!peer) {
				return;
			}
			peers.splice(index, 1);
			if (peer.sessionId) {
				await fetch(`/api/session/${peer.sessionId}`, {method: "DELETE"}).catch(() => {});
			}
			if (peer.pc) {
				for (const sender of peer.pc.getSenders()) {
					sender.track?.stop();
				}
				for (const receiver of peer.pc.getReceivers()) {
					receiver.track?.stop();
				}
				peer.pc.close();
			}
			videosEl.children[index]?.remove();
			window.__simplirtcPCs = peers.map((item) => item.pc);
		}

		window.__simplirtcHarness = {
			closePeer,
			closeSessions,
			sessions: () => peers.map((peer) => peer.sessionId),
		};

		document.getElementById("connect").addEventListener("click", () => {
			connect().catch((error) => log(`error ${error.message}`));
		});
		document.getElementById("close").addEventListener("click", () => {
			closeSessions().catch((error) => log(`close error ${error.message}`));
		});
		window.addEventListener("beforeunload", () => {
			for (const peer of peers) {
				if (peer.sessionId) {
					navigator.sendBeacon(`/api/session/${peer.sessionId}/close`, "");
				}
			}
		});
	</script>
</body>
</html>
"""


if __name__ == "__main__":
	main()
