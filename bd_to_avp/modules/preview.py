import math

from dataclasses import dataclass
from pathlib import Path

from bd_to_avp.modules.command import run_command, run_ffprobe
from bd_to_avp.modules.config import config
from bd_to_avp.modules.preview_range import PreviewRange


@dataclass(frozen=True)
class MediaTiming:
    start_seconds: float
    duration_seconds: float


def resolve_preview_range(
    source_duration_seconds: float,
    requested_duration_seconds: int,
    position: str,
) -> PreviewRange:
    if not math.isfinite(source_duration_seconds) or source_duration_seconds <= 0:
        raise ValueError("The source duration is not available for preview.")
    if requested_duration_seconds <= 0:
        raise ValueError("The preview duration must be greater than zero.")

    duration_seconds = min(float(requested_duration_seconds), source_duration_seconds)
    if position == "beginning":
        start_seconds = 0.0
    elif position == "middle":
        start_seconds = (source_duration_seconds - duration_seconds) / 2
    elif position == "end":
        start_seconds = source_duration_seconds - duration_seconds
    else:
        raise ValueError(f"Unsupported preview position: {position!r}.")

    return PreviewRange(
        start_seconds=max(0.0, start_seconds),
        duration_seconds=duration_seconds,
        source_duration_seconds=source_duration_seconds,
    )


def create_bounded_preview_source(
    input_path: Path,
    output_folder: Path,
    preview_range: PreviewRange,
) -> tuple[Path, PreviewRange]:
    source_timing = probe_media_timing(input_path)
    requested_end = min(
        preview_range.source_duration_seconds,
        preview_range.start_seconds + preview_range.duration_seconds,
    )
    output_path = output_folder / f"{input_path.stem}_preview.mkv"
    ranged_path = output_path.with_suffix(".range.mkv")
    temporary_path = output_path.with_suffix(".part.mkv")
    output_path.unlink(missing_ok=True)
    ranged_path.unlink(missing_ok=True)
    temporary_path.unlink(missing_ok=True)
    range_command = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        format_seconds(preview_range.start_seconds),
        "-copyts",
        "-i",
        input_path,
        "-to",
        format_seconds(source_timing.start_seconds + requested_end),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-map",
        "0:s?",
        "-c",
        "copy",
        "-y",
        ranged_path,
    ]
    normalize_command = [
        config.FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        ranged_path,
        "-map",
        "0",
        "-c",
        "copy",
        "-avoid_negative_ts",
        "make_zero",
        "-y",
        temporary_path,
    ]
    try:
        run_command(range_command, "Create bounded preview source")
        ranged_timing = probe_media_timing(ranged_path)
        actual_start = max(0.0, ranged_timing.start_seconds - source_timing.start_seconds)
        if actual_start > preview_range.start_seconds + 0.25:
            raise RuntimeError("The bounded preview started after the selected source range.")

        run_command(normalize_command, "Normalize bounded preview timestamps")
        final_timing = probe_media_timing(temporary_path)
        minimum_duration = requested_end - actual_start
        if final_timing.duration_seconds + 0.25 < minimum_duration:
            raise RuntimeError("The bounded preview did not cover the selected source range.")
        temporary_path.replace(output_path)
    finally:
        ranged_path.unlink(missing_ok=True)
        temporary_path.unlink(missing_ok=True)
    return (
        output_path,
        PreviewRange(
            start_seconds=actual_start,
            duration_seconds=final_timing.duration_seconds,
            source_duration_seconds=preview_range.source_duration_seconds,
        ),
    )


def probe_media_timing(input_path: Path) -> MediaTiming:
    probe = run_ffprobe(
        input_path,
        show_entries="format=start_time,duration:stream=codec_type,start_time",
    )
    format_data = probe.get("format", {})
    video_stream = next(
        (stream for stream in probe.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if video_stream is None:
        raise RuntimeError("The bounded preview source did not contain a video stream.")
    start_value = video_stream.get("start_time") or format_data.get("start_time") or 0
    duration_value = format_data.get("duration")
    if duration_value is None:
        raise RuntimeError("The bounded preview duration could not be measured.")
    return MediaTiming(
        start_seconds=float(start_value),
        duration_seconds=float(duration_value),
    )


def format_seconds(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")
