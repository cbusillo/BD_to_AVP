#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import statistics
import tempfile

from dataclasses import replace
from pathlib import Path

from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import (
    DIRECT_REQUIRED_BOX_TYPES,
    MP4BOX,
    SPATIAL_MEDIA_TOOL,
    FixtureOptions,
    QualificationFailure,
    box_types,
    command_path,
    encode_current,
    encode_direct,
    generate_reference,
    split_mv_hevc,
    ssim,
)


DEFAULT_RUNS = 3
DEFAULT_SEARCH_ITERATIONS = 8
DEFAULT_QUALITY_MARGIN = 0.002
RunRecord = dict[str, float | int | str | None]


def effective_bitrate_mbps(file_size_bytes: int, duration_seconds: float) -> float:
    if file_size_bytes < 0:
        raise ValueError("file size must not be negative")
    if duration_seconds <= 0:
        raise ValueError("duration must be positive")
    return file_size_bytes * 8 / (duration_seconds * 1_000_000)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def measure_eye_quality(
    ffmpeg: str,
    movie_path: Path,
    reference_left: Path,
    reference_right: Path,
    split_directory: Path,
) -> dict[str, float]:
    left, right = split_mv_hevc(movie_path, split_directory)
    left_match = ssim(ffmpeg, left, reference_left)
    left_cross = ssim(ffmpeg, left, reference_right)
    right_match = ssim(ffmpeg, right, reference_right)
    right_cross = ssim(ffmpeg, right, reference_left)
    if left_match <= left_cross + 0.15 or right_match <= right_cross + 0.15:
        raise QualificationFailure("Quality-match output does not preserve distinguishable eye order.")
    return {
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "min_same_eye_ssim": min(left_match, right_match),
        "right_cross_ssim": right_cross,
        "right_match_ssim": right_match,
    }


def run_record(
    movie_path: Path,
    quality: dict[str, float],
    duration_seconds: int,
    *,
    target_bitrate_mbps: float | None,
) -> RunRecord:
    final_bytes = movie_path.stat().st_size
    return {
        "effective_bitrate_mbps": round(
            effective_bitrate_mbps(final_bytes, duration_seconds),
            6,
        ),
        "final_bytes": final_bytes,
        "left_cross_ssim": quality["left_cross_ssim"],
        "left_eye_order_margin": quality["left_match_ssim"] - quality["left_cross_ssim"],
        "left_match_ssim": quality["left_match_ssim"],
        "min_eye_order_margin": min(
            quality["left_match_ssim"] - quality["left_cross_ssim"],
            quality["right_match_ssim"] - quality["right_cross_ssim"],
        ),
        "min_same_eye_ssim": quality["min_same_eye_ssim"],
        "right_cross_ssim": quality["right_cross_ssim"],
        "right_eye_order_margin": quality["right_match_ssim"] - quality["right_cross_ssim"],
        "right_match_ssim": quality["right_match_ssim"],
        "sha256": sha256_file(movie_path),
        "target_bitrate_mbps": target_bitrate_mbps,
    }


def summarize_quality_match(
    current_runs: list[RunRecord],
    direct_runs: list[RunRecord],
    *,
    required_quality_margin: float = 0.0,
) -> dict[str, float | bool]:
    if not current_runs or not direct_runs:
        raise ValueError("current and direct runs are required")

    current_quality = statistics.median(float(run["min_same_eye_ssim"]) for run in current_runs)
    direct_quality = statistics.median(float(run["min_same_eye_ssim"]) for run in direct_runs)
    current_size = statistics.median(int(run["final_bytes"]) for run in current_runs)
    direct_size = statistics.median(int(run["final_bytes"]) for run in direct_runs)
    if current_size <= 0:
        raise ValueError("current median size must be positive")
    required_direct_quality = current_quality + required_quality_margin
    all_direct_runs_not_lower = all(float(run["min_same_eye_ssim"]) >= current_quality for run in direct_runs)
    all_direct_runs_match = all(float(run["min_same_eye_ssim"]) >= required_direct_quality for run in direct_runs)
    quality_not_lower = direct_quality >= current_quality
    quality_margin_met = direct_quality >= required_direct_quality
    size_not_larger = direct_size <= current_size
    direct_hashes = [str(run.get("sha256", "")) for run in direct_runs]
    direct_runs_byte_identical = bool(direct_hashes[0]) and len(set(direct_hashes)) == 1
    return {
        "all_direct_runs_meet_required_quality": all_direct_runs_match,
        "all_direct_runs_quality_not_lower": all_direct_runs_not_lower,
        "current_median_final_bytes": current_size,
        "current_median_min_same_eye_ssim": current_quality,
        "direct_median_final_bytes": direct_size,
        "direct_median_min_same_eye_ssim": direct_quality,
        "direct_runs_byte_identical": direct_runs_byte_identical,
        "direct_to_current_size_ratio": direct_size / current_size,
        "passed": all_direct_runs_match and quality_margin_met and size_not_larger,
        "quality_delta": direct_quality - current_quality,
        "quality_margin_met": quality_margin_met,
        "quality_not_lower": quality_not_lower,
        "required_direct_min_same_eye_ssim": required_direct_quality,
        "required_quality_margin": required_quality_margin,
        "size_not_larger": size_not_larger,
    }


def evaluate_direct_candidate(
    ffmpeg: str,
    encoder_path: Path,
    work_directory: Path,
    reference_left: Path,
    reference_right: Path,
    options: FixtureOptions,
    bitrate_mbps: float,
    candidate_index: int,
) -> RunRecord:
    candidate_options = replace(options, bitrate_mbps=bitrate_mbps)
    output_path = work_directory / f"search-{candidate_index:02d}.mov"
    encode_direct(ffmpeg, encoder_path, output_path, candidate_options)
    quality = measure_eye_quality(
        ffmpeg,
        output_path,
        reference_left,
        reference_right,
        work_directory / f"search-{candidate_index:02d}-split",
    )
    return run_record(
        output_path,
        quality,
        options.duration_seconds,
        target_bitrate_mbps=bitrate_mbps,
    )


def search_quality_matched_bitrate(
    ffmpeg: str,
    encoder_path: Path,
    work_directory: Path,
    reference_left: Path,
    reference_right: Path,
    options: FixtureOptions,
    target_ssim: float,
    *,
    iterations: int,
    quality_margin: float,
) -> tuple[float, list[RunRecord]]:
    lower_bitrate = max(0.05, options.bitrate_mbps / 80)
    upper_bitrate = options.bitrate_mbps
    required_ssim = target_ssim + quality_margin
    candidates: list[RunRecord] = []

    upper_record = evaluate_direct_candidate(
        ffmpeg,
        encoder_path,
        work_directory,
        reference_left,
        reference_right,
        options,
        upper_bitrate,
        0,
    )
    candidates.append(upper_record)
    if float(upper_record["min_same_eye_ssim"]) < required_ssim:
        raise QualificationFailure("Configured direct bitrate cannot match the current path's decoded quality.")

    best_record = upper_record
    for candidate_index in range(1, iterations + 1):
        candidate_bitrate = (lower_bitrate + upper_bitrate) / 2
        record = evaluate_direct_candidate(
            ffmpeg,
            encoder_path,
            work_directory,
            reference_left,
            reference_right,
            options,
            candidate_bitrate,
            candidate_index,
        )
        candidates.append(record)
        if float(record["min_same_eye_ssim"]) >= required_ssim:
            best_record = record
            upper_bitrate = candidate_bitrate
        else:
            lower_bitrate = candidate_bitrate

    return float(best_record["target_bitrate_mbps"]), candidates


def qualify_quality_match(
    output_path: Path,
    encoder_path: Path,
    options: FixtureOptions,
    *,
    runs: int,
    search_iterations: int,
    quality_margin: float,
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("Quality-matched MV-HEVC qualification requires macOS arm64.")
    ffmpeg = command_path("ffmpeg")
    for tool in (SPATIAL_MEDIA_TOOL, MP4BOX):
        if not tool.is_file() or not tool.stat().st_mode & 0o111:
            raise QualificationFailure(f"Required bundled tool is unavailable: {tool.name}")
    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mv-hevc-quality-match-") as temporary_directory:
        work_directory = Path(temporary_directory)
        reference_left = work_directory / "left-reference.mkv"
        reference_right = work_directory / "right-reference.mkv"
        generate_reference(ffmpeg, reference_left, options, right_eye=False)
        generate_reference(ffmpeg, reference_right, options, right_eye=True)

        current_runs: list[RunRecord] = []
        for run_index in range(runs):
            run_directory = work_directory / f"current-run-{run_index + 1}"
            run_directory.mkdir()
            movie_path = run_directory / "current.mov"
            encode_current(ffmpeg, movie_path, run_directory, options)
            quality = measure_eye_quality(
                ffmpeg,
                movie_path,
                reference_left,
                reference_right,
                run_directory / "split",
            )
            current_runs.append(
                run_record(
                    movie_path,
                    quality,
                    options.duration_seconds,
                    target_bitrate_mbps=options.bitrate_mbps,
                )
            )

        current_target_ssim = statistics.median(float(run["min_same_eye_ssim"]) for run in current_runs)
        search_directory = work_directory / "search"
        search_directory.mkdir()
        matched_bitrate, search_candidates = search_quality_matched_bitrate(
            ffmpeg,
            encoder_path,
            search_directory,
            reference_left,
            reference_right,
            options,
            current_target_ssim,
            iterations=search_iterations,
            quality_margin=quality_margin,
        )

        direct_runs: list[RunRecord] = []
        selected_output: Path | None = None
        matched_options = replace(options, bitrate_mbps=matched_bitrate)
        for run_index in range(runs):
            run_directory = work_directory / f"direct-run-{run_index + 1}"
            run_directory.mkdir()
            movie_path = run_directory / "direct.mov"
            encode_direct(ffmpeg, encoder_path, movie_path, matched_options)
            missing_boxes = DIRECT_REQUIRED_BOX_TYPES - box_types(movie_path)
            if missing_boxes:
                raise QualificationFailure(
                    f"Quality-matched direct output is missing boxes: {', '.join(sorted(missing_boxes))}"
                )
            quality = measure_eye_quality(
                ffmpeg,
                movie_path,
                reference_left,
                reference_right,
                run_directory / "split",
            )
            direct_runs.append(
                run_record(
                    movie_path,
                    quality,
                    options.duration_seconds,
                    target_bitrate_mbps=matched_bitrate,
                )
            )
            selected_output = movie_path

        assert selected_output is not None
        shutil.copy2(selected_output, output_path)
        acceptance = summarize_quality_match(
            current_runs,
            direct_runs,
            required_quality_margin=quality_margin,
        )
        return {
            "acceptance": acceptance,
            "current_path": {
                "merge_quality": 75,
                "runs": current_runs,
                "target_aggregate_eye_bitrate_mbps": options.bitrate_mbps,
            },
            "direct_path": {
                "matched_target_bitrate_mbps": matched_bitrate,
                "runs": direct_runs,
            },
            "fixture": {
                "disparity_pixels": options.disparity_pixels,
                "duration_seconds": options.duration_seconds,
                "eye_height": options.eye_height,
                "eye_width": options.eye_width,
                "frame_count": options.frame_count,
                "frame_rate": options.frame_rate,
            },
            "method": {
                "quality_margin": quality_margin,
                "run_count": runs,
                "search_candidates": search_candidates,
                "search_iterations": search_iterations,
                "quality_metric": "minimum decoded same-eye SSIM",
            },
            "schema_version": 2,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Find and verify a direct MV-HEVC bitrate that matches the current path's decoded quality."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("build/direct-mv-hevc-quality-match/direct.mov"),
    )
    parser.add_argument(
        "--encoder",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
    )
    parser.add_argument("--eye-width", type=int, default=320)
    parser.add_argument("--eye-height", type=int, default=180)
    parser.add_argument("--frame-rate", type=int, default=24)
    parser.add_argument("--duration", type=int, default=2)
    parser.add_argument("--disparity-pixels", type=int, default=16)
    parser.add_argument("--bitrate-mbps", type=float, default=4.0)
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS)
    parser.add_argument("--search-iterations", type=int, default=DEFAULT_SEARCH_ITERATIONS)
    parser.add_argument("--quality-margin", type=float, default=DEFAULT_QUALITY_MARGIN)
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
    if args.runs <= 0 or args.runs > 10:
        parser.error("runs must be between 1 and 10")
    if args.search_iterations <= 0 or args.search_iterations > 12:
        parser.error("search iterations must be between 1 and 12")
    if args.quality_margin < 0 or args.quality_margin > 0.1:
        parser.error("quality margin must be between 0 and 0.1")

    options = FixtureOptions(
        eye_width=args.eye_width,
        eye_height=args.eye_height,
        frame_rate=args.frame_rate,
        duration_seconds=args.duration,
        disparity_pixels=args.disparity_pixels,
        bitrate_mbps=args.bitrate_mbps,
    )
    try:
        result = qualify_quality_match(
            args.output.resolve(),
            args.encoder.resolve(),
            options,
            runs=args.runs,
            search_iterations=args.search_iterations,
            quality_margin=args.quality_margin,
        )
    except QualificationFailure as error:
        parser.exit(1, f"error: {error}\n")

    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_output:
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if bool(result["acceptance"]["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
