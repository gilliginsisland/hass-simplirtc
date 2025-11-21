from google.protobuf import empty_pb2 as _empty_pb2
import livekit_models_pb2 as _livekit_models_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListReplaysRequest(_message.Message):
    __slots__ = ("room_name", "page_token")
    ROOM_NAME_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    room_name: str
    page_token: _livekit_models_pb2.TokenPagination
    def __init__(self, room_name: _Optional[str] = ..., page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ...) -> None: ...

class ListReplaysResponse(_message.Message):
    __slots__ = ("replays", "next_page_token")
    REPLAYS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    replays: _containers.RepeatedCompositeFieldContainer[ReplayInfo]
    next_page_token: _livekit_models_pb2.TokenPagination
    def __init__(self, replays: _Optional[_Iterable[_Union[ReplayInfo, _Mapping]]] = ..., next_page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ...) -> None: ...

class ReplayInfo(_message.Message):
    __slots__ = ("replay_id", "room_name", "start_time", "duration")
    REPLAY_ID_FIELD_NUMBER: _ClassVar[int]
    ROOM_NAME_FIELD_NUMBER: _ClassVar[int]
    START_TIME_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    replay_id: str
    room_name: str
    start_time: int
    duration: int
    def __init__(self, replay_id: _Optional[str] = ..., room_name: _Optional[str] = ..., start_time: _Optional[int] = ..., duration: _Optional[int] = ...) -> None: ...

class DeleteReplayRequest(_message.Message):
    __slots__ = ("replay_id",)
    REPLAY_ID_FIELD_NUMBER: _ClassVar[int]
    replay_id: str
    def __init__(self, replay_id: _Optional[str] = ...) -> None: ...

class PlaybackRequest(_message.Message):
    __slots__ = ("replay_id", "playback_room", "seek_offset")
    REPLAY_ID_FIELD_NUMBER: _ClassVar[int]
    PLAYBACK_ROOM_FIELD_NUMBER: _ClassVar[int]
    SEEK_OFFSET_FIELD_NUMBER: _ClassVar[int]
    replay_id: str
    playback_room: str
    seek_offset: int
    def __init__(self, replay_id: _Optional[str] = ..., playback_room: _Optional[str] = ..., seek_offset: _Optional[int] = ...) -> None: ...

class PlaybackResponse(_message.Message):
    __slots__ = ("playback_id",)
    PLAYBACK_ID_FIELD_NUMBER: _ClassVar[int]
    playback_id: str
    def __init__(self, playback_id: _Optional[str] = ...) -> None: ...

class SeekRequest(_message.Message):
    __slots__ = ("playback_id", "seek_offset")
    PLAYBACK_ID_FIELD_NUMBER: _ClassVar[int]
    SEEK_OFFSET_FIELD_NUMBER: _ClassVar[int]
    playback_id: str
    seek_offset: int
    def __init__(self, playback_id: _Optional[str] = ..., seek_offset: _Optional[int] = ...) -> None: ...

class ClosePlaybackRequest(_message.Message):
    __slots__ = ("playback_id",)
    PLAYBACK_ID_FIELD_NUMBER: _ClassVar[int]
    playback_id: str
    def __init__(self, playback_id: _Optional[str] = ...) -> None: ...
