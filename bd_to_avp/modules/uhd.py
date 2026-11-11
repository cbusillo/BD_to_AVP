from __future__ import annotations

import json
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from bd_to_avp.modules.command import run_ffprobe
from bd_to_avp.modules.config import config
from bd_to_avp.modules.disc import DiscInfo
from bd_to_avp.modules.uhd_color import (
    FloatImage,
    HDRReferenceInfo,
    SAMPLE_SIZE,
    UHD_SIZE,
    TitleColorMap,
    fit_title_color_map,
    pack_hdr_stereo,
    read_hdr_reference_info,
)
from bd_to_avp.modules.uhd_match import (
    MAX_OFFSET_SECONDS,
    SAMPLE_BINS,
    MatchedFrame,
    aligned_color_pixels,
    check_reference_timing,
    match_frame,
    source_sample_indices,
)
from bd_to_avp.modules.uhd_stream import FrameStream, binary_process_spec, encode_frame_stream
from bd_to_avp.modules.video import (
    Edge264DamageCollector,
    MvcProducer,
    generate_direct_mv_hevc_encoder_command,
    generate_mvc_annex_b_stream_command,
    generate_native_mvc_splitter_command,
    remove_direct_mv_hevc_attempt_artifacts,
    resolve_direct_mv_hevc_artifact,
)
from bd_to_avp.modules.video_route import ResolvedVideoRoute, VideoRoutePreflightError
from bd_to_avp.observability import ObservabilityContext
from bd_to_avp.presentation import cli_message
from bd_to_avp.process_runner import ProcessArtifactProbe, ProcessSpec
from bd_to_avp.runtime import RunContext


@dataclass(frozen=True)
class UHDConversionPlan:
    reference_path: Path
    info: HDRReferenceInfo
    frame_offset: int
    color_map: TitleColorMap
    registration: FloatImage
    sampled_frames: int
    source_duration_seconds: float = 0

    def report(self) -> dict[str, object]:
        return {
            "sampled_frames": self.sampled_frames,
            "frame_offset": self.frame_offset,
            "registration": self.registration.tolist(),
            "color_validation": self.color_map.validation,
            "mastering_display": self.info.mastering_display,
            "max_cll": self.info.max_cll,
        }


def mvc_frame_specs(
    source: Path, *, filters: str, mvc_producer: MvcProducer | None, width: int, height: int
) -> tuple[ProcessSpec, ...]:
    specs = []
    if mvc_producer is not None:
        specs.append(binary_process_spec(mvc_producer.command, mvc_producer.tool_id))
    elif source.suffix.lower() in {".mkv", ".mts", ".m2ts"}:
        specs.append(binary_process_spec(tuple(generate_mvc_annex_b_stream_command(source)), "ffmpeg"))
    splitter_input = Path("-") if specs else source
    specs.append(binary_process_spec(tuple(generate_native_mvc_splitter_command(splitter_input)), "edge264"))
    specs.append(
        binary_process_spec(
            (
                str(config.FFMPEG_PATH),
                "-v",
                "error",
                "-f",
                "yuv4mpegpipe",
                "-i",
                "-",
                "-vf",
                f"{filters}scale={width}:{height}:flags=lanczos:in_color_matrix=bt709:in_range=tv:out_range=pc",
                "-fps_mode",
                "passthrough",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ),
            "ffmpeg",
        )
    )
    return tuple(specs)


def reference_frame_spec(reference: Path, filters: str, *, sample: bool) -> ProcessSpec:
    output_filters = (
        f"{filters}scale={SAMPLE_SIZE[0]}:{SAMPLE_SIZE[1]}:flags=area:in_color_matrix=bt2020:in_range=tv:out_range=pc"
        if sample
        else filters.rstrip(",") or "null"
    )
    return binary_process_spec(
        (
            str(config.FFMPEG_PATH),
            "-v",
            "error",
            "-i",
            str(reference),
            "-map",
            "0:v:0",
            "-vf",
            output_filters,
            "-fps_mode",
            "passthrough",
            "-pix_fmt",
            "rgb48le" if sample else "yuv420p10le",
            "-f",
            "rawvideo",
            "-",
        ),
        "ffmpeg",
    )


def collect_stereo_samples(
    source: Path,
    indices: tuple[int, ...],
    *,
    mvc_producer: MvcProducer | None,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> list[tuple[FloatImage, FloatImage]]:
    selection = "+".join(f"eq(n,{index})" for index in indices)
    specs = mvc_frame_specs(
        source,
        filters=f"select='{selection}',",
        mvc_producer=mvc_producer,
        width=SAMPLE_SIZE[0] * 2,
        height=SAMPLE_SIZE[1],
    )
    samples = []
    damage = Edge264DamageCollector()
    with FrameStream(
        specs,
        SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 6,
        run_context=run_context,
        cancellation_event=cancellation_event,
        line_handler=damage.handle_line,
        timeout_seconds=900,
    ) as stream:
        for _ in indices:
            payload = stream.read()
            if payload is None:
                raise VideoRoutePreflightError(
                    "The 3D source ends before its stated runtime. Its cut cannot be checked."
                )
            image = np.frombuffer(payload, np.uint8).reshape(SAMPLE_SIZE[1], SAMPLE_SIZE[0] * 2, 3).astype(
                np.float32
            ) / np.float32(255)
            samples.append((image[:, : SAMPLE_SIZE[0]], image[:, SAMPLE_SIZE[0] :]))
        if stream.read() is not None:
            raise RuntimeError("The MVC sample reader returned unexpected pictures.")
    if damage.skipped_count:
        raise VideoRoutePreflightError(
            "The 3D source has damaged pictures. A frame-accurate 4K match cannot be checked."
        )
    return samples


def collect_matched_frames(
    reference: Path,
    info: HDRReferenceInfo,
    indices: tuple[int, ...],
    stereo: list[tuple[FloatImage, FloatImage]],
    *,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> tuple[list[MatchedFrame], int]:
    radius = round(float(info.frame_rate) * MAX_OFFSET_SECONDS)
    last_reference_frame = int(info.duration_seconds * info.frame_rate) - 1
    windows = [range(max(0, index - radius), min(last_reference_frame, index + radius) + 1) for index in indices]
    selection = "+".join(f"between(n,{window.start},{window.stop - 1})" for window in windows)
    selected_indices = sorted({index for window in windows for index in window})
    matched: list[MatchedFrame] = []
    frame_offset = None
    checked_bins: set[int] = set()
    with FrameStream(
        (reference_frame_spec(reference, f"select='{selection}',", sample=True),),
        SAMPLE_SIZE[0] * SAMPLE_SIZE[1] * 6,
        run_context=run_context,
        cancellation_event=cancellation_event,
        timeout_seconds=900,
    ) as stream:
        pending: dict[int, FloatImage] = {}
        position = 0
        for sample_position, (source_index, eyes, window) in enumerate(zip(indices, stereo, windows, strict=True)):
            while position < len(selected_indices) and selected_indices[position] < window.stop:
                payload = stream.read()
                if payload is None:
                    raise VideoRoutePreflightError(
                        "The 4K copy ends before the matching pictures. Choose the same cut."
                    )
                pending[selected_indices[position]] = np.frombuffer(payload, "<u2").reshape(
                    SAMPLE_SIZE[1], SAMPLE_SIZE[0], 3
                ).astype(np.float32) / np.float32(65535)
                position += 1
            candidates = {index: pending[index] for index in window if index in pending}
            result = match_frame(source_index, eyes[0], eyes[1], candidates, expected_offset=frame_offset)
            if result is not None:
                if frame_offset is None and result.temporal_verified:
                    frame_offset = result.reference_index - source_index
                    if frame_offset < 0:
                        raise VideoRoutePreflightError("The 4K copy is missing the beginning of the 3D source.")
                matched.append(result)
                checked_bins.add(
                    min(
                        SAMPLE_BINS - 1,
                        int(source_index / float(info.frame_rate) / info.duration_seconds * SAMPLE_BINS),
                    )
                )
            next_start = (
                windows[sample_position + 1].start if sample_position + 1 < len(windows) else last_reference_frame + 1
            )
            pending = {index: image for index, image in pending.items() if index >= next_start}
        if stream.read() is not None:
            raise RuntimeError("The 4K sample reader returned unexpected pictures.")
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
    mvc_producer: MvcProducer | None = None,
    run_context: RunContext | None = None,
    cancellation_event: threading.Event | None = None,
) -> UHDConversionPlan:
    info = read_hdr_reference_info(
        run_ffprobe(reference, run_context=run_context, cancellation_event=cancellation_event)
    )
    check_reference_timing(info, disc_info.frame_rate, disc_info.duration_seconds)
    if disc_info.resolution != "1920x1080" or disc_info.is_interlaced or disc_info.color_depth != 8:
        raise VideoRoutePreflightError("The 3D source must be progressive 1920 by 1080 Blu-ray MVC video.")
    indices = source_sample_indices(disc_info.duration_seconds, info.frame_rate)
    cli_message(
        "Checking the cut, matching eye and HDR color across the whole film. This decodes both copies once.",
        run_context=run_context,
    )
    stereo = collect_stereo_samples(
        source, indices, mvc_producer=mvc_producer, run_context=run_context, cancellation_event=cancellation_event
    )
    matched, frame_offset = collect_matched_frames(
        reference, info, indices, stereo, run_context=run_context, cancellation_event=cancellation_event
    )
    training = [aligned_color_pixels(frame) for frame in matched[::2]]
    validation = [aligned_color_pixels(frame) for frame in matched[1::2]]
    if info.duration_seconds + 0.1 < disc_info.duration_seconds + frame_offset / float(info.frame_rate):
        raise VideoRoutePreflightError("The matching 4K copy does not cover the whole 3D movie.")
    color_map = fit_title_color_map(training, validation)
    registration = np.median(np.stack([frame.registration.matrix for frame in matched]), axis=0).astype(np.float32)
    plan = UHDConversionPlan(
        reference, info, frame_offset, color_map, registration, len(matched), disc_info.duration_seconds
    )
    cli_message(
        f"4K match accepted: {len(matched)} frames across the film. "
        f"HDR color check: {json.dumps(color_map.validation)}",
        run_context=run_context,
    )
    return plan


def hdr_stereo_frames(
    source: Path,
    plan: UHDConversionPlan,
    *,
    mvc_producer: MvcProducer | None,
    stop: threading.Event,
    run_context: RunContext | None,
    cancellation_event: threading.Event | None,
) -> Generator[bytes, None, None]:
    width, height = UHD_SIZE
    rate = plan.info.frame_rate
    yield (
        f"YUV4MPEG2 W{width * 2} H{height} F{rate.numerator}:{rate.denominator} Ip A0:0 C420p10 XCOLORRANGE=LIMITED\n"
    ).encode()
    source_specs = mvc_frame_specs(source, filters="", mvc_producer=mvc_producer, width=3840, height=1080)
    reference_spec = reference_frame_spec(plan.reference_path, f"trim=start_frame={plan.frame_offset},", sample=False)
    matrix = plan.registration.copy()
    matrix[:, 2] *= width / SAMPLE_SIZE[0]
    damage = Edge264DamageCollector()
    frame_count = 0
    with (
        FrameStream(
            source_specs,
            3840 * 1080 * 3,
            run_context=run_context,
            cancellation_event=stop,
            line_handler=damage.handle_line,
        ) as original,
        FrameStream(
            (reference_spec,), width * height * 3, run_context=run_context, cancellation_event=stop
        ) as reference,
    ):
        while True:
            if cancellation_event is not None and cancellation_event.is_set():
                stop.set()
            stereo = original.read()
            if stereo is None:
                break
            left_hdr = reference.read()
            if left_hdr is None:
                raise RuntimeError("The 4K copy ended before the 3D source. No movie was completed.")
            if damage.skipped_count:
                raise RuntimeError("The 3D decoder found damaged pictures. The matching 4K encode was stopped.")
            right = np.frombuffer(stereo, np.uint8).reshape(1080, 3840, 3)[:, 1920:].astype(np.float32) / np.float32(
                255
            )
            upscaled = cv2.resize(right, UHD_SIZE, interpolation=cv2.INTER_LANCZOS4)
            aligned = cv2.warpAffine(
                upscaled,
                matrix,
                UHD_SIZE,
                flags=cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP,
                borderMode=cv2.BORDER_REPLICATE,
            )
            yield pack_hdr_stereo(left_hdr, plan.color_map.apply(np.clip(aligned, 0, 1)))
            frame_count += 1
        if plan.source_duration_seconds and abs(frame_count / float(rate) - plan.source_duration_seconds) > 1:
            raise RuntimeError("The 3D decoder ended at a different runtime. No 4K HDR movie was completed.")


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
) -> Path:
    from dataclasses import replace

    command = generate_direct_mv_hevc_encoder_command(output, route.direct_bitrate_mbps, route.direct_quality)
    command.extend(["--color", "hdr10"])
    if plan.info.mastering_display is not None:
        command.extend(["--mastering-display", plan.info.mastering_display])
    if plan.info.max_cll is not None:
        command.extend(["--max-cll", plan.info.max_cll])
    spec = replace(
        binary_process_spec(tuple(command), "mv_hevc_encoder"),
        event_context=observability_context or ObservabilityContext(),
        artifacts=(
            ProcessArtifactProbe("stereo_video_output", resolver=lambda: resolve_direct_mv_hevc_artifact(output)),
        ),
        artifact_no_growth_timeout_seconds=120,
    )
    try:
        encode_frame_stream(
            spec,
            lambda stop: hdr_stereo_frames(
                source,
                plan,
                mvc_producer=mvc_producer,
                stop=stop,
                run_context=run_context,
                cancellation_event=cancellation_event,
            ),
            run_context=run_context,
            cancellation_event=cancellation_event,
        )
    except BaseException:
        remove_direct_mv_hevc_attempt_artifacts(output)
        raise
    return output
