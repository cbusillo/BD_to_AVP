#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import platform
import re
import resource
import shutil
import subprocess
import tempfile
import time

from dataclasses import dataclass
from pathlib import Path

from scripts import build_mv_hevc_encoder_macos
from scripts.verify_apple_media import verify_apple_media_compatible


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPATIAL_MEDIA_TOOL = REPOSITORY_ROOT / "bd_to_avp/bin/spatial-media-kit-tool"
MP4BOX = REPOSITORY_ROOT / "bd_to_avp/bin/MP4Box"
DIRECT_REQUIRED_BOX_TYPES = {"eyes", "hfov", "hvcC", "lhvC", "proj", "vexu"}
CURRENT_REQUIRED_BOX_TYPES = DIRECT_REQUIRED_BOX_TYPES - {"proj"}
BOX_TYPE_PATTERN = re.compile(r'Type="([A-Za-z0-9 ]{4})"')
SSIM_PATTERN = re.compile(r"All:([0-9.]+)")
COMMAND_TIMEOUT_SECONDS = 180


class QualificationFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class FixtureOptions:
    eye_width: int
    eye_height: int
    frame_rate: int
    duration_seconds: int
    disparity_pixels: int
    bitrate_mbps: float

    @property
    def frame_count(self) -> int:
        return self.frame_rate * self.duration_seconds

    @property
    def source_width(self) -> int:
        return self.eye_width + self.disparity_pixels


@dataclass(frozen=True)
class ProcessMetrics:
    elapsed_seconds: float
    user_cpu_seconds: float
    system_cpu_seconds: float


def command_path(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise QualificationFailure(f"Required command is unavailable: {name}")
    return path


def run(
    command: list[str | Path],
    *,
    timeout: int = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(item) for item in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as error:
        output = (error.stderr or error.stdout or "").strip()
        raise QualificationFailure(f"Command failed: {' '.join(map(str, command))}\n{output}") from error


def measure(command_runner) -> tuple[object, ProcessMetrics]:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    result = command_runner()
    elapsed = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    return result, ProcessMetrics(
        elapsed_seconds=elapsed,
        user_cpu_seconds=after.ru_utime - before.ru_utime,
        system_cpu_seconds=after.ru_stime - before.ru_stime,
    )


def source_filter(options: FixtureOptions) -> str:
    return (
        "[0:v]split=2[left_source][right_source];"
        f"[left_source]crop={options.eye_width}:{options.eye_height}:0:0[left];"
        f"[right_source]crop={options.eye_width}:{options.eye_height}:{options.disparity_pixels}:0[right]"
    )


def generate_reference(
    ffmpeg: str,
    output_path: Path,
    options: FixtureOptions,
    *,
    right_eye: bool,
) -> None:
    horizontal_offset = options.disparity_pixels if right_eye else 0
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            (
                f"testsrc2=size={options.source_width}x{options.eye_height}:"
                f"rate={options.frame_rate}:duration={options.duration_seconds}"
            ),
            "-vf",
            f"crop={options.eye_width}:{options.eye_height}:{horizontal_offset}:0,format=yuv420p",
            "-c:v",
            "ffv1",
            "-y",
            output_path,
        ]
    )


def direct_generator_command(ffmpeg: str, options: FixtureOptions) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        (
            f"testsrc2=size={options.source_width}x{options.eye_height}:"
            f"rate={options.frame_rate}:duration={options.duration_seconds}"
        ),
        "-filter_complex",
        f"{source_filter(options)};[left][right]hstack=inputs=2,format=yuv420p[stereo]",
        "-map",
        "[stereo]",
        "-f",
        "yuv4mpegpipe",
        "-",
    ]


def encoder_command(encoder: Path, output_path: Path, options: FixtureOptions) -> list[str | Path]:
    return [
        encoder,
        "--output",
        output_path,
        "--bitrate-mbps",
        str(options.bitrate_mbps),
        "--fov",
        "90",
        "--baseline-mm",
        "64",
        "--disparity-adjustment",
        "0",
        "--expected-frames",
        str(options.frame_count),
        "--overwrite",
    ]


def select_pipeline_failure(
    generator_status: int,
    generator_stderr: str,
    encoder_status: int,
    encoder_stderr: str,
) -> str | None:
    generator_error = generator_stderr.strip()
    encoder_error = encoder_stderr.strip()
    if generator_status == 0 and encoder_status == 0:
        return None
    if encoder_status != 0 and generator_status != 0:
        upstream_eof_markers = (
            "input is empty",
            "incomplete frame",
            "incomplete header line",
        )
        if any(marker in encoder_error.lower() for marker in upstream_eof_markers):
            return f"Fixture generator failed:\n{generator_error or 'no diagnostic output'}"
        return f"Direct MV-HEVC encoder failed:\n{encoder_error or 'no diagnostic output'}"
    if encoder_status != 0:
        return f"Direct MV-HEVC encoder failed:\n{encoder_error or 'no diagnostic output'}"
    return f"Fixture generator failed:\n{generator_error or 'no diagnostic output'}"


def kill_and_reap(*processes: subprocess.Popen) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()

    unreaped = 0
    for process in processes:
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            unreaped += 1
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    if unreaped:
        raise QualificationFailure(f"Failed to reap {unreaped} timed-out qualification process(es).")


def encode_direct(
    ffmpeg: str,
    encoder: Path,
    output_path: Path,
    options: FixtureOptions,
) -> tuple[dict[str, object], ProcessMetrics]:
    def execute() -> dict[str, object]:
        generator = subprocess.Popen(
            direct_generator_command(ffmpeg, options),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert generator.stdout is not None
        encoder_process = subprocess.Popen(
            [str(item) for item in encoder_command(encoder, output_path, options)],
            stdin=generator.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        generator.stdout.close()
        try:
            encoder_stdout, encoder_stderr = encoder_process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            try:
                kill_and_reap(encoder_process, generator)
            except QualificationFailure as cleanup_error:
                raise cleanup_error from error
            raise QualificationFailure("Direct MV-HEVC encoding timed out.") from error
        try:
            generator_status = generator.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            try:
                kill_and_reap(generator)
            except QualificationFailure as cleanup_error:
                raise cleanup_error from error
            raise QualificationFailure("Fixture generator did not exit after the encoder completed.") from error
        generator_stderr = generator.stderr.read().decode("utf-8", errors="replace") if generator.stderr else ""
        for stream in (generator.stderr, encoder_process.stdout, encoder_process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        failure = select_pipeline_failure(
            generator_status,
            generator_stderr,
            encoder_process.returncode,
            encoder_stderr,
        )
        if failure:
            raise QualificationFailure(failure)
        try:
            summary = json.loads(encoder_stdout)
        except json.JSONDecodeError as error:
            raise QualificationFailure("Direct MV-HEVC encoder returned invalid JSON.") from error
        if not isinstance(summary, dict) or summary.get("schema_version") != 1:
            raise QualificationFailure("Direct MV-HEVC encoder returned an unsupported summary.")
        return summary

    summary, metrics = measure(execute)
    return summary, metrics


def encode_current(
    ffmpeg: str,
    output_path: Path,
    work_directory: Path,
    options: FixtureOptions,
) -> tuple[ProcessMetrics, int]:
    left_path = work_directory / "current-left.mov"
    right_path = work_directory / "current-right.mov"

    def execute() -> None:
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                (
                    f"testsrc2=size={options.source_width}x{options.eye_height}:"
                    f"rate={options.frame_rate}:duration={options.duration_seconds}"
                ),
                "-filter_complex",
                source_filter(options),
                "-map",
                "[left]",
                "-c:v",
                "hevc_videotoolbox",
                "-tag:v",
                "hvc1",
                "-b:v",
                f"{options.bitrate_mbps / 2:g}M",
                "-y",
                left_path,
                "-map",
                "[right]",
                "-c:v",
                "hevc_videotoolbox",
                "-tag:v",
                "hvc1",
                "-b:v",
                f"{options.bitrate_mbps / 2:g}M",
                "-y",
                right_path,
            ]
        )
        run(
            [
                SPATIAL_MEDIA_TOOL,
                "merge",
                "--left-file",
                left_path,
                "--right-file",
                right_path,
                "--quality",
                "75",
                "--left-is-primary",
                "--horizontal-field-of-view",
                "90",
                "--horizontal-disparity-adjustment",
                "0",
                "--output-file",
                output_path,
            ]
        )

    _, metrics = measure(execute)
    peak_intermediate_bytes = left_path.stat().st_size + right_path.stat().st_size
    return metrics, peak_intermediate_bytes


def ffprobe_stream(ffprobe: str, path: Path) -> dict[str, object]:
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            (
                "stream=codec_name,codec_tag_string,width,height,avg_frame_rate,nb_read_frames,"
                "color_range,color_space,color_transfer,color_primaries"
            ),
            "-of",
            "json",
            path,
        ]
    )
    data = json.loads(completed.stdout)
    streams = data.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], dict):
        raise QualificationFailure(f"Expected one video stream in {path.name}.")
    return streams[0]


def box_types(path: Path) -> set[str]:
    completed = run([MP4BOX, "-diso", path, "-std"])
    return set(BOX_TYPE_PATTERN.findall(completed.stdout))


def split_mv_hevc(path: Path, output_directory: Path) -> tuple[Path, Path]:
    output_directory.mkdir(parents=True, exist_ok=True)
    run([SPATIAL_MEDIA_TOOL, "split", "--input-file", path, "--output-dir", output_directory])
    left = sorted(output_directory.glob("*LEFT*.mov"))
    right = sorted(output_directory.glob("*RIGHT*.mov"))
    if len(left) != 1 or len(right) != 1:
        raise QualificationFailure("Spatial Media Toolkit did not produce one left and one right output.")
    return left[0], right[0]


def ssim(ffmpeg: str, candidate: Path, reference: Path) -> float:
    process = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            candidate,
            "-i",
            reference,
            "-lavfi",
            "[0:v][1:v]ssim",
            "-f",
            "null",
            "-",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if process.returncode != 0:
        raise QualificationFailure(f"SSIM comparison failed:\n{process.stderr.strip()}")
    matches = SSIM_PATTERN.findall(process.stderr)
    if not matches:
        raise QualificationFailure("SSIM comparison did not report an aggregate score.")
    return float(matches[-1])


def verify_seeks(ffmpeg: str, path: Path, duration_seconds: int) -> None:
    for position in (0, duration_seconds / 2, max(0, duration_seconds - 0.1)):
        run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(position),
                "-i",
                path,
                "-frames:v",
                "1",
                "-f",
                "null",
                "-",
            ]
        )


def validate_output(
    ffmpeg: str,
    ffprobe: str,
    path: Path,
    reference_left: Path,
    reference_right: Path,
    split_directory: Path,
    options: FixtureOptions,
    *,
    label: str,
    required_box_types: set[str],
) -> dict[str, object]:
    stream = ffprobe_stream(ffprobe, path)
    if stream.get("codec_name") != "hevc" or stream.get("codec_tag_string") != "hvc1":
        raise QualificationFailure(f"{label} output is not an hvc1 HEVC stream.")
    if stream.get("width") != options.eye_width or stream.get("height") != options.eye_height:
        raise QualificationFailure(f"{label} output has unexpected per-eye dimensions.")
    if stream.get("nb_read_frames") != str(options.frame_count):
        raise QualificationFailure(f"{label} output has an unexpected decoded frame count.")
    expected_color = {
        "color_primaries": "bt709",
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
    }
    if any(stream.get(field) != value for field, value in expected_color.items()):
        raise QualificationFailure(f"{label} output does not preserve limited-range BT.709 signaling.")

    observed_boxes = box_types(path)
    missing_boxes = sorted(required_box_types - observed_boxes)
    if missing_boxes:
        raise QualificationFailure(f"{label} output is missing required boxes: {', '.join(missing_boxes)}")

    left, right = split_mv_hevc(path, split_directory)
    left_stream = ffprobe_stream(ffprobe, left)
    right_stream = ffprobe_stream(ffprobe, right)
    for split_stream in (left_stream, right_stream):
        if split_stream.get("nb_read_frames") != str(options.frame_count):
            raise QualificationFailure("A split eye output has an unexpected frame count.")
        if split_stream.get("width") != options.eye_width or split_stream.get("height") != options.eye_height:
            raise QualificationFailure("A split eye output has unexpected dimensions.")

    left_match = ssim(ffmpeg, left, reference_left)
    left_cross = ssim(ffmpeg, left, reference_right)
    right_match = ssim(ffmpeg, right, reference_right)
    right_cross = ssim(ffmpeg, right, reference_left)
    if min(left_match, right_match) < 0.85:
        raise QualificationFailure(f"{label} output eye quality fell below the bounded fixture threshold.")
    if left_match <= left_cross + 0.15 or right_match <= right_cross + 0.15:
        raise QualificationFailure(f"{label} output eye order is not distinguishable from the crossed comparison.")

    verify_apple_media_compatible(path)
    verify_seeks(ffmpeg, path, options.duration_seconds)
    return {
        "box_types": sorted(observed_boxes & DIRECT_REQUIRED_BOX_TYPES),
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "right_cross_ssim": right_cross,
        "right_match_ssim": right_match,
        "stream": stream,
    }


def metric_dict(metrics: ProcessMetrics) -> dict[str, float]:
    return {
        "elapsed_seconds": round(metrics.elapsed_seconds, 6),
        "system_cpu_seconds": round(metrics.system_cpu_seconds, 6),
        "user_cpu_seconds": round(metrics.user_cpu_seconds, 6),
    }


def qualify(output_path: Path, encoder_path: Path, options: FixtureOptions) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("Direct MV-HEVC qualification requires macOS arm64.")
    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    for tool in (SPATIAL_MEDIA_TOOL, MP4BOX):
        if not tool.is_file() or not tool.stat().st_mode & 0o111:
            raise QualificationFailure(f"Required bundled tool is unavailable: {tool.name}")

    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="direct-mv-hevc-qualification-") as temporary_directory:
        work_directory = Path(temporary_directory)
        reference_left = work_directory / "left-reference.mkv"
        reference_right = work_directory / "right-reference.mkv"
        current_output = work_directory / "current.mov"
        direct_output = work_directory / "direct.mov"

        generate_reference(ffmpeg, reference_left, options, right_eye=False)
        generate_reference(ffmpeg, reference_right, options, right_eye=True)
        direct_summary, direct_metrics = encode_direct(ffmpeg, encoder_path, direct_output, options)
        current_metrics, current_intermediate_bytes = encode_current(
            ffmpeg,
            current_output,
            work_directory,
            options,
        )
        direct_validation = validate_output(
            ffmpeg,
            ffprobe,
            direct_output,
            reference_left,
            reference_right,
            work_directory / "direct-split",
            options,
            label="Direct",
            required_box_types=DIRECT_REQUIRED_BOX_TYPES,
        )
        current_validation = validate_output(
            ffmpeg,
            ffprobe,
            current_output,
            reference_left,
            reference_right,
            work_directory / "current-split",
            options,
            label="Current",
            required_box_types=CURRENT_REQUIRED_BOX_TYPES,
        )
        shutil.copy2(direct_output, output_path)

        direct_size = direct_output.stat().st_size
        current_size = current_output.stat().st_size
        return {
            "capabilities": {
                "gpu_measurement": "not_available_in_bounded_cli_probe",
                "physical_vision_pro_validation": "required_before_production_default",
                "stereo_mv_hevc_encode": True,
            },
            "comparison": {
                "direct_to_current_elapsed_ratio": round(
                    direct_metrics.elapsed_seconds / current_metrics.elapsed_seconds,
                    6,
                ),
                "eliminated_eye_intermediate_bytes": current_intermediate_bytes,
                "final_size_delta_bytes": direct_size - current_size,
            },
            "current_path": {
                **metric_dict(current_metrics),
                "final_bytes": current_size,
                "peak_eye_intermediate_bytes": current_intermediate_bytes,
                "validation": current_validation,
            },
            "direct_path": {
                **metric_dict(direct_metrics),
                "encoder_summary": direct_summary,
                "final_bytes": direct_size,
                "peak_eye_intermediate_bytes": 0,
                "validation": direct_validation,
            },
            "fixture": {
                "disparity_pixels": options.disparity_pixels,
                "duration_seconds": options.duration_seconds,
                "eye_height": options.eye_height,
                "eye_width": options.eye_width,
                "frame_count": options.frame_count,
                "frame_rate": options.frame_rate,
            },
            "schema_version": 1,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Build and qualify the decoded-stereo-to-direct-MV-HEVC prototype.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/direct-mv-hevc/direct.mov"),
        help="Destination for the validated direct MV-HEVC fixture.",
    )
    parser.add_argument(
        "--encoder",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
        help="Prototype encoder executable; it is built when missing.",
    )
    parser.add_argument("--eye-width", type=int, default=320)
    parser.add_argument("--eye-height", type=int, default=180)
    parser.add_argument("--frame-rate", type=int, default=24)
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--disparity-pixels", type=int, default=16)
    parser.add_argument("--bitrate-mbps", type=float, default=4.0)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()

    if args.eye_width <= 0 or args.eye_height <= 0 or args.eye_width % 2 or args.eye_height % 2:
        parser.error("eye dimensions must be positive even integers")
    if args.frame_rate <= 0 or args.duration <= 0:
        parser.error("frame rate and duration must be positive")
    if args.disparity_pixels < 0 or args.disparity_pixels % 2:
        parser.error("disparity pixels must be a non-negative even integer")
    if args.bitrate_mbps <= 0:
        parser.error("bitrate must be positive")

    options = FixtureOptions(
        eye_width=args.eye_width,
        eye_height=args.eye_height,
        frame_rate=args.frame_rate,
        duration_seconds=args.duration,
        disparity_pixels=args.disparity_pixels,
        bitrate_mbps=args.bitrate_mbps,
    )
    try:
        result = qualify(args.output.resolve(), args.encoder.resolve(), options)
    except QualificationFailure as error:
        parser.exit(1, f"error: {error}\n")

    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
