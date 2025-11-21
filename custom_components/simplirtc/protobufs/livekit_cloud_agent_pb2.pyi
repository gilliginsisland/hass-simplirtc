import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AgentSecretKind(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AGENT_SECRET_KIND_UNKNOWN: _ClassVar[AgentSecretKind]
    AGENT_SECRET_KIND_ENVIRONMENT: _ClassVar[AgentSecretKind]
    AGENT_SECRET_KIND_FILE: _ClassVar[AgentSecretKind]
AGENT_SECRET_KIND_UNKNOWN: AgentSecretKind
AGENT_SECRET_KIND_ENVIRONMENT: AgentSecretKind
AGENT_SECRET_KIND_FILE: AgentSecretKind

class AgentSecret(_message.Message):
    __slots__ = ("name", "value", "created_at", "updated_at", "kind")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    KIND_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: bytes
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    kind: AgentSecretKind
    def __init__(self, name: _Optional[str] = ..., value: _Optional[bytes] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., kind: _Optional[_Union[AgentSecretKind, str]] = ...) -> None: ...

class CreateAgentRequest(_message.Message):
    __slots__ = ("agent_name", "secrets", "replicas", "max_replicas", "cpu_req", "regions")
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    CPU_REQ_FIELD_NUMBER: _ClassVar[int]
    REGIONS_FIELD_NUMBER: _ClassVar[int]
    agent_name: str
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    replicas: int
    max_replicas: int
    cpu_req: str
    regions: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, agent_name: _Optional[str] = ..., secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ..., replicas: _Optional[int] = ..., max_replicas: _Optional[int] = ..., cpu_req: _Optional[str] = ..., regions: _Optional[_Iterable[str]] = ...) -> None: ...

class CreateAgentResponse(_message.Message):
    __slots__ = ("agent_id", "agent_name", "status", "version", "presigned_url", "tag", "server_regions", "presigned_post_request")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    PRESIGNED_URL_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    SERVER_REGIONS_FIELD_NUMBER: _ClassVar[int]
    PRESIGNED_POST_REQUEST_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    status: str
    version: str
    presigned_url: str
    tag: str
    server_regions: _containers.RepeatedScalarFieldContainer[str]
    presigned_post_request: PresignedPostRequest
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., status: _Optional[str] = ..., version: _Optional[str] = ..., presigned_url: _Optional[str] = ..., tag: _Optional[str] = ..., server_regions: _Optional[_Iterable[str]] = ..., presigned_post_request: _Optional[_Union[PresignedPostRequest, _Mapping]] = ...) -> None: ...

class PresignedPostRequest(_message.Message):
    __slots__ = ("url", "values")
    class ValuesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    URL_FIELD_NUMBER: _ClassVar[int]
    VALUES_FIELD_NUMBER: _ClassVar[int]
    url: str
    values: _containers.ScalarMap[str, str]
    def __init__(self, url: _Optional[str] = ..., values: _Optional[_Mapping[str, str]] = ...) -> None: ...

class AgentDeployment(_message.Message):
    __slots__ = ("region", "agent_id", "status", "replicas", "min_replicas", "max_replicas", "cpu_req", "cur_cpu", "cur_mem", "mem_req", "mem_limit", "cpu_limit", "server_region")
    REGION_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MIN_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    CPU_REQ_FIELD_NUMBER: _ClassVar[int]
    CUR_CPU_FIELD_NUMBER: _ClassVar[int]
    CUR_MEM_FIELD_NUMBER: _ClassVar[int]
    MEM_REQ_FIELD_NUMBER: _ClassVar[int]
    MEM_LIMIT_FIELD_NUMBER: _ClassVar[int]
    CPU_LIMIT_FIELD_NUMBER: _ClassVar[int]
    SERVER_REGION_FIELD_NUMBER: _ClassVar[int]
    region: str
    agent_id: str
    status: str
    replicas: int
    min_replicas: int
    max_replicas: int
    cpu_req: str
    cur_cpu: str
    cur_mem: str
    mem_req: str
    mem_limit: str
    cpu_limit: str
    server_region: str
    def __init__(self, region: _Optional[str] = ..., agent_id: _Optional[str] = ..., status: _Optional[str] = ..., replicas: _Optional[int] = ..., min_replicas: _Optional[int] = ..., max_replicas: _Optional[int] = ..., cpu_req: _Optional[str] = ..., cur_cpu: _Optional[str] = ..., cur_mem: _Optional[str] = ..., mem_req: _Optional[str] = ..., mem_limit: _Optional[str] = ..., cpu_limit: _Optional[str] = ..., server_region: _Optional[str] = ...) -> None: ...

class AgentInfo(_message.Message):
    __slots__ = ("agent_id", "agent_name", "version", "agent_deployments", "secrets", "deployed_at")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    AGENT_DEPLOYMENTS_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    DEPLOYED_AT_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    version: str
    agent_deployments: _containers.RepeatedCompositeFieldContainer[AgentDeployment]
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    deployed_at: _timestamp_pb2.Timestamp
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., version: _Optional[str] = ..., agent_deployments: _Optional[_Iterable[_Union[AgentDeployment, _Mapping]]] = ..., secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ..., deployed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class ListAgentsRequest(_message.Message):
    __slots__ = ("agent_name", "agent_id")
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    agent_name: str
    agent_id: str
    def __init__(self, agent_name: _Optional[str] = ..., agent_id: _Optional[str] = ...) -> None: ...

class ListAgentsResponse(_message.Message):
    __slots__ = ("agents",)
    AGENTS_FIELD_NUMBER: _ClassVar[int]
    agents: _containers.RepeatedCompositeFieldContainer[AgentInfo]
    def __init__(self, agents: _Optional[_Iterable[_Union[AgentInfo, _Mapping]]] = ...) -> None: ...

class AgentVersion(_message.Message):
    __slots__ = ("version", "current", "created_at", "deployed_at", "attributes", "status", "owner")
    class AttributesEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    VERSION_FIELD_NUMBER: _ClassVar[int]
    CURRENT_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    DEPLOYED_AT_FIELD_NUMBER: _ClassVar[int]
    ATTRIBUTES_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    version: str
    current: bool
    created_at: _timestamp_pb2.Timestamp
    deployed_at: _timestamp_pb2.Timestamp
    attributes: _containers.ScalarMap[str, str]
    status: str
    owner: str
    def __init__(self, version: _Optional[str] = ..., current: _Optional[bool] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., deployed_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., attributes: _Optional[_Mapping[str, str]] = ..., status: _Optional[str] = ..., owner: _Optional[str] = ...) -> None: ...

class ListAgentVersionsRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ...) -> None: ...

class ListAgentVersionsResponse(_message.Message):
    __slots__ = ("versions",)
    VERSIONS_FIELD_NUMBER: _ClassVar[int]
    versions: _containers.RepeatedCompositeFieldContainer[AgentVersion]
    def __init__(self, versions: _Optional[_Iterable[_Union[AgentVersion, _Mapping]]] = ...) -> None: ...

class UpdateAgentRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name", "replicas", "max_replicas", "cpu_req", "regions", "secrets")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    CPU_REQ_FIELD_NUMBER: _ClassVar[int]
    REGIONS_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    replicas: int
    max_replicas: int
    cpu_req: str
    regions: _containers.RepeatedScalarFieldContainer[str]
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., replicas: _Optional[int] = ..., max_replicas: _Optional[int] = ..., cpu_req: _Optional[str] = ..., regions: _Optional[_Iterable[str]] = ..., secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ...) -> None: ...

class UpdateAgentResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class RestartAgentRequest(_message.Message):
    __slots__ = ("agent_id",)
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    def __init__(self, agent_id: _Optional[str] = ...) -> None: ...

class RestartAgentResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class DeployAgentRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name", "secrets", "replicas", "max_replicas", "cpu_req")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    CPU_REQ_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    replicas: int
    max_replicas: int
    cpu_req: str
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ..., replicas: _Optional[int] = ..., max_replicas: _Optional[int] = ..., cpu_req: _Optional[str] = ...) -> None: ...

class DeployAgentResponse(_message.Message):
    __slots__ = ("success", "message", "agent_id", "presigned_url", "tag", "presigned_post_request")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    PRESIGNED_URL_FIELD_NUMBER: _ClassVar[int]
    TAG_FIELD_NUMBER: _ClassVar[int]
    PRESIGNED_POST_REQUEST_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    agent_id: str
    presigned_url: str
    tag: str
    presigned_post_request: PresignedPostRequest
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ..., agent_id: _Optional[str] = ..., presigned_url: _Optional[str] = ..., tag: _Optional[str] = ..., presigned_post_request: _Optional[_Union[PresignedPostRequest, _Mapping]] = ...) -> None: ...

class UpdateAgentSecretsRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name", "overwrite", "secrets")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    OVERWRITE_FIELD_NUMBER: _ClassVar[int]
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    overwrite: bool
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., overwrite: _Optional[bool] = ..., secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ...) -> None: ...

class UpdateAgentSecretsResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class RollbackAgentRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name", "version")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    version: str
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ..., version: _Optional[str] = ...) -> None: ...

class RollbackAgentResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class DeleteAgentRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ...) -> None: ...

class DeleteAgentResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: _Optional[bool] = ..., message: _Optional[str] = ...) -> None: ...

class ListAgentSecretsRequest(_message.Message):
    __slots__ = ("agent_id", "agent_name")
    AGENT_ID_FIELD_NUMBER: _ClassVar[int]
    AGENT_NAME_FIELD_NUMBER: _ClassVar[int]
    agent_id: str
    agent_name: str
    def __init__(self, agent_id: _Optional[str] = ..., agent_name: _Optional[str] = ...) -> None: ...

class ListAgentSecretsResponse(_message.Message):
    __slots__ = ("secrets",)
    SECRETS_FIELD_NUMBER: _ClassVar[int]
    secrets: _containers.RepeatedCompositeFieldContainer[AgentSecret]
    def __init__(self, secrets: _Optional[_Iterable[_Union[AgentSecret, _Mapping]]] = ...) -> None: ...

class SettingsParam(_message.Message):
    __slots__ = ("name", "value")
    NAME_FIELD_NUMBER: _ClassVar[int]
    VALUE_FIELD_NUMBER: _ClassVar[int]
    name: str
    value: str
    def __init__(self, name: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...

class ClientSettingsResponse(_message.Message):
    __slots__ = ("params",)
    PARAMS_FIELD_NUMBER: _ClassVar[int]
    params: _containers.RepeatedCompositeFieldContainer[SettingsParam]
    def __init__(self, params: _Optional[_Iterable[_Union[SettingsParam, _Mapping]]] = ...) -> None: ...

class ClientSettingsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...
