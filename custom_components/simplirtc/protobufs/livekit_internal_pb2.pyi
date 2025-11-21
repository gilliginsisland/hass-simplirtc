import livekit_models_pb2 as _livekit_models_pb2
import livekit_egress_pb2 as _livekit_egress_pb2
import livekit_agent_dispatch_pb2 as _livekit_agent_dispatch_pb2
import livekit_room_pb2 as _livekit_room_pb2
import livekit_rtc_pb2 as _livekit_rtc_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class NodeType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    SERVER: _ClassVar[NodeType]
    CONTROLLER: _ClassVar[NodeType]
    MEDIA: _ClassVar[NodeType]
    TURN: _ClassVar[NodeType]
    SWEEPER: _ClassVar[NodeType]
    DIRECTOR: _ClassVar[NodeType]
    HOSTED_AGENT: _ClassVar[NodeType]
    SETTINGS: _ClassVar[NodeType]

class NodeState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STARTING_UP: _ClassVar[NodeState]
    SERVING: _ClassVar[NodeState]
    SHUTTING_DOWN: _ClassVar[NodeState]

class ICECandidateType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ICT_NONE: _ClassVar[ICECandidateType]
    ICT_TCP: _ClassVar[ICECandidateType]
    ICT_TLS: _ClassVar[ICECandidateType]
SERVER: NodeType
CONTROLLER: NodeType
MEDIA: NodeType
TURN: NodeType
SWEEPER: NodeType
DIRECTOR: NodeType
HOSTED_AGENT: NodeType
SETTINGS: NodeType
STARTING_UP: NodeState
SERVING: NodeState
SHUTTING_DOWN: NodeState
ICT_NONE: ICECandidateType
ICT_TCP: ICECandidateType
ICT_TLS: ICECandidateType

class Node(_message.Message):
    __slots__ = ("id", "ip", "num_cpus", "stats", "type", "state", "region")
    ID_FIELD_NUMBER: _ClassVar[int]
    IP_FIELD_NUMBER: _ClassVar[int]
    NUM_CPUS_FIELD_NUMBER: _ClassVar[int]
    STATS_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    id: str
    ip: str
    num_cpus: int
    stats: NodeStats
    type: NodeType
    state: NodeState
    region: str
    def __init__(self, id: _Optional[str] = ..., ip: _Optional[str] = ..., num_cpus: _Optional[int] = ..., stats: _Optional[_Union[NodeStats, _Mapping]] = ..., type: _Optional[_Union[NodeType, str]] = ..., state: _Optional[_Union[NodeState, str]] = ..., region: _Optional[str] = ...) -> None: ...

class NodeStats(_message.Message):
    __slots__ = ("started_at", "updated_at", "num_rooms", "num_clients", "num_tracks_in", "num_tracks_out", "num_track_publish_attempts", "track_publish_attempts_per_sec", "num_track_publish_success", "track_publish_success_per_sec", "num_track_subscribe_attempts", "track_subscribe_attempts_per_sec", "num_track_subscribe_success", "track_subscribe_success_per_sec", "bytes_in", "bytes_out", "packets_in", "packets_out", "nack_total", "bytes_in_per_sec", "bytes_out_per_sec", "packets_in_per_sec", "packets_out_per_sec", "nack_per_sec", "num_cpus", "load_avg_last1min", "load_avg_last5min", "load_avg_last15min", "cpu_load", "memory_load", "memory_total", "memory_used", "sys_packets_out", "sys_packets_dropped", "sys_packets_out_per_sec", "sys_packets_dropped_per_sec", "sys_packets_dropped_pct_per_sec", "retransmit_bytes_out", "retransmit_packets_out", "retransmit_bytes_out_per_sec", "retransmit_packets_out_per_sec", "participant_signal_connected", "participant_signal_connected_per_sec", "participant_rtc_connected", "participant_rtc_connected_per_sec", "participant_rtc_init", "participant_rtc_init_per_sec", "forward_latency", "forward_jitter", "rates")
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    NUM_ROOMS_FIELD_NUMBER: _ClassVar[int]
    NUM_CLIENTS_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACKS_IN_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACKS_OUT_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACK_PUBLISH_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    TRACK_PUBLISH_ATTEMPTS_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACK_PUBLISH_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    TRACK_PUBLISH_SUCCESS_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACK_SUBSCRIBE_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    TRACK_SUBSCRIBE_ATTEMPTS_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    NUM_TRACK_SUBSCRIBE_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    TRACK_SUBSCRIBE_SUCCESS_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    BYTES_IN_FIELD_NUMBER: _ClassVar[int]
    BYTES_OUT_FIELD_NUMBER: _ClassVar[int]
    PACKETS_IN_FIELD_NUMBER: _ClassVar[int]
    PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    NACK_TOTAL_FIELD_NUMBER: _ClassVar[int]
    BYTES_IN_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    BYTES_OUT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    PACKETS_IN_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    PACKETS_OUT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    NACK_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    NUM_CPUS_FIELD_NUMBER: _ClassVar[int]
    LOAD_AVG_LAST1MIN_FIELD_NUMBER: _ClassVar[int]
    LOAD_AVG_LAST5MIN_FIELD_NUMBER: _ClassVar[int]
    LOAD_AVG_LAST15MIN_FIELD_NUMBER: _ClassVar[int]
    CPU_LOAD_FIELD_NUMBER: _ClassVar[int]
    MEMORY_LOAD_FIELD_NUMBER: _ClassVar[int]
    MEMORY_TOTAL_FIELD_NUMBER: _ClassVar[int]
    MEMORY_USED_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_DROPPED_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_OUT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_DROPPED_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_DROPPED_PCT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_BYTES_OUT_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_BYTES_OUT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_PACKETS_OUT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_SIGNAL_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_SIGNAL_CONNECTED_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_CONNECTED_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_INIT_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_INIT_PER_SEC_FIELD_NUMBER: _ClassVar[int]
    FORWARD_LATENCY_FIELD_NUMBER: _ClassVar[int]
    FORWARD_JITTER_FIELD_NUMBER: _ClassVar[int]
    RATES_FIELD_NUMBER: _ClassVar[int]
    started_at: int
    updated_at: int
    num_rooms: int
    num_clients: int
    num_tracks_in: int
    num_tracks_out: int
    num_track_publish_attempts: int
    track_publish_attempts_per_sec: float
    num_track_publish_success: int
    track_publish_success_per_sec: float
    num_track_subscribe_attempts: int
    track_subscribe_attempts_per_sec: float
    num_track_subscribe_success: int
    track_subscribe_success_per_sec: float
    bytes_in: int
    bytes_out: int
    packets_in: int
    packets_out: int
    nack_total: int
    bytes_in_per_sec: float
    bytes_out_per_sec: float
    packets_in_per_sec: float
    packets_out_per_sec: float
    nack_per_sec: float
    num_cpus: int
    load_avg_last1min: float
    load_avg_last5min: float
    load_avg_last15min: float
    cpu_load: float
    memory_load: float
    memory_total: int
    memory_used: int
    sys_packets_out: int
    sys_packets_dropped: int
    sys_packets_out_per_sec: float
    sys_packets_dropped_per_sec: float
    sys_packets_dropped_pct_per_sec: float
    retransmit_bytes_out: int
    retransmit_packets_out: int
    retransmit_bytes_out_per_sec: float
    retransmit_packets_out_per_sec: float
    participant_signal_connected: int
    participant_signal_connected_per_sec: float
    participant_rtc_connected: int
    participant_rtc_connected_per_sec: float
    participant_rtc_init: int
    participant_rtc_init_per_sec: float
    forward_latency: int
    forward_jitter: int
    rates: _containers.RepeatedCompositeFieldContainer[NodeStatsRate]
    def __init__(self, started_at: _Optional[int] = ..., updated_at: _Optional[int] = ..., num_rooms: _Optional[int] = ..., num_clients: _Optional[int] = ..., num_tracks_in: _Optional[int] = ..., num_tracks_out: _Optional[int] = ..., num_track_publish_attempts: _Optional[int] = ..., track_publish_attempts_per_sec: _Optional[float] = ..., num_track_publish_success: _Optional[int] = ..., track_publish_success_per_sec: _Optional[float] = ..., num_track_subscribe_attempts: _Optional[int] = ..., track_subscribe_attempts_per_sec: _Optional[float] = ..., num_track_subscribe_success: _Optional[int] = ..., track_subscribe_success_per_sec: _Optional[float] = ..., bytes_in: _Optional[int] = ..., bytes_out: _Optional[int] = ..., packets_in: _Optional[int] = ..., packets_out: _Optional[int] = ..., nack_total: _Optional[int] = ..., bytes_in_per_sec: _Optional[float] = ..., bytes_out_per_sec: _Optional[float] = ..., packets_in_per_sec: _Optional[float] = ..., packets_out_per_sec: _Optional[float] = ..., nack_per_sec: _Optional[float] = ..., num_cpus: _Optional[int] = ..., load_avg_last1min: _Optional[float] = ..., load_avg_last5min: _Optional[float] = ..., load_avg_last15min: _Optional[float] = ..., cpu_load: _Optional[float] = ..., memory_load: _Optional[float] = ..., memory_total: _Optional[int] = ..., memory_used: _Optional[int] = ..., sys_packets_out: _Optional[int] = ..., sys_packets_dropped: _Optional[int] = ..., sys_packets_out_per_sec: _Optional[float] = ..., sys_packets_dropped_per_sec: _Optional[float] = ..., sys_packets_dropped_pct_per_sec: _Optional[float] = ..., retransmit_bytes_out: _Optional[int] = ..., retransmit_packets_out: _Optional[int] = ..., retransmit_bytes_out_per_sec: _Optional[float] = ..., retransmit_packets_out_per_sec: _Optional[float] = ..., participant_signal_connected: _Optional[int] = ..., participant_signal_connected_per_sec: _Optional[float] = ..., participant_rtc_connected: _Optional[int] = ..., participant_rtc_connected_per_sec: _Optional[float] = ..., participant_rtc_init: _Optional[int] = ..., participant_rtc_init_per_sec: _Optional[float] = ..., forward_latency: _Optional[int] = ..., forward_jitter: _Optional[int] = ..., rates: _Optional[_Iterable[_Union[NodeStatsRate, _Mapping]]] = ...) -> None: ...

class NodeStatsRate(_message.Message):
    __slots__ = ("started_at", "ended_at", "duration", "track_publish_attempts", "track_publish_success", "track_subscribe_attempts", "track_subscribe_success", "bytes_in", "bytes_out", "packets_in", "packets_out", "nack_total", "sys_packets_out", "sys_packets_dropped", "retransmit_bytes_out", "retransmit_packets_out", "participant_signal_connected", "participant_rtc_connected", "participant_rtc_init", "cpu_load", "memory_load", "memory_used", "memory_total")
    STARTED_AT_FIELD_NUMBER: _ClassVar[int]
    ENDED_AT_FIELD_NUMBER: _ClassVar[int]
    DURATION_FIELD_NUMBER: _ClassVar[int]
    TRACK_PUBLISH_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    TRACK_PUBLISH_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    TRACK_SUBSCRIBE_ATTEMPTS_FIELD_NUMBER: _ClassVar[int]
    TRACK_SUBSCRIBE_SUCCESS_FIELD_NUMBER: _ClassVar[int]
    BYTES_IN_FIELD_NUMBER: _ClassVar[int]
    BYTES_OUT_FIELD_NUMBER: _ClassVar[int]
    PACKETS_IN_FIELD_NUMBER: _ClassVar[int]
    PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    NACK_TOTAL_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    SYS_PACKETS_DROPPED_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_BYTES_OUT_FIELD_NUMBER: _ClassVar[int]
    RETRANSMIT_PACKETS_OUT_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_SIGNAL_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_CONNECTED_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_RTC_INIT_FIELD_NUMBER: _ClassVar[int]
    CPU_LOAD_FIELD_NUMBER: _ClassVar[int]
    MEMORY_LOAD_FIELD_NUMBER: _ClassVar[int]
    MEMORY_USED_FIELD_NUMBER: _ClassVar[int]
    MEMORY_TOTAL_FIELD_NUMBER: _ClassVar[int]
    started_at: int
    ended_at: int
    duration: int
    track_publish_attempts: float
    track_publish_success: float
    track_subscribe_attempts: float
    track_subscribe_success: float
    bytes_in: float
    bytes_out: float
    packets_in: float
    packets_out: float
    nack_total: float
    sys_packets_out: float
    sys_packets_dropped: float
    retransmit_bytes_out: float
    retransmit_packets_out: float
    participant_signal_connected: float
    participant_rtc_connected: float
    participant_rtc_init: float
    cpu_load: float
    memory_load: float
    memory_used: float
    memory_total: float
    def __init__(self, started_at: _Optional[int] = ..., ended_at: _Optional[int] = ..., duration: _Optional[int] = ..., track_publish_attempts: _Optional[float] = ..., track_publish_success: _Optional[float] = ..., track_subscribe_attempts: _Optional[float] = ..., track_subscribe_success: _Optional[float] = ..., bytes_in: _Optional[float] = ..., bytes_out: _Optional[float] = ..., packets_in: _Optional[float] = ..., packets_out: _Optional[float] = ..., nack_total: _Optional[float] = ..., sys_packets_out: _Optional[float] = ..., sys_packets_dropped: _Optional[float] = ..., retransmit_bytes_out: _Optional[float] = ..., retransmit_packets_out: _Optional[float] = ..., participant_signal_connected: _Optional[float] = ..., participant_rtc_connected: _Optional[float] = ..., participant_rtc_init: _Optional[float] = ..., cpu_load: _Optional[float] = ..., memory_load: _Optional[float] = ..., memory_used: _Optional[float] = ..., memory_total: _Optional[float] = ...) -> None: ...

class StartSession(_message.Message):
    __slots__ = ("room_name", "identity", "connection_id", "reconnect", "auto_subscribe", "hidden", "client", "recorder", "name", "grants_json", "adaptive_stream", "participant_id", "reconnect_reason", "subscriber_allow_pause", "disable_ice_lite", "create_room", "add_track_requests", "publisher_offer", "sync_state", "use_single_peer_connection")
    ROOM_NAME_FIELD_NUMBER: _ClassVar[int]
    IDENTITY_FIELD_NUMBER: _ClassVar[int]
    CONNECTION_ID_FIELD_NUMBER: _ClassVar[int]
    RECONNECT_FIELD_NUMBER: _ClassVar[int]
    AUTO_SUBSCRIBE_FIELD_NUMBER: _ClassVar[int]
    HIDDEN_FIELD_NUMBER: _ClassVar[int]
    CLIENT_FIELD_NUMBER: _ClassVar[int]
    RECORDER_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    GRANTS_JSON_FIELD_NUMBER: _ClassVar[int]
    ADAPTIVE_STREAM_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_ID_FIELD_NUMBER: _ClassVar[int]
    RECONNECT_REASON_FIELD_NUMBER: _ClassVar[int]
    SUBSCRIBER_ALLOW_PAUSE_FIELD_NUMBER: _ClassVar[int]
    DISABLE_ICE_LITE_FIELD_NUMBER: _ClassVar[int]
    CREATE_ROOM_FIELD_NUMBER: _ClassVar[int]
    ADD_TRACK_REQUESTS_FIELD_NUMBER: _ClassVar[int]
    PUBLISHER_OFFER_FIELD_NUMBER: _ClassVar[int]
    SYNC_STATE_FIELD_NUMBER: _ClassVar[int]
    USE_SINGLE_PEER_CONNECTION_FIELD_NUMBER: _ClassVar[int]
    room_name: str
    identity: str
    connection_id: str
    reconnect: bool
    auto_subscribe: bool
    hidden: bool
    client: _livekit_models_pb2.ClientInfo
    recorder: bool
    name: str
    grants_json: str
    adaptive_stream: bool
    participant_id: str
    reconnect_reason: _livekit_models_pb2.ReconnectReason
    subscriber_allow_pause: bool
    disable_ice_lite: bool
    create_room: _livekit_room_pb2.CreateRoomRequest
    add_track_requests: _containers.RepeatedCompositeFieldContainer[_livekit_rtc_pb2.AddTrackRequest]
    publisher_offer: _livekit_rtc_pb2.SessionDescription
    sync_state: _livekit_rtc_pb2.SyncState
    use_single_peer_connection: bool
    def __init__(self, room_name: _Optional[str] = ..., identity: _Optional[str] = ..., connection_id: _Optional[str] = ..., reconnect: _Optional[bool] = ..., auto_subscribe: _Optional[bool] = ..., hidden: _Optional[bool] = ..., client: _Optional[_Union[_livekit_models_pb2.ClientInfo, _Mapping]] = ..., recorder: _Optional[bool] = ..., name: _Optional[str] = ..., grants_json: _Optional[str] = ..., adaptive_stream: _Optional[bool] = ..., participant_id: _Optional[str] = ..., reconnect_reason: _Optional[_Union[_livekit_models_pb2.ReconnectReason, str]] = ..., subscriber_allow_pause: _Optional[bool] = ..., disable_ice_lite: _Optional[bool] = ..., create_room: _Optional[_Union[_livekit_room_pb2.CreateRoomRequest, _Mapping]] = ..., add_track_requests: _Optional[_Iterable[_Union[_livekit_rtc_pb2.AddTrackRequest, _Mapping]]] = ..., publisher_offer: _Optional[_Union[_livekit_rtc_pb2.SessionDescription, _Mapping]] = ..., sync_state: _Optional[_Union[_livekit_rtc_pb2.SyncState, _Mapping]] = ..., use_single_peer_connection: _Optional[bool] = ...) -> None: ...

class RoomInternal(_message.Message):
    __slots__ = ("track_egress", "participant_egress", "playout_delay", "agent_dispatches", "sync_streams", "replay_enabled")
    TRACK_EGRESS_FIELD_NUMBER: _ClassVar[int]
    PARTICIPANT_EGRESS_FIELD_NUMBER: _ClassVar[int]
    PLAYOUT_DELAY_FIELD_NUMBER: _ClassVar[int]
    AGENT_DISPATCHES_FIELD_NUMBER: _ClassVar[int]
    SYNC_STREAMS_FIELD_NUMBER: _ClassVar[int]
    REPLAY_ENABLED_FIELD_NUMBER: _ClassVar[int]
    track_egress: _livekit_egress_pb2.AutoTrackEgress
    participant_egress: _livekit_egress_pb2.AutoParticipantEgress
    playout_delay: _livekit_models_pb2.PlayoutDelay
    agent_dispatches: _containers.RepeatedCompositeFieldContainer[_livekit_agent_dispatch_pb2.RoomAgentDispatch]
    sync_streams: bool
    replay_enabled: bool
    def __init__(self, track_egress: _Optional[_Union[_livekit_egress_pb2.AutoTrackEgress, _Mapping]] = ..., participant_egress: _Optional[_Union[_livekit_egress_pb2.AutoParticipantEgress, _Mapping]] = ..., playout_delay: _Optional[_Union[_livekit_models_pb2.PlayoutDelay, _Mapping]] = ..., agent_dispatches: _Optional[_Iterable[_Union[_livekit_agent_dispatch_pb2.RoomAgentDispatch, _Mapping]]] = ..., sync_streams: _Optional[bool] = ..., replay_enabled: _Optional[bool] = ...) -> None: ...

class ICEConfig(_message.Message):
    __slots__ = ("preference_subscriber", "preference_publisher")
    PREFERENCE_SUBSCRIBER_FIELD_NUMBER: _ClassVar[int]
    PREFERENCE_PUBLISHER_FIELD_NUMBER: _ClassVar[int]
    preference_subscriber: ICECandidateType
    preference_publisher: ICECandidateType
    def __init__(self, preference_subscriber: _Optional[_Union[ICECandidateType, str]] = ..., preference_publisher: _Optional[_Union[ICECandidateType, str]] = ...) -> None: ...
