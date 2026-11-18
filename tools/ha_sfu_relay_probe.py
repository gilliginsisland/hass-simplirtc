"""Paste after ha_startup_benchmark.py in HA's authenticated Debug console.

Call await measure_sfu_relay(camera). Set all_legs=True to also require relay
candidates on the HA consumer. Run without other viewers: this probe temporarily
instruments raw peer construction and TURN channel binding, and waits for the
previous producer to expire. It never records credentials or peer addresses.
"""

import asyncio
from collections import Counter
from dataclasses import replace
from unittest.mock import patch

from aiortc import RTCIceTransportPolicy
from aioice.turn import TurnClientMixin
from custom_components.simplirtc.rtp_router import RawRtpPeerConnection


async def measure_sfu_relay(camera, *, all_legs=False, timeout=20):
    if room := camera._sfu_room:
        assert not room.sfu._consumers, "Close all camera viewers first"
        if producer := room.sfu._producer_task:
            async with asyncio.timeout(35):
                await asyncio.shield(producer)
    peers = []
    bindings = Counter()
    original_init = RawRtpPeerConnection.__init__
    original_bind = TurnClientMixin.channel_bind

    def configure(self, **kwargs):
        relay = kwargs['side'] == 'producer' or all_legs
        kwargs['configuration'] = replace(
            kwargs['configuration'],
            iceTransportPolicy=RTCIceTransportPolicy.RELAY if relay else RTCIceTransportPolicy.ALL,
        )
        original_init(self, **kwargs)
        peers.append(self)

    async def bind(self, channel_number, addr):
        try:
            await original_bind(self, channel_number, addr)
            bindings['ok'] += 1
        except Exception as err:
            response = getattr(err, 'response', None)
            code = response.attributes.get('ERROR-CODE') if response else None
            bindings[str(code) if code else type(err).__name__] += 1
            raise

    with patch.object(RawRtpPeerConnection, '__init__', configure), patch.object(TurnClientMixin, 'channel_bind', bind):
        result = await measure_camera_start(
            camera, 'sfu-all-relay' if all_legs else 'sfu-upstream-relay',
            observed_peers=peers, timeout=timeout,
        )
    result['turn_bindings'] = dict(bindings)
    return result
