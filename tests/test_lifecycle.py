"""Exercise session ownership, cancellation, expiry, and provider disconnects."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest
from aiohttp import web
from homeassistant.components.camera import WebRTCAnswer

from custom_components.simplirtc.camera import SimpliSafeCamera
from custom_components.simplirtc.kinesis import KinesisSession
from custom_components.simplirtc.livekit import LiveKitSession, WarmupSession
from custom_components.simplirtc.protobufs.livekit_models_pb2 import AUDIO, MICROPHONE, VIDEO, CAMERA, ParticipantInfo, TrackInfo
from custom_components.simplirtc.protobufs.livekit_rtc_pb2 import JoinResponse, SignalRequest, SignalResponse, SessionDescription
from custom_components.simplirtc.webrtc import SimpliSafeWebRTCSession


class Session(SimpliSafeWebRTCSession[str]):
    def __init__(self, *, delayed_cleanup=False, fail_send=False):
        super().__init__(logger=logging.getLogger('test.lifecycle'), name='test-session-owner')
        self.entered = asyncio.Event()
        self.disconnect = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.release_cleanup = asyncio.Event()
        self.resources_closed = False
        self.delayed_cleanup = delayed_cleanup
        self.fail_send = fail_send
        self.sent = []
        self.written = asyncio.Event()

    @asynccontextmanager
    async def connection(self):
        self.entered.set()
        try:
            yield
        finally:
            self.cleaning.set()
            if self.delayed_cleanup:
                await self.release_cleanup.wait()
            self.resources_closed = True

    async def _read(self):
        async def send(message):
            if self.fail_send:
                raise RuntimeError('test write failed')
            self.sent.append(message)
            self.written.set()
        async with self.connection(), self._request_queue.sending(send):
            await self.disconnect.wait()

    async def handle_offer(self, offer_sdp, send_message):
        self._accept_offer(send_message)
        self._request_queue.put_nowait(offer_sdp)

    async def send_candidate(self, candidate):
        self._check_open()
        self._request_queue.put_nowait(candidate)


async def closed(session):
    async with asyncio.timeout(2):
        await session.wait_closed()
    assert session._task.done()
    assert session._request_queue._pending.empty()


@pytest.mark.asyncio
async def test_stop_waits_for_cleanup_and_notifies_once():
    session = Session(delayed_cleanup=True)
    notices = []
    session.start(lambda: notices.append(session.resources_closed))
    await session.entered.wait()
    await session.handle_offer('offer', lambda _: None)
    await session.written.wait()
    session.close()
    await session.cleaning.wait()
    session.close()  # Must not send a second cancellation into cleanup.
    assert not session._closed.is_set()
    assert notices == []
    with pytest.raises(RuntimeError, match='not running'):
        await session.handle_offer('late offer', lambda _: None)
    session.release_cleanup.set()
    await closed(session)
    assert notices == [True]


@pytest.mark.asyncio
async def test_stop_before_owner_starts_completes_without_opening_resources():
    session = Session()
    notices = []
    session.start(lambda: notices.append(True))
    session.close()
    await closed(session)
    assert notices == [True]
    assert not session.entered.is_set()
    with pytest.raises(RuntimeError):
        session.start(lambda: None)


@pytest.mark.asyncio
async def test_close_before_start_and_task_factory_failure_do_not_hang():
    session = Session()
    session.close()
    await session.wait_closed()
    with pytest.raises(RuntimeError):
        session.start(lambda: None)
    session = Session()
    def fail(*args, **kwargs):
        raise RuntimeError('entry unloaded')
    with pytest.raises(RuntimeError, match='entry unloaded'):
        session.start(lambda: None, create_task=fail)
    await session.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize('fail_send', [False, True])
async def test_remote_eof_or_writer_failure_closes_the_entire_scope(fail_send):
    session = Session(fail_send=fail_send)
    session.start(lambda: None)
    await session.entered.wait()
    if fail_send:
        await session.handle_offer('offer', lambda _: None)
    else:
        session.disconnect.set()
    await closed(session)
    assert session.resources_closed


@pytest.mark.asyncio
async def test_messages_can_be_queued_before_connection_and_are_sent_in_order():
    session = Session()
    session.start(lambda: None)
    await session.handle_offer('offer', lambda _: None)
    await session.send_candidate('candidate')
    await session.written.wait()
    # Sender runs both synchronous test writes in queue order.
    assert session.sent == ['offer', 'candidate']
    session.close()
    await closed(session)


@pytest.mark.asyncio
async def test_unused_deadline_closes_owner_but_offer_disarms_it():
    unused = Session()
    unused.start(lambda: None, unused_timeout=0.01)
    await closed(unused)
    assert unused.resources_closed
    used = Session()
    used.start(lambda: None, unused_timeout=0.01)
    await used.handle_offer('offer', lambda _: None)
    await asyncio.sleep(0.03)
    assert not used._closed.is_set()
    used.close()
    await closed(used)


@pytest.mark.asyncio
async def test_canceled_waiter_does_not_cancel_cleanup():
    session = Session(delayed_cleanup=True)
    session.start(lambda: None)
    await session.entered.wait()
    session.close()
    await session.cleaning.wait()
    waiter = asyncio.create_task(session.wait_closed())
    waiter.cancel()
    await asyncio.gather(waiter, return_exceptions=True)
    assert not session._task.done()
    session.release_cleanup.set()
    await closed(session)
    assert session.resources_closed


@pytest.mark.asyncio
async def test_ha_cancellation_followed_by_close_does_not_interrupt_cleanup():
    session = Session(delayed_cleanup=True)
    session.start(lambda: None)
    await session.entered.wait()
    session._task.cancel()  # HA cancels its managed task on unload/shutdown.
    await session.cleaning.wait()
    session.close()
    session.release_cleanup.set()
    await closed(session)
    assert session.resources_closed


@pytest.mark.asyncio
async def test_warmup_peer_is_closed_on_cancellation():
    started = asyncio.Event()
    peer = SimpleNamespace(addTransceiver=lambda *a, **kw: None, close=AsyncMock())
    async def blocked_offer():
        started.set()
        await asyncio.Future()
    with patch('custom_components.simplirtc.livekit.RTCPeerConnection', return_value=peer):
        warmup = WarmupSession(None, ice_servers=[])
        warmup._send_peer_offer = blocked_offer
        async def run():
            async with warmup:
                await warmup.run()
        task = asyncio.create_task(run())
        await started.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    peer.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_camera_removal_waits_for_active_and_prepared_sessions():
    camera = SimpliSafeCamera.__new__(SimpliSafeCamera)
    camera._provider_sessions = set()
    camera._unused_sessions = {}
    camera._sessions = {}
    sessions = [Session(delayed_cleanup=True), Session()]
    for n, session in enumerate(sessions):
        camera._provider_sessions.add(session)
        session.start(lambda s=session: camera._session_closed(s))
        await session.entered.wait()
        if n:
            camera._unused_sessions[session] = None
        else:
            camera._sessions['browser'] = session
    removal = asyncio.create_task(camera.async_will_remove_from_hass())
    await sessions[0].cleaning.wait()
    assert not removal.done()
    assert camera._sessions
    sessions[0].release_cleanup.set()
    await removal
    assert not camera._provider_sessions
    assert not camera._unused_sessions
    assert not camera._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('provider', ['livekit', 'kinesis'])
async def test_real_websocket_remote_close_finishes_sender_and_owner(provider):
    """Exercise each actual provider against a local WebSocket server."""
    request_received = asyncio.Event()
    async def socket(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        if provider == 'livekit':
            join = JoinResponse(ping_interval=60, other_participants=[ParticipantInfo(
                is_publisher=True, tracks=[TrackInfo(sid='a', type=AUDIO, source=MICROPHONE),
                                         TrackInfo(sid='v', type=VIDEO, source=CAMERA)],
            )])
            await ws.send_bytes(SignalResponse(join=join).SerializeToString())
        async for frame in ws:
            if provider == 'livekit':
                request = SignalRequest.FromString(frame.data)
                if request.WhichOneof('message') != 'offer':
                    continue
                assert request.offer.sdp == 'test offer'
                await ws.send_bytes(SignalResponse(answer=SessionDescription(sdp='answer')).SerializeToString())
            else:
                import json, base64
                message = json.loads(frame.data)
                assert message['action'] == 'SDP_OFFER'
                await ws.send_json({'messageType': 'SDP_ANSWER', 'messagePayload': base64.b64encode(
                    json.dumps({'sdp': 'answer'}).encode()).decode()})
            request_received.set()
            await ws.close()
            break
        return ws
    app = web.Application()
    app.router.add_get('/rtc' if provider == 'livekit' else '/kinesis', socket)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    if provider == 'livekit':
        session = LiveKitSession(livekit_url=f'ws://127.0.0.1:{port}', user_token='test', send_ice_servers=lambda _: None)
    else:
        session = KinesisSession(channel_endpoint=f'ws://127.0.0.1:{port}/kinesis', client_id='test')
    answers = []
    try:
        session.start(lambda: None)
        await session.handle_offer('test offer', answers.append)
        await closed(session)
        assert request_received.is_set()
        assert answers == [WebRTCAnswer(answer='answer')]
    finally:
        session.close()
        await session.wait_closed()
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize('raise_in_body', [False, True])
async def test_session_context_exits_only_after_resources_close(raise_in_body):
    session = Session()
    try:
        async with session:
            await session.entered.wait()
            if raise_in_body:
                raise ValueError('caller failed')
    except ValueError:
        assert raise_in_body
    assert session.resources_closed
    assert session._closed.is_set()
    assert session._task.done()


@pytest.mark.asyncio
async def test_camera_ignores_late_candidates_while_session_is_closing():
    camera = SimpliSafeCamera.__new__(SimpliSafeCamera)
    camera._provider_sessions = set()
    camera._unused_sessions = {}
    camera._sessions = {}
    session = Session(delayed_cleanup=True)
    camera._provider_sessions.add(session)
    camera._sessions['browser'] = session
    session.start(lambda: camera._session_closed(session))
    await session.entered.wait()
    camera.close_webrtc_session('browser')
    await session.cleaning.wait()
    await camera.async_on_webrtc_candidate('browser', 'late candidate')
    assert session.sent == []
    session.release_cleanup.set()
    await closed(session)
    assert not camera._sessions


@pytest.mark.asyncio
@pytest.mark.parametrize('provider,cancel_prepare', [('mist', False), ('mist', True), ('kvs', False)])
async def test_camera_preparation_uses_managed_owner_and_handles_cancellation(provider, cancel_prepare):
    camera = SimpliSafeCamera.__new__(SimpliSafeCamera)
    camera.hass = SimpleNamespace(loop=asyncio.get_running_loop())
    camera.entity_id = 'camera.test'
    camera._provider_sessions = set()
    camera._unused_sessions = {}
    camera._sessions = {}
    camera._removing = False
    managed = []
    def create_task(hass, coroutine, name, eager_start):
        assert hass is camera.hass
        assert not eager_start
        task = asyncio.create_task(coroutine, name=name)
        managed.append(task)
        return task
    camera._entry = SimpleNamespace(async_create_background_task=create_task)
    servers = [SimpleNamespace(urls=['turn:example.test:3478'], username='test', credential='test')]
    camera._create_stream = AsyncMock(return_value=SimpleNamespace(liveKitDetails=SimpleNamespace(
        liveKitURL='ws://localhost', userToken='test',
    ), signedChannelEndpoint='ws://localhost', clientId='test', iceServers=servers))
    session = Session(delayed_cleanup=True)
    read = session._read
    def factory(*args, send_ice_servers=None, **kwargs):
        async def receive():
            if send_ice_servers is not None and not cancel_prepare:
                send_ice_servers(servers)
            await read()
        session._read = receive
        return session
    with patch.object(SimpliSafeCamera, '_web_rtc_provider', new_callable=PropertyMock, return_value=provider), patch(
        'custom_components.simplirtc.camera.LiveKitSession', side_effect=factory,
    ), patch('custom_components.simplirtc.camera.KinesisSession', side_effect=factory):
        prepare = asyncio.create_task(camera.async_prepare_webrtc_client_configuration())
        await session.entered.wait()
        if cancel_prepare:
            prepare.cancel()
            await session.cleaning.wait()
            assert not prepare.done()
            session.release_cleanup.set()
            result = await asyncio.gather(prepare, return_exceptions=True)
            assert isinstance(result[0], asyncio.CancelledError)
            assert session._closed.is_set()
            assert not camera._provider_sessions
            assert not camera._unused_sessions
        else:
            config = await prepare
            assert len(config.configuration.ice_servers) == 1
            assert config.configuration.ice_servers[0].urls == servers[0].urls
            assert config.configuration.ice_servers[0].username == 'test'
            assert config.configuration.ice_servers[0].credential == 'test'
            assert session in camera._unused_sessions
            assert managed == [session._task]
            # Claiming the prepared session disarms its scoped expiration.
            await camera.async_handle_async_webrtc_offer('offer', 'browser', lambda _: None)
            assert not camera._unused_sessions
            assert camera._sessions['browser'] is session
            camera.close_webrtc_session('browser')
            await session.cleaning.wait()
            assert camera._sessions
            session.release_cleanup.set()
            await session.wait_closed()
            assert not camera._provider_sessions
            assert not camera._sessions


@pytest.mark.asyncio
async def test_kinesis_offer_and_candidate_keep_their_wire_payloads_and_order():
    import base64
    from dataclasses import asdict
    import json
    from webrtc_models import RTCIceCandidateInit

    session = KinesisSession(channel_endpoint='ws://localhost/kinesis', client_id='camera-client')
    session._task = SimpleNamespace(done=lambda: False)
    requests = []
    async def record(request):
        requests.append(asdict(request))
    candidate = RTCIceCandidateInit(candidate='candidate:1 1 UDP 1 192.0.2.1 1234 typ host',
                                   sdp_mid='audio', sdp_m_line_index=0)
    async with session._request_queue.sending(record):
        await session.handle_offer('first offer', lambda _: None)
        await session.send_candidate(candidate)
        await session.handle_offer('second offer', lambda _: None)
        await asyncio.sleep(0)
    assert [r['action'] for r in requests] == ['SDP_OFFER', 'ICE_CANDIDATE', 'SDP_OFFER']
    assert [r['recipientClientId'] for r in requests] == ['camera-client'] * 3
    assert [r['correlationId'] for r in requests] == [f'{session._id}.{i}' for i in (1, 2, 3)]
    assert [json.loads(base64.b64decode(r['messagePayload'])) for r in requests] == [
        {'type': 'offer', 'sdp': 'first offer'},
        {'candidate': candidate.candidate, 'sdpMid': 'audio', 'sdpMLineIndex': 0, 'usernameFragment': None},
        {'type': 'offer', 'sdp': 'second offer'},
    ]
