from __future__ import annotations

import os
import subprocess

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ffmpeg

from bd_to_avp import preflight
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc import get_disc_and_mvc_video_info, MKVCreationError
from bd_to_avp.modules.process import ProcessingCancelled, start_process
from bd_to_avp.modules.sub import SRTCreationError
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import JobSpec, WorkerActivityReporter, WorkerOperation

DIRECT_VIDEO_EXTENSIONS = frozenset({".mkv", ".mts", ".m2ts"})
DISC_IMAGE_EXTENSIONS = frozenset({".iso"})
SUPPORTED_INSPECTION_EXTENSIONS = DIRECT_VIDEO_EXTENSIONS | DISC_IMAGE_EXTENSIONS
SUPPORTED_CONVERSION_EXTENSIONS = SUPPORTED_INSPECTION_EXTENSIONS
TOOL_ENVIRONMENT_KEYS = ("PATH", "FFMPEG_BINARY", "FFPROBE_BINARY", "TMPDIR")
CONFIG_SNAPSHOT_FIELDS = (
    "source_str",
    "source_path",
    "source_folder_path",
    "output_root_path",
    "overwrite",
    "transcode_audio",
    "audio_bitrate",
    "left_right_bitrate",
    "link_quality",
    "mv_hevc_quality",
    "upscale_quality",
    "fov",
    "frame_rate",
    "resolution",
    "keep_files",
    "start_stage",
    "remove_original",
    "swap_eyes",
    "skip_subtitles",
    "crop_black_bars",
    "output_commands",
    "software_encoder",
    "fx_upscale",
    "continue_on_error",
    "language_code",
    "remove_extra_languages",
    "keep_awake",
)


@dataclass
class WorkerOperationError(Exception):
    code: str
    message: str
    details: str | None = None
    retryable: bool = False

    def __str__(self) -> str:
        return self.message


@dataclass
class WorkerDecisionRequired(Exception):
    code: str
    message: str
    details: str | None = None
    choices: tuple[str, ...] = ()


def run_operation(
    job: JobSpec,
    owner: WorkerProcessOwner,
    activity: WorkerActivityReporter | None = None,
) -> dict[str, object]:
    if job.operation is WorkerOperation.INSPECT_SOURCE:
        return inspect_source(job.source.path, owner)
    if job.operation is WorkerOperation.CONVERT_SOURCE:
        if activity is None:
            raise WorkerOperationError("internal_error", "Conversion requires an activity reporter.")
        return convert_source(job, owner, activity)
    raise WorkerOperationError("unsupported_operation", f"Unsupported worker operation: {job.operation.value}.")


def inspect_source(source_path: Path, owner: WorkerProcessOwner) -> dict[str, object]:
    if not source_path.exists():
        raise WorkerOperationError("source_not_found", "The selected source no longer exists.")
    if not source_path.is_file():
        raise WorkerOperationError("source_not_file", "The selected source is not a regular file.")
    if source_path.suffix.lower() not in SUPPORTED_INSPECTION_EXTENSIONS:
        raise WorkerOperationError(
            "unsupported_source",
            "Source inspection supports ISO, MKV, MTS, and M2TS files.",
        )
    if source_path.suffix.lower() in DISC_IMAGE_EXTENSIONS and not config.MAKEMKVCON_PATH.is_file():
        raise WorkerOperationError(
            "makemkv_missing",
            "MakeMKV is required to inspect a Blu-ray ISO.",
            retryable=True,
        )
    if source_path.suffix.lower() in DIRECT_VIDEO_EXTENSIONS and not config.FFPROBE_PATH.is_file():
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
    except subprocess.CalledProcessError as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        details = error.output if isinstance(error.output, str) else None
        raise WorkerOperationError(
            "disc_inspection_failed",
            "MakeMKV could not inspect the selected Blu-ray ISO.",
            details,
            retryable=True,
        ) from error
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


def convert_source(job: JobSpec, owner: WorkerProcessOwner, activity: WorkerActivityReporter) -> dict[str, object]:
    source_path = job.source.path
    destination = job.destination
    encoding = job.encoding
    job_options = job.job
    if destination is None or encoding is None or job_options is None:
        raise WorkerOperationError(
            "invalid_request", "Conversion requests require destination, encoding, and job options."
        )
    if not source_path.exists():
        raise WorkerOperationError("source_not_found", "The selected source no longer exists.")
    if not source_path.is_file():
        raise WorkerOperationError("source_not_file", "The selected source is not a regular file.")
    if source_path.suffix.lower() not in SUPPORTED_CONVERSION_EXTENSIONS:
        raise WorkerOperationError(
            "unsupported_source",
            "Conversion currently supports ISO, MKV, MTS, and M2TS files.",
        )
    if destination.path.exists() and not destination.path.is_dir():
        raise WorkerOperationError("destination_not_directory", "The destination path must be a folder.")

    owner.check_cancelled()
    with configured_conversion(job):
        try:
            activity.stage_started("configure", "Preparing conversion settings")
            config.configure_tool_environment()
            activity.log("Tool environment configured", stage="configure")
            final_output_path = start_process(
                cancellation_event=owner.cancellation_event,
                activity=activity,
            )
            owner.check_cancelled()
        except ProcessingCancelled as error:
            raise WorkerCancelled("The conversion was cancelled.") from error
        except MKVCreationError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            raise WorkerDecisionRequired(
                "mkv_creation_decision_required",
                "MakeMKV reported errors while creating the intermediate MKV.",
                "If a usable MKV was created, enable Continue on Error and retry from Extract MVC and Audio."
                f"\n\n{error}",
                ("retry_continue_on_error", "cancel"),
            ) from error
        except SRTCreationError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            raise WorkerDecisionRequired(
                "subtitle_decision_required",
                "Subtitle extraction did not produce usable subtitle files.",
                f"Turn off Include subtitles to retry without subtitles.\n\n{error}",
                ("retry_without_subtitles", "cancel"),
            ) from error
        except preflight.DependencyPreflightError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            raise WorkerOperationError(
                "dependency_preflight_failed",
                "A required conversion helper is missing or unavailable.",
                str(error),
            ) from error
        except FileExistsError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            raise WorkerOperationError("output_exists", str(error), retryable=True) from error
        except ffmpeg.Error as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else None
            raise WorkerOperationError("ffmpeg_failed", "FFmpeg failed during conversion.", stderr) from error
        except subprocess.CalledProcessError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            details = error.output if isinstance(error.output, str) else None
            raise WorkerOperationError(
                "tool_failed",
                "A conversion helper failed.",
                details,
            ) from error
        except (OSError, RuntimeError, ValueError) as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            raise WorkerOperationError(
                "conversion_failed",
                "The source could not be converted.",
                str(error),
            ) from error

    if final_output_path is None or not final_output_path.exists():
        raise WorkerOperationError(
            "output_missing",
            "The conversion finished without producing the expected output file.",
        )
    activity.log("Conversion output ready", stage="move_files", output_path=final_output_path.as_posix())
    return {
        "source_path": source_path.as_posix(),
        "destination_path": destination.path.as_posix(),
        "output_path": final_output_path.as_posix(),
        "size_bytes": final_output_path.stat().st_size,
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


@contextmanager
def configured_conversion(job: JobSpec) -> Iterator[None]:
    assert job.destination is not None
    assert job.encoding is not None
    assert job.job is not None
    config_snapshot = {field: getattr(config, field) for field in CONFIG_SNAPSHOT_FIELDS}
    environment_snapshot = {key: os.environ.get(key) for key in TOOL_ENVIRONMENT_KEYS}
    try:
        config.source_str = None
        config.source_path = job.source.path
        config.source_folder_path = None
        config.output_root_path = job.destination.path
        config.overwrite = job.job.overwrite
        config.transcode_audio = job.encoding.transcode_audio
        config.audio_bitrate = job.encoding.audio_bitrate
        config.left_right_bitrate = job.encoding.left_right_bitrate
        config.link_quality = job.encoding.link_quality
        config.mv_hevc_quality = job.encoding.mv_hevc_quality
        config.upscale_quality = job.encoding.upscale_quality
        config.fov = job.encoding.fov
        config.frame_rate = job.encoding.frame_rate
        config.resolution = job.encoding.resolution
        config.keep_files = job.job.keep_files
        config.start_stage = Stage.get_stage(job.job.start_stage)
        config.remove_original = job.job.remove_original
        config.swap_eyes = job.encoding.swap_eyes
        config.skip_subtitles = job.encoding.skip_subtitles
        config.crop_black_bars = job.encoding.crop_black_bars
        config.output_commands = job.job.output_commands
        config.software_encoder = job.job.software_encoder
        config.fx_upscale = job.encoding.fx_upscale
        config.continue_on_error = job.job.continue_on_error
        config.language_code = job.encoding.language_code
        config.remove_extra_languages = job.encoding.remove_extra_languages
        config.keep_awake = job.job.keep_awake
        yield
    finally:
        for field, value in config_snapshot.items():
            setattr(config, field, value)
        for key, value in environment_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
