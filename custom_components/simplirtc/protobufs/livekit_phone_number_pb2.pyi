import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
import livekit_models_pb2 as _livekit_models_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PhoneNumberStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PHONE_NUMBER_STATUS_UNSPECIFIED: _ClassVar[PhoneNumberStatus]
    PHONE_NUMBER_STATUS_ACTIVE: _ClassVar[PhoneNumberStatus]
    PHONE_NUMBER_STATUS_PENDING: _ClassVar[PhoneNumberStatus]
    PHONE_NUMBER_STATUS_RELEASED: _ClassVar[PhoneNumberStatus]

class PhoneNumberType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PHONE_NUMBER_TYPE_UNKNOWN: _ClassVar[PhoneNumberType]
    PHONE_NUMBER_TYPE_MOBILE: _ClassVar[PhoneNumberType]
    PHONE_NUMBER_TYPE_LOCAL: _ClassVar[PhoneNumberType]
    PHONE_NUMBER_TYPE_TOLL_FREE: _ClassVar[PhoneNumberType]
PHONE_NUMBER_STATUS_UNSPECIFIED: PhoneNumberStatus
PHONE_NUMBER_STATUS_ACTIVE: PhoneNumberStatus
PHONE_NUMBER_STATUS_PENDING: PhoneNumberStatus
PHONE_NUMBER_STATUS_RELEASED: PhoneNumberStatus
PHONE_NUMBER_TYPE_UNKNOWN: PhoneNumberType
PHONE_NUMBER_TYPE_MOBILE: PhoneNumberType
PHONE_NUMBER_TYPE_LOCAL: PhoneNumberType
PHONE_NUMBER_TYPE_TOLL_FREE: PhoneNumberType

class SearchPhoneNumbersRequest(_message.Message):
    __slots__ = ("country_code", "area_code", "limit", "page_token")
    COUNTRY_CODE_FIELD_NUMBER: _ClassVar[int]
    AREA_CODE_FIELD_NUMBER: _ClassVar[int]
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    country_code: str
    area_code: str
    limit: int
    page_token: _livekit_models_pb2.TokenPagination
    def __init__(self, country_code: _Optional[str] = ..., area_code: _Optional[str] = ..., limit: _Optional[int] = ..., page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ...) -> None: ...

class SearchPhoneNumbersResponse(_message.Message):
    __slots__ = ("items", "next_page_token")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PhoneNumber]
    next_page_token: _livekit_models_pb2.TokenPagination
    def __init__(self, items: _Optional[_Iterable[_Union[PhoneNumber, _Mapping]]] = ..., next_page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ...) -> None: ...

class PurchasePhoneNumberRequest(_message.Message):
    __slots__ = ("phone_numbers", "sip_dispatch_rule_id")
    PHONE_NUMBERS_FIELD_NUMBER: _ClassVar[int]
    SIP_DISPATCH_RULE_ID_FIELD_NUMBER: _ClassVar[int]
    phone_numbers: _containers.RepeatedScalarFieldContainer[str]
    sip_dispatch_rule_id: str
    def __init__(self, phone_numbers: _Optional[_Iterable[str]] = ..., sip_dispatch_rule_id: _Optional[str] = ...) -> None: ...

class PurchasePhoneNumberResponse(_message.Message):
    __slots__ = ("phone_numbers",)
    PHONE_NUMBERS_FIELD_NUMBER: _ClassVar[int]
    phone_numbers: _containers.RepeatedCompositeFieldContainer[PhoneNumber]
    def __init__(self, phone_numbers: _Optional[_Iterable[_Union[PhoneNumber, _Mapping]]] = ...) -> None: ...

class ListPhoneNumbersRequest(_message.Message):
    __slots__ = ("limit", "statuses", "page_token", "sip_dispatch_rule_id")
    LIMIT_FIELD_NUMBER: _ClassVar[int]
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    SIP_DISPATCH_RULE_ID_FIELD_NUMBER: _ClassVar[int]
    limit: int
    statuses: _containers.RepeatedScalarFieldContainer[PhoneNumberStatus]
    page_token: _livekit_models_pb2.TokenPagination
    sip_dispatch_rule_id: str
    def __init__(self, limit: _Optional[int] = ..., statuses: _Optional[_Iterable[_Union[PhoneNumberStatus, str]]] = ..., page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ..., sip_dispatch_rule_id: _Optional[str] = ...) -> None: ...

class ListPhoneNumbersResponse(_message.Message):
    __slots__ = ("items", "next_page_token", "total_count")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    NEXT_PAGE_TOKEN_FIELD_NUMBER: _ClassVar[int]
    TOTAL_COUNT_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[PhoneNumber]
    next_page_token: _livekit_models_pb2.TokenPagination
    total_count: int
    def __init__(self, items: _Optional[_Iterable[_Union[PhoneNumber, _Mapping]]] = ..., next_page_token: _Optional[_Union[_livekit_models_pb2.TokenPagination, _Mapping]] = ..., total_count: _Optional[int] = ...) -> None: ...

class GetPhoneNumberRequest(_message.Message):
    __slots__ = ("id", "phone_number")
    ID_FIELD_NUMBER: _ClassVar[int]
    PHONE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    id: str
    phone_number: str
    def __init__(self, id: _Optional[str] = ..., phone_number: _Optional[str] = ...) -> None: ...

class GetPhoneNumberResponse(_message.Message):
    __slots__ = ("phone_number",)
    PHONE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    phone_number: PhoneNumber
    def __init__(self, phone_number: _Optional[_Union[PhoneNumber, _Mapping]] = ...) -> None: ...

class UpdatePhoneNumberRequest(_message.Message):
    __slots__ = ("id", "phone_number", "sip_dispatch_rule_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    PHONE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    SIP_DISPATCH_RULE_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    phone_number: str
    sip_dispatch_rule_id: str
    def __init__(self, id: _Optional[str] = ..., phone_number: _Optional[str] = ..., sip_dispatch_rule_id: _Optional[str] = ...) -> None: ...

class UpdatePhoneNumberResponse(_message.Message):
    __slots__ = ("phone_number",)
    PHONE_NUMBER_FIELD_NUMBER: _ClassVar[int]
    phone_number: PhoneNumber
    def __init__(self, phone_number: _Optional[_Union[PhoneNumber, _Mapping]] = ...) -> None: ...

class ReleasePhoneNumbersRequest(_message.Message):
    __slots__ = ("ids", "phone_numbers")
    IDS_FIELD_NUMBER: _ClassVar[int]
    PHONE_NUMBERS_FIELD_NUMBER: _ClassVar[int]
    ids: _containers.RepeatedScalarFieldContainer[str]
    phone_numbers: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, ids: _Optional[_Iterable[str]] = ..., phone_numbers: _Optional[_Iterable[str]] = ...) -> None: ...

class ReleasePhoneNumbersResponse(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class PhoneNumber(_message.Message):
    __slots__ = ("id", "e164_format", "country_code", "area_code", "number_type", "locality", "region", "spam_score", "created_at", "updated_at", "capabilities", "status", "assigned_at", "released_at", "sip_dispatch_rule_id")
    ID_FIELD_NUMBER: _ClassVar[int]
    E164_FORMAT_FIELD_NUMBER: _ClassVar[int]
    COUNTRY_CODE_FIELD_NUMBER: _ClassVar[int]
    AREA_CODE_FIELD_NUMBER: _ClassVar[int]
    NUMBER_TYPE_FIELD_NUMBER: _ClassVar[int]
    LOCALITY_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    SPAM_SCORE_FIELD_NUMBER: _ClassVar[int]
    CREATED_AT_FIELD_NUMBER: _ClassVar[int]
    UPDATED_AT_FIELD_NUMBER: _ClassVar[int]
    CAPABILITIES_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    ASSIGNED_AT_FIELD_NUMBER: _ClassVar[int]
    RELEASED_AT_FIELD_NUMBER: _ClassVar[int]
    SIP_DISPATCH_RULE_ID_FIELD_NUMBER: _ClassVar[int]
    id: str
    e164_format: str
    country_code: str
    area_code: str
    number_type: PhoneNumberType
    locality: str
    region: str
    spam_score: float
    created_at: _timestamp_pb2.Timestamp
    updated_at: _timestamp_pb2.Timestamp
    capabilities: _containers.RepeatedScalarFieldContainer[str]
    status: PhoneNumberStatus
    assigned_at: _timestamp_pb2.Timestamp
    released_at: _timestamp_pb2.Timestamp
    sip_dispatch_rule_id: str
    def __init__(self, id: _Optional[str] = ..., e164_format: _Optional[str] = ..., country_code: _Optional[str] = ..., area_code: _Optional[str] = ..., number_type: _Optional[_Union[PhoneNumberType, str]] = ..., locality: _Optional[str] = ..., region: _Optional[str] = ..., spam_score: _Optional[float] = ..., created_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., updated_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., capabilities: _Optional[_Iterable[str]] = ..., status: _Optional[_Union[PhoneNumberStatus, str]] = ..., assigned_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., released_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., sip_dispatch_rule_id: _Optional[str] = ...) -> None: ...
