from __future__ import annotations

import os
import stat
import subprocess

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ffmpeg

from bd_to_avp import preflight
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.disc import DiscTitleSelectionError, get_disc_and_mvc_video_info, MKVCreationError
from bd_to_avp.modules.file import path_is_relative_to
from bd_to_avp.modules.preview import PreviewRange, resolve_preview_range
from bd_to_avp.modules.process import ProcessingCancelled, start_process
from bd_to_avp.modules.sub import SRTCreationError
from bd_to_avp.worker.ownership import WorkerCancelled, WorkerProcessOwner
from bd_to_avp.worker.protocol import (
    JobSource,
    JobSpec,
    WorkerActivityReporter,
    WorkerOperation,
    WorkerSourceKind,
)

DIRECT_VIDEO_EXTENSIONS = frozenset({".mkv", ".mts", ".m2ts"})
DISC_IMAGE_EXTENSIONS = frozenset({".iso"})
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
    "preview_range",
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
        return inspect_source(job.source, owner)
    if job.operation is WorkerOperation.CONVERT_SOURCE:
        if activity is None:
            raise WorkerOperationError("internal_error", "Conversion requires an activity reporter.")
        return convert_source(job, owner, activity)
    if job.operation is WorkerOperation.PREVIEW_SOURCE:
        if activity is None:
            raise WorkerOperationError("internal_error", "Preview requires an activity reporter.")
        return preview_source(job, owner, activity)
    raise WorkerOperationError("unsupported_operation", f"Unsupported worker operation: {job.operation.value}.")


def inspect_source(source: JobSource, owner: WorkerProcessOwner) -> dict[str, object]:
    source_path = validate_source(source)
    if (
        source.kind
        in {
            WorkerSourceKind.DISC_IMAGE,
            WorkerSourceKind.BLU_RAY_FOLDER,
            WorkerSourceKind.PHYSICAL_DISC,
        }
        and not config.MAKEMKVCON_PATH.is_file()
    ):
        raise WorkerOperationError(
            "makemkv_missing",
            "MakeMKV is required to inspect this Blu-ray source.",
            retryable=True,
        )
    if source.kind is WorkerSourceKind.DIRECT_FILE and not config.FFPROBE_PATH.is_file():
        raise WorkerOperationError("ffprobe_missing", "The FFprobe helper could not be found.")

    owner.check_cancelled()
    try:
        with configured_source(source, source_path):
            disc_info = (
                get_disc_and_mvc_video_info(source.title_id)
                if source.title_id is not None
                else get_disc_and_mvc_video_info()
            )
    except ffmpeg.Error as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        stderr = error.stderr.decode("utf-8", errors="replace") if error.stderr else None
        raise WorkerOperationError("probe_failed", "FFprobe could not inspect the selected source.", stderr) from error
    except subprocess.CalledProcessError as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        details: str | None
        if isinstance(error.output, bytes):
            details = error.output.decode("utf-8", errors="replace")
        else:
            details = error.output if isinstance(error.output, str) else None
        message = (
            "MakeMKV could not read the selected Blu-ray disc. Confirm it is inserted, "
            "wait for the drive to finish spinning up, and try again."
            if source.kind is WorkerSourceKind.PHYSICAL_DISC
            else "MakeMKV could not inspect the selected Blu-ray source."
        )
        raise WorkerOperationError(
            "disc_inspection_failed",
            message,
            details,
            retryable=True,
        ) from error
    except DiscTitleSelectionError as error:
        raise WorkerOperationError("title_unavailable", str(error), retryable=True) from error
    except (OSError, KeyError, TypeError, ValueError) as error:
        if owner.cancellation_event.is_set():
            raise WorkerCancelled("The source inspection was cancelled.") from error
        raise WorkerOperationError(
            "probe_failed",
            "The selected source metadata could not be read.",
            str(error),
        ) from error

    owner.check_cancelled()
    result: dict[str, object] = {
        "name": disc_info.name,
        "resolution": disc_info.resolution,
        "frame_rate": disc_info.frame_rate,
        "interlaced": disc_info.is_interlaced,
    }
    if disc_info.duration_seconds > 0:
        result["duration_seconds"] = disc_info.duration_seconds
    result["titles"] = [
        {
            "id": title.id,
            "name": title.name,
            "output_name": title.output_name,
            "duration_seconds": title.duration_seconds,
            "resolution": title.resolution,
            "frame_rate": title.frame_rate,
            "main_feature": title.main_feature,
        }
        for title in disc_info.titles
    ]
    if source_path.is_file():
        result["size_bytes"] = source_path.stat().st_size
    return result


def convert_source(job: JobSpec, owner: WorkerProcessOwner, activity: WorkerActivityReporter) -> dict[str, object]:
    return _convert_source(job, owner, activity)


def preview_source(job: JobSpec, owner: WorkerProcessOwner, activity: WorkerActivityReporter) -> dict[str, object]:
    preview = job.preview
    if preview is None:
        raise WorkerOperationError("invalid_request", "Preview requests require preview options.")

    inspection = inspect_source(job.source, owner)
    try:
        duration_value = inspection["duration_seconds"]
        if isinstance(duration_value, bool) or not isinstance(duration_value, (int, float)):
            raise TypeError("Source duration must be numeric.")
        preview_range = resolve_preview_range(
            float(duration_value),
            preview.duration_seconds,
            preview.position.value,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise WorkerOperationError(
            "preview_range_unavailable",
            "The selected preview range could not be resolved from the source duration.",
            str(error),
        ) from error

    result = _convert_source(
        job,
        owner,
        activity,
        preview_range=preview_range,
        allow_recovery=False,
    )
    artifact = {
        **result,
        "parent_job_id": preview.parent_job_id,
        "position": preview.position.value,
    }
    activity.artifact_ready(artifact)
    return artifact


def _convert_source(
    job: JobSpec,
    owner: WorkerProcessOwner,
    activity: WorkerActivityReporter,
    *,
    preview_range: PreviewRange | None = None,
    allow_recovery: bool = True,
) -> dict[str, object]:
    source_path = validate_source(job.source)
    destination = job.destination
    encoding = job.encoding
    job_options = job.job
    if destination is None or encoding is None or job_options is None:
        raise WorkerOperationError(
            "invalid_request", "Conversion requests require destination, encoding, and job options."
        )
    if destination.path.exists() and not destination.path.is_dir():
        raise WorkerOperationError("destination_not_directory", "The destination path must be a folder.")
    if job.source.kind is WorkerSourceKind.BLU_RAY_FOLDER and path_is_relative_to(destination.path, source_path):
        raise WorkerOperationError(
            "destination_inside_source",
            "Choose a destination outside the Blu-ray source folder.",
        )

    owner.check_cancelled()
    with configured_conversion(job, source_path, preview_range=preview_range):
        try:
            activity.stage_started("configure", "Preparing conversion settings")
            config.configure_tool_environment()
            activity.log("Tool environment configured", stage="configure")
            final_output_path = start_process(
                cancellation_event=owner.cancellation_event,
                activity=activity,
                selected_title_id=job.source.title_id,
            )
            resolved_preview_range = config.preview_range
            owner.check_cancelled()
        except ProcessingCancelled as error:
            raise WorkerCancelled("The conversion was cancelled.") from error
        except MKVCreationError as error:
            if owner.cancellation_event.is_set():
                raise WorkerCancelled("The conversion was cancelled.") from error
            if not allow_recovery:
                raise WorkerOperationError(
                    "preview_source_preparation_failed",
                    "MakeMKV could not prepare the selected preview source.",
                    str(error),
                ) from error
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
            if not allow_recovery:
                raise WorkerOperationError(
                    "preview_subtitle_failed",
                    "Subtitle extraction failed while preparing the preview.",
                    str(error),
                ) from error
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
        except DiscTitleSelectionError as error:
            raise WorkerOperationError("title_unavailable", str(error), retryable=True) from error
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
    result: dict[str, object] = {
        "source_path": source_path.as_posix(),
        "destination_path": destination.path.as_posix(),
        "output_path": final_output_path.as_posix(),
        "size_bytes": final_output_path.stat().st_size,
    }
    if job.source.title_id is not None:
        result["title_id"] = job.source.title_id
    if resolved_preview_range is not None:
        result.update(
            {
                "start_seconds": resolved_preview_range.start_seconds,
                "duration_seconds": resolved_preview_range.duration_seconds,
                "source_duration_seconds": resolved_preview_range.source_duration_seconds,
            }
        )
    return result


@contextmanager
def configured_source(source: JobSource, source_path: Path) -> Iterator[None]:
    previous_source_path = config.source_path
    previous_source_str = config.source_str
    previous_source_folder_path = config.source_folder_path
    try:
        config.source_path = None if source.kind is WorkerSourceKind.PHYSICAL_DISC else source_path
        config.source_str = makemkv_source(source.kind, source_path)
        config.source_folder_path = None
        yield
    finally:
        config.source_path = previous_source_path
        config.source_str = previous_source_str
        config.source_folder_path = previous_source_folder_path


@contextmanager
def configured_conversion(
    job: JobSpec,
    source_path: Path,
    *,
    preview_range: PreviewRange | None = None,
) -> Iterator[None]:
    assert job.destination is not None
    assert job.encoding is not None
    assert job.job is not None
    config_snapshot = {field: getattr(config, field) for field in CONFIG_SNAPSHOT_FIELDS}
    environment_snapshot = {key: os.environ.get(key) for key in TOOL_ENVIRONMENT_KEYS}
    try:
        config.source_str = makemkv_source(job.source.kind, source_path)
        config.source_path = None if job.source.kind is WorkerSourceKind.PHYSICAL_DISC else source_path
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
        config.remove_original = False if job.source.kind is WorkerSourceKind.PHYSICAL_DISC else job.job.remove_original
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
        config.preview_range = preview_range
        yield
    finally:
        for field, value in config_snapshot.items():
            setattr(config, field, value)
        for key, value in environment_snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def validate_source(source: JobSource) -> Path:
    source_path = source.path
    if source.kind is WorkerSourceKind.PHYSICAL_DISC:
        if not physical_disc_device_path_is_valid(source_path):
            raise WorkerOperationError(
                "source_kind_mismatch",
                "Physical-disc sources must use a macOS device path such as /dev/disk4.",
            )
        if not physical_disc_device_is_available(source_path):
            raise WorkerOperationError(
                "disc_unavailable",
                "The selected Blu-ray disc is no longer available. Reinsert it and try again.",
                retryable=True,
            )
        return source_path

    if not source_path.exists():
        raise WorkerOperationError("source_not_found", "The selected source no longer exists.")

    if source.kind is WorkerSourceKind.DIRECT_FILE:
        if not source_path.is_file() or source_path.suffix.lower() not in DIRECT_VIDEO_EXTENSIONS:
            raise WorkerOperationError(
                "source_kind_mismatch",
                "Direct-file sources must be MKV, MTS, or M2TS files.",
            )
        return source_path

    if source.kind is WorkerSourceKind.DISC_IMAGE:
        if not source_path.is_file() or source_path.suffix.lower() not in DISC_IMAGE_EXTENSIONS:
            raise WorkerOperationError(
                "source_kind_mismatch",
                "Disc-image sources must be ISO files.",
            )
        return source_path

    blu_ray_root = normalize_blu_ray_root(source_path)
    if blu_ray_root is None:
        raise WorkerOperationError(
            "invalid_bluray_folder",
            "Choose a Blu-ray folder containing a BDMV directory.",
        )
    return blu_ray_root


def physical_disc_device_path_is_valid(source_path: Path) -> bool:
    device_name = source_path.name
    if source_path.parent != Path("/dev"):
        return False
    if device_name.startswith("rdisk"):
        identifier = device_name.removeprefix("rdisk")
    elif device_name.startswith("disk"):
        identifier = device_name.removeprefix("disk")
    else:
        return False
    return identifier.isdecimal()


def physical_disc_device_is_available(source_path: Path) -> bool:
    try:
        mode = source_path.stat().st_mode
    except OSError:
        return False
    return stat.S_ISBLK(mode) or stat.S_ISCHR(mode)


def normalize_blu_ray_root(source_path: Path) -> Path | None:
    if not source_path.is_dir():
        return None
    if source_path.name.casefold() == "bdmv":
        return source_path.parent
    try:
        has_bdmv = any(child.is_dir() and child.name.casefold() == "bdmv" for child in source_path.iterdir())
    except OSError:
        return None
    return source_path if has_bdmv else None


def makemkv_source(source_kind: WorkerSourceKind, source_path: Path) -> str | None:
    if source_kind is WorkerSourceKind.PHYSICAL_DISC:
        return f"dev:{source_path}"
    if source_kind is WorkerSourceKind.DISC_IMAGE:
        return f"iso:{source_path}"
    if source_kind is WorkerSourceKind.BLU_RAY_FOLDER:
        return f"file:{source_path}"
    return None
