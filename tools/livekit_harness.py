"""Standalone LiveKit WebRTC harness for the Home Assistant signaling path."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import importlib
import json
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


@dataclass(slots=True)
class HarnessSession:
	livekit_session: Any
	event_queue: asyncio.Queue[dict[str, Any]]


def simplirtc_module(module_name: str) -> Any:
	"""Import a SimpliRTC module without importing the HA integration package."""
	if HARNESS_PACKAGE_NAME not in sys.modules:
		package = types.ModuleType(HARNESS_PACKAGE_NAME)
		package.__path__ = [str(SIMPLIRTC_PACKAGE_DIR)]  # type: ignore[attr-defined]
		sys.modules[HARNESS_PACKAGE_NAME] = package
	return importlib.import_module(f"{HARNESS_PACKAGE_NAME}.{module_name}")


def livekit_module() -> Any:
	return simplirtc_module("livekit")


def livekit_session_class() -> type[Any]:
	return livekit_module().LiveKitSession


async def fetch_stream_info(stream_info_url: str) -> tuple[str, str]:
	async with ClientSession() as session, session.get(stream_info_url) as response:
		body = await response.text()
		if not response.ok:
			raise web.HTTPBadGateway(
				text=f"Stream info request failed: {response.status} {body}"
			)
		try:
			data = json.loads(body)
		except ValueError as err:
			raise web.HTTPBadGateway(text=f"Stream info response was not JSON: {body}") from err

	if isinstance(data, dict):
		if isinstance(data.get("url"), str) and isinstance(data.get("token"), str):
			return data["url"], data["token"]
		if isinstance(details := data.get("liveKitDetails"), dict):
			livekit_url = details.get("liveKitURL")
			user_token = details.get("userToken")
			if isinstance(livekit_url, str) and isinstance(user_token, str):
				return livekit_url, user_token

	raise web.HTTPBadGateway(text=f"Stream info response did not include LiveKit credentials: {data!r}")


async def index(request: web.Request) -> web.Response:
	return web.Response(
		text=HTML.replace("__DEFAULT_STREAM_INFO_URL__", request.app["stream_info_url"]),
		content_type="text/html",
	)


async def create_session(request: web.Request) -> web.Response:
	body = await request.json()
	offer_sdp = body.get("offer")
	if not isinstance(offer_sdp, str) or not offer_sdp:
		raise web.HTTPBadRequest(text="Missing browser offer")

	if (livekit_url := body.get("url")) and (user_token := body.get("token")):
		connection_info = (str(livekit_url), str(user_token))
	else:
		connection_info = await fetch_stream_info(
			str(body.get("streamInfoUrl") or request.app["stream_info_url"])
		)
	livekit_url, user_token = connection_info

	session_id = uuid4().hex
	event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

	def send_answer(answer_sdp: str) -> None:
		event_queue.put_nowait({"type": "answer", "answer": answer_sdp})

	def send_candidate(
		candidate: str,
		sdp_mid: str | None,
		sdp_m_line_index: int | None,
	) -> None:
		event_queue.put_nowait({
			"type": "candidate",
			"candidate": {
				"candidate": candidate,
				"sdpMid": sdp_mid,
				"sdpMLineIndex": sdp_m_line_index,
			} if candidate else None,
		})

	def on_close() -> None:
		if request.app["sessions"].get(session_id) is harness_session:
			request.app["sessions"].pop(session_id, None)

	livekit_session = livekit_session_class()(
		session_id=session_id,
		livekit_url=livekit_url,
		user_token=user_token,
		offer_sdp=offer_sdp,
		send_answer=send_answer,
		send_candidate=send_candidate,
		on_close=on_close,
	)
	harness_session = HarnessSession(
		livekit_session=livekit_session,
		event_queue=event_queue,
	)
	request.app["sessions"][session_id] = harness_session

	try:
		await livekit_session.start()
	except BaseException:
		if request.app["sessions"].get(session_id) is harness_session:
			request.app["sessions"].pop(session_id, None)
		livekit_session.close()
		raise

	return web.json_response({"sessionId": session_id})


async def ice_configuration(request: web.Request) -> web.Response:
	body = await request.json()
	if (livekit_url := body.get("url")) and (user_token := body.get("token")):
		connection_info = (str(livekit_url), str(user_token))
	else:
		connection_info = await fetch_stream_info(
			str(body.get("streamInfoUrl") or request.app["stream_info_url"])
		)
	livekit_url, user_token = connection_info
	ice_servers = await livekit_module().fetch_ice_servers(livekit_url, user_token)
	LOGGER.info("Fetched %s LiveKit ICE servers", len(ice_servers))
	return web.json_response({
		"iceServers": ice_servers,
		"url": livekit_url,
		"token": user_token,
	})


async def add_candidate(request: web.Request) -> web.Response:
	if not (session := request.app["sessions"].get(request.match_info["session_id"])):
		return web.Response(status=404, text="Unknown session")

	body = await request.json()
	candidate = body.get("candidate")
	if candidate is None:
		await session.livekit_session.send_candidate(
			"",
			sdp_mid=None,
			sdp_m_line_index=None,
		)
	elif isinstance(candidate, dict):
		await session.livekit_session.send_candidate(
			str(candidate.get("candidate") or ""),
			sdp_mid=candidate.get("sdpMid") if isinstance(candidate.get("sdpMid"), str) else None,
			sdp_m_line_index=(
				candidate.get("sdpMLineIndex")
				if isinstance(candidate.get("sdpMLineIndex"), int)
				else None
			),
		)
	else:
		raise web.HTTPBadRequest(text="Candidate must be an object or null")

	return web.json_response({"ok": True})


async def session_events(request: web.Request) -> web.Response:
	if not (session := request.app["sessions"].get(request.match_info["session_id"])):
		return web.Response(status=404, text="Unknown session")

	events: list[dict[str, Any]] = []
	try:
		events.append(await asyncio.wait_for(session.event_queue.get(), timeout=15))
	except TimeoutError:
		return web.json_response({"events": events})

	while len(events) < 20:
		try:
			events.append(session.event_queue.get_nowait())
		except asyncio.QueueEmpty:
			break
	return web.json_response({"events": events})


async def close_session(request: web.Request) -> web.Response:
	if session := request.app["sessions"].pop(request.match_info["session_id"], None):
		session.livekit_session.close()
	return web.json_response({"ok": True})


async def debug_state(request: web.Request) -> web.Response:
	return web.json_response({
		"sessions": [
			{
				"sessionId": session_id,
				"queuedEvents": session.event_queue.qsize(),
			}
			for session_id, session in request.app["sessions"].items()
		],
	})


async def on_shutdown(app: web.Application) -> None:
	sessions = list(app["sessions"].values())
	app["sessions"].clear()
	for session in sessions:
		session.livekit_session.close()


def build_app(stream_info_url: str) -> web.Application:
	app = web.Application()
	app["stream_info_url"] = stream_info_url
	app["sessions"] = {}
	app.router.add_get("/", index)
	app.router.add_post("/api/ice-configuration", ice_configuration)
	app.router.add_post("/api/session", create_session)
	app.router.add_post("/api/session/{session_id}/candidate", add_candidate)
	app.router.add_get("/api/session/{session_id}/events", session_events)
	app.router.add_delete("/api/session/{session_id}", close_session)
	app.router.add_post("/api/session/{session_id}/close", close_session)
	app.router.add_get("/api/debug/state", debug_state)
	app.on_shutdown.append(on_shutdown)
	return app


HTML = r"""<!doctype html>
<html lang="en">
<head>
	<meta charset="utf-8">
	<meta name="viewport" content="width=device-width, initial-scale=1">
	<title>SimpliRTC LiveKit Harness</title>
	<style>
		body {
			background: #f5f6f8;
			color: #172033;
			font: 14px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
			margin: 0;
		}
		main {
			margin: 0 auto;
			max-width: 1180px;
			padding: 24px;
		}
		h1 {
			font-size: 20px;
			font-weight: 650;
			margin: 0 0 18px;
		}
		.controls {
			display: grid;
			gap: 12px;
			grid-template-columns: repeat(2, minmax(0, 1fr));
			margin-bottom: 16px;
		}
		label {
			display: grid;
			gap: 5px;
			font-weight: 600;
		}
		input, select, button {
			border: 1px solid #c6cad3;
			border-radius: 6px;
			box-sizing: border-box;
			font: inherit;
			min-height: 36px;
			padding: 7px 9px;
		}
		button {
			background: #1f6feb;
			border-color: #1f6feb;
			color: white;
			cursor: pointer;
			font-weight: 650;
		}
		button.secondary {
			background: white;
			color: #172033;
		}
		.actions {
			display: flex;
			gap: 8px;
			margin-bottom: 18px;
		}
		#videos {
			display: grid;
			gap: 12px;
			grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
		}
		video {
			aspect-ratio: 16 / 9;
			background: #05070b;
			border-radius: 6px;
			width: 100%;
		}
		pre {
			background: #101827;
			border-radius: 6px;
			color: #dce6f5;
			min-height: 160px;
			overflow: auto;
			padding: 12px;
			white-space: pre-wrap;
		}
		@media (max-width: 760px) {
			main {
				padding: 16px;
			}
			.controls {
				grid-template-columns: 1fr;
			}
		}
	</style>
</head>
<body>
	<main>
		<h1>SimpliRTC LiveKit Harness</h1>
		<section class="controls">
			<label>Stream info URL
				<input id="streamInfoUrl" value="__DEFAULT_STREAM_INFO_URL__">
			</label>
			<label>Video codec
				<select id="videoCodec">
					<option value="auto">Auto</option>
					<option value="h264">Prefer H.264</option>
					<option value="vp8">Prefer VP8</option>
				</select>
			</label>
			<label>LiveKit URL
				<input id="livekitUrl" placeholder="wss://...">
			</label>
			<label>LiveKit token
				<input id="token" placeholder="eyJ...">
			</label>
			<label>Viewer sessions
				<input id="consumerCount" type="number" min="1" max="6" value="1">
			</label>
		</section>
		<section class="actions">
			<button id="connect">Connect</button>
			<button id="close" class="secondary">Close</button>
		</section>
		<section id="videos"></section>
		<pre id="log"></pre>
	</main>
	<script>
		const sessions = [];
		const logEl = document.getElementById("log");
		const videosEl = document.getElementById("videos");

		function log(message) {
			const line = `[${new Date().toISOString()}] ${message}`;
			logEl.textContent += `${line}\n`;
			logEl.scrollTop = logEl.scrollHeight;
			console.log(line);
		}

		function preferCodec(sdp, codec) {
			if (codec === "auto") {
				return sdp;
			}
			const lines = sdp.split("\r\n");
			const mLineIndex = lines.findIndex((line) => line.startsWith("m=video "));
			if (mLineIndex === -1) {
				return sdp;
			}
			const preferredPayloads = new Set();
			for (const line of lines) {
				const match = line.match(/^a=rtpmap:(\d+) ([^/]+)/i);
				if (match && match[2].toLowerCase() === codec) {
					preferredPayloads.add(match[1]);
				}
			}
			if (!preferredPayloads.size) {
				return sdp;
			}
			const parts = lines[mLineIndex].split(" ");
			const header = parts.slice(0, 3);
			const payloads = parts.slice(3);
			const preferred = payloads.filter((payload) => preferredPayloads.has(payload));
			const rest = payloads.filter((payload) => !preferredPayloads.has(payload));
			lines[mLineIndex] = [...header, ...preferred, ...rest].join(" ");
			return lines.join("\r\n");
		}

		async function postJson(url, body) {
			const response = await fetch(url, {
				method: "POST",
				headers: {"content-type": "application/json"},
				body: JSON.stringify(body),
			});
			const text = await response.text();
			if (!response.ok) {
				throw new Error(`${url} failed: ${response.status} ${text}`);
			}
			return text ? JSON.parse(text) : {};
		}

		async function loadIceConfiguration() {
			const data = await postJson("/api/ice-configuration", {
				streamInfoUrl: document.getElementById("streamInfoUrl").value,
				url: document.getElementById("livekitUrl").value,
				token: document.getElementById("token").value,
			});
			if (data.url) {
				document.getElementById("livekitUrl").value = data.url;
			}
			if (data.token) {
				document.getElementById("token").value = data.token;
			}
			const iceServers = Array.isArray(data.iceServers) ? data.iceServers : [];
			const schemes = new Set(
				iceServers
					.flatMap((server) => Array.isArray(server.urls) ? server.urls : [server.urls])
					.filter(Boolean)
					.map((url) => String(url).split(":")[0])
			);
			log(`ice servers ${iceServers.length}: ${Array.from(schemes).join(", ") || "none"}`);
			return {iceServers};
		}

		async function pollEvents(session) {
			while (!session.closed) {
				const response = await fetch(`/api/session/${session.sessionId}/events`);
				if (!response.ok) {
					if (!session.closed) {
						log(`consumer ${session.index} events failed: ${response.status}`);
					}
					return;
				}
				const data = await response.json();
				for (const event of data.events || []) {
					if (event.type === "answer") {
						await session.pc.setRemoteDescription({type: "answer", sdp: event.answer});
						for (const candidate of session.remoteCandidates.splice(0)) {
							await session.pc.addIceCandidate(candidate);
						}
					} else if (event.type === "candidate") {
						if (session.pc.remoteDescription) {
							await session.pc.addIceCandidate(event.candidate);
						} else {
							session.remoteCandidates.push(event.candidate);
						}
					}
				}
			}
		}

		async function sendCandidate(session, candidate) {
			const body = {
				candidate: candidate ? candidate.toJSON() : null,
				localDescription: session.pc.localDescription,
			};
			if (!session.sessionId) {
				session.pendingCandidates.push(body);
				return;
			}
			await postJson(`/api/session/${session.sessionId}/candidate`, body);
		}

		async function closePeer(index) {
			const session = sessions[index];
			if (!session || session.closed) {
				return;
			}
			session.closed = true;
			if (session.sessionId) {
				await fetch(`/api/session/${session.sessionId}`, {method: "DELETE"}).catch(() => undefined);
			}
			session.pc.close();
			log(`consumer ${session.index} closed`);
		}

		async function closeSessions() {
			await Promise.all(sessions.map((_, index) => closePeer(index)));
			sessions.length = 0;
			videosEl.replaceChildren();
		}

		async function connectOne(index, iceConfiguration) {
			const pc = new RTCPeerConnection(iceConfiguration);
			const video = document.createElement("video");
			video.autoplay = true;
			video.muted = true;
			video.playsInline = true;
			const remoteStream = new MediaStream();
			video.srcObject = remoteStream;
			videosEl.append(video);

			const session = {
				index,
				pc,
				video,
				remoteStream,
				sessionId: null,
				pendingCandidates: [],
				remoteCandidates: [],
				closed: false,
			};
			sessions.push(session);

			pc.addTransceiver("video", {direction: "recvonly"});
			pc.addTransceiver("audio", {direction: "recvonly"});
			pc.ontrack = (event) => {
				const tracks = event.streams[0]?.getTracks() || [event.track];
				for (const track of tracks) {
					if (!remoteStream.getTrackById(track.id)) {
						remoteStream.addTrack(track);
					}
				}
			};
			pc.onicecandidate = (event) => {
				sendCandidate(session, event.candidate).catch((error) => {
					if (!session.closed) {
						log(`consumer ${index} candidate failed: ${error.message}`);
					}
				});
			};
			pc.onconnectionstatechange = () => log(`consumer ${index} connection ${pc.connectionState}`);
			pc.oniceconnectionstatechange = () => log(`consumer ${index} ice ${pc.iceConnectionState}`);

			const offer = await pc.createOffer();
			offer.sdp = preferCodec(offer.sdp, document.getElementById("videoCodec").value);
			await pc.setLocalDescription(offer);

			const data = await postJson("/api/session", {
				offer: pc.localDescription.sdp,
				streamInfoUrl: document.getElementById("streamInfoUrl").value,
				url: document.getElementById("livekitUrl").value,
				token: document.getElementById("token").value,
			});
			session.sessionId = data.sessionId;
			pollEvents(session).catch((error) => {
				if (!session.closed) {
					log(`consumer ${index} events failed: ${error.message}`);
				}
			});
			for (const candidate of session.pendingCandidates.splice(0)) {
				await postJson(`/api/session/${session.sessionId}/candidate`, candidate);
			}
			log(`consumer ${index} session ${session.sessionId}`);
		}

		document.getElementById("connect").addEventListener("click", async () => {
			try {
				await closeSessions();
				const count = Math.max(1, Number(document.getElementById("consumerCount").value) || 1);
				const iceConfiguration = await loadIceConfiguration();
				for (let index = 1; index <= count; index += 1) {
					await connectOne(index, iceConfiguration);
				}
			} catch (error) {
				log(error.stack || error.message);
			}
		});
		document.getElementById("close").addEventListener("click", () => {
			closeSessions().catch((error) => log(error.message));
		});
		window.__simplirtcHarness = {sessions, closePeer, closeSessions};
		window.closeSessions = closeSessions;
	</script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser()
	parser.add_argument("--host", default="127.0.0.1")
	parser.add_argument("--port", type=int, default=8765)
	parser.add_argument("--stream-info-url", default=DEFAULT_STREAM_INFO_URL)
	parser.add_argument("--log-level", default="INFO")
	return parser.parse_args()


def main() -> None:
	args = parse_args()
	logging.basicConfig(level=args.log_level.upper())
	logging.getLogger(f"{HARNESS_PACKAGE_NAME}.livekit").setLevel(args.log_level.upper())
	web.run_app(build_app(args.stream_info_url), host=args.host, port=args.port)


if __name__ == "__main__":
	main()
