#!/usr/bin/env node

const attachOnly = process.argv.includes("--attach");
const lifecycleTest = process.argv.includes("--lifecycle-test");
const streamInfoIndex = process.argv.indexOf("--stream-info-url");
const streamInfoUrl = streamInfoIndex === -1 ? null : process.argv[streamInfoIndex + 1];
const videoCodecIndex = process.argv.indexOf("--video-codec");
const videoCodec = videoCodecIndex === -1 ? "auto" : process.argv[videoCodecIndex + 1];
if (!["auto", "h264", "vp8"].includes(videoCodec)) {
	throw new Error("--video-codec must be one of: auto, h264, vp8");
}
const consumerCountIndex = process.argv.indexOf("--consumer-count");
const consumerCount = consumerCountIndex === -1 ? 2 : Number(process.argv[consumerCountIndex + 1]);
if (!Number.isInteger(consumerCount) || consumerCount < 1) {
	throw new Error("--consumer-count must be a positive integer");
}
const positionalArgs = process.argv.slice(2).filter((arg, index, args) => (
	arg !== "--attach"
	&& arg !== "--lifecycle-test"
	&& arg !== "--stream-info-url"
	&& arg !== "--video-codec"
	&& arg !== "--consumer-count"
	&& args[index - 1] !== "--stream-info-url"
	&& args[index - 1] !== "--video-codec"
	&& args[index - 1] !== "--consumer-count"
));
const cdpBase = positionalArgs[0] ?? "http://127.0.0.1:9222";
const harnessUrl = positionalArgs[1] ?? "http://127.0.0.1:8765/";
const debugStateUrl = new URL("/api/debug/state", harnessUrl).toString();

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function cdp(path, init) {
	const response = await fetch(`${cdpBase}${path}`, init);
	if (!response.ok) {
		throw new Error(`${path} failed: ${response.status} ${await response.text()}`);
	}
	return response.json();
}

async function pageTargets() {
	return (await cdp("/json/list")).filter((item) => item.type === "page");
}

async function closeTarget(target) {
	await cdp(`/json/close/${target.id}`).catch(() => undefined);
}

async function reusableTarget(url) {
	const targets = (await pageTargets()).filter((item) => item.url.startsWith(url));
	const [target, ...duplicates] = targets;
	await Promise.all(duplicates.map(closeTarget));
	if (target) {
		return target;
	}
	return cdp(`/json/new?${encodeURIComponent(url)}`, {method: "PUT"});
}

class DevTools {
	constructor(url) {
		this.url = url;
		this.nextId = 1;
		this.pending = new Map();
		this.events = [];
	}

	async open() {
		this.ws = new WebSocket(this.url);
		this.ws.addEventListener("message", (event) => this.onMessage(JSON.parse(event.data)));
		this.ws.addEventListener("close", () => this.rejectPending(new Error(`DevTools websocket closed: ${this.url}`)));
		this.ws.addEventListener("error", () => this.rejectPending(new Error(`DevTools websocket error: ${this.url}`)));
		await new Promise((resolve, reject) => {
			this.ws.addEventListener("open", resolve, {once: true});
			this.ws.addEventListener("error", reject, {once: true});
		});
	}

	onMessage(message) {
		if (message.id && this.pending.has(message.id)) {
			const {resolve, reject} = this.pending.get(message.id);
			this.pending.delete(message.id);
			if (message.error) {
				reject(new Error(`${message.error.code}: ${message.error.message}`));
			} else {
				resolve(message.result);
			}
			return;
		}
		this.events.push(message);
		if (message.method === "Runtime.consoleAPICalled") {
			const args = message.params.args.map((arg) => arg.value ?? arg.description).join(" ");
			console.log(`[console.${message.params.type}] ${args}`);
		} else if (message.method === "Log.entryAdded") {
			console.log(`[log.${message.params.entry.level}] ${message.params.entry.text}`);
		}
	}

	rejectPending(error) {
		for (const {reject} of this.pending.values()) {
			reject(error);
		}
		this.pending.clear();
	}

	send(method, params = {}) {
		const id = this.nextId++;
		this.ws.send(JSON.stringify({id, method, params}));
		return new Promise((resolve, reject) => {
			this.pending.set(id, {resolve, reject});
		});
	}

	close() {
		this.ws.close();
	}
}

async function evaluate(devtools, expression) {
	const result = await devtools.send("Runtime.evaluate", {
		expression,
		awaitPromise: true,
		returnByValue: true,
	});
	if (result.exceptionDetails) {
		throw new Error(result.exceptionDetails.exception?.description ?? result.exceptionDetails.text);
	}
	return result.result.value;
}

async function waitFor(devtools, expression, timeoutMs = 10000) {
	const deadline = Date.now() + timeoutMs;
	while (Date.now() < deadline) {
		if (await evaluate(devtools, expression)) {
			return;
		}
		await sleep(200);
	}
	throw new Error(`Timed out waiting for ${expression}`);
}

async function harnessState() {
	const response = await fetch(debugStateUrl);
	if (!response.ok) {
		throw new Error(`debug state failed: ${response.status} ${await response.text()}`);
	}
	return response.json();
}

async function waitForHarnessState(label, predicate, timeoutMs = 10000) {
	const deadline = Date.now() + timeoutMs;
	let state = null;
	while (Date.now() < deadline) {
		state = await harnessState();
		if (predicate(state)) {
			console.log(`[state.${label}] ${JSON.stringify(state)}`);
			return state;
		}
		await sleep(250);
	}
	console.log(`[state.${label}.timeout] ${JSON.stringify(state)}`);
	throw new Error(`Timed out waiting for harness state: ${label}`);
}

async function readStreamInfoFromChrome(url) {
	await Promise.all(
		(await pageTargets()).filter((item) => item.url.startsWith(url)).map(closeTarget)
	);
	const infoTarget = await cdp(`/json/new?${encodeURIComponent(url)}`, {method: "PUT"});
	const infoDevtools = new DevTools(infoTarget.webSocketDebuggerUrl);
	await infoDevtools.open();
	await infoDevtools.send("Runtime.enable");
	await infoDevtools.send("Page.enable");
	await waitFor(infoDevtools, `document.readyState === "complete" && document.body?.innerText?.length > 0`);
	let value = null;
	for (let index = 0; index < 180; index += 1) {
		value = await evaluate(infoDevtools, `
				(() => {
					const text = document.body?.innerText || document.documentElement?.innerText || "";
					try {
						return JSON.parse(text);
					} catch {
						return {text: text.slice(0, 200)};
					}
				})()
		`);
		if (value?.url && value?.token) {
			break;
		}
		if (index === 0) {
			console.log(`Waiting for stream-info JSON. Log into Home Assistant in the Chrome window if needed.`);
			console.log(`Current stream-info body: ${value?.text ?? ""}`);
		}
		await sleep(1000);
	}
	infoDevtools.close();
	await cdp(`/json/close/${infoTarget.id}`).catch(() => undefined);
	if (!value?.url || !value?.token) {
		throw new Error(`Stream-info page did not return LiveKit URL/token`);
	}
	return value;
}

const streamInfo = streamInfoUrl ? await readStreamInfoFromChrome(streamInfoUrl) : null;
const target = attachOnly
	? (await cdp("/json/list")).find((item) => item.type === "page" && item.url.startsWith(harnessUrl))
	: await reusableTarget(harnessUrl);
if (!target) {
	throw new Error(`No remote-debuggable harness tab found for ${harnessUrl}`);
}
const devtools = new DevTools(target.webSocketDebuggerUrl);
await devtools.open();
await devtools.send("Runtime.enable");
await devtools.send("Log.enable");
await devtools.send("Page.enable");
await devtools.send("Page.bringToFront");
if (!attachOnly) {
	await devtools.send("Page.navigate", {url: harnessUrl});
}
await waitFor(devtools, `document.readyState !== "loading" && !!document.getElementById("connect")`);
if (!attachOnly) {
	await evaluate(devtools, `
			(() => {
				window.__simplirtcPCs = [];
				const Original = window.RTCPeerConnection;
				window.RTCPeerConnection = function(...args) {
					const pc = new Original(...args);
					window.__simplirtcPCs.push(pc);
					window.__simplirtcPC = pc;
					return pc;
				};
				window.RTCPeerConnection.prototype = Original.prototype;
				window.RTCPeerConnection.__proto__ = Original;
			})()
	`);
	await evaluate(devtools, `
				(() => {
					const streamInfo = ${JSON.stringify(streamInfo)};
					if (streamInfo) {
						document.getElementById("livekitUrl").value = streamInfo.url;
						document.getElementById("token").value = streamInfo.token;
					}
					document.getElementById("videoCodec").value = ${JSON.stringify(videoCodec)};
					document.getElementById("consumerCount").value = ${JSON.stringify(String(consumerCount))};
					document.getElementById("connect").click();
				})()
		`);
}

let last = null;
for (let index = 0; index < 18; index += 1) {
	await sleep(1000);
	const value = await evaluate(devtools, `
				(async () => {
					const log = document.getElementById("log")?.textContent ?? "";
					const pcs = window.__simplirtcPCs ?? [];
					const videos = Array.from(document.querySelectorAll("video"));
					if (!pcs.length) {
						return {consumers: [], log};
					}
					const consumers = [];
					for (let index = 0; index < pcs.length; index += 1) {
						const pc = pcs[index];
						const video = videos[index];
						const stats = [];
						for (const report of (await pc.getStats()).values()) {
							if ([
								"inbound-rtp",
								"outbound-rtp",
								"remote-inbound-rtp",
								"remote-outbound-rtp",
								"candidate-pair",
								"transport",
								"codec",
								"track",
							].includes(report.type)) {
								stats.push(Object.fromEntries(Object.entries(report)));
							}
						}
						consumers.push({
							index: index + 1,
							connectionState: pc.connectionState,
							iceConnectionState: pc.iceConnectionState,
							iceGatheringState: pc.iceGatheringState,
							signalingState: pc.signalingState,
							transceivers: pc.getTransceivers().map((t) => ({
								mid: t.mid,
								direction: t.direction,
								currentDirection: t.currentDirection,
								receiverTrackKind: t.receiver.track?.kind,
								receiverTrackReadyState: t.receiver.track?.readyState,
								senderTrackKind: t.sender.track?.kind ?? null,
							})),
							video: video && {
								readyState: video.readyState,
								networkState: video.networkState,
								paused: video.paused,
								videoWidth: video.videoWidth,
								videoHeight: video.videoHeight,
								currentTime: video.currentTime,
								error: video.error && {
									code: video.error.code,
									message: video.error.message,
								},
							},
							stats,
							localDescription: pc.localDescription?.sdp,
							remoteDescription: pc.remoteDescription?.sdp,
						});
					}
					return {consumers, log};
				})()
		`);
		last = value;
		console.log(JSON.stringify({
			second: index + 1,
			consumers: last?.consumers?.map((consumer) => {
				const videoInbound = consumer.stats?.find((report) => report.type === "inbound-rtp" && report.kind === "video");
				const audioInbound = consumer.stats?.find((report) => report.type === "inbound-rtp" && report.kind === "audio");
				return {
					index: consumer.index,
					connectionState: consumer.connectionState,
					iceConnectionState: consumer.iceConnectionState,
					video: consumer.video,
					videoInbound: videoInbound && {
						codecId: videoInbound.codecId,
						packetsReceived: videoInbound.packetsReceived,
						bytesReceived: videoInbound.bytesReceived,
						framesReceived: videoInbound.framesReceived,
						framesDecoded: videoInbound.framesDecoded,
						framesDropped: videoInbound.framesDropped,
						keyFramesDecoded: videoInbound.keyFramesDecoded,
						pliCount: videoInbound.pliCount,
						firCount: videoInbound.firCount,
						nackCount: videoInbound.nackCount,
						totalDecodeTime: videoInbound.totalDecodeTime,
					},
					audioInbound: audioInbound && {
						codecId: audioInbound.codecId,
						packetsReceived: audioInbound.packetsReceived,
						bytesReceived: audioInbound.bytesReceived,
						concealedSamples: audioInbound.concealedSamples,
					},
				};
			}),
		}));
	}

console.log("=== final ===");
console.log(JSON.stringify(last, null, 2));

if (lifecycleTest) {
	await waitForHarnessState(
		"connected",
		(state) => (
			state.sessions.length === consumerCount
			&& Object.keys(state.sfu.consumers).length === consumerCount
			&& Object.keys(state.router.outputs).length === consumerCount
			&& Object.values(state.router.tracks).some((track) => track.subscriptions.length === consumerCount)
		),
	);
	await evaluate(devtools, `window.__simplirtcHarness.closePeer(0)`);
	await waitForHarnessState(
		"one-consumer-closed",
		(state) => (
			state.sessions.length === consumerCount - 1
			&& Object.keys(state.sfu.consumers).length === consumerCount - 1
			&& Object.keys(state.router.outputs).length === consumerCount - 1
			&& Object.values(state.router.tracks).every((track) => track.subscriptions.length === consumerCount - 1)
			&& state.sfu.producer
		),
	);
	await sleep(3000);
	const remainingConsumerState = await evaluate(devtools, `
		(async () => {
			const pc = window.__simplirtcPCs?.[0];
			if (!pc) {
				return null;
			}
			const inbound = [];
			for (const report of (await pc.getStats()).values()) {
				if (report.type === "inbound-rtp") {
					inbound.push(Object.fromEntries(Object.entries(report)));
				}
			}
			return {
				connectionState: pc.connectionState,
				iceConnectionState: pc.iceConnectionState,
				inbound,
			};
		})()
	`);
	console.log(`[state.remaining-consumer] ${JSON.stringify(remainingConsumerState)}`);
	if (!remainingConsumerState || !["connected", "connecting"].includes(remainingConsumerState.connectionState)) {
		throw new Error("Remaining consumer did not stay connected after closing one session");
	}
	await evaluate(devtools, `window.__simplirtcHarness.closeSessions()`);
	await waitForHarnessState(
		"all-consumers-closed",
		(state) => (
			state.sessions.length === 0
			&& Object.keys(state.sfu.consumers).length === 0
			&& Object.keys(state.router.outputs).length === 0
		),
	);
	await waitForHarnessState(
		"idle-producer-closed",
		(state) => (
			state.sessions.length === 0
			&& Object.keys(state.sfu.consumers).length === 0
			&& !state.sfu.producer
			&& !state.sfu.producerSetupTask
			&& !state.sfu.idleTask
			&& Object.keys(state.router.tracks).length === 0
			&& Object.keys(state.router.outputs).length === 0
		),
		15000,
	);
} else if (!attachOnly) {
	await evaluate(devtools, `window.closeSessions?.()`).catch((error) => {
		console.log(`[cleanup.warn] ${error.message}`);
	});
}
devtools.close();
if (!attachOnly) {
	await closeTarget(target);
}
