from __future__ import annotations

import json
import threading

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, TextIO
from uuid import UUID

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_EVENT_BYTES = 1024 * 1024
ZERO_JOB_ID = str(UUID(int=0))


class WorkerOperation(StrEnum):
    INSPECT_SOURCE = "inspect_source"


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
    path: Path


@dataclass(frozen=True)
class JobSpec:
    protocol_version: int
    job_id: str
    operation: WorkerOperation
    source: JobSource

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

        source = raw.get("source")
        if not isinstance(source, Mapping) or not isinstance(source.get("path"), str):
            raise WorkerProtocolError(
                "invalid_source",
                "The request must contain a source path.",
                job_id=job_id,
            )

        source_path = Path(source["path"]).expanduser()
        if not source_path.is_absolute():
            raise WorkerProtocolError(
                "source_not_absolute",
                "The source path must be absolute.",
                job_id=job_id,
            )

        return cls(
            protocol_version=protocol_version,
            job_id=job_id,
            operation=operation,
            source=JobSource(path=source_path),
        )

    @staticmethod
    def _parse_job_id(value: Any) -> str:
        if not isinstance(value, str):
            raise WorkerProtocolError("invalid_job_id", "The request must contain a UUID job_id.")
        try:
            return str(UUID(value))
        except ValueError as error:
            raise WorkerProtocolError("invalid_job_id", "The request job_id must be a UUID.") from error


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
            error["details"] = details
        self.emit(WorkerEventType.JOB_FAILED, {"error": error})
