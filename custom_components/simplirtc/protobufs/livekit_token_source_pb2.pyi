import livekit_room_pb2 as _livekit_room_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class TokenSourceRequest(_message.Message):
    __slots__ = ("room_name", "participant_name", "participant_identity", "participant_metadata", "participant_attributes", "room_config")
    class ParticipantAttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ROOM_NAME_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_NAME_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_IDENTITY_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_METADATA_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    ROOM_CONFIG_FIELD_NUMBER: _ClassVar[int]
    room_name: str
    participant_name: str
    participant_identity: str
    participant_metadata: str
    participant_attributes: _containers.ScalarMap[str, str]
    room_config: _livekit_room_pb2.RoomConfiguration
    def __init__(self, room_name: _Optional[str] = ..., participant_name: _Optional[str] = ..., participant_identity: _Optional[str] = ..., participant_metadata: _Optional[str] = ..., participant_attributes: _Optional[_Mapping[str, str]] = ..., room_config: _Optional[_Union[_livekit_room_pb2.RoomConfiguration, _Mapping]] = ...) -> None: ...

class TokenSourceResponse(_message.Message):
    __slots__ = ("server_url", "participant_token")
    SERVER_URL_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_TOKEN_FIELD_NUMBER: _ClassVar[int]
    server_url: str
    participant_token: str
    def __init__(self, server_url: _Optional[str] = ..., participant_token: _Optional[str] = ...) -> None: ...
