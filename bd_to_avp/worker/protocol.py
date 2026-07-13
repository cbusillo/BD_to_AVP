from __future__ import annotations

import json
import threading

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, TextIO
from uuid import UUID

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_BYTES = 1024 * 1024
MAX_DETAIL_BYTES = 64 * 1024
ZERO_JOB_ID = str(UUID(int=0))


def bounded_detail(value: str) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= MAX_DETAIL_BYTES:
        return value
    suffix = "\n… details truncated"
    available = MAX_DETAIL_BYTES - len(suffix.encode("utf-8"))
    return encoded[:available].decode("utf-8", errors="ignore") + suffix


class WorkerOperation(StrEnum):
    INSPECT_SOURCE = "inspect_source"
    CONVERT_SOURCE = "convert_source"


class WorkerSourceKind(StrEnum):
    DIRECT_FILE = "direct_file"
    DISC_IMAGE = "disc_image"
    BLU_RAY_FOLDER = "blu_ray_folder"


class WorkerEventType(StrEnum):
    WORKER_READY = "worker.ready"
    JOB_STARTED = "job.started"
    STAGE_STARTED = "stage.started"
    HEARTBEAT = "heartbeat"
    LOG = "log"
    WARNING = "warning"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_CANCELLED = "job.cancelled"
    JOB_DECISION_REQUIRED = "job.decision_required"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.JOB_COMPLETED,
            self.JOB_FAILED,
            self.JOB_CANCELLED,
            self.JOB_DECISION_REQUIRED,
        }


class WorkerProtocolError(ValueError):
    def __init__(self, code: str, message: str, *, job_id: str = ZERO_JOB_ID) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.job_id = job_id


@dataclass(frozen=True)
class JobSource:
    kind: WorkerSourceKind
    path: Path


@dataclass(frozen=True)
class JobDestination:
    path: Path


@dataclass(frozen=True)
class EncodingOptions:
    transcode_audio: bool
    audio_bitrate: int
    left_right_bitrate: int
    link_quality: bool
    mv_hevc_quality: int
    upscale_quality: int
    fov: int
    frame_rate: str
    resolution: str
    skip_subtitles: bool
    crop_black_bars: bool
    swap_eyes: bool
    fx_upscale: bool
    language_code: str
    remove_extra_languages: bool


@dataclass(frozen=True)
class JobOptions:
    start_stage: int
    keep_files: bool
    overwrite: bool
    remove_original: bool
    continue_on_error: bool
    software_encoder: bool
    output_commands: bool
    keep_awake: bool
    output_length: str


@dataclass(frozen=True)
class JobSpec:
    protocol_version: int
    job_id: str
    operation: WorkerOperation
    source: JobSource
    destination: JobDestination | None = None
    encoding: EncodingOptions | None = None
    job: JobOptions | None = None

    @classmethod
    def from_json_line(cls, line: str) -> "JobSpec":
        if not line.strip():
            raise WorkerProtocolError("empty_request", "The worker request was empty.")
        if len(line.encode("utf-8")) > MAX_REQUEST_BYTES:
            raise WorkerProtocolError("request_too_large", "The worker request exceeded the size limit.")

        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise WorkerProtocolError("invalid_json", "The worker request was not valid JSON.") from error

        if not isinstance(raw, Mapping):
            raise WorkerProtocolError("invalid_request", "The worker request must be a JSON object.")

        job_id = cls._parse_job_id(raw.get("job_id"))
        base_keys = {"protocol_version", "type", "job_id", "operation", "source"}
        protocol_version = raw.get("protocol_version")
        if (
            not isinstance(protocol_version, int)
            or isinstance(protocol_version, bool)
            or protocol_version != PROTOCOL_VERSION
        ):
            raise WorkerProtocolError(
                "protocol_mismatch",
                f"Unsupported worker protocol version: {protocol_version!r}.",
                job_id=job_id,
            )
        if raw.get("type") != "job.start":
            raise WorkerProtocolError(
                "invalid_request_type",
                "The worker request type must be 'job.start'.",
                job_id=job_id,
            )

        raw_operation = raw.get("operation")
        if not isinstance(raw_operation, str):
            raise WorkerProtocolError(
                "unsupported_operation",
                f"Unsupported worker operation: {raw_operation!r}.",
                job_id=job_id,
            )
        try:
            operation = WorkerOperation(raw_operation)
        except ValueError as error:
            raise WorkerProtocolError(
                "unsupported_operation",
                f"Unsupported worker operation: {raw_operation!r}.",
                job_id=job_id,
            ) from error

        operation_keys = {
            WorkerOperation.INSPECT_SOURCE: base_keys,
            WorkerOperation.CONVERT_SOURCE: base_keys | {"destination", "encoding", "job"},
        }[operation]
        cls._reject_unknown_keys(raw, operation_keys, "request", job_id)

        source = raw.get("source")
        if not isinstance(source, Mapping):
            raise WorkerProtocolError(
                "invalid_source",
                "The request must contain a source kind and path.",
                job_id=job_id,
            )
        cls._require_exact_keys(source, {"kind", "path"}, "source", job_id)
        raw_source_kind = source.get("kind")
        if not isinstance(raw_source_kind, str):
            raise WorkerProtocolError(
                "invalid_source",
                "source.kind must be a string.",
                job_id=job_id,
            )
        try:
            source_kind = WorkerSourceKind(raw_source_kind)
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_source",
                f"Unsupported source kind: {raw_source_kind!r}.",
                job_id=job_id,
            ) from error
        raw_source_path = source.get("path")
        if not isinstance(raw_source_path, str):
            raise WorkerProtocolError(
                "invalid_source",
                "source.path must be a string.",
                job_id=job_id,
            )
        source_path = cls._parse_absolute_path(raw_source_path, "source", job_id)

        destination: JobDestination | None = None
        encoding: EncodingOptions | None = None
        job_options: JobOptions | None = None
        if operation is WorkerOperation.CONVERT_SOURCE:
            destination = JobDestination(
                path=cls._parse_destination(raw.get("destination"), job_id),
            )
            encoding = cls._parse_encoding(raw.get("encoding"), job_id)
            job_options = cls._parse_job_options(raw.get("job"), job_id)

        return cls(
            protocol_version=protocol_version,
            job_id=job_id,
            operation=operation,
            source=JobSource(kind=source_kind, path=source_path),
            destination=destination,
            encoding=encoding,
            job=job_options,
        )

    @staticmethod
    def _parse_job_id(value: Any) -> str:
        if not isinstance(value, str):
            raise WorkerProtocolError("invalid_job_id", "The request must contain a UUID job_id.")
        try:
            return str(UUID(value))
        except ValueError as error:
            raise WorkerProtocolError("invalid_job_id", "The request job_id must be a UUID.") from error

    @classmethod
    def _parse_destination(cls, value: Any, job_id: str) -> Path:
        if not isinstance(value, Mapping) or not isinstance(value.get("path"), str):
            raise WorkerProtocolError(
                "invalid_destination",
                "The conversion request must contain a destination path.",
                job_id=job_id,
            )
        cls._reject_unknown_keys(value, {"path"}, "destination", job_id)
        return cls._parse_absolute_path(value["path"], "destination", job_id)

    @staticmethod
    def _parse_absolute_path(value: str, label: str, job_id: str) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise WorkerProtocolError(
                f"{label}_not_absolute",
                f"The {label} path must be absolute.",
                job_id=job_id,
            )
        return path

    @classmethod
    def _parse_encoding(cls, value: Any, job_id: str) -> EncodingOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "The conversion request must contain encoding options.",
                job_id=job_id,
            )
        required_keys = {
            "transcode_audio",
            "audio_bitrate",
            "left_right_bitrate",
            "link_quality",
            "mv_hevc_quality",
            "upscale_quality",
            "fov",
            "frame_rate",
            "resolution",
            "skip_subtitles",
            "crop_black_bars",
            "swap_eyes",
            "fx_upscale",
            "language_code",
            "remove_extra_languages",
        }
        cls._require_exact_keys(value, required_keys, "encoding", job_id)
        language_code = cls._parse_string(value, "language_code", "encoding", job_id)
        if len(language_code) != 3 or not language_code.isalpha() or language_code != language_code.lower():
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.language_code must be a lowercase ISO 639-2 code.",
                job_id=job_id,
            )
        return EncodingOptions(
            transcode_audio=cls._parse_bool(value, "transcode_audio", "encoding", job_id),
            audio_bitrate=cls._parse_int(value, "audio_bitrate", "encoding", job_id, minimum=1, maximum=4096),
            left_right_bitrate=cls._parse_int(value, "left_right_bitrate", "encoding", job_id, minimum=1, maximum=500),
            link_quality=cls._parse_bool(value, "link_quality", "encoding", job_id),
            mv_hevc_quality=cls._parse_int(value, "mv_hevc_quality", "encoding", job_id, minimum=0, maximum=100),
            upscale_quality=cls._parse_int(value, "upscale_quality", "encoding", job_id, minimum=0, maximum=100),
            fov=cls._parse_int(value, "fov", "encoding", job_id, minimum=0, maximum=360),
            frame_rate=cls._parse_string(value, "frame_rate", "encoding", job_id),
            resolution=cls._parse_string(value, "resolution", "encoding", job_id),
            skip_subtitles=cls._parse_bool(value, "skip_subtitles", "encoding", job_id),
            crop_black_bars=cls._parse_bool(value, "crop_black_bars", "encoding", job_id),
            swap_eyes=cls._parse_bool(value, "swap_eyes", "encoding", job_id),
            fx_upscale=cls._parse_bool(value, "fx_upscale", "encoding", job_id),
            language_code=language_code,
            remove_extra_languages=cls._parse_bool(value, "remove_extra_languages", "encoding", job_id),
        )

    @classmethod
    def _parse_job_options(cls, value: Any, job_id: str) -> JobOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_job_options",
                "The conversion request must contain job options.",
                job_id=job_id,
            )
        required_keys = {
            "start_stage",
            "keep_files",
            "overwrite",
            "remove_original",
            "continue_on_error",
            "software_encoder",
            "output_commands",
            "keep_awake",
            "output_length",
        }
        cls._require_exact_keys(value, required_keys, "job", job_id)
        output_length = cls._parse_string(value, "output_length", "job", job_id)
        if output_length != "full_movie":
            raise WorkerProtocolError(
                "invalid_job_options",
                "job.output_length must be 'full_movie'.",
                job_id=job_id,
            )
        return JobOptions(
            start_stage=cls._parse_int(value, "start_stage", "job", job_id, minimum=1, maximum=9),
            keep_files=cls._parse_bool(value, "keep_files", "job", job_id),
            overwrite=cls._parse_bool(value, "overwrite", "job", job_id),
            remove_original=cls._parse_bool(value, "remove_original", "job", job_id),
            continue_on_error=cls._parse_bool(value, "continue_on_error", "job", job_id),
            software_encoder=cls._parse_bool(value, "software_encoder", "job", job_id),
            output_commands=cls._parse_bool(value, "output_commands", "job", job_id),
            keep_awake=cls._parse_bool(value, "keep_awake", "job", job_id),
            output_length=output_length,
        )

    @classmethod
    def _require_exact_keys(
        cls,
        value: Mapping[str, Any],
        expected_keys: set[str],
        label: str,
        job_id: str,
    ) -> None:
        missing_keys = expected_keys - set(value.keys())
        if missing_keys:
            missing = ", ".join(sorted(missing_keys))
            raise WorkerProtocolError(
                f"invalid_{label}_options" if label in {"encoding", "job"} else f"invalid_{label}",
                f"The {label} object is missing required field(s): {missing}.",
                job_id=job_id,
            )
        cls._reject_unknown_keys(value, expected_keys, label, job_id)

    @staticmethod
    def _reject_unknown_keys(
        value: Mapping[str, Any],
        expected_keys: set[str],
        label: str,
        job_id: str,
    ) -> None:
        unknown_keys = set(value.keys()) - expected_keys
        if unknown_keys:
            unknown = ", ".join(sorted(str(key) for key in unknown_keys))
            raise WorkerProtocolError(
                "invalid_request",
                f"The {label} object contains unsupported field(s): {unknown}.",
                job_id=job_id,
            )

    @staticmethod
    def _parse_bool(value: Mapping[str, Any], key: str, label: str, job_id: str) -> bool:
        field_value = value.get(key)
        if not isinstance(field_value, bool):
            raise WorkerProtocolError(
                f"invalid_{label}_options",
                f"{label}.{key} must be a boolean.",
                job_id=job_id,
            )
        return field_value

    @staticmethod
    def _parse_int(
        value: Mapping[str, Any],
        key: str,
        label: str,
        job_id: str,
        *,
        minimum: int,
        maximum: int,
    ) -> int:
        field_value = value.get(key)
        if not isinstance(field_value, int) or isinstance(field_value, bool):
            raise WorkerProtocolError(
                f"invalid_{label}_options",
                f"{label}.{key} must be an integer.",
                job_id=job_id,
            )
        if not minimum <= field_value <= maximum:
            raise WorkerProtocolError(
                f"invalid_{label}_options",
                f"{label}.{key} must be between {minimum} and {maximum}.",
                job_id=job_id,
            )
        return field_value

    @staticmethod
    def _parse_string(value: Mapping[str, Any], key: str, label: str, job_id: str) -> str:
        field_value = value.get(key)
        if not isinstance(field_value, str):
            raise WorkerProtocolError(
                f"invalid_{label}_options",
                f"{label}.{key} must be a string.",
                job_id=job_id,
            )
        return field_value


class WorkerActivityReporter:
    def __init__(self, emitter: "WorkerEventEmitter") -> None:
        self._emitter = emitter
        self._current_stage: str | None = None
        self._lock = threading.Lock()

    @property
    def current_stage(self) -> str | None:
        with self._lock:
            return self._current_stage

    def stage_started(self, stage: str, message: str) -> None:
        with self._lock:
            self._current_stage = stage
        self._emitter.emit(WorkerEventType.STAGE_STARTED, {"stage": stage, "message": message})

    def log(self, message: str, *, stage: str | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"message": message, **fields}
        payload["stage"] = stage or self.current_stage
        self._emitter.emit(WorkerEventType.LOG, payload)

    def warning(self, message: str, *, stage: str | None = None, **fields: Any) -> None:
        payload: dict[str, Any] = {"message": message, **fields}
        payload["stage"] = stage or self.current_stage
        self._emitter.emit(WorkerEventType.WARNING, payload)

    def heartbeat_payload(self, elapsed_seconds: int) -> dict[str, Any]:
        return {
            "stage": self.current_stage,
            "elapsed_seconds": max(0, elapsed_seconds),
        }


class WorkerEventEmitter:
    def __init__(self, output: TextIO, job_id: str) -> None:
        self._output = output
        self._job_id = job_id
        self._sequence = -1
        self._terminal_emitted = False
        self._lock = threading.Lock()

    @property
    def terminal_emitted(self) -> bool:
        with self._lock:
            return self._terminal_emitted

    def emit(self, event_type: WorkerEventType, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._terminal_emitted:
                raise RuntimeError("Cannot emit an event after a terminal worker event.")

            next_sequence = self._sequence + 1
            event = {
                "protocol_version": PROTOCOL_VERSION,
                "type": event_type.value,
                "job_id": self._job_id,
                "sequence": next_sequence,
                "payload": dict(payload or {}),
            }
            encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_EVENT_BYTES:
                raise RuntimeError("Worker event exceeded the size limit.")

            self._output.write(encoded + "\n")
            self._output.flush()
            self._sequence = next_sequence
            if event_type.is_terminal:
                self._terminal_emitted = True
            return event

    def fail(
        self,
        code: str,
        message: str,
        *,
        details: str | None = None,
        retryable: bool = False,
    ) -> None:
        error: dict[str, Any] = {
            "code": code,
            "message": message,
            "retryable": retryable,
        }
        if details:
            error["details"] = bounded_detail(details)
        self.emit(WorkerEventType.JOB_FAILED, {"error": error})
