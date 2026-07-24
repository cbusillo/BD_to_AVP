#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil

from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import QualificationFailure, ffprobe_stream, select_pipeline_failure


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
INPUT_EYE_WIDTH = 1_920
INPUT_EYE_HEIGHT = 1_080
OUTPUT_EYE_WIDTH = 3_840
OUTPUT_EYE_HEIGHT = 2_160
SOURCE_BUFFER_LIMIT = 4
CONVERTED_INPUT_BUFFER_LIMIT_PER_EYE = 2
OUTPUT_BUFFER_LIMIT_PER_EYE = 4
PRIVATE_OUTPUT_TEXTURE_LIMIT_PER_EYE = 1
COMMAND_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class BenchmarkRun:
    elapsed_seconds: float
    encoder_peak_rss_bytes: int
    mode: str
    output_bytes: int
    stream: dict[str, object]
    summary: dict[str, object]


def command_path(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise QualificationFailure(f"Required command is unavailable: {name}")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def generator_command(ffmpeg: str, *, frames: int, frame_rate: int) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={INPUT_EYE_WIDTH}x{INPUT_EYE_HEIGHT}:rate={frame_rate}",
        "-filter_complex",
        "[0:v]split=2[left][right];[right]hflip[right_flipped];"
        "[left][right_flipped]hstack=inputs=2,format=yuv420p[stereo]",
        "-map",
        "[stereo]",
        "-frames:v",
        str(frames),
        "-f",
        "yuv4mpegpipe",
        "-",
    ]


def encoder_command(
    encoder: Path,
    output_path: Path,
    *,
    frames: int,
    bitrate_mbps: float,
    upscale_mode: str | None,
) -> list[str]:
    command = [
        str(encoder),
        "--output",
        str(output_path),
        "--bitrate-mbps",
        str(bitrate_mbps),
        "--expected-frames",
        str(frames),
        "--overwrite",
    ]
    if upscale_mode is not None:
        command.extend(["--upscale-mode", upscale_mode])
    return command


def declared_pool_payload_bytes(mode: str) -> int:
    if mode not in {"metalfx", "pixel-transfer"}:
        raise ValueError("mode must be metalfx or pixel-transfer")
    source_nv12 = INPUT_EYE_WIDTH * INPUT_EYE_HEIGHT * 3 // 2 * SOURCE_BUFFER_LIMIT
    input_bgra = (
        INPUT_EYE_WIDTH * INPUT_EYE_HEIGHT * 4 * CONVERTED_INPUT_BUFFER_LIMIT_PER_EYE * 2 if mode == "metalfx" else 0
    )
    intermediate_bgra = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * 2 if mode == "metalfx" else 0
    output_bgra = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * OUTPUT_BUFFER_LIMIT_PER_EYE * 2
    return source_nv12 + input_bgra + intermediate_bgra + output_bgra


def declared_metal_payload_bytes(mode: str) -> int:
    if mode == "pixel-transfer":
        return 0
    if mode != "metalfx":
        raise ValueError("mode must be metalfx or pixel-transfer")
    converted_input = INPUT_EYE_WIDTH * INPUT_EYE_HEIGHT * 4 * CONVERTED_INPUT_BUFFER_LIMIT_PER_EYE * 2
    private_output = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * PRIVATE_OUTPUT_TEXTURE_LIMIT_PER_EYE * 2
    pooled_output = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * OUTPUT_BUFFER_LIMIT_PER_EYE * 2
    return converted_input + private_output + pooled_output


def process_rss_bytes(process: psutil.Process) -> int:
    try:
        total = process.memory_info().rss
        for child in process.children(recursive=True):
            try:
                total += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return total
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def kill_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def validate_stream(
    stream: dict[str, object],
    *,
    expected_width: int,
    expected_height: int,
    expected_frames: int,
) -> None:
    if stream.get("width") != expected_width or stream.get("height") != expected_height:
        raise QualificationFailure(f"Unexpected output dimensions: {stream.get('width')}x{stream.get('height')}")
    if stream.get("nb_read_frames") != str(expected_frames):
        raise QualificationFailure(f"Unexpected decoded frame count: {stream.get('nb_read_frames')}")
    expected_color = {
        "color_primaries": "bt709",
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
    }
    if any(stream.get(key) != value for key, value in expected_color.items()):
        raise QualificationFailure("Output does not signal limited-range BT.709 color metadata.")


def run_pipeline(
    ffmpeg: str,
    ffprobe: str,
    encoder: Path,
    output_path: Path,
    *,
    frames: int,
    frame_rate: int,
    bitrate_mbps: float,
    upscale_mode: str | None,
) -> BenchmarkRun:
    output_path.unlink(missing_ok=True)
    generator = subprocess.Popen(
        generator_command(ffmpeg, frames=frames, frame_rate=frame_rate),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert generator.stdout is not None
    encoder_process = subprocess.Popen(
        encoder_command(
            encoder,
            output_path,
            frames=frames,
            bitrate_mbps=bitrate_mbps,
            upscale_mode=upscale_mode,
        ),
        stdin=generator.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    generator.stdout.close()
    measured_process = psutil.Process(encoder_process.pid)
    peak_rss_bytes = process_rss_bytes(measured_process)
    started = time.monotonic()
    try:
        while encoder_process.poll() is None:
            peak_rss_bytes = max(peak_rss_bytes, process_rss_bytes(measured_process))
            if time.monotonic() - started > COMMAND_TIMEOUT_SECONDS:
                raise QualificationFailure("MetalFX benchmark timed out.")
            time.sleep(0.02)
        encoder_stdout, encoder_stderr = encoder_process.communicate(timeout=30)
        generator_stderr = generator.stderr.read() if generator.stderr else b""
        generator_status = generator.wait(timeout=30)
    except BaseException:
        kill_process(encoder_process)
        kill_process(generator)
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
    if failure is not None:
        raise QualificationFailure(failure)

    try:
        summary = json.loads(encoder_stdout)
    except json.JSONDecodeError as error:
        raise QualificationFailure("Encoder did not emit a valid JSON summary.") from error
    if summary.get("frame_count") != frames:
        raise QualificationFailure("Encoder summary frame count does not match the requested fixture.")

    stream = ffprobe_stream(ffprobe, output_path)
    validate_stream(
        stream,
        expected_width=OUTPUT_EYE_WIDTH if upscale_mode else INPUT_EYE_WIDTH,
        expected_height=OUTPUT_EYE_HEIGHT if upscale_mode else INPUT_EYE_HEIGHT,
        expected_frames=frames,
    )
    if upscale_mode is not None:
        expected_limits = {
            "upscale_converted_input_buffer_limit_per_eye": CONVERTED_INPUT_BUFFER_LIMIT_PER_EYE,
            "upscale_output_buffer_limit_per_eye": OUTPUT_BUFFER_LIMIT_PER_EYE,
            "upscale_source_buffer_limit": SOURCE_BUFFER_LIMIT,
        }
        if any(summary.get(key) != value for key, value in expected_limits.items()):
            raise QualificationFailure("Encoder summary does not report the expected bounded upscale pools.")
        if summary.get("upscale_mode") != upscale_mode:
            raise QualificationFailure("Encoder summary does not report the requested upscale mode.")
    if upscale_mode == "metalfx":
        for key in (
            "metalfx_device_final_allocated_size_bytes",
            "metalfx_device_initial_allocated_size_bytes",
            "metalfx_device_peak_allocated_size_bytes",
            "metalfx_device_peak_delta_bytes",
        ):
            if not isinstance(summary.get(key), int) or summary[key] < 0:
                raise QualificationFailure(f"Encoder summary does not report valid Metal allocation telemetry: {key}")

    return BenchmarkRun(
        elapsed_seconds=round(time.monotonic() - started, 6),
        encoder_peak_rss_bytes=peak_rss_bytes,
        mode=upscale_mode or "baseline",
        output_bytes=output_path.stat().st_size,
        stream=stream,
        summary=summary,
    )


def boundedness_result(
    baseline_short_run: BenchmarkRun,
    baseline_long_run: BenchmarkRun,
    short_run: BenchmarkRun,
    long_run: BenchmarkRun,
    *,
    mode: str,
    max_unmodeled_growth_bytes: int,
) -> dict[str, object]:
    declared_payload_bytes = declared_pool_payload_bytes(mode)
    baseline_duration_growth_bytes = max(
        0,
        baseline_long_run.encoder_peak_rss_bytes - baseline_short_run.encoder_peak_rss_bytes,
    )
    duration_growth_bytes = max(0, long_run.encoder_peak_rss_bytes - short_run.encoder_peak_rss_bytes)
    excess_duration_growth_bytes = max(0, duration_growth_bytes - baseline_duration_growth_bytes)
    upscale_over_baseline_bytes = long_run.encoder_peak_rss_bytes - baseline_long_run.encoder_peak_rss_bytes
    peak_rss_ceiling_bytes = (
        baseline_long_run.encoder_peak_rss_bytes + declared_payload_bytes + max_unmodeled_growth_bytes
    )
    rss_passed = (
        excess_duration_growth_bytes <= max_unmodeled_growth_bytes
        and long_run.encoder_peak_rss_bytes <= peak_rss_ceiling_bytes
    )
    result: dict[str, object] = {
        "baseline_duration_peak_rss_growth_bytes": baseline_duration_growth_bytes,
        "baseline_long_peak_rss_bytes": baseline_long_run.encoder_peak_rss_bytes,
        "declared_pool_payload_bytes": declared_payload_bytes,
        "excess_duration_peak_rss_growth_bytes": excess_duration_growth_bytes,
        "max_unmodeled_growth_bytes": max_unmodeled_growth_bytes,
        "observed_duration_peak_rss_growth_bytes": duration_growth_bytes,
        "observed_upscale_peak_rss_over_baseline_bytes": upscale_over_baseline_bytes,
        "mode": mode,
        "peak_rss_ceiling_bytes": peak_rss_ceiling_bytes,
        "rss_passed": rss_passed,
    }
    metal_passed = True
    if mode == "metalfx":
        short_metal_peak = int(short_run.summary["metalfx_device_peak_delta_bytes"])
        long_metal_peak = int(long_run.summary["metalfx_device_peak_delta_bytes"])
        declared_metal_payload = declared_metal_payload_bytes(mode)
        metal_duration_growth = max(0, long_metal_peak - short_metal_peak)
        metal_peak_ceiling = declared_metal_payload + max_unmodeled_growth_bytes
        metal_passed = metal_duration_growth <= max_unmodeled_growth_bytes and long_metal_peak <= metal_peak_ceiling
        result["metal"] = {
            "declared_payload_bytes": declared_metal_payload,
            "duration_growth_bytes": metal_duration_growth,
            "long_peak_delta_bytes": long_metal_peak,
            "passed": metal_passed,
            "peak_ceiling_bytes": metal_peak_ceiling,
            "short_peak_delta_bytes": short_metal_peak,
        }
    result["passed"] = rss_passed and metal_passed
    return result


def run_benchmark(args: argparse.Namespace) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("The MetalFX benchmark requires macOS arm64.")
    if args.short_frames <= 0 or args.long_frames < args.short_frames:
        raise QualificationFailure("--long-frames must be at least --short-frames and both must be positive.")
    if args.frame_rate <= 0 or args.bitrate_mbps <= 0 or args.max_rss_growth_mib < 0:
        raise QualificationFailure("Frame rate, bitrate, and RSS growth limit must be valid positive values.")

    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    artifacts_directory = args.artifacts_directory
    artifacts_directory.mkdir(parents=True, exist_ok=True)
    encoder = args.encoder
    if encoder is None:
        encoder = artifacts_directory / "mv-hevc-encoder"
        build_mv_hevc_encoder_macos.build_encoder(encoder)
    encoder = encoder.resolve()
    if not encoder.is_file():
        raise QualificationFailure(f"Encoder is unavailable: {encoder}")

    capability = subprocess.run(
        [str(encoder), "--capability-probe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if capability.returncode != 0:
        detail = capability.stderr.strip() or capability.stdout.strip()
        raise QualificationFailure(f"Upscale prototype capability probe failed:\n{detail}")
    capability_payload = json.loads(capability.stdout)
    for key in ("metalfx_2x_mv_hevc_supported", "pixel_transfer_2x_mv_hevc_supported"):
        if capability_payload.get(key) is not True:
            raise QualificationFailure(f"Required upscale capability is unavailable: {key}")

    baseline_short = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "baseline-short.mov",
        frames=args.short_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode=None,
    )
    baseline_long = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "baseline-long.mov",
        frames=args.long_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode=None,
    )
    pixel_transfer_short = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "pixel-transfer-short.mov",
        frames=args.short_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode="pixel-transfer",
    )
    pixel_transfer_long = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "pixel-transfer-long.mov",
        frames=args.long_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode="pixel-transfer",
    )
    metalfx_short = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "metalfx-short.mov",
        frames=args.short_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode="metalfx",
    )
    metalfx_long = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "metalfx-long.mov",
        frames=args.long_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        upscale_mode="metalfx",
    )
    metalfx_boundedness = boundedness_result(
        baseline_short,
        baseline_long,
        metalfx_short,
        metalfx_long,
        mode="metalfx",
        max_unmodeled_growth_bytes=args.max_rss_growth_mib * 1024 * 1024,
    )
    pixel_transfer_boundedness = boundedness_result(
        baseline_short,
        baseline_long,
        pixel_transfer_short,
        pixel_transfer_long,
        mode="pixel-transfer",
        max_unmodeled_growth_bytes=args.max_rss_growth_mib * 1024 * 1024,
    )
    evidence = {
        "boundedness": {
            "metalfx": metalfx_boundedness,
            "passed": metalfx_boundedness["passed"] and pixel_transfer_boundedness["passed"],
            "pixel_transfer": pixel_transfer_boundedness,
        },
        "configuration": {
            "bitrate_mbps": args.bitrate_mbps,
            "frame_rate": args.frame_rate,
            "long_frames": args.long_frames,
            "short_frames": args.short_frames,
        },
        "provenance": {
            "benchmark_script_sha256": sha256_file(Path(__file__)),
            "encoder_sha256": sha256_file(encoder),
            "encoder_source_sha256": sha256_file(build_mv_hevc_encoder_macos.SOURCE_PATH),
            "frame_upscaler_source_sha256": sha256_file(build_mv_hevc_encoder_macos.FRAME_UPSCALER_SOURCE_PATH),
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
        "runs": {
            "baseline_long": asdict(baseline_long),
            "baseline_short": asdict(baseline_short),
            "metalfx_long": asdict(metalfx_long),
            "metalfx_short": asdict(metalfx_short),
            "pixel_transfer_long": asdict(pixel_transfer_long),
            "pixel_transfer_short": asdict(pixel_transfer_short),
        },
        "schema_version": 1,
    }
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare direct 2x upscale modes with baseline MV-HEVC dimensions, frame count, and bounded RSS."
    )
    parser.add_argument("--encoder", type=Path, help="Use an existing native encoder instead of building one.")
    parser.add_argument("--output", type=Path, required=True, help="Destination JSON evidence file.")
    parser.add_argument(
        "--artifacts-directory",
        type=Path,
        help="Directory for the encoder and generated MOV files (default: temporary directory).",
    )
    parser.add_argument("--short-frames", type=int, default=24)
    parser.add_argument("--long-frames", type=int, default=240)
    parser.add_argument("--frame-rate", type=int, default=24)
    parser.add_argument("--bitrate-mbps", type=float, default=40.0)
    parser.add_argument(
        "--max-rss-growth-mib",
        type=int,
        default=64,
        help="Allowed RSS growth beyond the declared pool payload (default: 64 MiB).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    temporary_directory: tempfile.TemporaryDirectory[str] | None = None
    if args.artifacts_directory is None:
        temporary_directory = tempfile.TemporaryDirectory(prefix="mv-hevc-metalfx-benchmark-")
        args.artifacts_directory = Path(temporary_directory.name)
    try:
        evidence = run_benchmark(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {args.output.resolve()}")
        if not evidence["boundedness"]["passed"]:
            print(
                "error: Upscale peak RSS growth exceeded the configured duration-independence limit.",
                file=sys.stderr,
            )
            return 1
        return 0
    except QualificationFailure as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        if temporary_directory is not None:
            temporary_directory.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
