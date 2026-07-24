#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import statistics
import subprocess
import tempfile

from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Mapping, Sequence

from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import (
    COMMAND_TIMEOUT_SECONDS,
    MP4BOX,
    SPATIAL_MEDIA_TOOL,
    QualificationFailure,
    command_path,
    kill_and_reap,
    run,
    split_mv_hevc,
    ssim,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EDGE264 = REPOSITORY_ROOT / "bd_to_avp/bin/edge264_test"
DEFAULT_CANDIDATE_BITRATES = (
    0.5,
    0.75,
    1.0,
    1.5,
    2.0,
    3.0,
    4.0,
    5.0,
    6.0,
    7.0,
    8.0,
    10.0,
    12.0,
    13.0,
    14.0,
    15.0,
    16.0,
    17.0,
    18.0,
    20.0,
    24.0,
    30.0,
    40.0,
)
DEFAULT_QUALITY_TOLERANCE = 0.002
DEFAULT_MATCHED_MAX_SIZE_RATIO = 1.05
DEFAULT_POLICY_MAX_SIZE_RATIO = 1.10
DEFAULT_POLICY_HEADROOM = 0.0
DEFAULT_CURRENT_AUTOMATIC_BITRATE_MBPS = 40
DEFAULT_MINIMUM_EYE_ORDER_MARGIN = 0.001
MANIFEST_SCHEMA_VERSION = 1
EVIDENCE_SCHEMA_VERSION = 1
CASE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
PRIVATE_SOURCE_PLACEHOLDER = "<private-source>"


@dataclass(frozen=True)
class CorpusCase:
    case_id: str
    tags: tuple[str, ...]
    source: Mapping[str, object]
    eye_width: int
    eye_height: int
    frame_rate: str
    crop: tuple[int, int, int, int] | None = None
    frame_rate_override: str | None = None
    minimum_eye_order_margin: float = DEFAULT_MINIMUM_EYE_ORDER_MARGIN
    quality_gate: bool = True

    @property
    def output_eye_width(self) -> int:
        return self.crop[0] if self.crop else self.eye_width

    @property
    def output_eye_height(self) -> int:
        return self.crop[1] if self.crop else self.eye_height

    @property
    def output_frame_rate(self) -> str:
        return self.frame_rate_override or self.frame_rate


@dataclass(frozen=True)
class CorpusManifest:
    corpus_id: str
    required_coverage: tuple[str, ...]
    supported_source_bit_depths: tuple[int, ...]
    rejected_source_bit_depths: tuple[int, ...]
    cases: tuple[CorpusCase, ...]


@dataclass(frozen=True)
class PreparedCase:
    definition: CorpusCase
    source_path: Path
    reference_left: Path
    reference_right: Path
    duration_seconds: float
    frame_count: int
    source_evidence: Mapping[str, object]


def _require_mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationFailure(f"{label} must be an object.")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationFailure(f"{label} must be a non-empty string.")
    return value.strip()


def _require_positive_even(value: object, label: str) -> int:
    if type(value) is not int or value <= 0 or value % 2:
        raise QualificationFailure(f"{label} must be a positive even integer.")
    return value


def _require_positive_number(value: object, label: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(float(value)) or float(value) <= 0:
        raise QualificationFailure(f"{label} must be a positive finite number.")
    return float(value)


def _parse_frame_rate(value: object, label: str) -> str:
    text = _require_string(value, label)
    try:
        frame_rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as error:
        raise QualificationFailure(f"{label} must be a positive integer or fraction.") from error
    if frame_rate <= 0:
        raise QualificationFailure(f"{label} must be positive.")
    return text


def _parse_tags(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise QualificationFailure(f"{label} must be a non-empty array.")
    tags = tuple(_require_string(tag, label) for tag in value)
    if len(set(tags)) != len(tags):
        raise QualificationFailure(f"{label} contains duplicate entries.")
    return tags


def _parse_bit_depths(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise QualificationFailure(f"{label} must be a non-empty array.")
    bit_depths = tuple(value)
    if any(type(bit_depth) is not int or bit_depth <= 0 for bit_depth in bit_depths):
        raise QualificationFailure(f"{label} must contain positive integers.")
    if len(set(bit_depths)) != len(bit_depths):
        raise QualificationFailure(f"{label} contains duplicate entries.")
    return bit_depths


def parse_manifest(raw: object) -> CorpusManifest:
    document = _require_mapping(raw, "manifest")
    if document.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise QualificationFailure(f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}.")
    corpus_id = _require_string(document.get("corpus_id"), "manifest corpus_id")
    required_coverage = _parse_tags(document.get("required_coverage"), "manifest required_coverage")
    bit_depth_policy = _require_mapping(document.get("source_bit_depth_policy"), "source_bit_depth_policy")
    supported_source_bit_depths = _parse_bit_depths(
        bit_depth_policy.get("supported"),
        "source_bit_depth_policy supported",
    )
    rejected_source_bit_depths = _parse_bit_depths(
        bit_depth_policy.get("rejected"),
        "source_bit_depth_policy rejected",
    )
    if set(supported_source_bit_depths) & set(rejected_source_bit_depths):
        raise QualificationFailure("source bit-depth supported and rejected sets overlap.")

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise QualificationFailure("manifest cases must be a non-empty array.")
    cases: list[CorpusCase] = []
    observed_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases):
        case = _require_mapping(raw_case, f"cases[{index}]")
        case_id = _require_string(case.get("id"), f"cases[{index}].id")
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise QualificationFailure(f"case id {case_id!r} is not a stable lowercase identifier.")
        if case_id in observed_ids:
            raise QualificationFailure(f"duplicate corpus case id: {case_id}")
        observed_ids.add(case_id)
        tags = _parse_tags(case.get("tags"), f"cases[{index}].tags")
        source = _require_mapping(case.get("source"), f"cases[{index}].source")
        source_kind = _require_string(source.get("kind"), f"cases[{index}].source.kind")
        if source_kind not in {"mvc_container", "mvc_annex_b", "synthetic"}:
            raise QualificationFailure(f"unsupported source kind for {case_id}: {source_kind}")
        if source_kind in {"mvc_container", "mvc_annex_b"}:
            path_env = _require_string(source.get("path_env"), f"cases[{index}].source.path_env")
            if not path_env.startswith("BD_TO_AVP_"):
                raise QualificationFailure(f"source path_env for {case_id} must use the BD_TO_AVP_ prefix.")
        if source_kind == "mvc_container":
            start_seconds = source.get("start_seconds")
            if type(start_seconds) not in (int, float) or not math.isfinite(float(start_seconds)) or start_seconds < 0:
                raise QualificationFailure(f"source start_seconds for {case_id} must be non-negative.")
            _require_positive_number(source.get("duration_seconds"), f"source duration_seconds for {case_id}")
        if source_kind == "synthetic":
            _require_string(source.get("filter"), f"cases[{index}].source.filter")
            _require_positive_number(source.get("duration_seconds"), f"source duration_seconds for {case_id}")

        eye_width = _require_positive_even(case.get("eye_width"), f"cases[{index}].eye_width")
        eye_height = _require_positive_even(case.get("eye_height"), f"cases[{index}].eye_height")
        frame_rate = _parse_frame_rate(case.get("frame_rate"), f"cases[{index}].frame_rate")
        transforms = _require_mapping(case.get("transforms", {}), f"cases[{index}].transforms")
        crop: tuple[int, int, int, int] | None = None
        raw_crop = transforms.get("crop")
        if raw_crop is not None:
            if not isinstance(raw_crop, list) or len(raw_crop) != 4 or any(type(item) is not int for item in raw_crop):
                raise QualificationFailure(f"crop for {case_id} must be [width, height, x, y].")
            crop = tuple(raw_crop)
            crop_width, crop_height, crop_x, crop_y = crop
            if crop_width <= 0 or crop_height <= 0 or crop_width % 2 or crop_height % 2:
                raise QualificationFailure(f"crop dimensions for {case_id} must be positive even integers.")
            if crop_x < 0 or crop_y < 0 or crop_x + crop_width > eye_width or crop_y + crop_height > eye_height:
                raise QualificationFailure(f"crop for {case_id} falls outside one source eye.")
        frame_rate_override = transforms.get("frame_rate")
        if frame_rate_override is not None:
            frame_rate_override = _parse_frame_rate(
                frame_rate_override,
                f"cases[{index}].transforms.frame_rate",
            )
        raw_eye_order_margin = case.get("minimum_eye_order_margin", DEFAULT_MINIMUM_EYE_ORDER_MARGIN)
        if (
            type(raw_eye_order_margin) not in (int, float)
            or not math.isfinite(float(raw_eye_order_margin))
            or not 0 <= float(raw_eye_order_margin) <= 1
        ):
            raise QualificationFailure(f"minimum_eye_order_margin for {case_id} must be between 0 and 1.")
        quality_gate = case.get("quality_gate", True)
        if type(quality_gate) is not bool:
            raise QualificationFailure(f"quality_gate for {case_id} must be a Boolean.")
        cases.append(
            CorpusCase(
                case_id=case_id,
                tags=tags,
                source=source,
                eye_width=eye_width,
                eye_height=eye_height,
                frame_rate=frame_rate,
                crop=crop,
                frame_rate_override=frame_rate_override,
                minimum_eye_order_margin=float(raw_eye_order_margin),
                quality_gate=quality_gate,
            )
        )

    covered_tags = {tag for case in cases if case.quality_gate for tag in case.tags}
    missing_coverage = sorted(set(required_coverage) - covered_tags)
    if missing_coverage:
        raise QualificationFailure("corpus cases do not cover required tags: " + ", ".join(missing_coverage))
    if not any("real_mvc" in case.tags and case.quality_gate for case in cases):
        raise QualificationFailure("corpus must include at least one gated real_mvc case.")
    return CorpusManifest(
        corpus_id=corpus_id,
        required_coverage=required_coverage,
        supported_source_bit_depths=supported_source_bit_depths,
        rejected_source_bit_depths=rejected_source_bit_depths,
        cases=tuple(cases),
    )


def load_manifest(path: Path) -> CorpusManifest:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read corpus manifest {path}: {error}") from error
    return parse_manifest(raw)


def effective_bitrate_mbps(file_size_bytes: int, duration_seconds: float) -> float:
    if file_size_bytes < 0:
        raise ValueError("file size must not be negative")
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ValueError("duration must be positive")
    return file_size_bytes * 8 / (duration_seconds * 1_000_000)


def derive_policy_bitrate(
    selected_bitrates_mbps: Sequence[float],
    *,
    headroom_fraction: float,
) -> int:
    if not selected_bitrates_mbps:
        raise ValueError("selected bitrates are required")
    if any(not math.isfinite(value) or value <= 0 for value in selected_bitrates_mbps):
        raise ValueError("selected bitrates must be positive finite values")
    if not math.isfinite(headroom_fraction) or headroom_fraction < 0 or headroom_fraction > 1:
        raise ValueError("headroom fraction must be between 0 and 1")
    return min(500, max(1, math.ceil(max(selected_bitrates_mbps) * (1 + headroom_fraction))))


def summarize_quality_size_gate(
    current_runs: Sequence[Mapping[str, object]],
    direct_runs: Sequence[Mapping[str, object]],
    *,
    quality_tolerance: float,
    max_size_ratio: float,
    minimum_eye_order_margin: float = DEFAULT_MINIMUM_EYE_ORDER_MARGIN,
) -> dict[str, object]:
    if not current_runs or not direct_runs:
        raise ValueError("current and direct runs are required")
    current_quality = statistics.median(float(run["min_same_eye_ssim"]) for run in current_runs)
    direct_quality = statistics.median(float(run["min_same_eye_ssim"]) for run in direct_runs)
    current_size = statistics.median(int(run["final_bytes"]) for run in current_runs)
    direct_size = statistics.median(int(run["final_bytes"]) for run in direct_runs)
    if current_size <= 0:
        raise ValueError("current median size must be positive")
    required_quality = current_quality - quality_tolerance
    quality_passed = all(float(run["min_same_eye_ssim"]) >= required_quality for run in direct_runs)
    eye_order_passed = all(float(run["min_eye_order_margin"]) >= minimum_eye_order_margin for run in direct_runs)
    size_ratio = direct_size / current_size
    run_size_ratios = [int(run["final_bytes"]) / current_size for run in direct_runs]
    size_passed = all(run_size_ratio <= max_size_ratio for run_size_ratio in run_size_ratios)
    return {
        "current_median_final_bytes": current_size,
        "current_median_min_same_eye_ssim": current_quality,
        "direct_median_final_bytes": direct_size,
        "direct_median_min_same_eye_ssim": direct_quality,
        "quality_delta": direct_quality - current_quality,
        "required_direct_min_same_eye_ssim": required_quality,
        "quality_tolerance": quality_tolerance,
        "quality_passed": quality_passed,
        "eye_order_passed": eye_order_passed,
        "minimum_eye_order_margin": minimum_eye_order_margin,
        "size_ratio": size_ratio,
        "run_size_ratios": run_size_ratios,
        "max_run_size_ratio": max(run_size_ratios),
        "max_size_ratio": max_size_ratio,
        "size_passed": size_passed,
        "passed": quality_passed and eye_order_passed and size_passed,
    }


def _run_pipeline(
    producer_command: Sequence[str | Path],
    consumer_command: Sequence[str | Path],
    *,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> str:
    try:
        producer = subprocess.Popen(
            [str(item) for item in producer_command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise QualificationFailure("Could not start qualification pipeline producer.") from error
    assert producer.stdout is not None
    try:
        consumer = subprocess.Popen(
            [str(item) for item in consumer_command],
            stdin=producer.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        producer.stdout.close()
        try:
            kill_and_reap(producer)
        except QualificationFailure as cleanup_error:
            raise cleanup_error from error
        finally:
            if producer.stderr is not None and not producer.stderr.closed:
                producer.stderr.close()
        raise QualificationFailure("Could not start qualification pipeline consumer.") from error
    producer.stdout.close()
    try:
        try:
            consumer_stdout, consumer_stderr = consumer.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                kill_and_reap(consumer, producer)
            except QualificationFailure as cleanup_error:
                raise cleanup_error from error
            raise QualificationFailure("Qualification pipeline timed out.") from error
        try:
            producer_status = producer.wait(timeout=30)
        except subprocess.TimeoutExpired as error:
            try:
                kill_and_reap(producer)
            except QualificationFailure as cleanup_error:
                raise cleanup_error from error
            raise QualificationFailure("Qualification pipeline producer did not exit.") from error
        producer_stderr = producer.stderr.read() if producer.stderr else b""
    finally:
        for stream in (producer.stderr, consumer.stdout, consumer.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    if producer_status != 0:
        raise QualificationFailure(
            f"Pipeline producer failed with exit code {producer_status}:\n"
            + producer_stderr.decode("utf-8", errors="replace").strip()
        )
    if consumer.returncode != 0:
        raise QualificationFailure(
            f"Pipeline consumer failed with exit code {consumer.returncode}:\n"
            + consumer_stderr.decode("utf-8", errors="replace").strip()
        )
    return consumer_stdout.decode("utf-8", errors="strict")


def redact_private_source_paths(message: str, private_paths: Sequence[Path]) -> str:
    redacted = message
    for private_path in sorted({str(path) for path in private_paths}, key=len, reverse=True):
        redacted = redacted.replace(private_path, PRIVATE_SOURCE_PLACEHOLDER)
    return redacted


def _source_path_from_environment(case: CorpusCase) -> Path:
    path_env = str(case.source["path_env"])
    configured_path = os.environ.get(path_env)
    if not configured_path:
        raise QualificationFailure(f"Corpus case {case.case_id} requires environment variable {path_env}.")
    path = Path(configured_path).expanduser().resolve()
    if not path.is_file():
        raise QualificationFailure(f"Corpus source for {case.case_id} is unavailable: {path_env}")
    return path


def _normalizer_filter(case: CorpusCase) -> str:
    left_filters = [f"crop={case.eye_width}:{case.eye_height}:0:0"]
    right_filters = [f"crop={case.eye_width}:{case.eye_height}:{case.eye_width}:0"]
    if case.crop:
        crop_width, crop_height, crop_x, crop_y = case.crop
        crop_filter = f"crop={crop_width}:{crop_height}:{crop_x}:{crop_y}"
        left_filters.append(crop_filter)
        right_filters.append(crop_filter)
    if case.frame_rate_override:
        left_filters.append(f"fps={case.frame_rate_override}")
        right_filters.append(f"fps={case.frame_rate_override}")
    return (
        "[0:v]split=2[left_source][right_source];"
        f"[left_source]{','.join(left_filters)}[left];"
        f"[right_source]{','.join(right_filters)}[right];"
        "[left][right]hstack=inputs=2,format=yuv420p[stereo]"
    )


def _prepare_mvc_case(
    case: CorpusCase,
    work_directory: Path,
    *,
    ffmpeg: str,
) -> tuple[Path, Mapping[str, object]]:
    original_path = _source_path_from_environment(case)
    source_kind = str(case.source["kind"])
    if source_kind == "mvc_container":
        annex_b_path = work_directory / "source.264"
        try:
            run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(case.source["start_seconds"]),
                    "-i",
                    original_path,
                    "-t",
                    str(case.source["duration_seconds"]),
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-bsf:v",
                    "h264_mp4toannexb",
                    "-f",
                    "h264",
                    "-y",
                    annex_b_path,
                ]
            )
        except QualificationFailure as error:
            raise QualificationFailure(redact_private_source_paths(str(error), [original_path])) from None
    else:
        annex_b_path = original_path

    normalized_path = work_directory / "source-sbs.mkv"
    try:
        _run_pipeline(
            [EDGE264, annex_b_path, "-Omk"],
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "yuv4mpegpipe",
                "-r",
                case.frame_rate,
                "-i",
                "-",
                "-filter_complex",
                _normalizer_filter(case),
                "-map",
                "[stereo]",
                "-c:v",
                "ffv1",
                "-level",
                "3",
                "-g",
                "1",
                "-y",
                normalized_path,
            ],
        )
    except QualificationFailure as error:
        raise QualificationFailure(redact_private_source_paths(str(error), [original_path])) from None
    try:
        segment_bytes = annex_b_path.stat().st_size
        segment_sha256 = sha256_file(annex_b_path)
    except OSError as error:
        message = redact_private_source_paths(f"Could not fingerprint corpus source segment: {error}", [original_path])
        raise QualificationFailure(message) from None
    source_evidence: dict[str, object] = {
        "kind": source_kind,
        "path_env": str(case.source["path_env"]),
        "segment_bytes": segment_bytes,
        "segment_sha256": segment_sha256,
    }
    if source_kind == "mvc_container":
        source_evidence.update(
            {
                "start_seconds": float(case.source["start_seconds"]),
                "requested_duration_seconds": float(case.source["duration_seconds"]),
            }
        )
    return normalized_path, source_evidence


def _prepare_synthetic_case(
    case: CorpusCase,
    work_directory: Path,
    *,
    ffmpeg: str,
) -> tuple[Path, Mapping[str, object]]:
    output_path = work_directory / "source-sbs.mkv"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            str(case.source["filter"]),
            "-t",
            str(case.source["duration_seconds"]),
            "-r",
            case.output_frame_rate,
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-y",
            output_path,
        ]
    )
    return output_path, {
        "kind": "synthetic",
        "filter_sha256": __import__("hashlib").sha256(str(case.source["filter"]).encode()).hexdigest(),
        "requested_duration_seconds": float(case.source["duration_seconds"]),
    }


def _probe_prepared_source(ffprobe: str, source_path: Path, case: CorpusCase) -> tuple[float, int]:
    completed = run(
        [
            ffprobe,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,pix_fmt,avg_frame_rate,nb_read_frames:format=duration",
            "-of",
            "json",
            source_path,
        ]
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams")
    if not isinstance(streams, list) or len(streams) != 1 or not isinstance(streams[0], Mapping):
        raise QualificationFailure(f"Prepared source for {case.case_id} did not contain one video stream.")
    stream = streams[0]
    expected_width = case.output_eye_width * 2
    if stream.get("width") != expected_width or stream.get("height") != case.output_eye_height:
        raise QualificationFailure(
            f"Prepared source for {case.case_id} has {stream.get('width')}x{stream.get('height')}; "
            f"expected {expected_width}x{case.output_eye_height}."
        )
    if stream.get("pix_fmt") != "yuv420p":
        raise QualificationFailure(f"Prepared source for {case.case_id} is not 8-bit yuv420p.")
    try:
        duration_seconds = float(payload.get("format", {}).get("duration"))
    except (TypeError, ValueError) as error:
        raise QualificationFailure(f"Prepared source for {case.case_id} did not report a duration.") from error
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise QualificationFailure(f"Prepared source for {case.case_id} did not report a positive duration.")
    frame_count = int(stream.get("nb_read_frames") or 0)
    if frame_count <= 0:
        raise QualificationFailure(f"Prepared source for {case.case_id} did not report decoded frames.")
    return duration_seconds, frame_count


def _generate_references(ffmpeg: str, prepared: PreparedCase) -> None:
    width = prepared.definition.output_eye_width
    height = prepared.definition.output_eye_height
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            prepared.source_path,
            "-filter_complex",
            (
                "[0:v]split=2[left_source][right_source];"
                f"[left_source]crop={width}:{height}:0:0[left];"
                f"[right_source]crop={width}:{height}:{width}:0[right]"
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
            prepared.reference_left,
            "-map",
            "[right]",
            "-c:v",
            "ffv1",
            "-level",
            "3",
            "-g",
            "1",
            "-y",
            prepared.reference_right,
        ]
    )


def prepare_case(case: CorpusCase, work_directory: Path, *, ffmpeg: str, ffprobe: str) -> PreparedCase:
    work_directory.mkdir(parents=True, exist_ok=True)
    if str(case.source["kind"]) == "synthetic":
        source_path, source_evidence = _prepare_synthetic_case(case, work_directory, ffmpeg=ffmpeg)
    else:
        source_path, source_evidence = _prepare_mvc_case(case, work_directory, ffmpeg=ffmpeg)
    duration_seconds, frame_count = _probe_prepared_source(ffprobe, source_path, case)
    prepared = PreparedCase(
        definition=case,
        source_path=source_path,
        reference_left=work_directory / "reference-left.mkv",
        reference_right=work_directory / "reference-right.mkv",
        duration_seconds=duration_seconds,
        frame_count=frame_count,
        source_evidence=source_evidence,
    )
    _generate_references(ffmpeg, prepared)
    return prepared


def _encode_generated(
    ffmpeg: str,
    prepared: PreparedCase,
    output_path: Path,
    work_directory: Path,
    *,
    eye_bitrate_mbps: float,
    merge_quality: int,
) -> None:
    width = prepared.definition.output_eye_width
    height = prepared.definition.output_eye_height
    left_path = work_directory / "generated-left.mov"
    right_path = work_directory / "generated-right.mov"
    run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            prepared.source_path,
            "-filter_complex",
            (
                "[0:v]split=2[left_source][right_source];"
                f"[left_source]crop={width}:{height}:0:0[left];"
                f"[right_source]crop={width}:{height}:{width}:0[right]"
            ),
            "-map",
            "[left]",
            "-c:v",
            "hevc_videotoolbox",
            "-tag:v",
            "hvc1",
            "-b:v",
            f"{eye_bitrate_mbps:g}M",
            "-y",
            left_path,
            "-map",
            "[right]",
            "-c:v",
            "hevc_videotoolbox",
            "-tag:v",
            "hvc1",
            "-b:v",
            f"{eye_bitrate_mbps:g}M",
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
            str(merge_quality),
            "--left-is-primary",
            "--horizontal-field-of-view",
            "90",
            "--horizontal-disparity-adjustment",
            "0",
            "--output-file",
            output_path,
        ]
    )
    left_path.unlink(missing_ok=True)
    right_path.unlink(missing_ok=True)


def _encode_direct(
    ffmpeg: str,
    encoder: Path,
    prepared: PreparedCase,
    output_path: Path,
    *,
    bitrate_mbps: float | None = None,
    quality: float | None = None,
) -> None:
    if (bitrate_mbps is None) == (quality is None):
        raise ValueError("Direct qualification requires exactly one rate-control setting.")
    rate_control_arguments = (
        ["--quality", str(quality)] if quality is not None else ["--bitrate-mbps", str(bitrate_mbps)]
    )
    summary = _run_pipeline(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            prepared.source_path,
            "-pix_fmt",
            "yuv420p",
            "-f",
            "yuv4mpegpipe",
            "-",
        ],
        [
            encoder,
            "--output",
            output_path,
            *rate_control_arguments,
            "--fov",
            "90",
            "--disparity-adjustment",
            "0",
            "--expected-frames",
            str(prepared.frame_count),
            "--overwrite",
        ],
    )
    try:
        payload = json.loads(summary)
    except json.JSONDecodeError as error:
        raise QualificationFailure("Direct encoder returned invalid completion JSON.") from error
    if not isinstance(payload, Mapping) or payload.get("schema_version") != 1:
        raise QualificationFailure("Direct encoder returned an unsupported completion contract.")
    expected_rate_control = "quality" if quality is not None else "average_bitrate"
    if payload.get("rate_control") != expected_rate_control:
        raise QualificationFailure("Direct encoder reported an unexpected rate-control mode.")
    if quality is not None:
        if payload.get("quality") != quality or "bitrate_mbps" in payload:
            raise QualificationFailure("Direct encoder reported an unexpected quality setting.")
    elif payload.get("bitrate_mbps") != bitrate_mbps or "quality" in payload:
        raise QualificationFailure("Direct encoder reported an unexpected bitrate setting.")


def _measure_output(
    ffmpeg: str,
    prepared: PreparedCase,
    output_path: Path,
    split_directory: Path,
    *,
    target_bitrate_mbps: float | None,
) -> dict[str, object]:
    left, right = split_mv_hevc(output_path, split_directory)
    left_match = ssim(ffmpeg, left, prepared.reference_left)
    left_cross = ssim(ffmpeg, left, prepared.reference_right)
    right_match = ssim(ffmpeg, right, prepared.reference_right)
    right_cross = ssim(ffmpeg, right, prepared.reference_left)
    final_bytes = output_path.stat().st_size
    record: dict[str, object] = {
        "effective_bitrate_mbps": round(effective_bitrate_mbps(final_bytes, prepared.duration_seconds), 6),
        "final_bytes": final_bytes,
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "min_eye_order_margin": min(
            left_match - left_cross,
            right_match - right_cross,
        ),
        "min_same_eye_ssim": min(left_match, right_match),
        "right_cross_ssim": right_cross,
        "right_match_ssim": right_match,
        "sha256": sha256_file(output_path),
        "target_bitrate_mbps": target_bitrate_mbps,
    }
    shutil.rmtree(split_directory, ignore_errors=True)
    output_path.unlink(missing_ok=True)
    return record


def qualify_case(
    ffmpeg: str,
    encoder: Path,
    prepared: PreparedCase,
    work_directory: Path,
    *,
    current_runs: int,
    direct_runs: int,
    candidate_bitrates: Sequence[float],
    quality_tolerance: float,
    matched_max_size_ratio: float,
    generated_eye_bitrate_mbps: float,
    generated_merge_quality: int,
) -> dict[str, object]:
    current_records: list[dict[str, object]] = []
    for run_index in range(current_runs):
        output_path = work_directory / f"generated-{run_index}.mov"
        _encode_generated(
            ffmpeg,
            prepared,
            output_path,
            work_directory,
            eye_bitrate_mbps=generated_eye_bitrate_mbps,
            merge_quality=generated_merge_quality,
        )
        current_records.append(
            _measure_output(
                ffmpeg,
                prepared,
                output_path,
                work_directory / f"generated-{run_index}-split",
                target_bitrate_mbps=generated_eye_bitrate_mbps * 2,
            )
        )
    search_records: list[dict[str, object]] = []
    selected_bitrate: float | None = None
    selected_records: list[dict[str, object]] = []
    for candidate_index, candidate_bitrate in enumerate(sorted(set(candidate_bitrates))):
        candidate_records: list[dict[str, object]] = []
        candidate_acceptance: dict[str, object] | None = None
        for run_index in range(direct_runs):
            output_path = work_directory / f"search-{candidate_index}-{run_index}.mov"
            _encode_direct(
                ffmpeg,
                encoder,
                prepared,
                output_path,
                bitrate_mbps=candidate_bitrate,
            )
            candidate_records.append(
                _measure_output(
                    ffmpeg,
                    prepared,
                    output_path,
                    work_directory / f"search-{candidate_index}-{run_index}-split",
                    target_bitrate_mbps=candidate_bitrate,
                )
            )
            candidate_acceptance = summarize_quality_size_gate(
                current_records,
                candidate_records,
                quality_tolerance=quality_tolerance,
                max_size_ratio=matched_max_size_ratio,
                minimum_eye_order_margin=prepared.definition.minimum_eye_order_margin,
            )
            if not bool(candidate_acceptance["passed"]):
                break
        assert candidate_acceptance is not None
        search_records.append(
            {
                "target_bitrate_mbps": candidate_bitrate,
                "required_runs": direct_runs,
                "runs": candidate_records,
                "acceptance": candidate_acceptance,
            }
        )
        if len(candidate_records) == direct_runs and bool(candidate_acceptance["passed"]):
            selected_bitrate = candidate_bitrate
            selected_records = candidate_records
            break

    acceptance = (
        summarize_quality_size_gate(
            current_records,
            selected_records,
            quality_tolerance=quality_tolerance,
            max_size_ratio=matched_max_size_ratio,
            minimum_eye_order_margin=prepared.definition.minimum_eye_order_margin,
        )
        if selected_records
        else {
            "quality_tolerance": quality_tolerance,
            "max_size_ratio": matched_max_size_ratio,
            "passed": False,
            "blocker": "no_candidate_met_quality_size_gate",
        }
    )
    return {
        "id": prepared.definition.case_id,
        "quality_gate": prepared.definition.quality_gate,
        "tags": list(prepared.definition.tags),
        "source": prepared.source_evidence,
        "prepared": {
            "duration_seconds": prepared.duration_seconds,
            "eye_height": prepared.definition.output_eye_height,
            "eye_width": prepared.definition.output_eye_width,
            "frame_count": prepared.frame_count,
            "frame_rate": prepared.definition.output_frame_rate,
            "minimum_eye_order_margin": prepared.definition.minimum_eye_order_margin,
            "source_sha256": sha256_file(prepared.source_path),
        },
        "generated": {
            "eye_bitrate_mbps": generated_eye_bitrate_mbps,
            "merge_quality": generated_merge_quality,
            "runs": current_records,
        },
        "direct_search": {
            "candidates": search_records,
            "selected_bitrate_mbps": selected_bitrate,
        },
        "selected_direct_runs": selected_records,
        "acceptance": acceptance,
    }


def verify_policy_case(
    ffmpeg: str,
    encoder: Path,
    prepared: PreparedCase,
    work_directory: Path,
    current_runs: Sequence[Mapping[str, object]],
    *,
    policy_bitrate_mbps: int,
    quality_tolerance: float,
    policy_max_size_ratio: float,
) -> dict[str, object]:
    output_path = work_directory / "policy.mov"
    _encode_direct(
        ffmpeg,
        encoder,
        prepared,
        output_path,
        bitrate_mbps=policy_bitrate_mbps,
    )
    policy_run = _measure_output(
        ffmpeg,
        prepared,
        output_path,
        work_directory / "policy-split",
        target_bitrate_mbps=float(policy_bitrate_mbps),
    )
    acceptance = summarize_quality_size_gate(
        current_runs,
        [policy_run],
        quality_tolerance=quality_tolerance,
        max_size_ratio=policy_max_size_ratio,
        minimum_eye_order_margin=prepared.definition.minimum_eye_order_margin,
    )
    return {"run": policy_run, "acceptance": acceptance}


def _tool_version(command: Sequence[str | Path]) -> str:
    completed = run(list(command))
    text = (completed.stdout or completed.stderr).strip()
    return text.splitlines()[0] if text else "unknown"


def _environment_evidence(encoder: Path, ffmpeg: str, ffprobe: str) -> dict[str, object]:
    git_head = run(["git", "rev-parse", "HEAD"]).stdout.strip()
    return {
        "encoder_sha256": sha256_file(encoder),
        "edge264_sha256": sha256_file(EDGE264),
        "ffmpeg": _tool_version([ffmpeg, "-hide_banner", "-version"]),
        "ffprobe": _tool_version([ffprobe, "-hide_banner", "-version"]),
        "git_head": git_head,
        "machine": platform.machine(),
        "macos_version": platform.mac_ver()[0],
        "mp4box_sha256": sha256_file(MP4BOX),
        "platform": platform.system(),
        "spatial_media_tool_sha256": sha256_file(SPATIAL_MEDIA_TOOL),
    }


def qualify_corpus(
    manifest_path: Path,
    encoder_path: Path,
    *,
    current_runs: int,
    direct_runs: int,
    candidate_bitrates: Sequence[float],
    quality_tolerance: float,
    matched_max_size_ratio: float,
    policy_max_size_ratio: float,
    policy_headroom: float,
    current_automatic_bitrate_mbps: int,
    generated_eye_bitrate_mbps: float = 20,
    generated_merge_quality: int = 75,
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("MV-HEVC corpus qualification requires macOS arm64.")
    for tool in (EDGE264, SPATIAL_MEDIA_TOOL, MP4BOX):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise QualificationFailure(f"Required bundled tool is unavailable: {tool.name}")
    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    manifest = load_manifest(manifest_path)
    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)

    case_results: list[dict[str, object]] = []
    prepared_cases: list[tuple[PreparedCase, Path]] = []
    with tempfile.TemporaryDirectory(prefix="direct-mv-hevc-corpus-") as temporary_directory:
        root = Path(temporary_directory)
        for case in manifest.cases:
            case_directory = root / case.case_id
            prepared = prepare_case(case, case_directory, ffmpeg=ffmpeg, ffprobe=ffprobe)
            prepared_cases.append((prepared, case_directory))
            case_results.append(
                qualify_case(
                    ffmpeg,
                    encoder_path,
                    prepared,
                    case_directory,
                    current_runs=current_runs,
                    direct_runs=direct_runs,
                    candidate_bitrates=candidate_bitrates,
                    quality_tolerance=quality_tolerance,
                    matched_max_size_ratio=matched_max_size_ratio,
                    generated_eye_bitrate_mbps=generated_eye_bitrate_mbps,
                    generated_merge_quality=generated_merge_quality,
                )
            )

        gating_results = [result for result in case_results if bool(result["quality_gate"])]
        selected_bitrates = [
            float(result["direct_search"]["selected_bitrate_mbps"])
            for result in gating_results
            if result["direct_search"]["selected_bitrate_mbps"] is not None
        ]
        all_matched_cases_passed = len(selected_bitrates) == len(gating_results) and all(
            bool(result["acceptance"]["passed"]) for result in gating_results
        )
        policy_bitrate = (
            derive_policy_bitrate(selected_bitrates, headroom_fraction=policy_headroom)
            if all_matched_cases_passed
            else None
        )
        policy_results: dict[str, object] = {}
        if policy_bitrate is not None:
            for (prepared, case_directory), case_result in zip(prepared_cases, case_results, strict=True):
                if not prepared.definition.quality_gate:
                    continue
                policy_results[prepared.definition.case_id] = verify_policy_case(
                    ffmpeg,
                    encoder_path,
                    prepared,
                    case_directory,
                    case_result["generated"]["runs"],
                    policy_bitrate_mbps=policy_bitrate,
                    quality_tolerance=quality_tolerance,
                    policy_max_size_ratio=policy_max_size_ratio,
                )
        policy_passed = bool(policy_results) and all(
            bool(result["acceptance"]["passed"]) for result in policy_results.values()
        )

    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "manifest": {
            "corpus_id": manifest.corpus_id,
            "required_coverage": list(manifest.required_coverage),
            "sha256": sha256_file(manifest_path),
            "source_bit_depth_policy": {
                "supported": list(manifest.supported_source_bit_depths),
                "rejected": list(manifest.rejected_source_bit_depths),
            },
        },
        "environment": _environment_evidence(encoder_path, ffmpeg, ffprobe),
        "method": {
            "candidate_bitrates_mbps": list(candidate_bitrates),
            "current_runs": current_runs,
            "direct_runs": direct_runs,
            "generated_eye_bitrate_mbps": generated_eye_bitrate_mbps,
            "generated_merge_quality": generated_merge_quality,
            "matched_max_size_ratio": matched_max_size_ratio,
            "eye_order_gate": "case_specific_same_eye_minus_cross_eye_ssim",
            "policy_headroom": policy_headroom,
            "policy_max_size_ratio": policy_max_size_ratio,
            "quality_metric": "minimum decoded same-eye SSIM",
            "quality_tolerance": quality_tolerance,
        },
        "cases": case_results,
        "automatic_policy": {
            "current_bitrate_mbps": current_automatic_bitrate_mbps,
            "recommended_bitrate_mbps": policy_bitrate,
            "verification": policy_results,
            "confirmed_current_policy": policy_passed and policy_bitrate == current_automatic_bitrate_mbps,
            "passed": policy_passed,
        },
        "acceptance": {
            "coverage_passed": True,
            "matched_quality_size_passed": all_matched_cases_passed,
            "automatic_policy_passed": policy_passed,
            "passed": all_matched_cases_passed and policy_passed,
        },
    }


def _parse_candidate_bitrates(value: str) -> tuple[float, ...]:
    try:
        candidates = tuple(float(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as error:
        raise argparse.ArgumentTypeError("candidate bitrates must be comma-separated numbers") from error
    if not candidates or any(not math.isfinite(item) or item <= 0 or item > 500 for item in candidates):
        raise argparse.ArgumentTypeError("candidate bitrates must be between 0 and 500 Mbps")
    return candidates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify direct MV-HEVC quality, size, and Automatic bitrate against a representative corpus."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--encoder",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--current-runs", type=int, default=3)
    parser.add_argument("--direct-runs", type=int, default=3)
    parser.add_argument(
        "--candidate-bitrates",
        type=_parse_candidate_bitrates,
        default=DEFAULT_CANDIDATE_BITRATES,
    )
    parser.add_argument("--quality-tolerance", type=float, default=DEFAULT_QUALITY_TOLERANCE)
    parser.add_argument("--matched-max-size-ratio", type=float, default=DEFAULT_MATCHED_MAX_SIZE_RATIO)
    parser.add_argument("--policy-max-size-ratio", type=float, default=DEFAULT_POLICY_MAX_SIZE_RATIO)
    parser.add_argument("--policy-headroom", type=float, default=DEFAULT_POLICY_HEADROOM)
    parser.add_argument(
        "--current-automatic-bitrate-mbps",
        type=int,
        default=DEFAULT_CURRENT_AUTOMATIC_BITRATE_MBPS,
    )
    args = parser.parse_args()

    if args.current_runs <= 0 or args.current_runs > 10:
        parser.error("current runs must be between 1 and 10")
    if args.direct_runs <= 0 or args.direct_runs > 10:
        parser.error("direct runs must be between 1 and 10")
    if not 0 <= args.quality_tolerance <= 0.1:
        parser.error("quality tolerance must be between 0 and 0.1")
    if args.matched_max_size_ratio <= 0 or args.policy_max_size_ratio <= 0:
        parser.error("size ratios must be positive")
    if not 0 <= args.policy_headroom <= 1:
        parser.error("policy headroom must be between 0 and 1")
    if not 1 <= args.current_automatic_bitrate_mbps <= 500:
        parser.error("current automatic bitrate must be between 1 and 500 Mbps")

    try:
        evidence = qualify_corpus(
            args.manifest.resolve(),
            args.encoder.resolve(),
            current_runs=args.current_runs,
            direct_runs=args.direct_runs,
            candidate_bitrates=args.candidate_bitrates,
            quality_tolerance=args.quality_tolerance,
            matched_max_size_ratio=args.matched_max_size_ratio,
            policy_max_size_ratio=args.policy_max_size_ratio,
            policy_headroom=args.policy_headroom,
            current_automatic_bitrate_mbps=args.current_automatic_bitrate_mbps,
        )
    except QualificationFailure as error:
        parser.exit(1, f"error: {error}\n")

    text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if bool(evidence["acceptance"]["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
