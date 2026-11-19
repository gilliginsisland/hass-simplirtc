"""Validate documented live-view ICE URL forms before passing them to HA."""

import pytest
from pydantic import TypeAdapter, ValidationError

from custom_components.simplirtc.camera import KenisisResponse


def kinesis_response(server):
    return {
        "signedChannelEndpoint": "wss://example.test/signaling",
        "clientId": "test-client",
        "iceServers": [server],
    }


@pytest.mark.parametrize("urls, expected", [
    ("turn:example.test:3478", ["turn:example.test:3478"]),
    (["turn:example.test:3478"], ["turn:example.test:3478"]),
    (
        ["stun:example.test:3478", "turn:example.test:3478"],
        ["stun:example.test:3478", "turn:example.test:3478"],
    ),
])
def test_kinesis_ice_urls_are_normalized_for_ha(urls, expected):
    response = TypeAdapter(KenisisResponse).validate_python(kinesis_response({
        "urls": urls, "username": "test-user", "credential": "test-credential",
    }))
    server = response.iceServers[0]
    assert server.urls == expected
    assert list(server.urls) == expected
    assert server.username == "test-user"
    assert server.credential == "test-credential"


@pytest.mark.parametrize("urls", [None, 123, {"url": "turn:example.test:3478"}, [123]])
def test_kinesis_ice_urls_still_reject_invalid_values(urls):
    with pytest.raises(ValidationError) as raised:
        TypeAdapter(KenisisResponse).validate_python(kinesis_response({"urls": urls}))
    assert raised.value.errors(include_input=False)[0]["loc"][:3] == ("iceServers", 0, "urls")


def test_kinesis_ice_urls_remain_required():
    with pytest.raises(ValidationError) as raised:
        TypeAdapter(KenisisResponse).validate_python(kinesis_response({}))
    assert raised.value.errors(include_input=False)[0]["loc"] == ("iceServers", 0, "urls")
