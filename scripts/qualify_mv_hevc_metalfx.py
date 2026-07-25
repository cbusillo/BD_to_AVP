#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from bd_to_avp.modules.video_route import AUTOMATIC_DIRECT_UPSCALE_QUALITY
from scripts import benchmark_mv_hevc_metalfx, build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import (
    CURRENT_REQUIRED_BOX_TYPES,
    DIRECT_REQUIRED_BOX_TYPES,
    QualificationFailure,
    box_types,
    command_path,
    ffprobe_stream,
    select_pipeline_failure,
    split_mv_hevc,
    ssim,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FX_UPSCALE = REPOSITORY_ROOT / "bd_to_avp/bin/fx-upscale"
INPUT_EYE_WIDTH = 1_920
INPUT_EYE_HEIGHT = 1_080
OUTPUT_EYE_WIDTH = 3_840
OUTPUT_EYE_HEIGHT = 2_160
COMMAND_TIMEOUT_SECONDS = 600
DEFAULT_AUTOMATIC_MAX_SIZE_RATIO = 1.05
FIXTURE_NAMES = ("motion", "dark", "grain", "animation", "crop", "disparity")


@dataclass(frozen=True)
class QualityRun:
    elapsed_seconds: float
    final_bytes: int
    left_cross_ssim: float
    left_match_ssim: float
    min_eye_order_margin: float
    min_same_eye_ssim: float
    right_cross_ssim: float
    right_match_ssim: float
    sha256: str


@dataclass(frozen=True)
class BitrateProbe:
    bitrate_mbps: float
    elapsed_seconds: float
    final_bytes: int
    sha256: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def high_resolution_source(frame_rate: int, fixture: str = "motion") -> str:
    source = f"testsrc2=size={OUTPUT_EYE_WIDTH}x{OUTPUT_EYE_HEIGHT}:rate={frame_rate}"
    if fixture == "dark":
        return f"{source},eq=brightness=-0.35:contrast=0.75"
    if fixture == "grain":
        return f"{source},noise=alls=12:allf=t+u:all_seed=357"
    if fixture == "animation":
        return f"testsrc=size={OUTPUT_EYE_WIDTH}x{OUTPUT_EYE_HEIGHT}:rate={frame_rate}"
    if fixture == "crop":
        return f"testsrc2=size=4096x2304:rate={frame_rate},crop=3840:2160:128:72"
    if fixture in {"motion", "disparity"}:
        return source
    raise ValueError(f"Unknown fixture: {fixture}")


def right_eye_filter(fixture: str) -> str:
    if fixture == "disparity":
        return "hflip,pad=3872:2160:32:0:black,crop=3840:2160:0:0"
    return "hflip"


def reference_command(
    ffmpeg: str,
    output_path: Path,
    *,
    frames: int,
    frame_rate: int,
    fixture: str,
    right_eye: bool,
) -> list[str | Path]:
    filter_value = f"{right_eye_filter(fixture)},format=yuv420p" if right_eye else "format=yuv420p"
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        high_resolution_source(frame_rate, fixture),
        "-vf",
        filter_value,
        "-frames:v",
        str(frames),
        "-c:v",
        "ffv1",
        "-y",
        output_path,
    ]


def generator_command(ffmpeg: str, *, frames: int, frame_rate: int, fixture: str = "motion") -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        high_resolution_source(frame_rate, fixture),
        "-filter_complex",
        "[0:v]split=2[left_source][right_source];"
        f"[left_source]scale={INPUT_EYE_WIDTH}:{INPUT_EYE_HEIGHT}:flags=lanczos[left];"
        f"[right_source]{right_eye_filter(fixture)},"
        f"scale={INPUT_EYE_WIDTH}:{INPUT_EYE_HEIGHT}:flags=lanczos[right];"
        "[left][right]hstack=inputs=2,format=yuv420p[stereo]",
        "-map",
        "[stereo]",
        "-frames:v",
        str(frames),
        "-f",
        "yuv4mpegpipe",
        "-",
    ]


def run_checked(command: list[str | Path]) -> None:
    try:
        subprocess.run(
            [str(item) for item in command],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="replace").strip()
        raise QualificationFailure(f"Command failed: {' '.join(map(str, command))}\n{detail}") from error


def encode_from_generator(
    ffmpeg: str,
    encoder: Path,
    output_path: Path,
    *,
    frames: int,
    frame_rate: int,
    bitrate_mbps: float | None = None,
    quality: float | None = None,
    upscale_mode: str | None,
    fixture: str = "motion",
) -> tuple[dict[str, object], float]:
    output_path.unlink(missing_ok=True)
    started = time.monotonic()
    generator = subprocess.Popen(
        generator_command(ffmpeg, frames=frames, frame_rate=frame_rate, fixture=fixture),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert generator.stdout is not None
    encoder_process = subprocess.Popen(
        benchmark_mv_hevc_metalfx.encoder_command(
            encoder,
            output_path,
            frames=frames,
            bitrate_mbps=bitrate_mbps,
            quality=quality,
            upscale_mode=upscale_mode,
        ),
        stdin=generator.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    generator.stdout.close()
    try:
        encoder_stdout, encoder_stderr = encoder_process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
        generator_stderr = generator.stderr.read() if generator.stderr else b""
        generator_status = generator.wait(timeout=30)
    except BaseException:
        benchmark_mv_hevc_metalfx.kill_process(encoder_process)
        benchmark_mv_hevc_metalfx.kill_process(generator)
        raise
    finally:
        if generator.stderr and not generator.stderr.closed:
            generator.stderr.close()
    failure = select_pipeline_failure(
        generator_status,
        generator_stderr.decode(errors="replace"),
        encoder_process.returncode,
        encoder_stderr.decode(errors="replace"),
    )
    if failure:
        raise QualificationFailure(failure)
    try:
        summary = json.loads(encoder_stdout)
    except json.JSONDecodeError as error:
        raise QualificationFailure("Encoder returned invalid JSON during MetalFX qualification.") from error
    return summary, time.monotonic() - started


def encode_file_based(
    ffmpeg: str,
    encoder: Path,
    output_path: Path,
    work_directory: Path,
    *,
    frames: int,
    frame_rate: int,
    base_bitrate_mbps: float,
    upscale_quality: float,
    fixture: str = "motion",
) -> tuple[float, int]:
    base_path = work_directory / "file-based-base.mov"
    _, base_elapsed = encode_from_generator(
        ffmpeg,
        encoder,
        base_path,
        frames=frames,
        frame_rate=frame_rate,
        bitrate_mbps=base_bitrate_mbps,
        upscale_mode=None,
        fixture=fixture,
    )
    generated_path = base_path.with_stem(f"{base_path.stem} Upscaled")
    generated_path.unlink(missing_ok=True)
    started = time.monotonic()
    run_checked(
        [
            FX_UPSCALE,
            "--bitrate-scaling-factor",
            str(upscale_quality),
            base_path,
        ]
    )
    upscale_elapsed = time.monotonic() - started
    if not generated_path.is_file():
        raise QualificationFailure("The bundled FX Upscale route did not create its expected output.")
    output_path.unlink(missing_ok=True)
    shutil.move(generated_path, output_path)
    return base_elapsed + upscale_elapsed, base_path.stat().st_size


def measure_quality(
    ffmpeg: str,
    movie_path: Path,
    reference_left: Path,
    reference_right: Path,
    split_directory: Path,
    elapsed_seconds: float,
) -> QualityRun:
    left, right = split_mv_hevc(movie_path, split_directory)
    left_match = ssim(ffmpeg, left, reference_left)
    left_cross = ssim(ffmpeg, left, reference_right)
    right_match = ssim(ffmpeg, right, reference_right)
    right_cross = ssim(ffmpeg, right, reference_left)
    return QualityRun(
        elapsed_seconds=round(elapsed_seconds, 6),
        final_bytes=movie_path.stat().st_size,
        left_cross_ssim=left_cross,
        left_match_ssim=left_match,
        min_eye_order_margin=min(left_match - left_cross, right_match - right_cross),
        min_same_eye_ssim=min(left_match, right_match),
        right_cross_ssim=right_cross,
        right_match_ssim=right_match,
        sha256=sha256_file(movie_path),
    )


def summarize_acceptance(
    file_based_runs: list[QualityRun],
    metalfx_runs: list[QualityRun],
    pixel_transfer_runs: list[QualityRun],
    *,
    quality_tolerance: float,
    size_target_ratio: float,
) -> dict[str, float | bool]:
    if not file_based_runs or not metalfx_runs or not pixel_transfer_runs:
        raise ValueError("all qualification paths require at least one run")
    file_quality = statistics.median(run.min_same_eye_ssim for run in file_based_runs)
    metalfx_quality = statistics.median(run.min_same_eye_ssim for run in metalfx_runs)
    pixel_quality = statistics.median(run.min_same_eye_ssim for run in pixel_transfer_runs)
    file_size = statistics.median(run.final_bytes for run in file_based_runs)
    metalfx_size = statistics.median(run.final_bytes for run in metalfx_runs)
    metalfx_max_size = max(run.final_bytes for run in metalfx_runs)
    file_elapsed = statistics.median(run.elapsed_seconds for run in file_based_runs)
    metalfx_elapsed = statistics.median(run.elapsed_seconds for run in metalfx_runs)
    eye_order_preserved = all(run.min_eye_order_margin > 0.15 for run in metalfx_runs)
    quality_matches = metalfx_quality + quality_tolerance >= file_quality
    size_target_bytes = int(file_size * size_target_ratio)
    size_has_headroom = metalfx_max_size <= size_target_bytes
    faster_than_file_based = metalfx_elapsed <= file_elapsed
    return {
        "eye_order_preserved": eye_order_preserved,
        "file_based_median_elapsed_seconds": file_elapsed,
        "file_based_median_final_bytes": file_size,
        "file_based_median_min_same_eye_ssim": file_quality,
        "metalfx_faster_than_file_based": faster_than_file_based,
        "metalfx_median_elapsed_seconds": metalfx_elapsed,
        "metalfx_median_final_bytes": metalfx_size,
        "metalfx_max_final_bytes": metalfx_max_size,
        "metalfx_median_min_same_eye_ssim": metalfx_quality,
        "metalfx_quality_matches_file_based": quality_matches,
        "metalfx_size_has_headroom": size_has_headroom,
        "passed": eye_order_preserved and quality_matches and size_has_headroom and faster_than_file_based,
        "pixel_transfer_median_min_same_eye_ssim": pixel_quality,
        "quality_tolerance": quality_tolerance,
        "size_target_bytes": size_target_bytes,
        "size_target_ratio": size_target_ratio,
    }


def summarize_automatic_acceptance(
    file_based_runs: list[QualityRun],
    automatic_runs: list[QualityRun],
    *,
    quality_tolerance: float,
    max_size_ratio: float,
) -> dict[str, object]:
    if not file_based_runs or not automatic_runs:
        raise ValueError("File-based and Automatic MetalFX runs are required.")
    file_quality = statistics.median(run.min_same_eye_ssim for run in file_based_runs)
    automatic_quality = statistics.median(run.min_same_eye_ssim for run in automatic_runs)
    file_size = statistics.median(run.final_bytes for run in file_based_runs)
    automatic_size = statistics.median(run.final_bytes for run in automatic_runs)
    automatic_max_size = max(run.final_bytes for run in automatic_runs)
    file_elapsed = statistics.median(run.elapsed_seconds for run in file_based_runs)
    automatic_elapsed = statistics.median(run.elapsed_seconds for run in automatic_runs)
    automatic_max_elapsed = max(run.elapsed_seconds for run in automatic_runs)
    automatic_worst_quality = min(run.min_same_eye_ssim for run in automatic_runs)
    eye_order_preserved = all(run.min_eye_order_margin > 0.15 for run in automatic_runs)
    quality_matches = automatic_worst_quality + quality_tolerance >= file_quality
    size_within_limit = automatic_max_size <= int(file_size * max_size_ratio)
    faster_than_file_based = automatic_max_elapsed <= file_elapsed
    return {
        "automatic_faster_than_file_based": faster_than_file_based,
        "automatic_max_elapsed_seconds": automatic_max_elapsed,
        "automatic_max_final_bytes": automatic_max_size,
        "automatic_median_elapsed_seconds": automatic_elapsed,
        "automatic_median_final_bytes": automatic_size,
        "automatic_median_min_same_eye_ssim": automatic_quality,
        "automatic_quality_matches_file_based": quality_matches,
        "automatic_size_within_limit": size_within_limit,
        "automatic_worst_min_same_eye_ssim": automatic_worst_quality,
        "eye_order_preserved": eye_order_preserved,
        "file_based_median_elapsed_seconds": file_elapsed,
        "file_based_median_final_bytes": file_size,
        "file_based_median_min_same_eye_ssim": file_quality,
        "max_size_ratio": max_size_ratio,
        "passed": eye_order_preserved and quality_matches and size_within_limit and faster_than_file_based,
        "quality_tolerance": quality_tolerance,
    }


def validate_output(ffprobe: str, path: Path, *, frames: int, required_boxes: set[str]) -> None:
    stream = ffprobe_stream(ffprobe, path)
    benchmark_mv_hevc_metalfx.validate_stream(
        stream,
        expected_width=OUTPUT_EYE_WIDTH,
        expected_height=OUTPUT_EYE_HEIGHT,
        expected_frames=frames,
    )
    missing = required_boxes - box_types(path)
    if missing:
        raise QualificationFailure(f"Output is missing required boxes: {', '.join(sorted(missing))}")


def probe_direct_bitrate(
    ffmpeg: str,
    ffprobe: str,
    encoder: Path,
    output_path: Path,
    *,
    bitrate_mbps: float,
    fixture: str,
    frames: int,
    frame_rate: int,
) -> BitrateProbe:
    _, elapsed = encode_from_generator(
        ffmpeg,
        encoder,
        output_path,
        frames=frames,
        frame_rate=frame_rate,
        bitrate_mbps=bitrate_mbps,
        upscale_mode="metalfx",
        fixture=fixture,
    )
    validate_output(ffprobe, output_path, frames=frames, required_boxes=DIRECT_REQUIRED_BOX_TYPES)
    return BitrateProbe(
        bitrate_mbps=bitrate_mbps,
        elapsed_seconds=round(elapsed, 6),
        final_bytes=output_path.stat().st_size,
        sha256=sha256_file(output_path),
    )


def select_direct_bitrate(
    ffmpeg: str,
    ffprobe: str,
    encoder: Path,
    search_directory: Path,
    *,
    file_size_bytes: int,
    fixture: str,
    frames: int,
    frame_rate: int,
    override_bitrate_mbps: float | None,
    search_iterations: int,
    search_max_bitrate_mbps: float,
    search_min_bitrate_mbps: float,
    search_precision_mbps: float,
    size_target_ratio: float,
) -> tuple[float, list[BitrateProbe]]:
    search_directory.mkdir(parents=True, exist_ok=True)
    target_bytes = int(file_size_bytes * size_target_ratio)
    probes: dict[float, BitrateProbe] = {}

    def run_probe(bitrate_mbps: float) -> BitrateProbe:
        normalized_bitrate = round(bitrate_mbps, 4)
        existing = probes.get(normalized_bitrate)
        if existing is not None:
            return existing
        output_name = f"metalfx-{normalized_bitrate:.4f}".replace(".", "-") + ".mov"
        probe = probe_direct_bitrate(
            ffmpeg,
            ffprobe,
            encoder,
            search_directory / output_name,
            bitrate_mbps=normalized_bitrate,
            fixture=fixture,
            frames=frames,
            frame_rate=frame_rate,
        )
        probes[normalized_bitrate] = probe
        return probe

    if override_bitrate_mbps is not None:
        selected = run_probe(override_bitrate_mbps)
        return selected.bitrate_mbps, [selected]

    low = search_min_bitrate_mbps
    high = search_max_bitrate_mbps
    low_probe = run_probe(low)
    if low_probe.final_bytes > target_bytes:
        raise QualificationFailure(
            "The minimum direct bitrate exceeds the configured size target; lower --search-min-bitrate-mbps."
        )
    high_probe = run_probe(high)
    if high_probe.final_bytes <= target_bytes:
        return high_probe.bitrate_mbps, sorted(probes.values(), key=lambda probe: probe.bitrate_mbps)

    for _ in range(search_iterations):
        if high - low <= search_precision_mbps:
            break
        candidate = round((low + high) / 2, 4)
        if candidate in probes:
            break
        candidate_probe = run_probe(candidate)
        if candidate_probe.final_bytes <= target_bytes:
            low = candidate
        else:
            high = candidate

    passing = [probe for probe in probes.values() if probe.final_bytes <= target_bytes]
    if not passing:
        raise QualificationFailure("No direct bitrate probe satisfied the configured size target.")
    selected = max(passing, key=lambda probe: probe.bitrate_mbps)
    return selected.bitrate_mbps, sorted(probes.values(), key=lambda probe: probe.bitrate_mbps)


def require_upscale_capability(encoder: Path) -> None:
    capability = subprocess.run(
        [str(encoder), "--capability-probe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        capability_payload = json.loads(capability.stdout)
    except json.JSONDecodeError as error:
        detail = capability.stderr.strip() or capability.stdout.strip() or "no capability payload"
        raise QualificationFailure(f"MetalFX capability probe returned invalid output:\n{detail}") from error
    if capability.returncode != 0:
        if capability_payload.get("stereo_mv_hevc_encode_supported") is False:
            raise QualificationFailure("Stereo MV-HEVC capability is unavailable.")
        detail = capability.stderr.strip() or capability.stdout.strip()
        raise QualificationFailure(f"MetalFX capability probe failed:\n{detail}")
    if capability_payload.get("metalfx_2x_mv_hevc_supported") is not True:
        raise QualificationFailure("MetalFX 2x MV-HEVC capability is unavailable.")
    if capability_payload.get("pixel_transfer_2x_mv_hevc_supported") is not True:
        raise QualificationFailure("Pixel-transfer 2x MV-HEVC capability is unavailable.")


def run_qualification(args: argparse.Namespace) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("MetalFX quality qualification requires macOS arm64.")
    if args.frames <= 0 or args.frame_rate <= 0 or args.runs <= 0:
        raise QualificationFailure("Frame count, frame rate, and run count must be positive.")
    if args.direct_bitrate_mbps is not None and args.direct_bitrate_mbps <= 0:
        raise QualificationFailure("The direct bitrate override must be positive.")
    if args.base_bitrate_mbps <= 0:
        raise QualificationFailure("Bitrates must be positive.")
    if (
        args.search_iterations <= 0
        or args.search_min_bitrate_mbps <= 0
        or args.search_max_bitrate_mbps <= args.search_min_bitrate_mbps
        or args.search_precision_mbps <= 0
    ):
        raise QualificationFailure("The direct bitrate search bounds and precision must be positive and ordered.")
    if not 0 < args.size_target_ratio < 1:
        raise QualificationFailure(
            "The matched-bitrate size target must be greater than zero and less than one to require strict headroom."
        )
    if (
        not 0 <= args.upscale_quality <= 1
        or not 0 <= args.automatic_quality <= 1
        or args.quality_tolerance < 0
        or args.automatic_max_size_ratio <= 0
    ):
        raise QualificationFailure(
            "Upscale and Automatic quality must be 0 through 1; tolerance and size limits must be positive."
        )
    if not FX_UPSCALE.is_file():
        raise QualificationFailure("The bundled FX Upscale executable is unavailable.")

    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    artifacts = args.artifacts_directory
    artifacts.mkdir(parents=True, exist_ok=True)
    encoder = args.encoder or artifacts / "mv-hevc-encoder"
    if args.encoder is None:
        build_mv_hevc_encoder_macos.build_encoder(encoder)
    encoder = encoder.resolve()

    require_upscale_capability(encoder)

    reference_left = artifacts / "reference-left.mkv"
    reference_right = artifacts / "reference-right.mkv"
    run_checked(
        reference_command(
            ffmpeg,
            reference_left,
            frames=args.frames,
            frame_rate=args.frame_rate,
            fixture=args.fixture,
            right_eye=False,
        )
    )
    run_checked(
        reference_command(
            ffmpeg,
            reference_right,
            frames=args.frames,
            frame_rate=args.frame_rate,
            fixture=args.fixture,
            right_eye=True,
        )
    )

    file_based_runs: list[QualityRun] = []
    metalfx_runs: list[QualityRun] = []
    automatic_metalfx_runs: list[QualityRun] = []
    pixel_transfer_runs: list[QualityRun] = []
    warmup_directory = artifacts / "warmup"
    warmup_directory.mkdir(parents=True, exist_ok=True)
    encode_file_based(
        ffmpeg,
        encoder,
        warmup_directory / "file-based.mov",
        warmup_directory,
        frames=args.frames,
        frame_rate=args.frame_rate,
        base_bitrate_mbps=args.base_bitrate_mbps,
        upscale_quality=args.upscale_quality,
        fixture=args.fixture,
    )

    for run_index in range(args.runs):
        run_directory = artifacts / f"file-run-{run_index + 1}"
        run_directory.mkdir(parents=True, exist_ok=True)
        file_path = run_directory / "file-based.mov"
        file_elapsed, intermediate_bytes = encode_file_based(
            ffmpeg,
            encoder,
            file_path,
            run_directory,
            frames=args.frames,
            frame_rate=args.frame_rate,
            base_bitrate_mbps=args.base_bitrate_mbps,
            upscale_quality=args.upscale_quality,
            fixture=args.fixture,
        )
        validate_output(ffprobe, file_path, frames=args.frames, required_boxes=CURRENT_REQUIRED_BOX_TYPES)
        file_based_runs.append(
            measure_quality(
                ffmpeg,
                file_path,
                reference_left,
                reference_right,
                run_directory / "file-split",
                file_elapsed,
            )
        )
        (run_directory / "file-based-intermediate-bytes.txt").write_text(
            f"{intermediate_bytes}\n",
            encoding="utf-8",
        )

    file_size_bytes = int(statistics.median(run.final_bytes for run in file_based_runs))
    selected_direct_bitrate, bitrate_probes = select_direct_bitrate(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts / "bitrate-search",
        file_size_bytes=file_size_bytes,
        fixture=args.fixture,
        frames=args.frames,
        frame_rate=args.frame_rate,
        override_bitrate_mbps=args.direct_bitrate_mbps,
        search_iterations=args.search_iterations,
        search_max_bitrate_mbps=args.search_max_bitrate_mbps,
        search_min_bitrate_mbps=args.search_min_bitrate_mbps,
        search_precision_mbps=args.search_precision_mbps,
        size_target_ratio=args.size_target_ratio,
    )

    selected_automatic_metalfx: Path | None = None
    for run_index in range(args.runs):
        run_directory = artifacts / f"direct-run-{run_index + 1}"
        run_directory.mkdir(parents=True, exist_ok=True)

        metalfx_path = run_directory / "metalfx.mov"
        _, metalfx_elapsed = encode_from_generator(
            ffmpeg,
            encoder,
            metalfx_path,
            frames=args.frames,
            frame_rate=args.frame_rate,
            bitrate_mbps=selected_direct_bitrate,
            upscale_mode="metalfx",
            fixture=args.fixture,
        )
        validate_output(ffprobe, metalfx_path, frames=args.frames, required_boxes=DIRECT_REQUIRED_BOX_TYPES)
        metalfx_runs.append(
            measure_quality(
                ffmpeg,
                metalfx_path,
                reference_left,
                reference_right,
                run_directory / "metalfx-split",
                metalfx_elapsed,
            )
        )
        pixel_path = run_directory / "pixel-transfer.mov"
        _, pixel_elapsed = encode_from_generator(
            ffmpeg,
            encoder,
            pixel_path,
            frames=args.frames,
            frame_rate=args.frame_rate,
            bitrate_mbps=selected_direct_bitrate,
            upscale_mode="pixel-transfer",
            fixture=args.fixture,
        )
        validate_output(ffprobe, pixel_path, frames=args.frames, required_boxes=DIRECT_REQUIRED_BOX_TYPES)
        pixel_transfer_runs.append(
            measure_quality(
                ffmpeg,
                pixel_path,
                reference_left,
                reference_right,
                run_directory / "pixel-split",
                pixel_elapsed,
            )
        )

        automatic_path = run_directory / "metalfx-automatic.mov"
        automatic_summary, automatic_elapsed = encode_from_generator(
            ffmpeg,
            encoder,
            automatic_path,
            frames=args.frames,
            frame_rate=args.frame_rate,
            quality=args.automatic_quality,
            upscale_mode="metalfx",
            fixture=args.fixture,
        )
        if (
            automatic_summary.get("rate_control") != "quality"
            or automatic_summary.get("quality") != args.automatic_quality
        ):
            raise QualificationFailure("Automatic MetalFX run did not use the requested quality rate control.")
        validate_output(ffprobe, automatic_path, frames=args.frames, required_boxes=DIRECT_REQUIRED_BOX_TYPES)
        automatic_metalfx_runs.append(
            measure_quality(
                ffmpeg,
                automatic_path,
                reference_left,
                reference_right,
                run_directory / "metalfx-automatic-split",
                automatic_elapsed,
            )
        )
        selected_automatic_metalfx = automatic_path

    assert selected_automatic_metalfx is not None
    args.output_movie.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(selected_automatic_metalfx, args.output_movie)
    matched_acceptance = summarize_acceptance(
        file_based_runs,
        metalfx_runs,
        pixel_transfer_runs,
        quality_tolerance=args.quality_tolerance,
        size_target_ratio=args.size_target_ratio,
    )
    automatic_acceptance = summarize_automatic_acceptance(
        file_based_runs,
        automatic_metalfx_runs,
        quality_tolerance=args.quality_tolerance,
        max_size_ratio=args.automatic_max_size_ratio,
    )
    acceptance = {
        **matched_acceptance,
        "automatic": automatic_acceptance,
        "matched_bitrate_passed": matched_acceptance["passed"],
        "passed": matched_acceptance["passed"] is True and automatic_acceptance["passed"] is True,
    }
    return {
        "acceptance": acceptance,
        "configuration": {
            "base_bitrate_mbps": args.base_bitrate_mbps,
            "automatic_max_size_ratio": args.automatic_max_size_ratio,
            "automatic_quality": args.automatic_quality,
            "direct_bitrate_mbps": selected_direct_bitrate,
            "direct_bitrate_override_mbps": args.direct_bitrate_mbps,
            "frame_rate": args.frame_rate,
            "frames": args.frames,
            "fixture": args.fixture,
            "runs": args.runs,
            "search_iterations": args.search_iterations,
            "search_max_bitrate_mbps": args.search_max_bitrate_mbps,
            "search_min_bitrate_mbps": args.search_min_bitrate_mbps,
            "search_precision_mbps": args.search_precision_mbps,
            "size_target_ratio": args.size_target_ratio,
            "upscale_quality": args.upscale_quality,
        },
        "bitrate_search": {
            "file_based_median_final_bytes": file_size_bytes,
            "probes": [asdict(probe) for probe in bitrate_probes],
            "selected_bitrate_mbps": selected_direct_bitrate,
            "target_bytes": int(file_size_bytes * args.size_target_ratio),
        },
        "file_based_runs": [asdict(run) for run in file_based_runs],
        "metalfx_runs": [asdict(run) for run in metalfx_runs],
        "automatic_metalfx_runs": [asdict(run) for run in automatic_metalfx_runs],
        "pixel_transfer_runs": [asdict(run) for run in pixel_transfer_runs],
        "provenance": {
            "encoder_sha256": sha256_file(encoder),
            "encoder_source_sha256": sha256_file(build_mv_hevc_encoder_macos.SOURCE_PATH),
            "frame_upscaler_source_sha256": sha256_file(build_mv_hevc_encoder_macos.FRAME_UPSCALER_SOURCE_PATH),
            "fx_upscale_sha256": sha256_file(FX_UPSCALE),
            "qualification_script_sha256": sha256_file(Path(__file__)),
            "git_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=REPOSITORY_ROOT,
                text=True,
                timeout=30,
            ).strip(),
            "hardware_model": subprocess.check_output(
                ["sysctl", "-n", "hw.model"],
                text=True,
                timeout=30,
            ).strip(),
            "macos_version": platform.mac_ver()[0],
            "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        },
        "schema_version": 2,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare direct MetalFX, pixel-transfer, and file-based FX Upscale MV-HEVC quality."
    )
    parser.add_argument("--encoder", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="Destination JSON evidence file.")
    parser.add_argument("--output-movie", type=Path, required=True)
    parser.add_argument("--artifacts-directory", type=Path)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--frame-rate", type=int, default=24)
    parser.add_argument("--fixture", choices=FIXTURE_NAMES, default="motion")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--direct-bitrate-mbps",
        type=float,
        help="Bypass recorded size-matching search with an explicit direct target.",
    )
    parser.add_argument("--base-bitrate-mbps", type=float, default=10.0)
    parser.add_argument("--upscale-quality", type=float, default=0.75)
    parser.add_argument("--automatic-quality", type=float, default=AUTOMATIC_DIRECT_UPSCALE_QUALITY)
    parser.add_argument("--automatic-max-size-ratio", type=float, default=DEFAULT_AUTOMATIC_MAX_SIZE_RATIO)
    parser.add_argument("--quality-tolerance", type=float, default=0.002)
    parser.add_argument("--size-target-ratio", type=float, default=0.99)
    parser.add_argument("--search-min-bitrate-mbps", type=float, default=0.5)
    parser.add_argument("--search-max-bitrate-mbps", type=float, default=40.0)
    parser.add_argument("--search-precision-mbps", type=float, default=0.1)
    parser.add_argument("--search-iterations", type=int, default=9)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if args.artifacts_directory is None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="mv-hevc-metalfx-quality-")
        args.artifacts_directory = Path(temporary_directory.name)
    try:
        evidence = run_qualification(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {args.output.resolve()}")
        if not evidence["acceptance"]["passed"]:
            print("error: MetalFX quality qualification did not meet every gate.", file=sys.stderr)
            return 1
        return 0
    except (QualificationFailure, subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
