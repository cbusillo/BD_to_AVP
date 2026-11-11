from __future__ import annotations

import json
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path

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
    applied_uhd_registration,
    check_reference_frame_rate,
    match_frame,
    source_sample_indices,
)
from bd_to_avp.modules.uhd_padding import (
    BLACK_LUMA_8BIT,
    PictureExtent,
    check_black_padding,
    picture_extent,
    read_luma_maxima,
    reference_picture_extent,
    reference_window_stream,
)
from bd_to_avp.modules.video import (
    Edge264DamageCollector,
    MvcProducer,
    generate_direct_mv_hevc_encoder_command,
    generate_mvc_annex_b_stream_command,
    remove_direct_mv_hevc_attempt_artifacts,
    run_direct_mv_hevc_encoding,
    run_native_mvc_encoding,
)
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRoutePreflightError
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.presentation import cli_message
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


@dataclass(frozen=True)
class StereoSamples:
    eyes: list[tuple[FloatImage, FloatImage]]
    extent: PictureExtent


def generate_stereo_sample_command(
    indices: tuple[int, ...],
    samples_path: Path,
    counter_path: Path,
    luma_path: Path,
) -> list[str]:
    selection = "+".join(f"eq(n,{index})" for index in indices)
    source = ffmpeg.input("pipe:0", f="yuv4mpegpipe", probesize=32, analyzeduration=0).video.filter_multi_output(
        "split", 2
    )
    counter = ffmpeg.output(
        source[0]
        .filter("signalstats")
        .filter("metadata", mode="print", key="lavfi.signalstats.YMAX", file=str(luma_path))
        .filter("scale", 1, 1, flags="neighbor"),
        str(counter_path),
        format="rawvideo",
        pix_fmt="gray",
        vcodec="rawvideo",
        **{"threads:v": 1, "fps_mode": "passthrough", "flush_packets": 1},
    )
    eyes = source[1].filter("select", selection).filter_multi_output("split", 2)
    scaled = [
        eyes[index]
        .filter("crop", "iw/2", "ih", "iw/2" if index else 0, 0)
        .filter("scale", *SAMPLE_SIZE, flags="lanczos", in_color_matrix="bt709", in_range="tv", out_range="pc")
        .filter("format", "rgb48le")
        for index in range(2)
    ]
    samples = ffmpeg.output(
        ffmpeg.filter(scaled, "hstack", inputs=2),
        str(samples_path),
        format="rawvideo",
        pix_fmt="rgb48le",
        fps_mode="passthrough",
    )
    return ffmpeg.compile(ffmpeg.merge_outputs(counter, samples), cmd=str(config.FFMPEG_PATH), overwrite_output=True)


def collect_stereo_samples(
    source: Path,
    indices: tuple[int, ...],
    *,
    workspace: Path,
    mvc_producer: MvcProducer | None,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> StereoSamples:
    samples = []
    producer = mvc_producer
    if producer is None and source.suffix.lower() in {*config.MTS_EXTENSIONS, ".mkv"}:
        producer = MvcProducer(tuple(generate_mvc_annex_b_stream_command(source)), "ffmpeg")
    with tempfile.TemporaryDirectory(dir=workspace, prefix="uhd-match-") as directory:
        samples_path = Path(directory) / "samples.rgb48"
        counter_path = Path(directory) / "decoded-frames.raw"
        luma_path = Path(directory) / "luma.txt"
        run_native_mvc_encoding(
            source,
            (counter_path,),
            generate_stereo_sample_command(indices, samples_path, counter_path, luma_path),
            mvc_producer=producer,
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
        decoded_count = counter_path.stat().st_size
        extent = picture_extent(read_luma_maxima(luma_path, decoded_count), BLACK_LUMA_8BIT)
        with samples_path.open("rb") as destination:
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
    if decoded_count <= indices[-1]:
        raise VideoRoutePreflightError("The 3D decoder did not confirm the whole movie's frame count.")
    return StereoSamples(samples, extent)


def read_reference_window(
    reference: Path,
    info: HDRReferenceInfo,
    window: range,
    *,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> dict[int, FloatImage]:
    stream = reference_window_stream(reference, info, window).filter(
        "scale", 960, 540, flags="area", in_color_matrix="bt2020", in_range="tv", out_range="pc"
    )
    command = ffmpeg.compile(
        ffmpeg.output(
            stream, "pipe:1", format="rawvideo", pix_fmt="rgb48le", vframes=len(window) + 1, fps_mode="passthrough"
        ),
        cmd=str(config.FFMPEG_PATH),
    )
    frames = {}
    frame_bytes = SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 6
    with tempfile.TemporaryFile(dir=workspace) as destination:
        run_process_capture(
            command,
            "Check matching 4K frames",
            tool_id="ffmpeg",
            stdout=destination.fileno(),
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
    source_frame_count: int,
    initial_offset: int,
    workspace: Path,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> tuple[list[MatchedFrame], int]:
    radius = round(float(info.frame_rate) * MAX_OFFSET_SECONDS)
    matched: list[MatchedFrame] = []
    frame_offset = None
    for source_index, eyes in zip(indices, stereo, strict=True):
        center = source_index + (frame_offset if frame_offset is not None else initial_offset)
        window = range(max(0, center - radius), min(info.frame_count, center + radius + 1))
        if not window:
            continue
        candidates = read_reference_window(
            reference, info, window, workspace=workspace, run_context=run_context, cancellation_event=cancellation_event
        )
        result = match_frame(source_index, eyes[0], eyes[1], candidates, expected_offset=frame_offset)
        if result is None:
            continue
        if frame_offset is None and result.temporal_verified:
            frame_offset = result.reference_index - source_index
        matched.append(result)
    if frame_offset is not None:
        corrected = []
        for frame in matched:
            expected_index = frame.source_index + frame_offset
            if frame.reference_index != expected_index:
                reference_frame = read_reference_window(
                    reference,
                    info,
                    range(expected_index, expected_index + 1),
                    workspace=workspace,
                    run_context=run_context,
                    cancellation_event=cancellation_event,
                )[expected_index]
                left, right = stereo[indices.index(frame.source_index)]
                corrected_frame = match_frame(
                    frame.source_index,
                    left,
                    right,
                    {frame.reference_index: frame.reference, expected_index: reference_frame},
                    expected_offset=frame_offset,
                )
                if corrected_frame is None:
                    continue
                frame = corrected_frame
            corrected.append(frame)
        matched = corrected
    checked_bins = {
        min(SAMPLE_BINS - 1, int(frame.source_index / source_frame_count * SAMPLE_BINS)) for frame in matched
    }
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
    duration = disc_info.duration_seconds
    indices = source_sample_indices(duration, info.frame_rate)
    cli_message("Checking the cut, matching eye and HDR color across the whole film.", run_context=run_context)
    samples = collect_stereo_samples(
        source,
        indices,
        workspace=workspace,
        mvc_producer=mvc_producer,
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    reference_extent = reference_picture_extent(
        reference, info, workspace=workspace, run_context=run_context, cancellation_event=cancellation_event
    )
    initial_offset = reference_extent.first - samples.extent.first
    matched, offset = collect_matched_frames(
        reference,
        info,
        indices,
        samples.eyes,
        source_frame_count=samples.extent.frame_count,
        initial_offset=initial_offset,
        workspace=workspace,
        run_context=run_context,
        cancellation_event=cancellation_event,
    )
    check_black_padding(samples.extent, reference_extent, offset)
    registration = applied_uhd_registration(
        np.median(np.stack([frame.registration.matrix for frame in matched]), axis=0).astype(np.float32)
    )
    color_map = fit_title_color_map(
        [aligned_color_pixels(frame, registration) for frame in matched[::2]],
        [aligned_color_pixels(frame, registration) for frame in matched[1::2]],
    )
    plan = UHDConversionPlan(reference, info, offset, color_map, registration, len(matched), samples.extent.frame_count)
    cli_message(f"4K match accepted: {json.dumps(plan.report())}", run_context=run_context)
    return plan


def write_title_lut(color_map: TitleColorMap, path: Path) -> None:
    axis = np.linspace(0, 1, 65, dtype=np.float32)
    coordinates = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"), axis=-1)[..., ::-1].reshape(-1, 3)
    mapped = np.clip(color_features(coordinates) @ color_map.coefficients, 0, 1)
    with path.open("w") as output:
        output.write("LUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n")
        np.savetxt(output, mapped, fmt="%.9f")


def register_uhd_stream(
    stream: ffmpeg.nodes.FilterableStream, registration: FloatImage, *, mask: bool = False
) -> ffmpeg.nodes.FilterableStream:
    matrix = applied_uhd_registration(registration).copy()
    if np.array_equal(matrix, np.eye(2, 3, dtype=np.float32)):
        return stream
    matrix[:, 2] *= UHD_SIZE[0] / SAMPLE_SIZE[0]
    x = f"{matrix[0, 0]:.10f}*X+{matrix[0, 1]:.10f}*Y+{matrix[0, 2]:.10f}"
    y = f"{matrix[1, 0]:.10f}*X+{matrix[1, 1]:.10f}*Y+{matrix[1, 2]:.10f}"
    inside = f"between({x},0,W-1)*between({y},0,H-1)"
    expressions = {channel: f"if({inside},{channel}({x},{y}),0)" for channel in (("lum",) if mask else ("r", "g", "b"))}
    return stream.filter("geq", **expressions, interpolation="nearest" if mask else "bilinear")


def uhd_picture_mask(source: ffmpeg.nodes.FilterableStream, registration: FloatImage) -> ffmpeg.nodes.FilterableStream:
    binary = (
        source.filter("extractplanes", "y")
        .filter("format", "gray16le")
        .filter("lut", c0=f"if(gt(val,{BLACK_LUMA_8BIT * 257}),65535,0)")
        .filter_multi_output("split", 2)
    )
    rows = (
        binary[0]
        .filter(
            "geq",
            lum="if(eq(X,0),65535*gt(lumsum(W-1,Y),0)*gt(lumsum(W-1,H-1)-if(gt(Y,0),lumsum(W-1,Y-1),0),0),0)",
            interpolation="nearest",
        )
        .filter("crop", 1, "ih", 0, 0)
    )
    columns = (
        binary[1]
        .filter(
            "geq",
            lum="if(eq(Y,0),65535*gt(lumsum(X,H-1),0)*gt(lumsum(W-1,H-1)-if(gt(X,0),lumsum(X-1,H-1),0),0),0)",
            interpolation="nearest",
        )
        .filter("crop", "iw", 1, 0, 0)
    )
    mask = ffmpeg.filter(
        [rows.filter("scale", *UHD_SIZE, flags="neighbor"), columns.filter("scale", *UHD_SIZE, flags="neighbor")],
        "blend",
        all_mode="multiply",
    )
    planes = (
        register_uhd_stream(mask, registration, mask=True).filter("format", "gray10le").filter_multi_output("split", 2)
    )
    chroma = planes[1].filter("scale", UHD_SIZE[0] // 2, UHD_SIZE[1] // 2, flags="neighbor")
    return ffmpeg.filter([planes[0], chroma], "mergeplanes", mapping=0x001010, format="yuv420p10le")


def mask_uhd_picture(
    picture: ffmpeg.nodes.FilterableStream, mask: ffmpeg.nodes.FilterableStream
) -> ffmpeg.nodes.FilterableStream:
    copies = picture.filter_multi_output("split", 2)
    black = copies[0].filter("lut", y="64", u="512", v="512")
    return ffmpeg.filter([black, copies[1], mask], "maskedmerge")


def generate_uhd_normalizer_command(plan: UHDConversionPlan, lut_path: Path) -> list[str]:
    rate = str(plan.info.frame_rate)
    clock = f"1/{plan.info.frame_rate.numerator}"
    frame_time = f"N*{plan.info.frame_rate.denominator}"
    source = ffmpeg.input("pipe:0", f="yuv4mpegpipe", r=rate)
    eyes = source.video.filter("settb", expr=clock).filter("setpts", frame_time).filter_multi_output("split", 2)
    left_mask = uhd_picture_mask(eyes[0].filter("crop", "iw/2", "ih", 0, 0), plan.registration)
    right_source = eyes[1].filter("crop", "iw/2", "ih", "iw/2", 0).filter_multi_output("split", 2)
    right_mask = uhd_picture_mask(right_source[1], plan.registration)
    right = (
        right_source[0]
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
    right = register_uhd_stream(right, plan.registration)
    right = (
        right.filter("lut3d", file=str(lut_path), interp="tetrahedral")
        .filter(
            "scale",
            in_range="pc",
            out_color_matrix="bt2020",
            out_range="tv",
        )
        .filter("format", "yuv420p10le")
    )
    right = mask_uhd_picture(right, right_mask)
    reference = ffmpeg.input(str(plan.reference_path))
    left = (
        reference.video.filter(
            "trim",
            start_frame=max(0, plan.frame_offset),
            end_frame=min(plan.info.frame_count, plan.source_frame_count + plan.frame_offset),
        )
        .filter("settb", expr=clock)
        .filter("setpts", frame_time)
        .filter("format", "yuv420p10le")
    )
    left = left.filter(
        "tpad",
        start=max(0, -plan.frame_offset),
        stop=max(0, plan.source_frame_count + plan.frame_offset - plan.info.frame_count),
        start_mode="add",
        stop_mode="add",
        color="black",
    )
    left = mask_uhd_picture(left, left_mask)
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
    previous_output = output.stat() if output.exists() else None
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
        current_output = output.stat() if output.exists() else None
        if current_output is not None and (
            previous_output is None
            or (current_output.st_dev, current_output.st_ino) != (previous_output.st_dev, previous_output.st_ino)
        ):
            output.unlink(missing_ok=True)
        raise
    finally:
        if not config.keep_files:
            lut_path.unlink(missing_ok=True)
    return output
