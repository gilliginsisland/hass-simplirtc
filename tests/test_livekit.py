"""Warmup and compatibility coverage against current Home Assistant."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.components.camera import WebRTCAnswer

from custom_components.simplirtc.camera import SimpliSafeCamera, LiveKitResponse
from custom_components.simplirtc.livekit import LiveKitSession, LiveKitSignal, WarmupSession
from custom_components.simplirtc.protobufs.livekit_models_pb2 import AUDIO, CAMERA, MICROPHONE, VIDEO, ParticipantInfo, TrackInfo
from custom_components.simplirtc.protobufs.livekit_rtc_pb2 import (
    JoinResponse, MediaSectionsRequirement, ParticipantUpdate,
    SessionDescription, SubscriptionPermissionUpdate,
)


def tracks():
    return ParticipantInfo(is_publisher=True, tracks=[
        TrackInfo(sid="audio", type=AUDIO, source=MICROPHONE),
        TrackInfo(sid="video", type=VIDEO, source=CAMERA),
    ])


class FakeSignal:
    def __init__(self, messages):
        self.messages = messages
        self.sent = []

    def __aiter__(self):
        async def iterate():
            for message in self.messages:
                await asyncio.sleep(0)
                yield message
        return iterate()

    async def send(self, message):
        self.sent.append(message)


@pytest.mark.asyncio
@pytest.mark.parametrize("already_published", [False, True])
async def test_conditional_warmup_and_browser_handoff(already_published):
    """Cold joins warm before browser signaling; already-published rooms skip it."""
    initial = FakeSignal([("join", JoinResponse(
        other_participants=[tracks()] if already_published else [],
    ))])
    browser = FakeSignal([("join", JoinResponse()),
        ("answer", SessionDescription(sdp="browser answer"))])
    opened, closed, answers = [], [], []

    @asynccontextmanager
    async def connect(*args, **kwargs):
        signal = initial if not opened else browser
        opened.append(signal)
        try:
            yield signal
        finally:
            closed.append(signal)

    client = LiveKitSession(livekit_url="ws://localhost", user_token="test", send_ice_servers=lambda _: None)
    client._task = SimpleNamespace(done=lambda: False)
    await client.handle_offer("browser offer", answers.append)
    run_browser = client._run_browser_signal

    async def browser_signal(signal):
        if not already_published:
            assert closed == [initial]
            assert warmup.await_count == 1
        await asyncio.sleep(0)
        await run_browser(signal)

    warmup = AsyncMock()
    with patch.object(LiveKitSignal, "connect", connect), patch(
        "custom_components.simplirtc.livekit.WarmupSession", return_value=AsyncMock(__aenter__=AsyncMock(return_value=SimpleNamespace(run=warmup))),
    ), patch.object(client, "_run_browser_signal", side_effect=browser_signal):
        await client._read()
        assert warmup.await_count == (0 if already_published else 1)
    assert len(opened) == (1 if already_published else 2)
    target = initial if already_published else browser
    assert [request.offer.sdp for request in target.sent] == ["browser offer"]
    if not already_published:
        assert answers == [WebRTCAnswer(answer="browser answer")]


@pytest.mark.asyncio
async def test_warmup_negotiates_late_audio_and_video_before_closing():
    """The warmup peer applies answers and renegotiates until both permissions arrive."""
    signal = FakeSignal([
        ("answer", SessionDescription(type="answer", sdp="initial")),
        ("update", ParticipantUpdate(participants=[tracks()])),
        ("subscription_permission_update", SubscriptionPermissionUpdate(track_sid="audio", allowed=True)),
        ("media_sections_requirement", MediaSectionsRequirement()),
        ("answer", SessionDescription(type="answer", sdp="late")),
        ("subscription_permission_update", SubscriptionPermissionUpdate(track_sid="video", allowed=True)),
    ])
    peer = SimpleNamespace(
        addTransceiver=lambda *args, **kwargs: None,
        setLocalDescription=AsyncMock(), setRemoteDescription=AsyncMock(), close=AsyncMock(),
        localDescription=SimpleNamespace(type="offer", sdp="warmup offer"),
    )
    with patch("custom_components.simplirtc.livekit.RTCPeerConnection", return_value=peer):
        async with WarmupSession(signal, ice_servers=[]) as warmup:
            await warmup.run()
    assert len(signal.sent) == 2
    assert peer.setRemoteDescription.await_count == 2
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_upstream_aiortc_creates_warmup_offer_with_current_pyav():
    signal = FakeSignal([])
    warmup = WarmupSession(signal, ice_servers=[])
    try:
        await warmup._send_peer_offer()
        assert "m=audio " in signal.sent[0].offer.sdp
        assert "m=video " in signal.sent[0].offer.sdp
    finally:
        await warmup._peer_connection.close()


@pytest.mark.asyncio
async def test_signal_context_cancels_ping_on_handoff():
    """Leaving warmup must not wait indefinitely for its ping task."""
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for _ in ws:
            pass
        return ws
    app = web.Application()
    app.router.add_get('/rtc', socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    import logging
    try:
        async with asyncio.timeout(2):
            async with LiveKitSignal.connect(f"ws://127.0.0.1:{port}", token="test", auto_subscribe=True, logger=logging.getLogger("test")) as signal:
                signal._start_ping(60)
                task = signal._ping_task
                await asyncio.sleep(0)
        assert task.done()
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_live_view_sets_host_header_for_current_simplipy():
    api = SimpleNamespace(async_request=AsyncMock(return_value={"liveKitDetails": {
        "liveKitURL": "wss://localhost", "userToken": "test",
    }}))
    camera = SimpleNamespace(
        _simplisafe=SimpleNamespace(_api=api),
        _device=SimpleNamespace(serial="test"), _system=SimpleNamespace(system_id=1),
    )
    await SimpliSafeCamera._create_stream(camera, LiveKitResponse)
    assert api.async_request.call_args.kwargs["headers"] == {
        "Host": "app-hub.prd.aser.simplisafe.com",
    }


@pytest.mark.asyncio
async def test_internal_replay_answers_do_not_reach_stable_browser():
    client = LiveKitSession(livekit_url="ws://localhost", user_token="test", send_ice_servers=lambda _: None)
    client._task = SimpleNamespace(done=lambda: False)
    answers = []
    await client.handle_offer("initial browser offer", answers.append)
    client._on_answer(SessionDescription(sdp="initial answer"))
    await client._on_media_sections_requirement(MediaSectionsRequirement())
    # A new HA offer may arrive while the internal replay is outstanding.
    await client.handle_offer("new browser offer", answers.append)
    client._on_answer(SessionDescription(sdp="internal replay answer"))
    client._on_answer(SessionDescription(sdp="new browser answer"))
    assert answers == [WebRTCAnswer(answer="initial answer"), WebRTCAnswer(answer="new browser answer")]
    assert not client._pending_answers
