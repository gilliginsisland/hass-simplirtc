"""Exercise real aiortc transports and raw packet forwarding without cloud credentials."""
import asyncio
from contextlib import asynccontextmanager

import pytest
from aiortc import AudioStreamTrack, VideoStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection
from aiortc.sdp import SessionDescription
from unittest.mock import patch

RawRtpSfu = pytest.importorskip("custom_components.simplirtc.sfu").RawRtpSfu


@pytest.fixture(autouse=True)
def loopback_ice(monkeypatch):
    monkeypatch.setattr("aioice.ice.get_host_addresses", lambda use_ipv4, use_ipv6: ["127.0.0.1"])


@pytest.mark.asyncio
async def test_late_media_is_forwarded_to_two_consumers_and_all_tasks_close():
    baseline = set(asyncio.all_tasks())
    source = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    upstream = None
    joins = 0
    @asynccontextmanager
    async def connect(producer):
        nonlocal joins, upstream
        joins += 1
        upstream = producer
        producer.rtc_configuration.iceServers = []
        yield
    room = RawRtpSfu(connect, idle_timeout=.02)
    browsers = [RTCPeerConnection(RTCConfiguration(iceServers=[])) for _ in range(2)]
    received = [set(), set()]
    try:
        async with room.consumer('one') as c1, room.consumer('two') as c2:
            for i, (browser, consumer) in enumerate(zip(browsers, [c1, c2])):
                @browser.on('track')
                def track(track, i=i):
                    async def receive():
                        await track.recv()
                        received[i].add(track.kind)
                    browser._test_tasks.append(asyncio.create_task(receive()))
                browser._test_tasks = []
                for kind in ('audio', 'video'):
                    browser.addTransceiver(kind, direction='recvonly')
                await browser.setLocalDescription()
                answer = await room.answer_offer(consumer, browser.localDescription.sdp)
                from aiortc import RTCSessionDescription
                await browser.setRemoteDescription(RTCSessionDescription(sdp=answer, type='answer'))
            assert joins == 1
            # Viewers have already received their only answer before upstream
            # tracks appear. Negotiate the source afterwards, as the camera does.
            source.addTrack(AudioStreamTrack())
            await source.setLocalDescription()
            await upstream.setRemoteDescription(source.localDescription)
            await upstream.setLocalDescription()
            await source.setRemoteDescription(upstream.localDescription)
            async with asyncio.timeout(5):
                while any('audio' not in kinds for kinds in received):
                    await asyncio.sleep(.01)
            source.addTrack(VideoStreamTrack())
            await source.setLocalDescription()
            await upstream.setRemoteDescription(source.localDescription)
            await upstream.setLocalDescription()
            await source.setRemoteDescription(upstream.localDescription)
            async with asyncio.timeout(10):
                while received != [{'audio', 'video'}, {'audio', 'video'}]:
                    await asyncio.sleep(.01)
            assert joins == 1
        async with asyncio.timeout(1):
            await room._producer_task
        assert not room._bridge._inputs_by_side_peer_kind
        assert not room._bridge._outputs_by_side_peer_kind
    finally:
        for browser in browsers:
            await browser.close()
            for task in getattr(browser, '_test_tasks', []):
                task.cancel()
            await asyncio.gather(*getattr(browser, '_test_tasks', []), return_exceptions=True)
        await source.close()
        await room.close()
    await asyncio.sleep(.05)
    assert not [t for t in asyncio.all_tasks() - baseline if not t.done()]


@pytest.mark.asyncio
async def test_provider_turn_applied_before_both_legs_construct_gatherers():
    servers = [RTCIceServer(urls=['turn:relay.example.test:3478'], username='provider-user', credential='provider-credential')]
    import custom_components.simplirtc.rtp_router as router
    real_gatherer = router.RTCIceGatherer
    seen = []
    def gatherer(**kwargs):
        seen.append(kwargs)
        return real_gatherer(**kwargs)
    @asynccontextmanager
    async def connect(producer):
        producer.rtc_configuration.iceServers = servers
        producer.addTransceiver('audio', direction='recvonly')
        yield
    room = RawRtpSfu(connect)
    with patch.object(router, 'RTCIceGatherer', side_effect=gatherer):
        async with room.consumer('browser') as consumer:
            consumer.addTransceiver('audio', direction='sendonly')
            assert len(seen) == 2
            assert all(item['iceServers'] is servers for item in seen)
            assert all('iceTransportPolicy' in item for item in seen)
        await room.close()


@pytest.mark.asyncio
async def test_canceled_viewer_during_producer_setup_is_cleaned_on_close():
    entered = asyncio.Event()
    cleaned = asyncio.Event()
    @asynccontextmanager
    async def connect(producer):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield
        finally:
            cleaned.set()
    room = RawRtpSfu(connect)
    async def viewer():
        async with room.consumer('canceled'):
            pass
    task = asyncio.create_task(viewer())
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await room.close()
    assert cleaned.is_set()
    assert not room._consumers
    assert room._producer_task.done()


@pytest.mark.asyncio
async def test_tracks_added_before_datachannel_handshake_register_immediately():
    from custom_components.simplirtc.rtp_router import RawRtpBridge, RawRtpPeerConnection
    source = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    bridge = RawRtpBridge()
    producer = RawRtpPeerConnection(
        bridge=bridge, side='producer', configuration=RTCConfiguration(iceServers=[]),
    )
    try:
        source.createDataChannel('subscriber')
        await source.setLocalDescription()
        await producer.setRemoteDescription(source.localDescription)
        await producer.setLocalDescription()
        assert not producer.getTransceivers()
        # Leave the data-channel offer unanswered at the source, so DTLS cannot
        # finish. New media must already be routable when its answer is ready.
        source.addTrack(AudioStreamTrack())
        source.addTrack(VideoStreamTrack())
        await source.setLocalDescription()
        await producer.setRemoteDescription(source.localDescription)
        await producer.setLocalDescription()
        assert producer.connectionState != 'connected'
        assert {t.kind for t in producer.getTransceivers() if t.receiver._raw_started} == {'audio', 'video'}
        assert {item.kind for item in bridge._inputs_by_side_peer_kind.values()} == {'audio', 'video'}
    finally:
        await asyncio.gather(source.close(), producer.close())
    assert not bridge._inputs_by_side_peer_kind
