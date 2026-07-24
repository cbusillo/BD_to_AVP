#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys

from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_route import AUTOMATIC_DIRECT_QUALITY
from scripts import build_mv_hevc_encoder_macos
from scripts.qualify_direct_mv_hevc import QualificationFailure, command_path
from scripts.qualify_mv_hevc_corpus import (
    DEFAULT_MATCHED_MAX_SIZE_RATIO,
    DEFAULT_QUALITY_TOLERANCE,
    _encode_direct,
    _environment_evidence,
    _measure_output,
    load_manifest,
    prepare_case,
    summarize_quality_size_gate,
)
from scripts.qualify_mv_hevc_quality_match import sha256_file


EVIDENCE_SCHEMA_VERSION = 1
DEFAULT_DIRECT_RUNS = 3


def load_generated_baseline(
    path: Path,
    *,
    expected_manifest_sha256: str,
    expected_case_ids: Sequence[str],
) -> tuple[dict[str, Sequence[Mapping[str, object]]], str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not read generated-route baseline {path}: {error}") from error
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise QualificationFailure("Generated-route baseline uses an unsupported evidence schema.")
    manifest = raw.get("manifest")
    if not isinstance(manifest, Mapping) or manifest.get("sha256") != expected_manifest_sha256:
        raise QualificationFailure("Generated-route baseline does not match the requested corpus manifest.")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list):
        raise QualificationFailure("Generated-route baseline does not contain corpus cases.")

    cases: dict[str, Sequence[Mapping[str, object]]] = {}
    for raw_case in raw_cases:
        if not isinstance(raw_case, Mapping) or not isinstance(raw_case.get("id"), str):
            raise QualificationFailure("Generated-route baseline contains an invalid case record.")
        generated = raw_case.get("generated")
        runs = generated.get("runs") if isinstance(generated, Mapping) else None
        if not isinstance(runs, list) or not runs or not all(isinstance(run, Mapping) for run in runs):
            raise QualificationFailure(f"Generated-route baseline case {raw_case['id']} has no valid runs.")
        cases[raw_case["id"]] = runs

    if set(cases) != set(expected_case_ids):
        raise QualificationFailure("Generated-route baseline case IDs do not match the corpus manifest.")
    return cases, sha256_file(path)


def summarize_adaptive_acceptance(cases: Sequence[Mapping[str, object]]) -> dict[str, object]:
    gated = [case for case in cases if case.get("quality_gate") is True]
    if not gated:
        raise ValueError("At least one quality-gated case is required.")
    acceptances = [case.get("acceptance") for case in gated]
    if not all(isinstance(acceptance, Mapping) for acceptance in acceptances):
        raise ValueError("Every quality-gated case requires an acceptance record.")
    typed_acceptances = [acceptance for acceptance in acceptances if isinstance(acceptance, Mapping)]
    return {
        "gated_case_count": len(gated),
        "minimum_quality_delta": min(float(item["quality_delta"]) for item in typed_acceptances),
        "maximum_size_ratio": max(float(item["max_run_size_ratio"]) for item in typed_acceptances),
        "minimum_eye_order_margin": min(
            min(float(run["min_eye_order_margin"]) for run in case["direct_runs"]) for case in gated
        ),
        "passed": all(item.get("passed") is True for item in typed_acceptances),
    }


def qualify_adaptive_policy(
    manifest_path: Path,
    baseline_path: Path,
    encoder_path: Path,
    output_path: Path,
    work_directory: Path,
    *,
    quality: float = AUTOMATIC_DIRECT_QUALITY,
    direct_runs: int = DEFAULT_DIRECT_RUNS,
    quality_tolerance: float = DEFAULT_QUALITY_TOLERANCE,
    max_size_ratio: float = DEFAULT_MATCHED_MAX_SIZE_RATIO,
) -> dict[str, object]:
    if not math.isfinite(quality) or not 0 <= quality <= 1:
        raise ValueError("quality must be between 0 and 1")
    if not 1 <= direct_runs <= 10:
        raise ValueError("direct runs must be between 1 and 10")

    manifest = load_manifest(manifest_path)
    manifest_sha256 = sha256_file(manifest_path)
    baseline_cases, baseline_sha256 = load_generated_baseline(
        baseline_path,
        expected_manifest_sha256=manifest_sha256,
        expected_case_ids=[case.case_id for case in manifest.cases],
    )
    ffmpeg = command_path("ffmpeg")
    ffprobe = command_path("ffprobe")
    encoder_path.parent.mkdir(parents=True, exist_ok=True)
    if not encoder_path.is_file():
        build_mv_hevc_encoder_macos.build_encoder(encoder_path)
    work_directory.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    evidence: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "policy": {"mode": "quality", "quality": quality},
        "manifest": {
            "corpus_id": manifest.corpus_id,
            "sha256": manifest_sha256,
        },
        "generated_baseline": {
            "schema_version": 1,
            "sha256": baseline_sha256,
        },
        "method": {
            "direct_runs": direct_runs,
            "quality_metric": "minimum decoded same-eye SSIM",
            "quality_tolerance": quality_tolerance,
            "max_size_ratio": max_size_ratio,
            "eye_order_gate": "case_specific_same_eye_minus_cross_eye_ssim",
        },
        "environment": _environment_evidence(encoder_path, ffmpeg, ffprobe),
        "cases": [],
        "acceptance": {"complete": False, "passed": False},
    }

    for definition in manifest.cases:
        case_work = work_directory / definition.case_id
        shutil.rmtree(case_work, ignore_errors=True)
        prepared = prepare_case(definition, case_work, ffmpeg=ffmpeg, ffprobe=ffprobe)
        measured_runs: list[dict[str, object]] = []
        for run_index in range(direct_runs):
            output = case_work / f"automatic-quality-{run_index + 1}.mov"
            _encode_direct(
                ffmpeg,
                encoder_path,
                prepared,
                output,
                quality=quality,
            )
            measured = _measure_output(
                ffmpeg,
                prepared,
                output,
                case_work / f"automatic-quality-{run_index + 1}-split",
                target_bitrate_mbps=None,
            )
            measured["target_quality"] = quality
            measured_runs.append(measured)

        acceptance = summarize_quality_size_gate(
            baseline_cases[definition.case_id],
            measured_runs,
            quality_tolerance=quality_tolerance,
            max_size_ratio=max_size_ratio,
            minimum_eye_order_margin=definition.minimum_eye_order_margin,
        )
        evidence["cases"].append(
            {
                "id": definition.case_id,
                "tags": list(definition.tags),
                "quality_gate": definition.quality_gate,
                "prepared": {
                    "duration_seconds": prepared.duration_seconds,
                    "frame_count": prepared.frame_count,
                    "eye_width": definition.output_eye_width,
                    "eye_height": definition.output_eye_height,
                    "frame_rate": definition.output_frame_rate,
                },
                "generated_runs": list(baseline_cases[definition.case_id]),
                "direct_runs": measured_runs,
                "acceptance": acceptance,
            }
        )
        output_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        shutil.rmtree(case_work, ignore_errors=True)

    evidence["acceptance"] = {
        "complete": True,
        **summarize_adaptive_acceptance(evidence["cases"]),
    }
    output_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify the content-adaptive Automatic direct MV-HEVC policy against a generated-route baseline."
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument(
        "--encoder",
        type=Path,
        default=Path("build/mv-hevc-encoder/mv-hevc-encoder"),
    )
    parser.add_argument("--quality", type=float, default=AUTOMATIC_DIRECT_QUALITY)
    parser.add_argument("--direct-runs", type=int, default=DEFAULT_DIRECT_RUNS)
    args = parser.parse_args()

    try:
        evidence = qualify_adaptive_policy(
            args.manifest.resolve(),
            args.baseline.resolve(),
            args.encoder.resolve(),
            args.output.resolve(),
            args.work_directory.resolve(),
            quality=args.quality,
            direct_runs=args.direct_runs,
        )
    except (OSError, QualificationFailure, ValueError) as error:
        print(f"Adaptive MV-HEVC qualification failed: {error}", file=sys.stderr)
        return 2
    return 0 if evidence["acceptance"]["passed"] is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
