import os
import stat
from pathlib import Path

from bd_to_avp.vendor.pgsrip.ocr import AppleVisionOcr, OcrError
from bd_to_avp.modules.config import Stage, config
from bd_to_avp.modules.video_mode import VideoMode


class DependencyPreflightError(RuntimeError):
    pass


def verify_runtime_ready() -> None:
    missing_binaries = get_missing_dependency_binaries_for_current_job()
    if not missing_binaries:
        verify_av1_encoder_ready()
        verify_apple_vision_ocr_ready()
        return

    message = build_missing_dependency_message(missing_binaries)
    raise DependencyPreflightError(message)


def get_missing_dependency_binaries_for_current_job() -> list[Path]:
    required_paths = get_required_dependency_binaries_for_current_job()
    missing_binaries = [path for path in required_paths if not path.exists()]
    if (
        needs_native_mvc_splitter()
        and config.EDGE264_TEST_PATH not in missing_binaries
        and not ensure_native_mvc_splitter_executable()
    ):
        missing_binaries.append(config.EDGE264_TEST_PATH)
    return missing_binaries


def get_required_dependency_binaries_for_current_job() -> list[Path]:
    required_paths = [config.FFMPEG_PATH, config.FFPROBE_PATH]

    if needs_makemkv():
        required_paths.append(config.MAKEMKVCON_PATH)
    if needs_native_mvc_splitter():
        required_paths.append(config.EDGE264_TEST_PATH)
    if config.video_mode is VideoMode.MV_HEVC and config.start_stage.value <= Stage.COMBINE_TO_MV_HEVC.value:
        required_paths.append(config.SPATIAL_MEDIA_PATH)
    if config.start_stage.value <= Stage.CREATE_FINAL_FILE.value:
        required_paths.append(config.MP4BOX_PATH)
    if config.fx_upscale and config.start_stage.value <= Stage.UPSCALE_VIDEO.value:
        required_paths.append(config.FX_UPSCALE_PATH)

    return dedupe_paths(required_paths)


def dedupe_paths(paths: list[Path]) -> list[Path]:
    deduped_paths: list[Path] = []
    for path in paths:
        if path not in deduped_paths:
            deduped_paths.append(path)
    return deduped_paths


def needs_makemkv() -> bool:
    source_path = config.source_path
    if source_path and source_path.suffix.lower() in [*config.MTS_EXTENSIONS, ".mkv"]:
        return False
    return True


def needs_subtitle_extraction() -> bool:
    return not config.skip_subtitles and config.start_stage.value <= Stage.EXTRACT_SUBTITLES.value


def needs_native_mvc_splitter() -> bool:
    return config.start_stage.value <= Stage.CREATE_LEFT_RIGHT_FILES.value


def build_missing_dependency_message(missing_binaries: list[Path]) -> str:
    missing_names = "\n".join(f"- {get_dependency_name(path)}" for path in missing_binaries)
    if config.app.is_gui:
        recovery_steps = get_gui_recovery_steps(missing_binaries)
        return (
            f"Blu-ray to Vision Pro needs these tools before it can convert video:\n{missing_names}\n\n{recovery_steps}"
        )

    missing_paths = "\n".join(f"- {path}" for path in missing_binaries)
    return (
        "Required tools are missing:\n"
        f"{missing_paths}\n\n"
        "Install MakeMKV for disc conversion. If you installed BD_to_AVP as a command-line tool, "
        "also make sure the listed tools are installed and available in PATH."
    )


def get_gui_recovery_steps(missing_binaries: list[Path]) -> str:
    missing_makemkv = config.MAKEMKVCON_PATH in missing_binaries
    missing_other_tools = any(
        path != config.MAKEMKVCON_PATH and not is_subtitle_tool_path(path) for path in missing_binaries
    )
    if missing_makemkv and missing_other_tools:
        return "Install MakeMKV for macOS. If other tools are listed, reinstall the app."
    if missing_makemkv:
        return "Install MakeMKV for macOS, then open the app again."
    return "Reinstall the app, then open it again."


def is_subtitle_tool_path(path: Path) -> bool:
    return False


def get_dependency_name(path: Path) -> str:
    dependency_names = {
        config.FFMPEG_PATH: "FFmpeg",
        config.FFPROBE_PATH: "FFprobe",
        config.MAKEMKVCON_PATH: "MakeMKV",
        config.MP4BOX_PATH: "MP4Box",
        config.EDGE264_TEST_PATH: "Native MVC helper",
        config.SPATIAL_MEDIA_PATH: "Spatial media tool",
        config.FX_UPSCALE_PATH: "FX Upscale",
    }
    return dependency_names.get(path, path.name)


def ensure_native_mvc_splitter_executable() -> bool:
    if not config.EDGE264_TEST_PATH.is_file():
        return False
    if os.access(config.EDGE264_TEST_PATH, os.X_OK):
        return True

    try:
        current_mode = config.EDGE264_TEST_PATH.stat().st_mode
        config.EDGE264_TEST_PATH.chmod(current_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        return False
    return os.access(config.EDGE264_TEST_PATH, os.X_OK)


def verify_av1_encoder_ready() -> None:
    if config.video_mode is not VideoMode.AV1_SBS or config.start_stage.value > Stage.CREATE_LEFT_RIGHT_FILES.value:
        return

    import subprocess

    try:
        encoders = subprocess.run(
            [config.FFMPEG_PATH, "-hide_banner", "-encoders"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
        bitstream_filters = subprocess.run(
            [config.FFMPEG_PATH, "-hide_banner", "-bsfs"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise DependencyPreflightError("FFmpeg could not verify the required AV1 encoding support.") from error
    if "libsvtav1" not in encoders:
        raise DependencyPreflightError(
            "AV1 stereo export requires an FFmpeg build with the libsvtav1 software encoder."
        )
    if "av1_metadata" not in bitstream_filters:
        raise DependencyPreflightError(
            "AV1 stereo export requires an FFmpeg build with the av1_metadata bitstream filter."
        )


def verify_apple_vision_ocr_ready() -> None:
    if not needs_subtitle_extraction():
        return
    try:
        AppleVisionOcr._load_frameworks()
    except OcrError as error:
        raise DependencyPreflightError(
            "Subtitle extraction requires Apple Vision OCR support from macOS and the packaged PyObjC frameworks. "
            "Reinstall the app or enable Skip Subtitles before processing."
        ) from error
