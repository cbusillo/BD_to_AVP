from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, cast

OBSERVABILITY_SCHEMA = "bd_to_avp.observability"
OBSERVABILITY_SCHEMA_VERSION = 1
MAX_IDENTIFIER_BYTES = 128
MAX_MESSAGE_BYTES = 4 * 1024
MAX_DETAIL_BYTES = 64 * 1024
MAX_INT32 = (1 << 31) - 1
MIN_INT32 = -(1 << 31)
MAX_INT64 = (1 << 63) - 1


class ObservabilityEmitter(StrEnum):
    APP = "app"
    WORKER = "worker"


class ObservabilitySeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ObservabilityPrivacy(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"


class ObservabilityRedaction(StrEnum):
    RAW = "raw"
    REDACTED = "redacted"
    OMITTED = "omitted"


def bounded_utf8(value: str, maximum_bytes: int) -> tuple[str, bool]:
    if maximum_bytes < 0:
        raise ValueError("maximum_bytes must be non-negative")
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("value must contain valid Unicode") from error
    if len(encoded) <= maximum_bytes:
        return value, False
    marker = "…"
    marker_bytes = marker.encode("utf-8")
    if maximum_bytes < len(marker_bytes):
        return "", True
    prefix = encoded[: maximum_bytes - len(marker_bytes)]
    while prefix:
        try:
            return prefix.decode("utf-8") + marker, True
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    return marker, True


def _validate_identifier(value: str | None, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must not be empty")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must contain valid Unicode") from error
    if len(encoded) > MAX_IDENTIFIER_BYTES:
        raise ValueError(f"{name} exceeds {MAX_IDENTIFIER_BYTES} UTF-8 bytes")


def _validate_int(
    value: int | None,
    name: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_INT64,
) -> None:
    if value is None:
        return
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_finite_number(value: float | None, name: str, *, positive: bool = False) -> None:
    if value is None:
        return
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be a number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise ValueError(f"{name} exceeds the supported floating-point range") from error
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive")


@dataclass(frozen=True)
class ObservabilityText:
    value: str
    privacy: ObservabilityPrivacy = ObservabilityPrivacy.PRIVATE
    truncated: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.privacy, ObservabilityPrivacy):
            raise TypeError("text privacy must be an ObservabilityPrivacy value")
        if type(self.truncated) is not bool:
            raise TypeError("text truncated must be a boolean")
        if self.privacy is ObservabilityPrivacy.SECRET:
            raise ValueError("secret text must be omitted rather than recorded")
        bounded, truncated = bounded_utf8(self.value, MAX_DETAIL_BYTES)
        if truncated or bounded != self.value:
            raise ValueError(f"text exceeds {MAX_DETAIL_BYTES} UTF-8 bytes")

    @classmethod
    def bounded(
        cls,
        value: str,
        *,
        privacy: ObservabilityPrivacy = ObservabilityPrivacy.PRIVATE,
        maximum_bytes: int = MAX_MESSAGE_BYTES,
    ) -> ObservabilityText:
        bounded, truncated = bounded_utf8(value, maximum_bytes)
        return cls(value=bounded, privacy=privacy, truncated=truncated)


@dataclass(frozen=True)
class ObservabilityCorrelation:
    job_id: str | None = None
    parent_job_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.job_id, "job_id")
        _validate_identifier(self.parent_job_id, "parent_job_id")


@dataclass(frozen=True)
class ObservabilityStage:
    id: str
    index: int | None = None
    count: int | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.id, "stage.id")
        _validate_int(self.index, "stage.index", minimum=1, maximum=MAX_INT32)
        _validate_int(self.count, "stage.count", minimum=1, maximum=MAX_INT32)
        if self.index is not None and self.count is not None and self.index > self.count:
            raise ValueError("stage.index must not exceed stage.count")


@dataclass(frozen=True)
class ObservabilityTool:
    id: str
    run_id: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.id, "tool.id")
        _validate_identifier(self.run_id, "tool.run_id")
        _validate_identifier(self.version, "tool.version")


@dataclass(frozen=True)
class ObservabilityProcess:
    pid: int | None = None
    process_group_id: int | None = None
    exit_code: int | None = None
    signal: int | None = None

    def __post_init__(self) -> None:
        _validate_int(self.pid, "process.pid", minimum=1, maximum=MAX_INT32)
        _validate_int(self.process_group_id, "process.process_group_id", minimum=1, maximum=MAX_INT32)
        _validate_int(self.exit_code, "process.exit_code", minimum=MIN_INT32, maximum=MAX_INT32)
        _validate_int(self.signal, "process.signal", minimum=1, maximum=MAX_INT32)


@dataclass(frozen=True)
class ObservabilityContext:
    correlation: ObservabilityCorrelation = ObservabilityCorrelation()
    stage: ObservabilityStage | None = None
    tool: ObservabilityTool | None = None
    process: ObservabilityProcess | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.correlation, ObservabilityCorrelation):
            raise TypeError("context.correlation must be an ObservabilityCorrelation value")
        for name, value, expected_type in (
            ("context.stage", self.stage, ObservabilityStage),
            ("context.tool", self.tool, ObservabilityTool),
            ("context.process", self.process, ObservabilityProcess),
        ):
            if value is not None and not isinstance(value, expected_type):
                raise TypeError(f"{name} has an invalid type")


@dataclass(frozen=True)
class ObservabilityProgress:
    fraction: float | None = None
    completed_units: float | None = None
    total_units: float | None = None
    unit: str | None = None

    def __post_init__(self) -> None:
        _validate_finite_number(self.fraction, "progress.fraction")
        _validate_finite_number(self.completed_units, "progress.completed_units")
        _validate_finite_number(self.total_units, "progress.total_units", positive=True)
        if self.fraction is not None and not 0 <= self.fraction <= 1:
            raise ValueError("progress.fraction must be between zero and one")
        if self.completed_units is not None and self.completed_units < 0:
            raise ValueError("progress.completed_units must be non-negative")
        _validate_identifier(self.unit, "progress.unit")


@dataclass(frozen=True)
class ObservabilityArtifact:
    role: str
    state: str | None = None
    location: ObservabilityText | None = None
    size_bytes: int | None = None
    modification_age_seconds: int | None = None
    growth_bytes_per_second: int | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.role, "artifact.role")
        _validate_identifier(self.state, "artifact.state")
        if self.location is not None and not isinstance(self.location, ObservabilityText):
            raise TypeError("artifact.location must be an ObservabilityText value")
        _validate_int(self.size_bytes, "artifact.size_bytes")
        _validate_int(self.modification_age_seconds, "artifact.modification_age_seconds")
        _validate_int(self.growth_bytes_per_second, "artifact.growth_bytes_per_second")


@dataclass(frozen=True)
class ObservabilityStorage:
    role: str
    status: str
    location: ObservabilityText | None = None
    size_bytes: int | None = None
    modification_age_seconds: int | None = None
    available_bytes: int | None = None
    total_bytes: int | None = None
    read_only: bool | None = None
    writable: bool | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.role, "storage.role")
        _validate_identifier(self.status, "storage.status")
        if self.location is not None and not isinstance(self.location, ObservabilityText):
            raise TypeError("storage.location must be an ObservabilityText value")
        _validate_int(self.size_bytes, "storage.size_bytes")
        _validate_int(self.modification_age_seconds, "storage.modification_age_seconds")
        _validate_int(self.available_bytes, "storage.available_bytes")
        _validate_int(self.total_bytes, "storage.total_bytes")
        if self.read_only is not None and type(self.read_only) is not bool:
            raise TypeError("storage.read_only must be a boolean")
        if self.writable is not None and type(self.writable) is not bool:
            raise TypeError("storage.writable must be a boolean")


@dataclass(frozen=True)
class ObservabilityFailure:
    code: str
    retryable: bool | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.code, "failure.code")
        if self.retryable is not None and type(self.retryable) is not bool:
            raise TypeError("failure.retryable must be a boolean")


@dataclass(frozen=True)
class ObservabilityCancellation:
    requested: bool
    forced: bool | None = None

    def __post_init__(self) -> None:
        if type(self.requested) is not bool:
            raise TypeError("cancellation.requested must be a boolean")
        if self.forced is not None and type(self.forced) is not bool:
            raise TypeError("cancellation.forced must be a boolean")


@dataclass(frozen=True)
class ObservabilityCounters:
    total_bytes: int | None = None
    retained_bytes: int | None = None
    dropped_bytes: int | None = None
    decode_replacements: int | None = None

    def __post_init__(self) -> None:
        _validate_int(self.total_bytes, "counters.total_bytes")
        _validate_int(self.retained_bytes, "counters.retained_bytes")
        _validate_int(self.dropped_bytes, "counters.dropped_bytes")
        _validate_int(self.decode_replacements, "counters.decode_replacements")


@dataclass(frozen=True)
class ObservabilityActivity:
    last_output_age_seconds: int | None = None

    def __post_init__(self) -> None:
        _validate_int(self.last_output_age_seconds, "activity.last_output_age_seconds")


@dataclass(frozen=True)
class ObservabilityData:
    message: ObservabilityText | None = None
    detail: ObservabilityText | None = None
    progress: ObservabilityProgress | None = None
    artifact: ObservabilityArtifact | None = None
    storage: ObservabilityStorage | None = None
    failure: ObservabilityFailure | None = None
    cancellation: ObservabilityCancellation | None = None
    counters: ObservabilityCounters | None = None
    activity: ObservabilityActivity | None = None

    def __post_init__(self) -> None:
        for name, value, expected_type in (
            ("data.message", self.message, ObservabilityText),
            ("data.detail", self.detail, ObservabilityText),
            ("data.progress", self.progress, ObservabilityProgress),
            ("data.artifact", self.artifact, ObservabilityArtifact),
            ("data.storage", self.storage, ObservabilityStorage),
            ("data.failure", self.failure, ObservabilityFailure),
            ("data.cancellation", self.cancellation, ObservabilityCancellation),
            ("data.counters", self.counters, ObservabilityCounters),
            ("data.activity", self.activity, ObservabilityActivity),
        ):
            if value is not None and not isinstance(value, expected_type):
                raise TypeError(f"{name} has an invalid type")


@dataclass(frozen=True)
class ObservabilityEvent:
    emitter: ObservabilityEmitter
    stream_id: str
    sequence: int
    occurred_at: datetime
    kind: str
    severity: ObservabilitySeverity
    privacy: ObservabilityPrivacy
    redaction: ObservabilityRedaction
    context: ObservabilityContext
    data: ObservabilityData
    elapsed_ms: int | None = None
    schema: str = OBSERVABILITY_SCHEMA
    schema_version: int = OBSERVABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.emitter, ObservabilityEmitter):
            raise TypeError("emitter must be an ObservabilityEmitter value")
        if not isinstance(self.severity, ObservabilitySeverity):
            raise TypeError("severity must be an ObservabilitySeverity value")
        if not isinstance(self.privacy, ObservabilityPrivacy):
            raise TypeError("privacy must be an ObservabilityPrivacy value")
        if not isinstance(self.redaction, ObservabilityRedaction):
            raise TypeError("redaction must be an ObservabilityRedaction value")
        if not isinstance(self.schema, str) or self.schema != OBSERVABILITY_SCHEMA:
            raise ValueError(f"unsupported observability schema: {self.schema}")
        if type(self.schema_version) is not int or self.schema_version != OBSERVABILITY_SCHEMA_VERSION:
            raise ValueError(f"unsupported observability schema version: {self.schema_version}")
        _validate_identifier(self.stream_id, "stream_id")
        _validate_identifier(self.kind, "kind")
        _validate_int(self.sequence, "sequence")
        _validate_int(self.elapsed_ms, "elapsed_ms")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        if not isinstance(self.context, ObservabilityContext):
            raise TypeError("context must be an ObservabilityContext value")
        if not isinstance(self.data, ObservabilityData):
            raise TypeError("data must be an ObservabilityData value")
        if self.privacy is ObservabilityPrivacy.SECRET:
            raise ValueError("secret events must be omitted rather than recorded")
        if self.data.message is not None and len(self.data.message.value.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} UTF-8 bytes")

    def to_dict(self) -> dict[str, Any]:
        return _json_value(self)

    def to_json_line(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ObservabilityEvent:
        context_value = value.get("context") or {}
        data_value = value.get("data") or {}
        return cls(
            schema=value.get("schema", ""),
            schema_version=value.get("schema_version", 0),
            emitter=ObservabilityEmitter(value["emitter"]),
            stream_id=value["stream_id"],
            sequence=value["sequence"],
            occurred_at=_parse_timestamp(value["occurred_at"]),
            elapsed_ms=value.get("elapsed_ms"),
            kind=value["kind"],
            severity=ObservabilitySeverity(value["severity"]),
            privacy=ObservabilityPrivacy(value["privacy"]),
            redaction=ObservabilityRedaction(value["redaction"]),
            context=ObservabilityContext(
                correlation=_required_dataclass(ObservabilityCorrelation, context_value.get("correlation")),
                stage=_optional_dataclass(ObservabilityStage, context_value.get("stage")),
                tool=_optional_dataclass(ObservabilityTool, context_value.get("tool")),
                process=_optional_dataclass(ObservabilityProcess, context_value.get("process")),
            ),
            data=ObservabilityData(
                message=_optional_text(data_value.get("message")),
                detail=_optional_text(data_value.get("detail")),
                progress=_optional_dataclass(ObservabilityProgress, data_value.get("progress")),
                artifact=_optional_artifact(data_value.get("artifact")),
                storage=_optional_storage(data_value.get("storage")),
                failure=_optional_dataclass(ObservabilityFailure, data_value.get("failure")),
                cancellation=_optional_dataclass(ObservabilityCancellation, data_value.get("cancellation")),
                counters=_optional_dataclass(ObservabilityCounters, data_value.get("counters")),
                activity=_optional_dataclass(ObservabilityActivity, data_value.get("activity")),
            ),
        )


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _json_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return _timestamp(value)
    if is_dataclass(value) and not isinstance(value, type):
        result: dict[str, Any] = {}
        for field in fields(value):
            encoded = _json_value(getattr(value, field.name))
            if encoded is not None:
                result[field.name] = encoded
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _optional_dataclass[T](kind: type[T], value: dict[str, Any] | None) -> T | None:
    if value is None:
        return None
    allowed_fields = {field.name for field in fields(cast(Any, kind))}
    return kind(**{name: field_value for name, field_value in value.items() if name in allowed_fields})


def _required_dataclass[T](kind: type[T], value: dict[str, Any] | None) -> T:
    result = _optional_dataclass(kind, value)
    if result is None:
        raise ValueError("required observability object is missing")
    return result


def _optional_text(value: dict[str, Any] | None) -> ObservabilityText | None:
    if value is None:
        return None
    return ObservabilityText(
        value=value["value"],
        privacy=ObservabilityPrivacy(value["privacy"]),
        truncated=value.get("truncated", False),
    )


def _optional_artifact(value: dict[str, Any] | None) -> ObservabilityArtifact | None:
    if value is None:
        return None
    return ObservabilityArtifact(
        role=value["role"],
        state=value.get("state"),
        location=_optional_text(value.get("location")),
        size_bytes=value.get("size_bytes"),
        modification_age_seconds=value.get("modification_age_seconds"),
        growth_bytes_per_second=value.get("growth_bytes_per_second"),
    )


def _optional_storage(value: dict[str, Any] | None) -> ObservabilityStorage | None:
    if value is None:
        return None
    return ObservabilityStorage(
        role=value["role"],
        status=value["status"],
        location=_optional_text(value.get("location")),
        size_bytes=value.get("size_bytes"),
        modification_age_seconds=value.get("modification_age_seconds"),
        available_bytes=value.get("available_bytes"),
        total_bytes=value.get("total_bytes"),
        read_only=value.get("read_only"),
        writable=value.get("writable"),
    )


class EventSink(Protocol):
    def emit(self, event: ObservabilityEvent) -> None:
        raise NotImplementedError


class NullEventSink:
    def emit(self, event: ObservabilityEvent) -> None:
        pass
