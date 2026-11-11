from __future__ import annotations

import json
import subprocess
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

import ffmpeg
import numpy as np

from bd_to_avp.modules.command import run_ffprobe, run_process_capture
from bd_to_avp.modules.config import config
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.uhd_color import (
    FloatImage,
    HDRReferenceInfo,
    SAMPLE_SIZE,
    UHD_SIZE,
    TitleColorMap,
    color_features,
    fit_title_color_map,
    read_hdr_reference_info,
)
from bd_to_avp.modules.uhd_match import (
    MAX_OFFSET_SECONDS,
    SAMPLE_BINS,
    MatchedFrame,
    aligned_color_pixels,
    check_reference_timing,
    check_reference_frame_rate,
    match_frame,
    source_sample_indices,
)
from bd_to_avp.modules.video import (
    Edge264DamageCollector,
    MvcProducer,
    generate_direct_mv_hevc_encoder_command,
    generate_mvc_annex_b_stream_command,
    generate_native_mvc_splitter_command,
    prepare_native_mvc_input,
    remove_direct_mv_hevc_attempt_artifacts,
    run_direct_mv_hevc_encoding,
    select_native_pipeline_error,
)
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRoutePreflightError
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.presentation import cli_message
from bd_to_avp.process_runner import (
    CaptureOverflowPolicy,
    ProcessPipelineError,
    ProcessPipelineRunner,
    ProcessPipelineStage,
    ProcessSpec,
)
from bd_to_avp.runtime import RunContext


@dataclass(frozen=True)
class UHDConversionPlan:
    reference_path: Path
    info: HDRReferenceInfo
    frame_offset: int
    color_map: TitleColorMap
    registration: FloatImage
    sampled_frames: int
    source_frame_count: int

    def report(self) -> dict[str, object]:
        return {
            "sampled_frames": self.sampled_frames,
            "source_frames": self.source_frame_count,
            "reference_frames": self.info.frame_count,
            "frame_offset": self.frame_offset,
            "registration": self.registration.tolist(),
            "color_validation": self.color_map.validation,
            "mastering_display": self.info.mastering_display,
            "max_cll": self.info.max_cll,
        }


def probe_video_frame_count(
    path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
) -> int:
    result = run_ffprobe(
        path,
        select_streams="v:0",
        count_packets=None,
        show_entries="stream=nb_read_packets",
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    try:
        count = int(result["streams"][0]["nb_read_packets"])
        if count <= 0:
            raise ValueError
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise VideoRoutePreflightError("The video frame count could not be checked before encoding.") from error
    return count


def probe_hdr_reference(
    path: Path,
    *,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
) -> HDRReferenceInfo:
    probe = run_ffprobe(
        path,
        select_streams="v:0",
        read_intervals="%+1",
        show_frames=None,
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    info = read_hdr_reference_info(probe)
    count = probe_video_frame_count(path, run_context=run_context, cancellation_event=cancellation_event)
    return replace(info, frame_count=count, duration_seconds=count / float(info.frame_rate))


def mvc_sample_stages(
    source: Path,
    indices: tuple[int, ...],
    destination: BinaryIO,
    *,
    mvc_producer: MvcProducer | None,
    damage: Edge264DamageCollector,
    count: list[int],
) -> tuple[ProcessPipelineStage, ...]:
    stages = []

    def stage(command: list[str | Path], tool_id: str, *, stdout: BinaryIO | None = None) -> ProcessPipelineStage:
        return ProcessPipelineStage(
            ProcessSpec(
                argv=tuple(command),
                tool_id=tool_id,
                display_name="Check matching 3D frames",
                merge_stderr=False,
                capture_overflow=CaptureOverflowPolicy.TRUNCATE,
                stdout=stdout if stdout is not None else subprocess.PIPE,
            ),
            line_handler=observe,
        )

    def observe(stream: object, line: str) -> None:
        damage.handle_line(stream, line)
        if line.startswith("frame="):
            count[0] = int(line.partition("=")[2].strip())

    if mvc_producer is not None:
        stages.append(stage(list(mvc_producer.command), mvc_producer.tool_id))
    elif source.suffix.lower() in {*config.MTS_EXTENSIONS, ".mkv"}:
        stages.append(stage(generate_mvc_annex_b_stream_command(source), "ffmpeg"))
    splitter_input = Path("-") if stages else prepare_native_mvc_input(source)
    stages.append(stage(generate_native_mvc_splitter_command(splitter_input), "edge264"))
    selection = "+".join(f"eq(n,{index})" for index in indices)
    command: list[str | Path] = [
        config.FFMPEG_PATH,
        "-v",
        "error",
        "-y",
        "-f",
        "yuv4mpegpipe",
        "-i",
        "-",
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-f",
        "null",
        "/dev/null",
        "-map",
        "0:v:0",
        "-vf",
        f"select='{selection}',scale=1920:540:flags=lanczos:in_color_matrix=bt709:in_range=tv:out_range=pc",
        "-fps_mode",
        "passthrough",
        "-pix_fmt",
        "rgb48le",
        "-f",
        "rawvideo",
        "pipe:1",
        "-progress",
        "pipe:2",
        "-nostats",
    ]
    stages.append(stage(command, "ffmpeg", stdout=destination))
    return tuple(stages)


def collect_stereo_samples(
    source: Path,
    indices: tuple[int, ...],
    *,
    workspace: Path,
    mvc_producer: MvcProducer | None,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> tuple[list[tuple[FloatImage, FloatImage]], int]:
    count = [0]
    damage = Edge264DamageCollector()
    samples = []
    with tempfile.TemporaryFile(dir=workspace) as destination:
        stages = mvc_sample_stages(source, indices, destination, mvc_producer=mvc_producer, damage=damage, count=count)
        try:
            ProcessPipelineRunner().run(stages, run_context=run_context, cancellation_event=cancellation_event)
        except ProcessPipelineError as error:
            selected = select_native_pipeline_error(error, producer_present=len(stages) == 3)
            if selected is not None:
                raise selected from error
            raise
        finally:
            splitter_argument = stages[-2].spec.argv[1]
            splitter_path = Path(
                splitter_argument.decode() if isinstance(splitter_argument, bytes) else splitter_argument
            )
            if splitter_path not in {source, Path("-")}:
                splitter_path.unlink(missing_ok=True)
        destination.seek(0)
        for _ in indices:
            payload = destination.read(SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 12)
            if len(payload) != SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 12:
                raise VideoRoutePreflightError(
                    "The 3D source ends before its stated video runtime. Its cut cannot be checked."
                )
            image = np.frombuffer(payload, "<u2").reshape(SAMPLE_SIZE[1], SAMPLE_SIZE[0] * 2, 3).astype(
                np.float32
            ) / np.float32(65535)
            samples.append((image[:, : SAMPLE_SIZE[0]], image[:, SAMPLE_SIZE[0] :]))
    if count[0] <= indices[-1]:
        raise VideoRoutePreflightError("The 3D decoder did not confirm the whole movie's frame count.")
    return samples, count[0]


def read_reference_window(
    reference: Path,
    info: HDRReferenceInfo,
    window: range,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> dict[int, FloatImage]:
    rate = float(info.frame_rate)
    first_time = info.start_seconds + (window.start - 0.5) / rate
    last_time = info.start_seconds + (window.stop - 0.5) / rate
    command = [
        config.FFMPEG_PATH,
        "-v",
        "error",
        "-ss",
        f"{max(0, window.start / rate - 1):.9f}",
        "-t",
        f"{len(window) / rate + 3:.9f}",
        "-copyts",
        "-i",
        reference,
        "-map",
        "0:v:0",
        "-vf",
        f"select='between(t,{first_time:.9f},{last_time:.9f})',"
        "scale=960:540:flags=area:in_color_matrix=bt2020:in_range=tv:out_range=pc",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(window) + 1),
        "-pix_fmt",
        "rgb48le",
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    frames = {}
    frame_bytes = SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 6
    with tempfile.TemporaryFile(dir=workspace) as destination:
        run_process_capture(
            command,
            "Check matching 4K frames",
            tool_id="ffmpeg",
            stdout=destination,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        destination.seek(0)
        for index in window:
            payload = destination.read(frame_bytes)
            if len(payload) != frame_bytes:
                raise VideoRoutePreflightError("The 4K copy has missing or unevenly timed pictures near its end.")
            frames[index] = np.frombuffer(payload, "<u2").reshape(SAMPLE_SIZE[1], SAMPLE_SIZE[0], 3).astype(
                np.float32
            ) / np.float32(65535)
        if destination.read(1):
            raise VideoRoutePreflightError("The 4K copy does not have a constant matching frame rate.")
    return frames


def collect_matched_frames(
    reference: Path,
    info: HDRReferenceInfo,
    indices: tuple[int, ...],
    stereo: list[tuple[FloatImage, FloatImage]],
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> tuple[list[MatchedFrame], int]:
    radius = round(float(info.frame_rate) * MAX_OFFSET_SECONDS)
    matched: list[MatchedFrame] = []
    frame_offset = None
    checked_bins: set[int] = set()
    for source_index, eyes in zip(indices, stereo, strict=True):
        window = range(max(0, source_index - radius), min(info.frame_count, source_index + radius + 1))
        candidates = read_reference_window(
            reference, info, window, workspace=workspace, run_context=run_context, cancellation_event=cancellation_event
        )
        result = match_frame(source_index, eyes[0], eyes[1], candidates, expected_offset=frame_offset)
        if result is None:
            continue
        if frame_offset is None and result.temporal_verified:
            frame_offset = result.reference_index - source_index
            if frame_offset < 0:
                raise VideoRoutePreflightError("The 4K copy is missing the beginning of the 3D source.")
        matched.append(result)
        checked_bins.add(min(SAMPLE_BINS - 1, int(source_index / info.frame_count * SAMPLE_BINS)))
    if (
        frame_offset is None
        or len(checked_bins) < SAMPLE_BINS - 2
        or len(matched) < SAMPLE_BINS
        or sum(frame.eye_verified for frame in matched) < 3
    ):
        raise VideoRoutePreflightError(
            "There are too few clear left-eye matches across the whole film to confirm its cut and eye."
        )
    matrices = np.stack([frame.registration.matrix for frame in matched])
    if np.max(np.abs(matrices - np.median(matrices, axis=0))) > 0.75:
        raise VideoRoutePreflightError("The framing changes during the film. The two pictures cannot stay lined up.")
    return matched, frame_offset


def prepare_uhd_conversion(
    source: Path,
    reference: Path,
    disc_info: DiscInfo,
    *,
    workspace: Path,
    mvc_producer: MvcProducer | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
) -> UHDConversionPlan:
    info = probe_hdr_reference(reference, run_context=run_context, cancellation_event=cancellation_event)
    if disc_info.resolution != "1920x1080" or disc_info.is_interlaced or disc_info.color_depth != 8:
        raise VideoRoutePreflightError("The 3D source must be progressive 1920 by 1080 Blu-ray MVC video.")
    check_reference_frame_rate(info, disc_info.frame_rate)
    duration = min(disc_info.duration_seconds, info.duration_seconds)
    indices = source_sample_indices(duration, info.frame_rate)
    cli_message("Checking the cut, matching eye and HDR color across the whole film.", run_context=run_context)
    stereo, decoded_count = collect_stereo_samples(
        source,
        indices,
        workspace=workspace,
        mvc_producer=mvc_producer,
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    check_reference_timing(info, disc_info.frame_rate, decoded_count / float(info.frame_rate))
    matched, offset = collect_matched_frames(
        reference,
        info,
        indices,
        stereo,
        workspace=workspace,
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    if decoded_count + offset > info.frame_count:
        raise VideoRoutePreflightError("The matching 4K copy is missing the last pictures of the 3D movie.")
    color_map = fit_title_color_map(
        [aligned_color_pixels(frame) for frame in matched[::2]],
        [aligned_color_pixels(frame) for frame in matched[1::2]],
    )
    registration = np.median(np.stack([frame.registration.matrix for frame in matched]), axis=0).astype(np.float32)
    plan = UHDConversionPlan(reference, info, offset, color_map, registration, len(matched), decoded_count)
    cli_message(f"4K match accepted: {json.dumps(plan.report())}", run_context=run_context)
    return plan


def write_title_lut(color_map: TitleColorMap, path: Path) -> None:
    axis = np.linspace(0, 1, 65, dtype=np.float32)
    coordinates = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)[..., ::-1].reshape(-1, 3)
    mapped = np.clip(color_features(coordinates) @ color_map.coefficients, 0, 1)
    with path.open("w") as output:
        output.write("LUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        np.savetxt(output, mapped, fmt="%.9f")


def generate_uhd_normalizer_command(plan: UHDConversionPlan, lut_path: Path) -> list[str]:
    rate = str(plan.info.frame_rate)
    clock = f"1/{plan.info.frame_rate.numerator}"
    frame_time = f"N*{plan.info.frame_rate.denominator}"
    matrix = plan.registration.copy()
    matrix[:, 2] *= UHD_SIZE[0] / SAMPLE_SIZE[0]
    corners = np.array([[0, 0, 1], [3840, 0, 1], [0, 2160, 1], [3840, 2160, 1]], dtype=np.float32)
    registered = corners @ matrix.T
    needs_registration = np.max(np.abs(registered - corners[:, :2])) > 0.25
    source = ffmpeg.input("pipe:0", f="yuv4mpegpipe", r=rate)
    reference = ffmpeg.input(str(plan.reference_path))
    right = (
        source.video.filter("crop", "iw/2", "ih", "iw/2", 0)
        .filter(
            "scale",
            *UHD_SIZE,
            flags="lanczos",
            in_color_matrix="bt709",
            in_range="tv",
            out_range="pc",
        )
        .filter("format", "gbrp16le")
    )
    if needs_registration:
        perspective = {
            f"{coordinate}{index}": float(point[axis])
            for index, point in enumerate(registered)
            for axis, coordinate in enumerate(("x", "y"))
        }
        right = right.filter("perspective", **perspective, sense="source", interpolation="cubic")
    right = (
        right.filter("lut3d", file=str(lut_path), interp="tetrahedral")
        .filter(
            "scale",
            in_range="pc",
            out_color_matrix="bt2020",
            out_range="tv",
        )
        .filter("format", "yuv420p10le")
        .filter("settb", expr=clock)
        .filter("setpts", frame_time)
    )
    left = (
        reference.video.filter("trim", start_frame=plan.frame_offset)
        .filter("settb", expr=clock)
        .filter("setpts", frame_time)
        .filter("format", "yuv420p10le")
    )
    packed = ffmpeg.filter([left, right], "hstack", inputs=2, shortest=1)
    output = ffmpeg.output(
        packed, "pipe:1", format="yuv4mpegpipe", pix_fmt="yuv420p10le", strict=-1, fps_mode="passthrough"
    )
    return [
        argument if argument != "pipe:0" else "-" for argument in ffmpeg.compile(output, cmd=str(config.FFMPEG_PATH))
    ]


def create_uhd_mv_hevc_file(
    source: Path,
    output: Path,
    plan: UHDConversionPlan,
    route: ResolvedVideoRoute,
    *,
    mvc_producer: MvcProducer | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
    observability_context: ObservabilityContext | None = None,
    damage_collector: Edge264DamageCollector | None = None,
) -> Path:
    lut_path = output.with_suffix(".cube")
    write_title_lut(plan.color_map, lut_path)
    command = generate_direct_mv_hevc_encoder_command(output, route.direct_bitrate_mbps, route.direct_quality)
    command.extend(["--color", "hdr10", "--expected-frames", str(plan.source_frame_count)])
    if plan.info.mastering_display is not None:
        command.extend(["--mastering-display", plan.info.mastering_display])
    if plan.info.max_cll is not None:
        command.extend(["--max-cll", plan.info.max_cll])
    try:
        run_direct_mv_hevc_encoding(
            source,
            output,
            generate_uhd_normalizer_command(plan, lut_path),
            command,
            mvc_producer=mvc_producer,
            run_context=run_context,
            cancellation_event=cancellation_event,
            observability_context=observability_context,
            damage_collector=damage_collector,
        )
    except BaseException:
        remove_direct_mv_hevc_attempt_artifacts(output)
        output.unlink(missing_ok=True)
        raise
    finally:
        if not config.keep_files:
            lut_path.unlink(missing_ok=True)
    return output
