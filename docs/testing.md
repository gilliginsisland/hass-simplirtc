# LiveKit development and testing notes

These notes accompany the tools on the `test-harness` branch.

## LiveKit signaling

The integration retains v1.5's conditional aiortc warmup. It first joins
LiveKit and checks for published camera audio and video tracks. If either is
missing, a local receive-only aiortc peer negotiates until both tracks are
published and subscription permission is granted. It then closes that peer and
joins again for Home Assistant's browser connection. If both tracks already
exist at the initial join, it skips warmup. Warmup checks track permissions;
it does not wait for decoded frames or remain connected during browser playback.

The browser receives media directly from LiveKit; the warmup peer is not a media
relay. The stored browser offer is replayed on negotiation requests, as in
v1.5, but answers to those internal replays stay inside the integration. Only
answers to offers actually received from HA are returned to HA; this prevents
applying a second answer while its browser is already stable. A warmup failure
is logged and falls back to attempting the browser session, as in v1.5.

For Home Assistant Core 2026.11.2, the integration uses upstream aiortc 1.16.0
instead of vendored aiortc. That release supports PyAV 19, which current HA
requires. There is no separate PyAV pin in the integration. The live-view API
request explicitly sets the app-hub Host header required with HA's current
SimpliSafe library. ICE configuration has a 15-second timeout, and signaling
ping tasks are canceled when a connection closes.

The experiments below record the alternatives tested during development.
In particular, the no-warmup, one-answer replay approach failed browser and
go2rtc cold-start tests and is not the integration's active implementation.

The restored integration passed a cold-start Chrome 157 harness test on
November 17, 2026 using HA 2026.11.2, aiortc 1.16.0, and PyAV 19. The initial
join had zero tracks; warmup completed and the browser session joined with both
tracks present. Over 65.73 seconds, Chrome sent one offer, applied one answer
with zero signaling errors, decoded 1,274 H.264 frames, rendered 1,269 frames,
and received 637 Opus packets. Both media counters were advancing at shutdown,
with video playing at 20 fps. This tests the integration's signaling in a real
browser; the updated integration has not been installed into the running HA.
The browser was muted, so this verifies audio reception/decoding, not audibility.

## Local live-camera probe

`tools/livekit_probe.py` exercises the integration's signaling with a local
aiortc peer that behaves like the HA frontend: one receive-only audio/video
offer and one applied answer. It reports decoded frame counts and frame
freshness without displaying SDP, credentials, or saving media. A successful
probe requires continuing audio and video through the end of the test.

Install the test dependencies into an isolated environment:

```sh
uv venv .venv-test --python 3.14
uv pip install --python .venv-test/bin/python \
  homeassistant==2026.11.2 simplisafe-python==2026.06.0 \
  aiortc==1.16.0 av==19.0.0 numpy==2.3.2 PyTurboJPEG==1.8.3 \
  'protobuf>=6.32.0,<8' 'pydantic>=2,<3' pytest pytest-asyncio
```

Run with a private file containing a SimpliSafe refresh token, or use
`--details-file` with a temporary live-view response instead:

```sh
.venv-test/bin/python tools/livekit_probe.py \
  --refresh-token-file /path/to/private/token-file --camera "Camera name" --duration 35
.venv-test/bin/python -m pytest -q tests
```

The probe authenticates to SimpliSafe and opens a real live-view session; it
does not install the integration or change Home Assistant configuration.

For actual browser validation, run the loopback-only harness and open its local
page in a browser. Click **Start browser test**, then check both the decode
counters and rendered video frames. An ICE connection and `ontrack` events
alone do not establish working media. The page also records whether tracks
were present at join and when the later negotiation requests arrived.

```sh
.venv-test/bin/python tools/livekit_harness.py \
  --refresh-token-file /path/to/private/token-file --camera "Camera name"
# Open http://127.0.0.1:8765
```

Use **Stop and hide video** to retain the counters while ending the live view.

Select **HA integration: conditional warmup** to exercise the current integration.
The harness also offers two replay-answer experiments under **Answer handling**:

- **Pass every answer directly to Chrome** demonstrates Chrome rejecting later
  answers in the `stable` signaling state.
- **Reapply original local offer, then apply every answer** first calls
  `setLocalDescription` with the original offer, then applies the later answer.
  This worked in a Chrome 157 cold-start test: late Opus audio and H.264 video
  decoded, and video rendered without a warmup peer. It requires browser-side
  behavior outside the current HA signaling flow; it does not establish that
  the integration can discard later answers.

In the replay-answer experiment, a 54-second session received three answers
(initial, late audio, late video), applied all three without signaling errors,
and decoded 993 H.264 video frames and received 506 Opus packets. The video
element rendered 203 frames before becoming paused at about 10 seconds of
playback, while RTP reception and decoding continued. This proves that applying
the updated answer enables browser media, but does not validate uninterrupted
video-element playback or audible audio content.

The **LiveKit server offers; Chrome answers** mode uses legacy `/rtc` joining
without a wrapped `join_request`. It relays LiveKit's subscriber offers directly
to Chrome and sends Chrome's answers and subscriber ICE candidates back. Chrome
does not create an initial offer in this mode. The server can offer a data-channel
connection first, then negotiate audio and video as they become available. This
is a browser control experiment; HA's frontend does not expose this signaling
direction to camera integrations.

The server-offer mode passed a 64.75-second Chrome 157 cold-start test with zero
published tracks at join. LiveKit sent three offers (data channels, then audio,
then video), and Chrome sent three answers. Audio and video first decoded at
about 2.02 seconds. At the end, Chrome had decoded 1,255 H.264 frames, rendered
1,256 frames, and received 629 Opus packets. The video clock reached 62.8
seconds, playback was not paused, and audio/video counters were still advancing.
No warmup peer or repeated-answer workaround was used.

The **HA offer + data channel; synthesize answers** mode creates `_reliable`
before Chrome's initial audio/video offer, then synthesizes answers between that
offer and LiveKit's legacy subscriber offers. It keeps one answer on the browser
side. This is an experimental SDP-only bridge, implemented in
`tools/sdp_bridge_probe.py`; it does not rewrite RTP packets or terminate media.

In a 54.91-second cold-start test, the browser offer included `m=application`,
Chrome accepted one synthesized answer, ICE/DTLS connected, and both the
browser-created channel and LiveKit's `_reliable`, `_lossy`, and `_data_track`
channels opened. LiveKit granted audio/video subscriptions and the harness
answered the later server offers, but Chrome received no usable audio/video and
rendered zero frames. Offering the data channel resolves startup in this
experiment, but this synthesis is not a working media bridge.

HA's `WebRTCClientConfiguration.data_channel` was added in core PR #124410 on
October 3, 2024 and is present in core 2024.11.0. It was not introduced by
2026.11.2.

The **Join with browser offer + data channel** and **Join /rtc/v1 with offer +
reliable/lossy channels** modes put the browser's original offer directly in
`JoinRequest.publisher_offer`. They do not synthesize answers, use a warmup
peer, or make a preliminary LiveKit join. The browser creates its offer first;
after the join returns ICE servers, it sets the configuration and applies that
same local offer once, then applies only the initial LiveKit answer. Later
negotiation requests replay the identical offer to LiveKit, while replay
answers stay internal.

Both variants reproduced the late-media failure: the initial answer was
accepted and data channels opened, but it had no audio/video SSRC mappings.
Later answers added those mappings. Keeping the initial answer in Chrome
resulted in no decoded or rendered media. The `/rtc/v1` variant also verified
that both `_reliable` and `_lossy` opened before the late tracks arrived.

The **Wrapped join without offer; wait for server offer** mode sends a wrapped
`/rtc/v1` join with no `publisher_offer`, then lets Chrome act only as an
answerer. LiveKit 1.13.10 returned `subscriber_primary=false` but sent no offer
during a 34.07-second test; Chrome stayed `new`/`stable` with zero offers and
answers. The separate **Join with empty SDP offer; wait for server offer** mode
sets `publisher_offer.type=offer` and an empty SDP string. That returned a join
response followed immediately by leave reason 6 (`STATE_MISMATCH`), with no
server offer. The single-PC transport expects the client to offer; it does not
switch to server-offer negotiation when the join omits an SDP.

The **Single PC; fresh browser offers on renegotiation** mode performs the
complete client-offerer protocol. It joins `/rtc/v1` without an inline offer,
then Chrome creates an offer including audio, video, and reliable/lossy data
channels. Each LiveKit `media_sections_requirement` is passed to Chrome; Chrome
adds any requested sections, creates a fresh offer, sets it locally, and sends
it to LiveKit. Every corresponding answer is applied in Chrome. Requests that
arrive during an outstanding negotiation are queued until signaling is stable.
This mode does not synthesize SDP or replay stored offers internally.

This native single-PC mode passed a 63.39-second Chrome 157 cold-start test.
The session joined with zero published tracks, Chrome sent three genuine offers
(initial, late audio negotiation, late video negotiation), and applied all three
LiveKit answers with zero signaling errors. Both audio and video first decoded
at about 2.98 seconds. At the end, Chrome reported 1,155 H.264 frames decoded,
1,146 video frames rendered, 615 Opus packets received, and 60.64 seconds of
video playback. Playback was active at approximately 20 fps and both media
counters were still advancing when the test stopped.

### Concurrent warmup, followed by a strict one-answer real session

Open `http://127.0.0.1:8765/?role=warmup`, select **Single PC; fresh browser
offers on renegotiation**, and start it. After decoded audio samples and video
frames are present, keep that tab connected. In a second tab, open
`http://127.0.0.1:8765/?role=real` and select **Real join-with-offer after warmup;
forbid renegotiation**. The server gates this mode on a recent connected/stable
warmup report containing both decoded media types. `/report?all=1` provides
separate sanitized reports for each role.

This concurrent test passed. Warmup began with zero published tracks, performed
three genuine offer/answer exchanges, and decoded both audio and video before
the real join. Separate live-view credentials had different participant
identities in the same room, so the peers coexisted. Warmup stayed connected
throughout the real session and was closed only after it ended.

The real session joined `/rtc/v1` with its offer after both tracks were ready.
Its first answer already contained audio/video SSRC and stream mappings.
During 74.15 seconds it sent one browser offer, received/applied one answer,
decoded 1,435 H.264 frames, rendered 1,429 frames, and received 730 Opus packets.
Playback reached 71.75 seconds at about 20 fps and was active when stopped.
LiveKit sent one zero-section negotiation request, which the strict harness
ignored without sending another offer. No synthesis or browser renegotiation
was performed in the real session.

### Cold signaling-only WHEP facade consumed by go2rtc

`tools/go2rtc_whep_probe.py` exposes a loopback WHEP source and proxies browser
signaling to an isolated go2rtc. Python opens HTTP and LiveKit signaling
websockets only; it creates no media peer. go2rtc's complete receive offer is
forwarded unchanged in a `/rtc/v1` join. The facade returns the first answer,
including gathered ICE candidates, and replays the same offer on LiveKit
negotiation requests. Subsequent answers remain internal because go2rtc's WHEP
client applies only the initial answer. The media path is LiveKit → go2rtc →
Chrome, with no Python media relay, transcoding, or warmup peer.

On November 17, 2026, official go2rtc 1.9.15 (darwin/arm64) and Chrome 157
failed this cold-start test. LiveKit 1.13.10 joined with zero published tracks.
The first answer had no audio/video SSRC mappings; later answers added audio
at 3.29 seconds and both tracks at 3.80 seconds. After 146.92 seconds Chrome
remained connected/stable with one offer and one applied answer, but had zero
rendered frames, zero video playback time, and no inbound media statistics.
go2rtc logged unhandled incoming RTP SSRCs, stating that `OnTrack` would not
fire because incoming SSRCs failed simulcast probing. Thus upstream RTP arrived,
but this version did not accept the late media using only its initial answer.

The initial attempt with HA's empty ICE-server configuration timed out before
subscriptions. For the valid late-track test, the isolated go2rtc was configured
with LiveKit-provided TURN credentials obtained through a signaling-only join
with subscriptions disabled. That discovery join created no media peer. The
test also enabled loopback candidates for the local browser connection. No live
HA configuration was changed. These network accommodations got transport
working but did not resolve the late-track failure.

## Session lifecycle

LiveKit and Kinesis share a session owner that runs across HA requests. Offers
and candidates are queued; a sender task belongs to the same connection scope
as the receive loop. Sender failure, receive-loop completion, and explicit
closure all unwind that scope before the owner completes.

The camera starts owners through the config entry's background-task API.
`close()` requests cancellation once, and `wait_closed()` waits for resource
cleanup and the completion callback. A session can also be used directly with
`async with session:`. Warmup peers and signaling connections have their own
nested context managers. A scoped deadline closes prepared sessions if no offer
arrives. Camera removal closes and awaits both active and prepared sessions.

`tests/test_lifecycle.py` covers startup cancellation, repeated close, delayed
cleanup, writer failure, remote EOF, preparation expiry, canceled waiters,
HA-managed task creation, removal, and both provider protocols against a local
WebSocket server.

## Core camera devices

One UI config entry enables live-view entities across all core SimpliSafe
accounts. Each entity links to the device owned by its source account using
`entity.device_entry`, with no `device_info`. Unsupported backends get no
SimpliRTC entity; motion and doorbell entities belong to core.

The migration baseline is the released v1.5 design, before the prerelease tags.
Setup uses HA's helper-device cleanup to relink old devices, transfers entity
registry records to the retained global entry, and removes redundant per-account
entries. Entity IDs remain unchanged. The old `simplirtc:` YAML setting imports
the global entry if none exists; new installations use the UI.

`tests/test_camera_setup.py` checks HA's actual entity-platform attachment path
without registering a device or changing its metadata.
`tests/test_device_discovery.py` covers UI confirmation, singleton enforcement,
YAML import, and migration from multiple original entries, including transferring
entity records before deleting old entries and avoiding repeated cleanup.

## Device-registry notifications

The one platform-managed watcher subscribes before scanning existing devices and
serializes subsequent device changes. It resolves each device's owning core
SimpliSafe account and uses that account's runtime. It does not subscribe to
config-entry change notifications or create account-specific helper entries.
Core registers devices during setup after assigning its runtime; the watcher
accepts these registrations before the source account reaches LOADED.

Source devices added later get live-view entities without an integration reload.
Removal awaits any in-flight entity addition and then the entity's cleanup.
Duplicate updates do not create duplicate entities. The watcher unsubscribes
when its background task ends on unload. Removing the final source device leaves
the global entry enabled so it can discover future devices and accounts.

`tests/test_device_lifecycle.py` exercises device additions, removals, identity and
provider changes, startup races, aborted entity addition, listener cleanup,
multiple source accounts with separate runtimes, and disabled or unready sources.
