from __future__ import annotations

import fcntl
import json
import math
import os
import stat
import threading
from collections import deque
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
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
class ObservabilityData:
    message: ObservabilityText | None = None
    detail: ObservabilityText | None = None
    progress: ObservabilityProgress | None = None
    artifact: ObservabilityArtifact | None = None
    storage: ObservabilityStorage | None = None
    failure: ObservabilityFailure | None = None
    cancellation: ObservabilityCancellation | None = None
    counters: ObservabilityCounters | None = None

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
        correlation_value = context_value["correlation"]
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
                correlation=_required_dataclass(ObservabilityCorrelation, correlation_value),
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


def _required_dataclass[T](kind: type[T], value: dict[str, Any]) -> T:
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


class CompositeEventSink:
    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = sinks
        self._lock = threading.Lock()
        self._failure_count = 0

    @property
    def failure_count(self) -> int:
        with self._lock:
            return self._failure_count

    def emit(self, event: ObservabilityEvent) -> None:
        failures = 0
        for sink in self._sinks:
            try:
                sink.emit(event)
            except Exception:
                failures += 1
        if failures:
            with self._lock:
                self._failure_count += failures


@dataclass(frozen=True)
class EventBufferSnapshot:
    events: tuple[ObservabilityEvent, ...]
    retained_bytes: int
    total_events: int
    dropped_events: int
    dropped_bytes: int
    failure_count: int


class BoundedEventSink:
    def __init__(self, maximum_events: int = 512, maximum_bytes: int = 384 * 1024) -> None:
        if maximum_events <= 0 or maximum_bytes <= 0:
            raise ValueError("event bounds must be positive")
        self._maximum_events = maximum_events
        self._maximum_bytes = maximum_bytes
        self._lock = threading.Lock()
        self._entries: deque[tuple[ObservabilityEvent, int]] = deque()
        self._retained_bytes = 0
        self._total_events = 0
        self._dropped_events = 0
        self._dropped_bytes = 0
        self._failure_count = 0

    def emit(self, event: ObservabilityEvent) -> None:
        try:
            size = len(event.to_json_line().encode("utf-8")) + 1
        except (TypeError, ValueError, UnicodeError):
            with self._lock:
                self._failure_count += 1
            return
        with self._lock:
            self._total_events += 1
            if size > self._maximum_bytes:
                self._dropped_events += 1
                self._dropped_bytes += size
                return
            self._entries.append((event, size))
            self._retained_bytes += size
            while len(self._entries) > self._maximum_events or self._retained_bytes > self._maximum_bytes:
                _, removed_size = self._entries.popleft()
                self._retained_bytes -= removed_size
                self._dropped_events += 1
                self._dropped_bytes += removed_size

    def snapshot(self) -> EventBufferSnapshot:
        with self._lock:
            return EventBufferSnapshot(
                events=tuple(event for event, _ in self._entries),
                retained_bytes=self._retained_bytes,
                total_events=self._total_events,
                dropped_events=self._dropped_events,
                dropped_bytes=self._dropped_bytes,
                failure_count=self._failure_count,
            )


@dataclass(frozen=True)
class JSONLSinkSnapshot:
    written_events: int
    dropped_events: int
    dropped_bytes: int
    failure_count: int


class RotatingJSONLEventSink:
    def __init__(self, path: Path, *, maximum_bytes: int = 4 * 1024 * 1024, backups: int = 2) -> None:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        if backups < 0:
            raise ValueError("backups must be non-negative")
        self._path = path.absolute()
        self._lock_name = f".{self._path.name}.lock"
        self._maximum_bytes = maximum_bytes
        self._backups = backups
        self._lock = threading.Lock()
        self._written_events = 0
        self._dropped_events = 0
        self._dropped_bytes = 0
        self._failure_count = 0

    def emit(self, event: ObservabilityEvent) -> None:
        with self._lock:
            try:
                payload = (event.to_json_line() + "\n").encode("utf-8")
                if len(payload) > self._maximum_bytes:
                    self._dropped_events += 1
                    self._dropped_bytes += len(payload)
                    return
                directory_descriptor = self._open_private_directory()
                try:
                    lock_descriptor = self._open_private_file(
                        self._lock_name,
                        os.O_CREAT | os.O_RDWR,
                        directory_descriptor,
                    )
                    locked = False
                    try:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                        locked = True
                        self._write_payload(payload, directory_descriptor)
                    finally:
                        if locked:
                            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                        os.close(lock_descriptor)
                finally:
                    os.close(directory_descriptor)
                self._written_events += 1
            except (OSError, TypeError, ValueError, UnicodeError):
                self._failure_count += 1

    def snapshot(self) -> JSONLSinkSnapshot:
        with self._lock:
            return JSONLSinkSnapshot(
                written_events=self._written_events,
                dropped_events=self._dropped_events,
                dropped_bytes=self._dropped_bytes,
                failure_count=self._failure_count,
            )

    def _rotate(self, directory_descriptor: int) -> None:
        if self._backups == 0:
            self._unlink_if_exists(self._path.name, directory_descriptor)
            return
        oldest = self._backup_name(self._backups)
        self._validate_existing_regular_file(oldest, directory_descriptor)
        self._unlink_if_exists(oldest, directory_descriptor)
        for index in range(self._backups - 1, 0, -1):
            source = self._backup_name(index)
            if self._path_exists(source, directory_descriptor):
                self._validate_existing_regular_file(source, directory_descriptor)
                os.replace(
                    source,
                    self._backup_name(index + 1),
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
        if self._path_exists(self._path.name, directory_descriptor):
            self._validate_existing_regular_file(self._path.name, directory_descriptor)
            os.replace(
                self._path.name,
                self._backup_name(1),
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )

    def _backup_name(self, index: int) -> str:
        return f"{self._path.name}.{index}"

    def _open_private_directory(self) -> int:
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self._path.anchor, directory_flags)
        try:
            for component in self._path.parent.parts[1:]:
                try:
                    next_descriptor = os.open(component, directory_flags | no_follow, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    next_descriptor = os.open(component, directory_flags | no_follow, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        except BaseException:
            os.close(descriptor)
            raise
        directory_status = os.fstat(descriptor)
        if not stat.S_ISDIR(directory_status.st_mode):
            os.close(descriptor)
            raise NotADirectoryError(self._path.parent)
        if stat.S_IMODE(directory_status.st_mode) & 0o077:
            os.close(descriptor)
            raise PermissionError("observability log directory must not be accessible by group or other users")
        return descriptor

    def _open_private_file(self, name: str, flags: int, directory_descriptor: int) -> int:
        no_follow = getattr(os, "O_NOFOLLOW", 0)
        close_on_exec = getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(name, flags | no_follow | close_on_exec, 0o600, dir_fd=directory_descriptor)
        try:
            file_status = os.fstat(descriptor)
            if not stat.S_ISREG(file_status.st_mode):
                raise OSError(f"observability sink path is not a regular file: {name}")
            if file_status.st_nlink != 1:
                raise OSError(f"observability sink path must not have hard links: {name}")
            os.fchmod(descriptor, 0o600)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _write_payload(self, payload: bytes, directory_descriptor: int) -> None:
        descriptor = self._open_private_file(
            self._path.name,
            os.O_APPEND | os.O_CREAT | os.O_WRONLY,
            directory_descriptor,
        )
        original_size = os.fstat(descriptor).st_size
        if original_size and original_size + len(payload) > self._maximum_bytes:
            os.close(descriptor)
            self._rotate(directory_descriptor)
            descriptor = self._open_private_file(
                self._path.name,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                directory_descriptor,
            )
            original_size = 0
        rollback_failed = False
        try:
            remaining = memoryview(payload)
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise OSError("observability sink made no write progress")
                remaining = remaining[written:]
        except BaseException:
            try:
                os.ftruncate(descriptor, original_size)
            except OSError:
                rollback_failed = True
            raise
        finally:
            os.close(descriptor)
            if rollback_failed:
                self._unlink_if_exists(self._path.name, directory_descriptor)

    @staticmethod
    def _validate_existing_regular_file(name: str, directory_descriptor: int) -> None:
        try:
            file_status = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(file_status.st_mode):
            raise OSError(f"observability sink path is not a regular file: {name}")
        if file_status.st_nlink != 1:
            raise OSError(f"observability sink path must not have hard links: {name}")
        if stat.S_IMODE(file_status.st_mode) & 0o077:
            raise PermissionError(f"observability sink path is not private: {name}")

    @staticmethod
    def _path_exists(name: str, directory_descriptor: int) -> bool:
        try:
            os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    @staticmethod
    def _unlink_if_exists(name: str, directory_descriptor: int) -> None:
        try:
            os.unlink(name, dir_fd=directory_descriptor)
        except FileNotFoundError:
            pass
