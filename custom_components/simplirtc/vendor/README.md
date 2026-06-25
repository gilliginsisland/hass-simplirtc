Vendored and pruned aiortc 1.14.0 runtime source.

Home Assistant 2026 beta constrains `av` to 17.0.1, while the upstream
`aiortc==1.14.0` distribution metadata requires `av<17.0.0`. The live stream
path uses aiortc for WebRTC signaling, ICE, DTLS, SRTP, and RTP packet helpers,
but does not use aiortc's media decode path.

This copy keeps the pieces needed by the integration's live WebRTC and snapshot
paths, with local raw-RTP hooks folded into `RTCPeerConnection`,
`RTCDtlsTransport`, `RTCRtpSender`, and `RTCRtpReceiver`.

Pruned upstream features:

- SCTP/data channels
- contrib media/signaling helpers
- RTP sender media encoding loop

Snapshot capture still uses the normal receiver decode path, so PyAV remains a
runtime requirement compatible with Home Assistant's pinned `av` version.
