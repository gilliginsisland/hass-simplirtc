# Raw RTP SFU comparison

Measured on 2026-11-18 against a real LiveKit-backed camera, using Home Assistant
2026.11.2 and aiortc 1.16.0. Production main was `b14c0e3`; the refreshed SFU was
`73a5db2` on `codex/livekit-multi-output-sfu`.

The SFU branch retains current main's config flow, device attachment, event
snapshots, Kinesis path, and session ownership. It ports the raw router from the
older SFU branch, configures both HA peers with provider TURN credentials before
ICE gathering, and uses normal LiveKit subscriber offers upstream. Each browser
gets its own answer. There is no separate warmup connection. A producer remains
available for 30 seconds after its last consumer closes.

## Method and limits

`tools/ha_startup_benchmark.py` runs inside HA's authenticated Debug console. The
caller passes a camera object; no instance URL, credentials, SDP, or media is
saved. Times begin before provider preparation and end at actual decoded frames.
The client is another aiortc peer on the HA host. These are **HA-host timings**,
not external-browser latency measurements. Browser playback is a separate check.

A fresh SFU trial waits for the previous producer to expire. This establishes a
fresh upstream connection; it does not prove the camera itself has returned to
an idle power state. Retained trials immediately reuse the previous producer.
Main's warmup invocation was instrumented so a join that already had tracks is
not mislabeled as a warmup run. The sample is small and not randomized; frame and
keyframe timing can dominate first-video latency.

All times below are seconds from the start of the request.

| Approach / trial | Configuration | Answer | First video | First audio | Warmup |
| --- | ---: | ---: | ---: | ---: | --- |
| Main 1 | 0.219 | 1.173 | 1.317 | 1.688 | 0.833 |
| Main 2 | 0.228 | 1.093 | 1.216 | 1.642 | 0.741 |
| Main 3, tracks already available | 0.222 | 0.300 | 1.411 | 0.875 | Skipped |
| SFU fresh 1 | 0.207 | 0.353 | 0.964 | 1.426 | None |
| SFU fresh 2 | 0.190 | 0.352 | 1.956 | 0.790 | None |
| SFU retained 1 | 0.002 | 0.084 | 0.530 | 0.560 | None |
| SFU retained 2 | 0.002 | 0.069 | 1.966 | 0.501 | None |
| SFU fresh, upstream forced to relay | 0.190 | 0.276 | 1.902 | 0.832 | None |

The SFU answers sooner and retained producers avoid a new provider API request
and signaling join. These runs do not show a consistent first-video advantage:
one retained trial still waited nearly two seconds for decodable video.

## TURN findings

The forced-upstream test used the credentials returned by SimpliSafe/LiveKit.
The producer gathered **only relay candidates**, selected a relay-to-host pair,
and delivered decoded audio and video. The consumer used a direct candidate.
This verifies the HA-to-LiveKit relay path rather than assuming a direct network.

A separate experiment forced relay on both HA peers. TURN allocations succeeded,
but browser-facing channel bindings returned STUN `401` errors and no downstream
ICE pair was selected. The upstream producer connected through relay. This does
not establish why the provider rejected those bindings or prove its TURN service
can be used for arbitrary browser-to-HA traffic. Do not treat the SFU as ready
for every network based on the successful local-consumer tests.

`tools/ha_sfu_relay_probe.py` collects candidate and nominated-pair state **before
cleanup**, since closing an aioice connection clears those collections. It also
counts TURN binding successes/errors without saving credentials or addresses.

## Late-track regression

The real test exposed a race inherited from the old raw-router branch: when
LiveKit adds audio/video during the initial data-channel DTLS handshake, aiortc's
concurrent connect passes can finish without starting the new raw receivers.
Packets reached the upstream transport while the bridge had no inputs.

The SFU now registers raw receiver metadata when the local answer is applied,
before DTLS needs to finish. Later negotiations refresh routing metadata. The
regression test fails without that override and passes with it. A separate local
media test forwards audio and then video to two consumers through upstream
renegotiation while each consumer retains its original SDP answer.

Microphone publication is disabled on the experimental SFU: its upstream
subscriber connection does not implement the separate LiveKit publisher path.

## Other branch review

| Branch | Finding / disposition |
| --- | --- |
| `codex/livekit-multi-output-sfu` | Refreshed on current main and used for this comparison. |
| `livekit-rtp-router` | Older raw-router implementation underlying the shared-producer SFU; retained as history. |
| `next` (`7177e3c`) | Has an additional queued-router refactor and its own harness. Not included in these timings or rewritten. Subscription sender tasks are canceled without an awaited close scope; its producer-close task is never reset after completion. These lifecycle differences need coverage before importing that refactor. |
| `livekit-native-webrtc` | Older direct-signaling experiment, still on aiortc 1.14.0 and the old config setup. Current main is the maintained direct comparison. |
| `async-turn` | Points to the v1.5 starting point; current main retains asynchronous provider ICE configuration and adds the subsequent HA updates. |
| `codex/prune-vendored-aiortc` | Historical vendored dependencies and PyAV <18 constraint; current main already removed vendoring and pins aiortc 1.16.0. |

## Verification

- 66 tests pass against the SFU implementation, including real local decoded
  media through two consumers, separate late audio/video negotiations, provider
  TURN at gatherer construction, cancellation during setup, and the DTLS race.
- Main-based `test-harness` passes its 62 production tests and skips the optional
  SFU module when that implementation is absent.
- The committed SFU was installed and HA restarted before final measurements.
  The native HA browser viewer produced advancing 960x960 video without a manual
  receiver intervention after that restart.
- After closing the browser and waiting for producer expiry: zero provider
  sessions, zero consumers, producer task finished, and empty input/output maps.
  The only named SimpliRTC task was the expected device-registry watcher.
- Forced relay on both HA legs was repeated on the final SFU: both gathered relay
  candidates, the producer selected a relay-to-host pair, the consumer selected
  no pair, and channel bindings reported one success and eighteen STUN `401`
  responses. The probe timed out at 20 seconds and closed its consumer session.
- After the final all-relay probe, idle cleanup again left zero sessions,
  consumers, and routing entries, with the producer finished and only the
  device-registry watcher running.
- HA was returned to current main `b14c0e3` and restarted. The loaded signaling
  source matched main (`7fb4af772fc8` SHA-256 prefix); the integration was loaded,
  two cameras were present, and there were zero active provider sessions. Live
  runtime versions were Python 3.14.10, aiortc 1.16.0, and PyAV 19.0.0. Local unit
  checks used Python 3.14.3 with the same HA and aiortc versions.
