from __future__ import annotations

import threading
import tempfile
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import ffmpeg

from bd_to_avp.modules.command import run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.modules.uhd_color import HDRReferenceInfo
from bd_to_avp.modules.uhd_match import MAX_OFFSET_SECONDS
from bd_to_avp.modules.video_route import VideoRoutePreflightError
from bd_to_avp.runtime import RunContext

BLACK_LUMA_8BIT = 18


@dataclass(frozen=True)
class PictureExtent:
    frame_count: int
    first: int
    stop: int


def read_luma_maxima(path: Path, frame_count: int) -> list[int]:
    prefix = "lavfi.signalstats.YMAX="
    values = [int(line.removeprefix(prefix)) for line in path.read_text().splitlines() if line.startswith(prefix)]
    if len(values) != frame_count:
        raise VideoRoutePreflightError("The black padding could not be checked for every video frame.")
    return values


def picture_indices(maxima: list[int], black_level: int) -> list[int]:
    return [index for index, maximum in enumerate(maxima) if maximum > black_level]


def picture_extent(maxima: list[int], black_level: int) -> PictureExtent:
    picture = picture_indices(maxima, black_level)
    if not picture:
        raise VideoRoutePreflightError("The video contains no clear picture to match.")
    return PictureExtent(len(maxima), picture[0], picture[-1] + 1)


def reference_window_stream(reference: Path, info: HDRReferenceInfo, frames: range) -> ffmpeg.nodes.FilterableStream:
    rate = float(info.frame_rate)
    first_time = info.start_seconds + (frames.start - 0.5) / rate
    last_time = info.start_seconds + (frames.stop - 0.5) / rate
    return ffmpeg.input(
        str(reference),
        ss=f"{max(0, frames.start / rate - 1):.9f}",
        t=f"{len(frames) / rate + 3:.9f}",
        copyts=None,
    ).video.filter("select", f"between(t,{first_time:.9f},{last_time:.9f})")


def read_reference_luma(
    reference: Path,
    info: HDRReferenceInfo,
    frames: range,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> list[int]:
    if not frames:
        return []
    with tempfile.TemporaryDirectory(dir=workspace, prefix="uhd-black-") as directory:
        metrics = Path(directory) / "luma.txt"
        stream = (
            reference_window_stream(reference, info, frames)
            .filter("signalstats")
            .filter("metadata", mode="print", key="lavfi.signalstats.YMAX", file=str(metrics))
        )
        command = ffmpeg.compile(
            ffmpeg.output(stream, "-", format="null", vframes=len(frames) + 1, fps_mode="passthrough"),
            cmd=str(config.FFMPEG_PATH),
        )
        run_process_capture(
            command,
            "Check 4K black padding",
            tool_id="ffmpeg",
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        return read_luma_maxima(metrics, len(frames))


def reference_picture_extent(
    reference: Path,
    info: HDRReferenceInfo,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> PictureExtent:
    chunk = max(1, round(float(info.frame_rate) * 10))
    first = None
    for start in range(0, info.frame_count, chunk):
        maxima = read_reference_luma(
            reference,
            info,
            range(start, min(info.frame_count, start + chunk)),
            workspace=workspace,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        picture = picture_indices(maxima, BLACK_LUMA_8BIT * 4)
        if picture:
            first = start + picture[0]
            break
    if first is None:
        raise VideoRoutePreflightError("The 4K copy contains no clear picture to match.")
    for stop in range(info.frame_count, first, -chunk):
        start = max(first, stop - chunk)
        maxima = read_reference_luma(
            reference,
            info,
            range(start, stop),
            workspace=workspace,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        picture = picture_indices(maxima, BLACK_LUMA_8BIT * 4)
        if picture:
            return PictureExtent(info.frame_count, first, start + picture[-1] + 1)
    raise VideoRoutePreflightError("The 4K picture ending could not be checked.")


def check_black_padding(source: PictureExtent, reference: PictureExtent, offset: int, rate: Fraction) -> None:
    if source.first + offset < 0 or source.stop + offset > reference.frame_count:
        raise VideoRoutePreflightError("The 4K copy is missing pictures at the beginning or end of the 3D movie.")
    if reference.first < offset or reference.stop > source.frame_count + offset:
        raise VideoRoutePreflightError("The 3D source is missing pictures at the beginning or end of the 4K movie.")
    tolerance = float(rate) * MAX_OFFSET_SECONDS
    if (
        abs(reference.first - source.first - offset) > tolerance
        or abs(reference.stop - source.stop - offset) > tolerance
    ):
        raise VideoRoutePreflightError(
            "The picture begins or ends differently in the two copies. They may be different cuts."
        )
