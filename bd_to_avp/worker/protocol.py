from __future__ import annotations

import json
import math
import threading

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence, TextIO
from uuid import UUID

from bd_to_avp.modules.audio_mode import AudioMode
from bd_to_avp.modules.languages import LanguageCodeError, normalize_language_code
from bd_to_avp.modules.video_mode import VideoMode
from bd_to_avp.observability import ObservabilityEvent
from bd_to_avp.runtime import RunContext

PROTOCOL_VERSION = 9
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
    PREVIEW_SOURCE = "preview_source"


class WorkerSourceKind(StrEnum):
    DIRECT_FILE = "direct_file"
    DISC_IMAGE = "disc_image"
    BLU_RAY_FOLDER = "blu_ray_folder"
    PHYSICAL_DISC = "physical_disc"


class SubtitleMode(StrEnum):
    OFF = "off"
    PREFERRED_ONLY = "preferred_only"
    PREFERRED_PLUS_OTHERS = "preferred_plus_others"


class WorkerEventType(StrEnum):
    WORKER_READY = "worker.ready"
    JOB_STARTED = "job.started"
    STAGE_STARTED = "stage.started"
    HEARTBEAT = "heartbeat"
    LOG = "log"
    WARNING = "warning"
    ARTIFACT_READY = "artifact.ready"
    OBSERVABILITY = "observability"
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
    title_id: str | None = None


@dataclass(frozen=True)
class JobDestination:
    path: Path


@dataclass(frozen=True)
class AudioOptions:
    mode: AudioMode
    bitrate: int
    preferred_language: str | None


@dataclass(frozen=True)
class SubtitleOptions:
    mode: SubtitleMode
    preferred_language: str | None


@dataclass(frozen=True)
class EncodingOptions:
    audio: AudioOptions
    video_mode: VideoMode
    av1_crf: int
    left_right_bitrate: int
    link_quality: bool
    mv_hevc_quality: int
    upscale_quality: int
    fov: int
    frame_rate: str
    resolution: str
    crop_black_bars: bool
    swap_eyes: bool
    fx_upscale: bool
    subtitles: SubtitleOptions


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


class PreviewPosition(StrEnum):
    BEGINNING = "beginning"
    MIDDLE = "middle"
    END = "end"


@dataclass(frozen=True)
class PreviewOptions:
    parent_job_id: str
    position: PreviewPosition
    duration_seconds: int


@dataclass(frozen=True)
class JobSpec:
    protocol_version: int
    job_id: str
    operation: WorkerOperation
    source: JobSource
    destination: JobDestination | None = None
    encoding: EncodingOptions | None = None
    job: JobOptions | None = None
    preview: PreviewOptions | None = None

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
            WorkerOperation.PREVIEW_SOURCE: base_keys | {"destination", "encoding", "job", "preview"},
        }[operation]
        cls._reject_unknown_keys(raw, operation_keys, "request", job_id)

        source = raw.get("source")
        if not isinstance(source, Mapping):
            raise WorkerProtocolError(
                "invalid_source",
                "The request must contain a source kind and path.",
                job_id=job_id,
            )
        required_source_keys = {"kind", "path"}
        missing_source_keys = required_source_keys - set(source.keys())
        if missing_source_keys:
            missing = ", ".join(sorted(missing_source_keys))
            raise WorkerProtocolError(
                "invalid_source",
                f"The source object is missing required field(s): {missing}.",
                job_id=job_id,
            )
        cls._reject_unknown_keys(source, required_source_keys | {"title_id"}, "source", job_id)
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
        has_title_id = "title_id" in source
        title_id = cls._parse_optional_title_id(source.get("title_id"), job_id)
        if operation is WorkerOperation.INSPECT_SOURCE and has_title_id:
            raise WorkerProtocolError(
                "invalid_source",
                "Inspection requests cannot select a source title.",
                job_id=job_id,
            )
        if source_kind is WorkerSourceKind.DIRECT_FILE and has_title_id:
            raise WorkerProtocolError(
                "invalid_title_selection",
                "Direct-file sources cannot select a disc title.",
                job_id=job_id,
            )
        title_selection_required = (
            operation is WorkerOperation.CONVERT_SOURCE and source_kind is not WorkerSourceKind.DIRECT_FILE
        ) or (operation is WorkerOperation.PREVIEW_SOURCE and source_kind is WorkerSourceKind.DISC_IMAGE)
        if title_selection_required and title_id is None:
            raise WorkerProtocolError(
                "invalid_title_selection",
                "Disc conversion requests must select a title returned by source inspection.",
                job_id=job_id,
            )

        destination: JobDestination | None = None
        encoding: EncodingOptions | None = None
        job_options: JobOptions | None = None
        preview_options: PreviewOptions | None = None
        if operation in {WorkerOperation.CONVERT_SOURCE, WorkerOperation.PREVIEW_SOURCE}:
            destination = JobDestination(
                path=cls._parse_destination(raw.get("destination"), job_id),
            )
            encoding = cls._parse_encoding(raw.get("encoding"), job_id)
            job_options = cls._parse_job_options(raw.get("job"), job_id)
            assert job_options is not None
        if operation is WorkerOperation.CONVERT_SOURCE:
            assert job_options is not None
            if source_kind is WorkerSourceKind.PHYSICAL_DISC and job_options.remove_original:
                raise WorkerProtocolError(
                    "invalid_job_options",
                    "job.remove_original must be false for physical discs.",
                    job_id=job_id,
                )
        elif operation is WorkerOperation.PREVIEW_SOURCE:
            assert job_options is not None
            preview_options = cls._parse_preview_options(raw.get("preview"), job_id)
            if source_kind not in {WorkerSourceKind.DIRECT_FILE, WorkerSourceKind.DISC_IMAGE}:
                raise WorkerProtocolError(
                    "invalid_preview_source",
                    "Preview currently supports MKV, MTS, M2TS, and ISO sources.",
                    job_id=job_id,
                )
            if job_options.start_stage != 1 or job_options.keep_files or not job_options.overwrite:
                raise WorkerProtocolError(
                    "invalid_preview_options",
                    "Preview jobs must start at stage 1, disable keep_files, and enable overwrite.",
                    job_id=job_id,
                )
            if job_options.remove_original or job_options.continue_on_error:
                raise WorkerProtocolError(
                    "invalid_preview_options",
                    "Preview jobs cannot remove the source or continue from partial output.",
                    job_id=job_id,
                )

        return cls(
            protocol_version=protocol_version,
            job_id=job_id,
            operation=operation,
            source=JobSource(kind=source_kind, path=source_path, title_id=title_id),
            destination=destination,
            encoding=encoding,
            job=job_options,
            preview=preview_options,
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

    @staticmethod
    def _parse_optional_title_id(value: Any, job_id: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise WorkerProtocolError(
                "invalid_source",
                "source.title_id must be a string.",
                job_id=job_id,
            )
        if not value or len(value) > 128 or any(ord(character) < 32 for character in value):
            raise WorkerProtocolError(
                "invalid_source",
                "source.title_id must be a non-empty identifier of at most 128 characters.",
                job_id=job_id,
            )
        return value

    @classmethod
    def _parse_encoding(cls, value: Any, job_id: str) -> EncodingOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "The conversion request must contain encoding options.",
                job_id=job_id,
            )
        required_keys = {
            "audio",
            "video_mode",
            "av1_crf",
            "left_right_bitrate",
            "link_quality",
            "mv_hevc_quality",
            "upscale_quality",
            "fov",
            "frame_rate",
            "resolution",
            "crop_black_bars",
            "swap_eyes",
            "fx_upscale",
            "subtitles",
        }
        cls._require_exact_keys(value, required_keys, "encoding", job_id)
        encoding = EncodingOptions(
            audio=cls._parse_audio_options(value.get("audio"), job_id),
            video_mode=cls._parse_video_mode(value.get("video_mode"), job_id),
            av1_crf=cls._parse_int(value, "av1_crf", "encoding", job_id, minimum=0, maximum=63),
            left_right_bitrate=cls._parse_int(value, "left_right_bitrate", "encoding", job_id, minimum=1, maximum=500),
            link_quality=cls._parse_bool(value, "link_quality", "encoding", job_id),
            mv_hevc_quality=cls._parse_int(value, "mv_hevc_quality", "encoding", job_id, minimum=0, maximum=100),
            upscale_quality=cls._parse_int(value, "upscale_quality", "encoding", job_id, minimum=0, maximum=100),
            fov=cls._parse_int(value, "fov", "encoding", job_id, minimum=0, maximum=360),
            frame_rate=cls._parse_string(value, "frame_rate", "encoding", job_id),
            resolution=cls._parse_string(value, "resolution", "encoding", job_id),
            crop_black_bars=cls._parse_bool(value, "crop_black_bars", "encoding", job_id),
            swap_eyes=cls._parse_bool(value, "swap_eyes", "encoding", job_id),
            fx_upscale=cls._parse_bool(value, "fx_upscale", "encoding", job_id),
            subtitles=cls._parse_subtitle_options(value.get("subtitles"), job_id),
        )
        if encoding.video_mode is VideoMode.AV1_SBS and encoding.fx_upscale:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "AV1 stereo export does not support AI FX upscale.",
                job_id=job_id,
            )
        if encoding.video_mode is VideoMode.AV1_SBS and encoding.resolution:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "AV1 stereo export always preserves full source resolution per eye.",
                job_id=job_id,
            )
        return encoding

    @staticmethod
    def _parse_video_mode(value: Any, job_id: str) -> VideoMode:
        if not isinstance(value, str):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.video_mode must be a string.",
                job_id=job_id,
            )
        try:
            return VideoMode(value)
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                f"Unsupported encoding.video_mode: {value!r}.",
                job_id=job_id,
            ) from error

    @classmethod
    def _parse_audio_options(cls, value: Any, job_id: str) -> AudioOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.audio must be an object.",
                job_id=job_id,
            )
        cls._require_exact_keys(
            value,
            {"mode", "bitrate", "preferred_language"},
            "encoding.audio",
            job_id,
            error_code="invalid_encoding_options",
        )

        raw_mode = value.get("mode")
        if not isinstance(raw_mode, str):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.audio.mode must be a string.",
                job_id=job_id,
            )
        try:
            mode = AudioMode(raw_mode)
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                f"Unsupported audio mode: {raw_mode!r}.",
                job_id=job_id,
            ) from error

        raw_language = value.get("preferred_language")
        if raw_language is None:
            preferred_language = None
        elif isinstance(raw_language, str):
            try:
                preferred_language = normalize_language_code(raw_language)
            except LanguageCodeError as error:
                raise WorkerProtocolError(
                    "invalid_encoding_options",
                    str(error),
                    job_id=job_id,
                ) from error
        else:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.audio.preferred_language must be null or a language code.",
                job_id=job_id,
            )

        return AudioOptions(
            mode=mode,
            bitrate=cls._parse_int(
                value,
                "bitrate",
                "encoding.audio",
                job_id,
                minimum=1,
                maximum=4096,
                error_code="invalid_encoding_options",
            ),
            preferred_language=preferred_language,
        )

    @classmethod
    def _parse_subtitle_options(cls, value: Any, job_id: str) -> SubtitleOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.subtitles must be an object.",
                job_id=job_id,
            )
        cls._require_exact_keys(
            value,
            {"mode", "preferred_language"},
            "encoding.subtitles",
            job_id,
            error_code="invalid_encoding_options",
        )

        raw_mode = value.get("mode")
        if not isinstance(raw_mode, str):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.subtitles.mode must be a string.",
                job_id=job_id,
            )
        try:
            mode = SubtitleMode(raw_mode)
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                f"Unsupported subtitle mode: {raw_mode!r}.",
                job_id=job_id,
            ) from error

        raw_language = value.get("preferred_language")
        if mode is SubtitleMode.OFF:
            if raw_language is not None:
                raise WorkerProtocolError(
                    "invalid_encoding_options",
                    "encoding.subtitles.preferred_language must be null when subtitles are off.",
                    job_id=job_id,
                )
            return SubtitleOptions(mode=mode, preferred_language=None)

        if not isinstance(raw_language, str):
            raise WorkerProtocolError(
                "invalid_encoding_options",
                "encoding.subtitles.preferred_language must be a language code.",
                job_id=job_id,
            )
        try:
            preferred_language = normalize_language_code(raw_language)
        except LanguageCodeError as error:
            raise WorkerProtocolError(
                "invalid_encoding_options",
                str(error),
                job_id=job_id,
            ) from error
        return SubtitleOptions(mode=mode, preferred_language=preferred_language)

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
        }
        cls._require_exact_keys(value, required_keys, "job", job_id)
        return JobOptions(
            start_stage=cls._parse_int(value, "start_stage", "job", job_id, minimum=1, maximum=9),
            keep_files=cls._parse_bool(value, "keep_files", "job", job_id),
            overwrite=cls._parse_bool(value, "overwrite", "job", job_id),
            remove_original=cls._parse_bool(value, "remove_original", "job", job_id),
            continue_on_error=cls._parse_bool(value, "continue_on_error", "job", job_id),
            software_encoder=cls._parse_bool(value, "software_encoder", "job", job_id),
            output_commands=cls._parse_bool(value, "output_commands", "job", job_id),
            keep_awake=cls._parse_bool(value, "keep_awake", "job", job_id),
        )

    @classmethod
    def _parse_preview_options(cls, value: Any, job_id: str) -> PreviewOptions:
        if not isinstance(value, Mapping):
            raise WorkerProtocolError(
                "invalid_preview_options",
                "The preview request must contain preview options.",
                job_id=job_id,
            )
        cls._require_exact_keys(
            value,
            {"parent_job_id", "position", "duration_seconds"},
            "preview",
            job_id,
        )
        raw_parent_job_id = cls._parse_string(value, "parent_job_id", "preview", job_id)
        try:
            parent_job_id = str(UUID(raw_parent_job_id))
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_preview_options",
                "preview.parent_job_id must be a UUID.",
                job_id=job_id,
            ) from error
        if parent_job_id == job_id:
            raise WorkerProtocolError(
                "invalid_preview_options",
                "preview.parent_job_id must differ from the preview job_id.",
                job_id=job_id,
            )
        raw_position = cls._parse_string(value, "position", "preview", job_id)
        try:
            position = PreviewPosition(raw_position)
        except ValueError as error:
            raise WorkerProtocolError(
                "invalid_preview_options",
                f"Unsupported preview position: {raw_position!r}.",
                job_id=job_id,
            ) from error
        return PreviewOptions(
            parent_job_id=parent_job_id,
            position=position,
            duration_seconds=cls._parse_int(
                value,
                "duration_seconds",
                "preview",
                job_id,
                minimum=1,
                maximum=300,
            ),
        )

    @classmethod
    def _require_exact_keys(
        cls,
        value: Mapping[str, Any],
        expected_keys: set[str],
        label: str,
        job_id: str,
        *,
        error_code: str | None = None,
    ) -> None:
        missing_keys = expected_keys - set(value.keys())
        if missing_keys:
            missing = ", ".join(sorted(missing_keys))
            raise WorkerProtocolError(
                error_code or (f"invalid_{label}_options" if label in {"encoding", "job"} else f"invalid_{label}"),
                f"The {label} object is missing required field(s): {missing}.",
                job_id=job_id,
            )
        cls._reject_unknown_keys(value, expected_keys, label, job_id, error_code=error_code)

    @staticmethod
    def _reject_unknown_keys(
        value: Mapping[str, Any],
        expected_keys: set[str],
        label: str,
        job_id: str,
        *,
        error_code: str | None = None,
    ) -> None:
        unknown_keys = set(value.keys()) - expected_keys
        if unknown_keys:
            unknown = ", ".join(sorted(str(key) for key in unknown_keys))
            raise WorkerProtocolError(
                error_code or "invalid_request",
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
        error_code: str | None = None,
    ) -> int:
        field_value = value.get(key)
        if not isinstance(field_value, int) or isinstance(field_value, bool):
            raise WorkerProtocolError(
                error_code or f"invalid_{label}_options",
                f"{label}.{key} must be an integer.",
                job_id=job_id,
            )
        if not minimum <= field_value <= maximum:
            raise WorkerProtocolError(
                error_code or f"invalid_{label}_options",
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
    def __init__(self, emitter: "WorkerEventEmitter", run_context: RunContext | None = None) -> None:
        self._emitter = emitter
        self._run_context = run_context
        self._current_stage: str | None = None
        self._stage_plan: tuple[str, ...] = ()
        self._stage_index: int | None = None
        self._stage_fraction: float | None = None
        self._lock = threading.Lock()

    @property
    def run_context(self) -> RunContext | None:
        return self._run_context

    def set_stage_plan(self, stages: Sequence[str]) -> None:
        stage_plan = tuple(stage for stage in stages if stage)
        with self._lock:
            self._stage_plan = stage_plan
            self._stage_index = None
            self._stage_fraction = None

    def stage_started(self, stage: str, message: str) -> None:
        with self._lock:
            self._current_stage = stage
            self._stage_fraction = None
            next_stage_index = 0 if self._stage_index is None else self._stage_index + 1
            if next_stage_index < len(self._stage_plan) and self._stage_plan[next_stage_index] == stage:
                self._stage_index = next_stage_index
            else:
                self._stage_plan = ()
                self._stage_index = None
            progress = self._progress_payload_locked()
            payload: dict[str, Any] = {"stage": stage, "message": message}
            if progress is not None:
                payload["progress"] = progress
            self._emitter.emit(WorkerEventType.STAGE_STARTED, payload)

    def stage_progress(self, completed_units: float, total_units: float) -> None:
        if not math.isfinite(completed_units) or not math.isfinite(total_units) or total_units <= 0:
            return
        fraction = min(1.0, max(0.0, completed_units / total_units))
        with self._lock:
            if self._stage_index is not None:
                self._stage_fraction = fraction

    def log(self, message: str, *, stage: str | None = None, **fields: Any) -> None:
        with self._lock:
            payload: dict[str, Any] = {"message": message, **fields}
            payload["stage"] = stage or self._current_stage
            self._emitter.emit(WorkerEventType.LOG, payload)

    def warning(self, message: str, *, stage: str | None = None, **fields: Any) -> None:
        with self._lock:
            payload: dict[str, Any] = {"message": message, **fields}
            payload["stage"] = stage or self._current_stage
            self._emitter.emit(WorkerEventType.WARNING, payload)

    def artifact_ready(self, artifact: Mapping[str, Any]) -> None:
        self._emitter.emit(WorkerEventType.ARTIFACT_READY, {"artifact": dict(artifact)})

    def heartbeat_payload(self, elapsed_seconds: int) -> dict[str, Any]:
        with self._lock:
            return self._heartbeat_payload_locked(elapsed_seconds)

    def emit_heartbeat(self, elapsed_seconds: int) -> None:
        with self._lock:
            self._emitter.emit(
                WorkerEventType.HEARTBEAT,
                self._heartbeat_payload_locked(elapsed_seconds),
            )

    def _heartbeat_payload_locked(self, elapsed_seconds: int) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "stage": self._current_stage,
            "elapsed_seconds": max(0, elapsed_seconds),
        }
        progress = self._progress_payload_locked()
        if progress is not None:
            payload["progress"] = progress
        return payload

    def _progress_payload_locked(self) -> dict[str, Any] | None:
        if self._stage_index is None or not self._stage_plan:
            return None
        progress: dict[str, Any] = {
            "current_stage": self._stage_index + 1,
            "total_stages": len(self._stage_plan),
        }
        if self._stage_fraction is not None:
            progress["stage_fraction"] = self._stage_fraction
        return progress


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


class WorkerObservabilitySink:
    def __init__(self, emitter: WorkerEventEmitter) -> None:
        self._emitter = emitter

    def emit(self, event: ObservabilityEvent) -> None:
        self._emitter.emit(WorkerEventType.OBSERVABILITY, {"event": event.to_dict()})
