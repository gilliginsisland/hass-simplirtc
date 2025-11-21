import livekit_models_pb2 as _livekit_models_pb2
import livekit_egress_pb2 as _livekit_egress_pb2
import livekit_ingress_pb2 as _livekit_ingress_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class WebhookEvent(_message.Message):
    __slots__ = ("event", "room", "participant", "egress_info", "ingress_info", "track", "id", "created_at", "num_dropped")
    EVENT_FIELD_NUMBER: _ClassVar[int]
    ROOM_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_FIELD_NUMBER: _ClassVar[int]
    EGRESS_INFO_FIELD_NUMBER: _ClassVar[int]
    INGRESS_INFO_FIELD_NUMBER: _ClassVar[int]
    TRACK_FIELD_NUMBER: _ClassVar[int]
    ID_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    NUM_DROPPED_FIELD_NUMBER: _ClassVar[int]
    event: str
    room: _livekit_models_pb2.Room
    participant: _livekit_models_pb2.ParticipantInfo
    egress_info: _livekit_egress_pb2.EgressInfo
    ingress_info: _livekit_ingress_pb2.IngressInfo
    track: _livekit_models_pb2.TrackInfo
    id: str
    created_at: int
    num_dropped: int
    def __init__(self, event: _Optional[str] = ..., room: _Optional[_Union[_livekit_models_pb2.Room, _Mapping]] = ..., participant: _Optional[_Union[_livekit_models_pb2.ParticipantInfo, _Mapping]] = ..., egress_info: _Optional[_Union[_livekit_egress_pb2.EgressInfo, _Mapping]] = ..., ingress_info: _Optional[_Union[_livekit_ingress_pb2.IngressInfo, _Mapping]] = ..., track: _Optional[_Union[_livekit_models_pb2.TrackInfo, _Mapping]] = ..., id: _Optional[str] = ..., created_at: _Optional[int] = ..., num_dropped: _Optional[int] = ...) -> None: ...
