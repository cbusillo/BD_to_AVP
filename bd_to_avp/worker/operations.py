from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ffmpeg

from bd_to_avp.modules.config import config
from bd_to_avp.modules.disc import get_disc_and_mvc_video_info
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import JobSpec, WorkerOperation

SUPPORTED_INSPECTION_EXTENSIONS = frozenset({".mkv", ".mts", ".m2ts"})


@dataclass(frozen=True)
class WorkerOperationError(Exception):
    code: str
    message: str
    details: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


def run_operation(job: JobSpec, owner: WorkerProcessOwner) -> dict[str, object]:
    if job.operation is WorkerOperation.INSPECT_SOURCE:
        return inspect_source(job.source.path, owner)
    raise WorkerOperationError("unsupported_operation", f"Unsupported worker operation: {job.operation.value}.")


def inspect_source(source_path: Path, owner: WorkerProcessOwner) -> dict[str, object]:
    if not source_path.exists():
        raise WorkerOperationError("source_not_found", "The selected source no longer exists.")
    if not source_path.is_file():
        raise WorkerOperationError("source_not_file", "The selected source is not a regular file.")
    if source_path.suffix.lower() not in SUPPORTED_INSPECTION_EXTENSIONS:
        raise WorkerOperationError(
            "unsupported_source",
            "Source inspection supports MKV, MTS, and M2TS files.",
        )
    if not config.FFPROBE_PATH.is_file():
        raise WorkerOperationError("ffprobe_missing", "The FFprobe helper could not be found.")

    owner.check_cancelled()
    try:
        with configured_source(source_path):
            disc_info = get_disc_and_mvc_video_info()
    except ffmpeg.Error as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else None
        raise WorkerOperationError("probe_failed", "FFprobe could not inspect the selected source.", stderr) from error
    except (OSError, KeyError, TypeError, ValueError) as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        raise WorkerOperationError(
            "probe_failed",
            "The selected source metadata could not be read.",
            str(error),
        ) from error

    owner.check_cancelled()
    return {
        "name": disc_info.name,
        "resolution": disc_info.resolution,
        "frame_rate": disc_info.frame_rate,
        "interlaced": disc_info.is_interlaced,
        "size_bytes": source_path.stat().st_size,
    }


@contextmanager
def configured_source(source_path: Path) -> Iterator[None]:
    previous_source_path = config.source_path
    previous_source_str = config.source_str
    previous_source_folder_path = config.source_folder_path
    try:
        config.source_path = source_path
        config.source_str = None
        config.source_folder_path = None
        yield
    finally:
        config.source_path = previous_source_path
        config.source_str = previous_source_str
        config.source_folder_path = previous_source_folder_path
