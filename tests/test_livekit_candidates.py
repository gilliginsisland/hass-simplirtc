"""Keep warmup and browser candidate parsing consistent at the JSON boundary."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.simplirtc.livekit import LiveKitSession, WarmupSession
from custom_components.simplirtc.protobufs.livekit_rtc_pb2 import SignalTarget, TrickleRequest

CANDIDATE = 'candidate:1 1 UDP 2122260223 192.0.2.1 5000 typ host'


async def receive_both(trickle):
    messages = []
    browser = LiveKitSession(
        livekit_url='wss://example.test', user_token='test', send_ice_servers=lambda _: None,
    )
    browser._send_message = messages.append
    browser._on_trickle(trickle)
    async with WarmupSession(SimpleNamespace(), ice_servers=[]) as warmup:
        with patch.object(warmup._peer_connection, 'addIceCandidate', new_callable=AsyncMock) as add:
            await warmup._on_trickle(trickle)
            candidates = [call.args[0] for call in add.await_args_list]
    return messages, candidates


@pytest.mark.asyncio
@pytest.mark.parametrize('metadata, expected_mid, expected_index', [
    ({'sdpMid': 'video', 'sdpMLineIndex': 2}, 'video', 2),
    ({}, None, None),
    ({'sdpMid': 123, 'sdpMLineIndex': '2'}, None, None),
    ({'sdpMid': [], 'sdpMLineIndex': {}}, None, None),
    ({'sdpMid': 'video', 'sdpMLineIndex': '2'}, 'video', None),
])
async def test_candidate_metadata_is_shared_without_coercing_invalid_types(metadata, expected_mid, expected_index):
    messages, candidates = await receive_both(TrickleRequest(
        target=SignalTarget.PUBLISHER,
        candidateInit=json.dumps({'candidate': CANDIDATE, **metadata}),
    ))
    assert len(messages) == len(candidates) == 1
    forwarded = messages[0].candidate
    parsed = candidates[0]
    assert forwarded.candidate == CANDIDATE
    assert parsed.ip == '192.0.2.1'
    assert forwarded.sdp_mid == parsed.sdpMid == expected_mid
    assert parsed.sdpMLineIndex == expected_index
    # HA's existing model defaults to the first m-line when neither is supplied.
    browser_index = 0 if expected_mid is None and expected_index is None else expected_index
    assert forwarded.sdp_m_line_index == browser_index


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['{', '[]', 'null', '"text"', '123', 'true', '{}', '{"candidate": null}', '{"candidate": 123}'])
async def test_malformed_candidate_messages_are_dropped_in_both_paths(payload):
    messages, candidates = await receive_both(TrickleRequest(
        target=SignalTarget.PUBLISHER, candidateInit=payload,
    ))
    assert messages == candidates == []


@pytest.mark.asyncio
async def test_candidate_validation_errors_do_not_log_candidate_contents(caplog):
    messages, candidates = await receive_both(TrickleRequest(
        target=SignalTarget.PUBLISHER,
        candidateInit=json.dumps({'candidate': ['private-candidate-content']}),
    ))
    assert messages == candidates == []
    assert 'Dropping invalid LiveKit ICE candidate' in caplog.text
    assert 'private-candidate-content' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', ['', '{"candidate": ""}'])
async def test_final_candidates_are_preserved(payload):
    messages, candidates = await receive_both(TrickleRequest(
        target=SignalTarget.PUBLISHER, final=True, candidateInit=payload,
    ))
    assert candidates == [None]
    assert len(messages) == 1
    assert messages[0].candidate.candidate == ''


@pytest.mark.asyncio
async def test_empty_nonfinal_messages_are_ignored():
    messages, candidates = await receive_both(TrickleRequest(target=SignalTarget.PUBLISHER))
    assert messages == candidates == []


@pytest.mark.asyncio
async def test_other_signal_target_is_ignored():
    messages, candidates = await receive_both(TrickleRequest(
        target=SignalTarget.SUBSCRIBER,
        candidateInit=json.dumps({'candidate': CANDIDATE}),
    ))
    assert messages == candidates == []
