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
SOURCE_BUFFER_LIMIT = 2
INPUT_BUFFER_LIMIT = 2
OUTPUT_BUFFER_LIMIT = 8
COMMAND_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class BenchmarkRun:
    elapsed_seconds: float
    encoder_peak_rss_bytes: int
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
    prototype_metalfx_upscale: bool,
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
    if prototype_metalfx_upscale:
        command.append("--prototype-metalfx-upscale")
    return command


def declared_pool_payload_bytes() -> int:
    source_nv12 = INPUT_EYE_WIDTH * INPUT_EYE_HEIGHT * 3 // 2 * SOURCE_BUFFER_LIMIT
    input_bgra = INPUT_EYE_WIDTH * INPUT_EYE_HEIGHT * 4 * INPUT_BUFFER_LIMIT
    intermediate_bgra = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * 2
    output_bgra = OUTPUT_EYE_WIDTH * OUTPUT_EYE_HEIGHT * 4 * OUTPUT_BUFFER_LIMIT
    return source_nv12 + input_bgra + intermediate_bgra + output_bgra


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
    prototype_metalfx_upscale: bool,
) -> BenchmarkRun:
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
            prototype_metalfx_upscale=prototype_metalfx_upscale,
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
        expected_width=OUTPUT_EYE_WIDTH if prototype_metalfx_upscale else INPUT_EYE_WIDTH,
        expected_height=OUTPUT_EYE_HEIGHT if prototype_metalfx_upscale else INPUT_EYE_HEIGHT,
        expected_frames=frames,
    )
    if prototype_metalfx_upscale:
        expected_limits = {
            "metalfx_input_buffer_limit": INPUT_BUFFER_LIMIT,
            "metalfx_output_buffer_limit": OUTPUT_BUFFER_LIMIT,
            "metalfx_source_buffer_limit": SOURCE_BUFFER_LIMIT,
        }
        if any(summary.get(key) != value for key, value in expected_limits.items()):
            raise QualificationFailure("Encoder summary does not report the expected bounded MetalFX pools.")

    return BenchmarkRun(
        elapsed_seconds=round(time.monotonic() - started, 6),
        encoder_peak_rss_bytes=peak_rss_bytes,
        output_bytes=output_path.stat().st_size,
        stream=stream,
        summary=summary,
    )


def boundedness_result(
    baseline_run: BenchmarkRun,
    short_run: BenchmarkRun,
    long_run: BenchmarkRun,
    *,
    max_unmodeled_growth_bytes: int,
) -> dict[str, object]:
    declared_payload_bytes = declared_pool_payload_bytes()
    duration_growth_bytes = long_run.encoder_peak_rss_bytes - short_run.encoder_peak_rss_bytes
    metalfx_over_baseline_bytes = long_run.encoder_peak_rss_bytes - baseline_run.encoder_peak_rss_bytes
    duration_growth_ceiling_bytes = declared_payload_bytes + max_unmodeled_growth_bytes
    peak_rss_ceiling_bytes = baseline_run.encoder_peak_rss_bytes + declared_payload_bytes + max_unmodeled_growth_bytes
    return {
        "baseline_peak_rss_bytes": baseline_run.encoder_peak_rss_bytes,
        "declared_pool_payload_bytes": declared_payload_bytes,
        "duration_growth_ceiling_bytes": duration_growth_ceiling_bytes,
        "max_unmodeled_growth_bytes": max_unmodeled_growth_bytes,
        "observed_duration_peak_rss_growth_bytes": duration_growth_bytes,
        "observed_metalfx_peak_rss_over_baseline_bytes": metalfx_over_baseline_bytes,
        "peak_rss_ceiling_bytes": peak_rss_ceiling_bytes,
        "passed": (
            duration_growth_bytes <= duration_growth_ceiling_bytes
            and long_run.encoder_peak_rss_bytes <= peak_rss_ceiling_bytes
        ),
    }


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
        [str(encoder), "--capability-probe", "--prototype-metalfx-upscale"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if capability.returncode != 0:
        detail = capability.stderr.strip() or capability.stdout.strip()
        raise QualificationFailure(f"MetalFX prototype capability probe failed:\n{detail}")

    baseline = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "baseline-short.mov",
        frames=args.short_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        prototype_metalfx_upscale=False,
    )
    metalfx_short = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "metalfx-short.mov",
        frames=args.short_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        prototype_metalfx_upscale=True,
    )
    metalfx_long = run_pipeline(
        ffmpeg,
        ffprobe,
        encoder,
        artifacts_directory / "metalfx-long.mov",
        frames=args.long_frames,
        frame_rate=args.frame_rate,
        bitrate_mbps=args.bitrate_mbps,
        prototype_metalfx_upscale=True,
    )
    boundedness = boundedness_result(
        baseline,
        metalfx_short,
        metalfx_long,
        max_unmodeled_growth_bytes=args.max_rss_growth_mib * 1024 * 1024,
    )
    evidence = {
        "boundedness": boundedness,
        "configuration": {
            "bitrate_mbps": args.bitrate_mbps,
            "frame_rate": args.frame_rate,
            "long_frames": args.long_frames,
            "short_frames": args.short_frames,
        },
        "provenance": {
            "encoder_sha256": sha256_file(encoder),
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
            "baseline_short": asdict(baseline),
            "metalfx_long": asdict(metalfx_long),
            "metalfx_short": asdict(metalfx_short),
        },
        "schema_version": 1,
    }
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare direct and prototype MetalFX MV-HEVC dimensions, frame count, and bounded RSS."
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
                "error: MetalFX peak RSS growth exceeded the configured duration-independence limit.",
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
