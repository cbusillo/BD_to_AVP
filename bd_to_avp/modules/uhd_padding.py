from __future__ import annotations

import threading
import tempfile
from dataclasses import dataclass
from pathlib import Path

from bd_to_avp.modules.command import run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.modules.uhd_color import HDRReferenceInfo
from bd_to_avp.modules.video_route import VideoRoutePreflightError
from bd_to_avp.runtime import RunContext

BLACK_LUMA_8BIT = 18


@dataclass(frozen=True)
class PictureExtent:
    frame_count: int
    first: int
    stop: int

    def contains_only_black(self, frames: range) -> bool:
        return not frames or frames.stop <= self.first or frames.start >= self.stop


def read_luma_maxima(path: Path, frame_count: int) -> list[int]:
    prefix = "lavfi.signalstats.YMAX="
    values = [int(line.removeprefix(prefix)) for line in path.read_text().splitlines() if line.startswith(prefix)]
    if len(values) != frame_count:
        raise VideoRoutePreflightError("The black padding could not be checked for every video frame.")
    return values


def picture_extent(maxima: list[int], black_level: int) -> PictureExtent:
    picture = [index for index, maximum in enumerate(maxima) if maximum > black_level]
    if not picture:
        raise VideoRoutePreflightError("The video contains no clear picture to match.")
    return PictureExtent(len(maxima), picture[0], picture[-1] + 1)


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
    rate = float(info.frame_rate)
    first_time = info.start_seconds + (frames.start - 0.5) / rate
    last_time = info.start_seconds + (frames.stop - 0.5) / rate
    with tempfile.TemporaryDirectory(dir=workspace, prefix="uhd-black-") as directory:
        metrics = Path(directory) / "luma.txt"
        run_process_capture(
            [
                config.FFMPEG_PATH,
                "-v",
                "error",
                "-ss",
                f"{max(0, frames.start / rate - 1):.9f}",
                "-t",
                f"{len(frames) / rate + 3:.9f}",
                "-copyts",
                "-i",
                reference,
                "-map",
                "0:v:0",
                "-vf",
                f"select='between(t,{first_time:.9f},{last_time:.9f})',signalstats,"
                f"metadata=mode=print:key=lavfi.signalstats.YMAX:file='{metrics}'",
                "-frames:v",
                str(len(frames) + 1),
                "-fps_mode",
                "passthrough",
                "-f",
                "null",
                "-",
            ],
            "Check 4K black padding",
            tool_id="ffmpeg",
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        return read_luma_maxima(metrics, len(frames))


def reference_picture_start(
    reference: Path,
    info: HDRReferenceInfo,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> int:
    chunk = max(1, round(float(info.frame_rate) * 10))
    for start in range(0, info.frame_count, chunk):
        maxima = read_reference_luma(
            reference,
            info,
            range(start, min(info.frame_count, start + chunk)),
            workspace=workspace,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        for index, maximum in enumerate(maxima):
            if maximum > BLACK_LUMA_8BIT * 4:
                return start + index
    raise VideoRoutePreflightError("The 4K copy contains no clear picture to match.")


def check_black_padding(
    reference: Path,
    info: HDRReferenceInfo,
    source: PictureExtent,
    offset: int,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> None:
    source_edges = (
        range(0, min(source.frame_count, max(0, -offset))),
        range(max(0, min(source.frame_count, info.frame_count - offset)), source.frame_count),
    )
    if any(not source.contains_only_black(frames) for frames in source_edges):
        raise VideoRoutePreflightError("The 4K copy is missing pictures at the beginning or end of the 3D movie.")
    reference_edges = (
        range(0, min(info.frame_count, max(0, offset))),
        range(max(0, min(info.frame_count, source.frame_count + offset)), info.frame_count),
    )
    for frames in reference_edges:
        maxima = read_reference_luma(
            reference,
            info,
            frames,
            workspace=workspace,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        if any(maximum > BLACK_LUMA_8BIT * 4 for maximum in maxima):
            raise VideoRoutePreflightError("The 3D source is missing pictures at the beginning or end of the 4K movie.")
