#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import signal
import subprocess
import time
import uuid

from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from scripts.qualify_direct_mv_hevc import (
    CURRENT_REQUIRED_BOX_TYPES,
    DIRECT_REQUIRED_BOX_TYPES,
    box_types,
    verify_seeks,
)
from scripts.qualify_mv_hevc_corpus import summarize_quality_size_gate
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.verify_packaged_mv_hevc_routes import (
    MEDIA_DURATION_TOLERANCE_SECONDS,
    PACKAGE_BIN_RELATIVE_PATH,
    AppBundle,
    PackagedRouteFailure,
    _media_duration,
    _media_probe,
    _media_streams,
    app_tree_sha256,
    build_worker_request,
    metalfx_unavailable_capability_app,
    read_app_bundle,
    run_worker,
    validate_route_pair,
    validate_stage_contract,
)


EXPECTED_RUNS = 3
QUALITY_TOLERANCE = 0.002
MAX_SIZE_RATIO = 1.05
MINIMUM_EYE_ORDER_MARGIN = 0.001
MAX_EVIDENCE_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 30 * 60
SSIM_PATTERN = re.compile(r"All:([0-9.]+)")


class RealMVCQualityFailure(RuntimeError):
    pass


def require_mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if not isinstance(value, Mapping):
        raise RealMVCQualityFailure(f"Real-MVC quality evidence omitted mapping {key!r}.")
    return value


def require_int(document: Mapping[str, object], key: str) -> int:
    value = document.get(key)
    if type(value) is not int:
        raise RealMVCQualityFailure(f"Real-MVC quality evidence omitted integer {key!r}.")
    return value


def require_float(document: Mapping[str, object], key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RealMVCQualityFailure(f"Real-MVC quality evidence omitted number {key!r}.")
    return float(value)


def require_bool(document: Mapping[str, object], key: str) -> bool:
    value = document.get(key)
    if type(value) is not bool:
        raise RealMVCQualityFailure(f"Real-MVC quality evidence omitted boolean {key!r}.")
    return value


def packaged_tool(app: AppBundle, name: str) -> Path:
    path = app.path / PACKAGE_BIN_RELATIVE_PATH / name
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RealMVCQualityFailure(f"Packaged quality tool is unavailable: {name}")
    return path


def run_checked(command: Sequence[str | Path], *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> str:
    try:
        completed = subprocess.run(
            [str(item) for item in command],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RealMVCQualityFailure("Real-MVC quality command failed.") from error
    return completed.stdout


def parse_json_object(text: str, description: str) -> Mapping[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise RealMVCQualityFailure(f"{description} emitted invalid JSON.") from error
    if not isinstance(payload, Mapping):
        raise RealMVCQualityFailure(f"{description} did not emit a JSON object.")
    return payload


def terminate_pipeline_processes(*processes: subprocess.Popen[bytes]) -> None:
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)


def run_pipeline(
    producer_command: Sequence[str | Path],
    consumer_command: Sequence[str | Path],
) -> None:
    try:
        producer = subprocess.Popen(
            [str(item) for item in producer_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        raise RealMVCQualityFailure("Could not start the real-MVC reference producer.") from error
    assert producer.stdout is not None
    try:
        consumer = subprocess.Popen(
            [str(item) for item in consumer_command],
            stdin=producer.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as error:
        terminate_pipeline_processes(producer)
        raise RealMVCQualityFailure("Could not start the real-MVC reference consumer.") from error
    producer.stdout.close()
    try:
        consumer.wait(timeout=COMMAND_TIMEOUT_SECONDS)
        producer.wait(timeout=60)
    except subprocess.TimeoutExpired as error:
        terminate_pipeline_processes(consumer, producer)
        raise RealMVCQualityFailure("Real-MVC reference preparation timed out.") from error
    if producer.returncode != 0 or consumer.returncode != 0:
        raise RealMVCQualityFailure("Real-MVC reference preparation failed.")


def source_video_info(ffprobe: Path, source: Path) -> dict[str, object]:
    payload = parse_json_object(
        run_checked(
            [
                ffprobe,
                "-v",
                "error",
                "-count_packets",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=avg_frame_rate,nb_read_packets:format=duration",
                "-of",
                "json",
                source,
            ]
        ),
        "Representative segment probe",
    )
    streams = payload.get("streams")
    format_data = payload.get("format")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise RealMVCQualityFailure("Representative segment did not report one video stream.")
    if not isinstance(format_data, Mapping):
        raise RealMVCQualityFailure("Representative segment did not report container timing.")
    frame_rate = streams[0].get("avg_frame_rate")
    packet_count = streams[0].get("nb_read_packets")
    duration = format_data.get("duration")
    if not isinstance(frame_rate, str) or not isinstance(packet_count, str) or duration is None:
        raise RealMVCQualityFailure("Representative segment omitted frame timing.")
    try:
        parsed_packets = int(packet_count)
        parsed_duration = float(duration)
    except ValueError as error:
        raise RealMVCQualityFailure("Representative segment reported invalid frame timing.") from error
    if parsed_packets <= 0 or parsed_duration <= 0:
        raise RealMVCQualityFailure("Representative segment reported non-positive frame timing.")
    return {
        "duration_seconds": parsed_duration,
        "frame_rate": frame_rate,
        "frame_count": parsed_packets,
    }


def probe_eye(ffprobe: Path, path: Path, *, expected_dimensions: tuple[int, int], expected_frames: int) -> int:
    payload = parse_json_object(
        run_checked(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,nb_read_frames",
                "-of",
                "json",
                path,
            ]
        ),
        "Quality eye probe",
    )
    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise RealMVCQualityFailure("Quality eye did not contain one video stream.")
    stream = streams[0]
    if (stream.get("width"), stream.get("height")) != expected_dimensions:
        raise RealMVCQualityFailure("Quality eye reported unexpected dimensions.")
    try:
        frame_count = int(stream.get("nb_read_frames") or 0)
    except (TypeError, ValueError) as error:
        raise RealMVCQualityFailure("Quality eye reported an invalid frame count.") from error
    if frame_count != expected_frames:
        raise RealMVCQualityFailure("Quality eye frame count did not match the representative source.")
    return frame_count


def prepare_reference_eyes(
    app: AppBundle,
    source: Path,
    work_directory: Path,
    source_info: Mapping[str, object],
) -> tuple[Path, Path]:
    edge264 = packaged_tool(app, "edge264_test")
    annex_b = work_directory / "reference-source.264"
    left = work_directory / "reference-left.mkv"
    right = work_directory / "reference-right.mkv"
    run_checked(
        [
            app.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "h264_mp4toannexb",
            "-f",
            "h264",
            "-y",
            annex_b,
        ]
    )
    run_pipeline(
        [edge264, annex_b, "-Omk"],
        [
            app.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "yuv4mpegpipe",
            "-r",
            str(source_info["frame_rate"]),
            "-i",
            "-",
            "-filter_complex",
            (
                "[0:v]split=2[left_source][right_source];"
                "[left_source]crop=1920:1080:0:0[left];"
                "[right_source]crop=1920:1080:1920:0[right]"
            ),
            "-map",
            "[left]",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-y",
            left,
            "-map",
            "[right]",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-y",
            right,
        ],
    )
    annex_b.unlink(missing_ok=True)
    for path in (left, right):
        path.chmod(0o600)
        probe_eye(
            app.ffprobe,
            path,
            expected_dimensions=(1920, 1080),
            expected_frames=require_int(source_info, "frame_count"),
        )
    return left, right


def split_mv_hevc(app: AppBundle, movie: Path, output_directory: Path) -> tuple[Path, Path]:
    spatial_media_tool = packaged_tool(app, "spatial-media-kit-tool")
    output_directory.mkdir(parents=True, mode=0o700)
    run_checked([spatial_media_tool, "split", "--input-file", movie, "--output-dir", output_directory])
    left = sorted(output_directory.glob("*LEFT*.mov"))
    right = sorted(output_directory.glob("*RIGHT*.mov"))
    if len(left) != 1 or len(right) != 1:
        raise RealMVCQualityFailure("Spatial Media Toolkit did not produce one left and one right quality eye.")
    left[0].chmod(0o600)
    right[0].chmod(0o600)
    return left[0], right[0]


def downscaled_ssim(ffmpeg: Path, candidate: Path, reference: Path) -> float:
    process = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "info",
            "-i",
            str(candidate),
            "-i",
            str(reference),
            "-lavfi",
            (
                "[0:v]scale=1920:1080:flags=lanczos,format=yuv420p,setpts=PTS-STARTPTS[candidate];"
                "[1:v]format=yuv420p,setpts=PTS-STARTPTS[reference];"
                "[candidate][reference]ssim"
            ),
            "-f",
            "null",
            "-",
        ],
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=COMMAND_TIMEOUT_SECONDS,
    )
    if process.returncode != 0:
        raise RealMVCQualityFailure("Real-MVC SSIM comparison failed.")
    matches = SSIM_PATTERN.findall(process.stderr)
    if not matches:
        raise RealMVCQualityFailure("Real-MVC SSIM comparison did not report an aggregate score.")
    return float(matches[-1])


def measure_output(
    app: AppBundle,
    movie: Path,
    reference_left: Path,
    reference_right: Path,
    split_directory: Path,
    *,
    expected_frames: int,
    elapsed_seconds: float,
) -> dict[str, object]:
    left, right = split_mv_hevc(app, movie, split_directory)
    left_frame_count = probe_eye(
        app.ffprobe,
        left,
        expected_dimensions=(3840, 2160),
        expected_frames=expected_frames,
    )
    right_frame_count = probe_eye(
        app.ffprobe,
        right,
        expected_dimensions=(3840, 2160),
        expected_frames=expected_frames,
    )
    left_match = downscaled_ssim(app.ffmpeg, left, reference_left)
    left_cross = downscaled_ssim(app.ffmpeg, left, reference_right)
    right_match = downscaled_ssim(app.ffmpeg, right, reference_right)
    right_cross = downscaled_ssim(app.ffmpeg, right, reference_left)
    record = {
        "elapsed_seconds": round(elapsed_seconds, 3),
        "final_bytes": movie.stat().st_size,
        "left_frame_count": left_frame_count,
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "min_eye_order_margin": min(left_match - left_cross, right_match - right_cross),
        "min_same_eye_ssim": min(left_match, right_match),
        "right_cross_ssim": right_cross,
        "right_frame_count": right_frame_count,
        "right_match_ssim": right_match,
        "sha256": sha256_file(movie),
    }
    shutil.rmtree(split_directory, ignore_errors=True)
    movie.unlink(missing_ok=True)
    return record


def probe_quality_artifact(
    app: AppBundle,
    path: Path,
    required_box_types: set[str],
    *,
    expected_duration_seconds: float,
) -> None:
    missing_boxes = sorted(required_box_types - box_types(path))
    if missing_boxes:
        raise RealMVCQualityFailure("Quality artifact is missing required spatial metadata.")
    probe = _media_probe(app, path)
    duration_seconds = _media_duration(probe)
    if abs(duration_seconds - expected_duration_seconds) > MEDIA_DURATION_TOLERANCE_SECONDS:
        raise RealMVCQualityFailure("Quality artifact duration did not match the source segment.")
    streams = _media_streams(probe)
    video_streams = [stream for stream in streams if stream.get("codec_type") == "video"]
    audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(streams) != 2 or len(video_streams) != 1 or len(audio_streams) != 1:
        raise RealMVCQualityFailure("Quality artifact did not contain exactly one video and one audio stream.")
    video = video_streams[0]
    audio = audio_streams[0]
    if video.get("codec_name") != "hevc" or (video.get("width"), video.get("height")) != (3840, 2160):
        raise RealMVCQualityFailure("Quality artifact did not preserve 3840x2160 HEVC video.")
    if audio.get("codec_name") != "aac" or audio.get("language") != "eng":
        raise RealMVCQualityFailure("Quality artifact did not preserve the expected English AAC audio.")
    verify_seeks(app.ffmpeg.as_posix(), path, max(1, math.ceil(duration_seconds)))


def run_quality_route(
    app: AppBundle,
    source: Path,
    destination: Path,
    *,
    source_duration_seconds: float,
    direct: bool,
) -> tuple[Path, float]:
    started = time.monotonic()
    request = build_worker_request(
        "convert_source",
        source,
        destination,
        job_id=str(uuid.uuid4()),
        upscale_enabled=True,
    )
    encoding = request.get("encoding")
    if not isinstance(encoding, dict):
        raise RealMVCQualityFailure("Quality request omitted encoding settings.")
    encoding["subtitles"] = {"mode": "off", "preferred_language": None}
    result = run_worker(
        app,
        request,
        home_directory=destination.with_name(f"{destination.name}-home"),
    )
    if direct:
        validate_route_pair(
            result,
            result,
            expected_selected="direct_mv_hevc",
            expected_fallback_reason=None,
            expected_upscale_mode="metalfx",
        )
        validate_stage_contract(
            result,
            required={"create_left_right_files"},
            forbidden={"combine_to_mv_hevc", "upscale_video"},
        )
        required_boxes = DIRECT_REQUIRED_BOX_TYPES
    else:
        validate_route_pair(
            result,
            result,
            expected_selected="generated_mv_hevc",
            expected_fallback_reason="metalfx_2x_mv_hevc_unavailable",
            expected_upscale_mode=None,
        )
        validate_stage_contract(
            result,
            required={"combine_to_mv_hevc", "create_left_right_files", "upscale_video"},
            forbidden=set(),
        )
        required_boxes = CURRENT_REQUIRED_BOX_TYPES
    probe_quality_artifact(
        app,
        result.output_path,
        required_boxes,
        expected_duration_seconds=source_duration_seconds,
    )
    result.output_path.chmod(0o600)
    return result.output_path, time.monotonic() - started


def summarize_real_mvc_quality(
    file_based_runs: Sequence[Mapping[str, object]],
    direct_runs: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if len(file_based_runs) != EXPECTED_RUNS or len(direct_runs) != EXPECTED_RUNS:
        raise ValueError(f"Real-MVC quality requires exactly {EXPECTED_RUNS} runs per route.")
    summary = summarize_quality_size_gate(
        file_based_runs,
        direct_runs,
        quality_tolerance=QUALITY_TOLERANCE,
        max_size_ratio=MAX_SIZE_RATIO,
        minimum_eye_order_margin=MINIMUM_EYE_ORDER_MARGIN,
    )
    file_based_eye_order_passed = all(
        require_float(run, "min_eye_order_margin") >= MINIMUM_EYE_ORDER_MARGIN for run in file_based_runs
    )
    summary.update(
        {
            "direct_run_count": len(direct_runs),
            "file_based_eye_order_passed": file_based_eye_order_passed,
            "file_based_run_count": len(file_based_runs),
            "passed": require_bool(summary, "passed") and file_based_eye_order_passed,
        }
    )
    return summary


def build_evidence(
    *,
    app: AppBundle,
    source: Path,
    source_info: Mapping[str, object],
    reference_left: Path,
    reference_right: Path,
    direct_runs: list[dict[str, object]],
    file_based_runs: list[dict[str, object]],
    fallback_helper_sha256: str,
) -> dict[str, object]:
    summary = summarize_real_mvc_quality(file_based_runs, direct_runs)
    return {
        "acceptance": {
            "direct_quality": bool(summary["quality_passed"]),
            "direct_size": bool(summary["size_passed"]),
            "eye_order": bool(summary["eye_order_passed"]) and bool(summary["file_based_eye_order_passed"]),
            "passed": bool(summary["passed"]),
            "repeated_full_routes": len(direct_runs) == EXPECTED_RUNS and len(file_based_runs) == EXPECTED_RUNS,
        },
        "direct_runs": direct_runs,
        "file_based_runs": file_based_runs,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "package": {
            "app_tree_sha256": app_tree_sha256(app.path),
            "bundle_identifier": app.bundle_identifier,
            "fallback_helper_sha256": fallback_helper_sha256,
            "helper_sha256": sha256_file(app.helper),
            "version": app.version,
            "worker_sha256": sha256_file(app.worker),
        },
        "reference": {
            "frame_count": require_int(source_info, "frame_count"),
            "left_sha256": sha256_file(reference_left),
            "right_sha256": sha256_file(reference_right),
            "size_bytes": reference_left.stat().st_size + reference_right.stat().st_size,
        },
        "schema_version": 1,
        "source": {
            "duration_seconds": require_float(source_info, "duration_seconds"),
            "frame_count": require_int(source_info, "frame_count"),
            "frame_rate": str(source_info["frame_rate"]),
            "sha256": sha256_file(source),
            "size_bytes": source.stat().st_size,
        },
        "summary": summary,
        "thresholds": {
            "max_size_ratio": MAX_SIZE_RATIO,
            "minimum_eye_order_margin": MINIMUM_EYE_ORDER_MARGIN,
            "quality_tolerance": QUALITY_TOLERANCE,
            "runs_per_route": EXPECTED_RUNS,
        },
        "tools": {
            "edge264_sha256": sha256_file(packaged_tool(app, "edge264_test")),
            "ffmpeg_sha256": sha256_file(app.ffmpeg),
            "ffprobe_sha256": sha256_file(app.ffprobe),
            "fx_upscale_sha256": sha256_file(packaged_tool(app, "fx-upscale")),
            "spatial_media_tool_sha256": sha256_file(packaged_tool(app, "spatial-media-kit-tool")),
        },
    }


def validate_paths(app: AppBundle, source: Path, output: Path, work_directory: Path, requested_work: Path) -> None:
    if requested_work.is_symlink():
        raise RealMVCQualityFailure("Real-MVC quality work directory must not be a symlink.")
    protected = {app.path, app.worker, app.helper, source, output}
    if work_directory in protected or work_directory in {Path("/"), Path.home().resolve()}:
        raise RealMVCQualityFailure("Real-MVC quality work directory is not a safe dedicated destination.")
    if any(path.is_relative_to(work_directory) for path in protected):
        raise RealMVCQualityFailure("Real-MVC quality work directory must not contain protected inputs.")
    if output.is_relative_to(work_directory) or output.is_relative_to(app.path):
        raise RealMVCQualityFailure("Real-MVC quality evidence output must remain outside transient inputs.")
    if work_directory.exists() and any(work_directory.iterdir()):
        raise RealMVCQualityFailure("Real-MVC quality work directory must be new or empty.")


def remove_private_work_directory(work_directory: Path) -> None:
    try:
        shutil.rmtree(work_directory)
    except FileNotFoundError:
        return
    except OSError as error:
        raise RealMVCQualityFailure("Could not remove the private real-MVC quality work directory.") from error
    if work_directory.exists():
        raise RealMVCQualityFailure("Private real-MVC quality work directory remained after cleanup.")


def qualify(args: argparse.Namespace) -> dict[str, object]:
    app = read_app_bundle(args.app)
    source = args.source.expanduser().resolve()
    requested_output = args.output.expanduser()
    if requested_output.is_symlink():
        raise RealMVCQualityFailure("Real-MVC quality evidence output must not be a symlink.")
    output = requested_output.resolve()
    requested_work = args.work_directory.expanduser()
    work_directory = requested_work.resolve()
    validate_paths(app, source, output, work_directory, requested_work)
    if not source.is_file():
        raise RealMVCQualityFailure("Representative MVC segment is unavailable.")
    if sha256_file(source) != args.expected_source_sha256.lower():
        raise RealMVCQualityFailure("Representative MVC segment SHA-256 did not match the expected fixture.")
    work_directory.mkdir(parents=True, mode=0o700)
    source_info = source_video_info(app.ffprobe, source)
    source_duration_seconds = require_float(source_info, "duration_seconds")
    source_frame_count = require_int(source_info, "frame_count")
    direct_runs: list[dict[str, object]] = []
    file_based_runs: list[dict[str, object]] = []
    evidence: dict[str, object]
    try:
        reference_left, reference_right = prepare_reference_eyes(app, source, work_directory, source_info)
        with metalfx_unavailable_capability_app(app, work_directory / "fallback-app") as fallback_app:
            fallback_helper_sha256 = sha256_file(fallback_app.helper)
            for run_index in range(EXPECTED_RUNS):
                direct_movie, direct_elapsed = run_quality_route(
                    app,
                    source,
                    work_directory / f"direct-{run_index}",
                    source_duration_seconds=source_duration_seconds,
                    direct=True,
                )
                direct_runs.append(
                    measure_output(
                        app,
                        direct_movie,
                        reference_left,
                        reference_right,
                        work_directory / f"direct-{run_index}-split",
                        expected_frames=source_frame_count,
                        elapsed_seconds=direct_elapsed,
                    )
                )
                file_based_movie, file_based_elapsed = run_quality_route(
                    fallback_app,
                    source,
                    work_directory / f"file-based-{run_index}",
                    source_duration_seconds=source_duration_seconds,
                    direct=False,
                )
                file_based_runs.append(
                    measure_output(
                        app,
                        file_based_movie,
                        reference_left,
                        reference_right,
                        work_directory / f"file-based-{run_index}-split",
                        expected_frames=source_frame_count,
                        elapsed_seconds=file_based_elapsed,
                    )
                )
        evidence = build_evidence(
            app=app,
            source=source,
            source_info=source_info,
            reference_left=reference_left,
            reference_right=reference_right,
            direct_runs=direct_runs,
            file_based_runs=file_based_runs,
            fallback_helper_sha256=fallback_helper_sha256,
        )
    finally:
        remove_private_work_directory(work_directory)
    evidence_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if len(evidence_text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise RealMVCQualityFailure("Real-MVC quality evidence exceeded its bounded size limit.")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output.with_suffix(output.suffix + ".tmp")
    temporary_output.write_text(evidence_text, encoding="utf-8")
    temporary_output.replace(output)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare repeated packaged direct 4K MetalFX and file-based FX Upscale quality on real MVC."
    )
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    return parser.parse_args()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    try:
        evidence = qualify(parse_args())
    except (OSError, PackagedRouteFailure, RealMVCQualityFailure, subprocess.SubprocessError) as error:
        parser.exit(1, f"error: {error}\n")
    print(json.dumps(evidence, indent=2, sort_keys=True))
    return 0 if require_bool(require_mapping(evidence, "acceptance"), "passed") else 2


if __name__ == "__main__":
    raise SystemExit(main())
