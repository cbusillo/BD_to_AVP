#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json

from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping

from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.qualify_real_mvc_feature import (
    FeatureQualificationFailure,
    require_bool_value,
    require_int_value,
    require_mapping_value,
    require_number_value,
    resource_boundedness,
)


MAX_EVIDENCE_BYTES = 512 * 1024


def reassess(document: Mapping[str, object], *, input_sha256: str) -> dict[str, object]:
    short_run = require_mapping_value(document, "short_run")
    feature_run = require_mapping_value(document, "feature_run")
    cancellation = require_mapping_value(document, "cancellation")
    configuration = require_mapping_value(document, "configuration")
    sources = require_mapping_value(document, "sources")
    feature_source = require_mapping_value(sources, "feature")
    feature_encoder = require_mapping_value(feature_run, "encoder")
    feature_artifact = require_mapping_value(feature_run, "artifact")
    short_acceptance = require_mapping_value(short_run, "acceptance")
    feature_acceptance = require_mapping_value(feature_run, "acceptance")
    cancellation_acceptance = require_mapping_value(cancellation, "acceptance")

    max_rss_growth_mib = require_int_value(configuration, "max_rss_growth_mib")
    boundedness = resource_boundedness(
        short_run,
        feature_run,
        max_rss_growth_bytes=max_rss_growth_mib * 1024 * 1024,
    )
    expected_frames = require_int_value(feature_source, "packet_count")
    observed_frames = require_int_value(feature_encoder, "frame_count")
    expected_duration = require_number_value(feature_source, "video_duration_seconds")
    observed_duration = require_number_value(feature_artifact, "duration_seconds")
    duration_tolerance = require_number_value(configuration, "output_duration_tolerance_seconds")
    complete_frames = observed_frames == expected_frames
    complete_duration = abs(observed_duration - expected_duration) <= duration_tolerance
    short_passed = require_bool_value(short_acceptance, "passed")
    feature_passed = require_bool_value(feature_acceptance, "passed")
    cancellation_passed = require_bool_value(cancellation_acceptance, "passed")
    boundedness_passed = require_bool_value(boundedness, "passed")
    passed = (
        short_passed
        and feature_passed
        and cancellation_passed
        and boundedness_passed
        and complete_frames
        and complete_duration
    )

    evidence = dict(document)
    evidence["acceptance"] = {
        "complete_feature_length_duration": complete_duration,
        "complete_feature_length_frame_count": complete_frames,
        "feature_direct_workload": feature_passed,
        "packaged_cancellation": cancellation_passed,
        "passed": passed,
        "resource_boundedness": boundedness_passed,
        "short_direct_workload": short_passed,
    }
    evidence["assessment"] = {
        "input_evidence_sha256": input_sha256,
        "policy": "per-process-and-aggregate-q3-q4-plateau-v3",
        "reassessed_at_utc": datetime.now(UTC).isoformat(),
    }
    evidence["boundedness"] = boundedness
    evidence["schema_version"] = 3
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reassess retained real-MVC feature telemetry against the reviewed per-process resource policy."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    input_path = args.input.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    if not input_path.is_file() or output_path == input_path:
        parser.exit(1, "error: reassessment input must exist and output must be distinct\n")
    try:
        loaded = json.loads(input_path.read_text(encoding="utf-8"))
        if not isinstance(loaded, Mapping):
            raise FeatureQualificationFailure("Feature reassessment input was not a JSON object.")
        evidence = reassess(loaded, input_sha256=sha256_file(input_path))
    except (FeatureQualificationFailure, json.JSONDecodeError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    evidence_text = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if len(evidence_text.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        parser.exit(1, "error: reassessed feature evidence exceeded its bounded size limit\n")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_output = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary_output.write_text(evidence_text, encoding="utf-8")
    temporary_output.replace(output_path)
    print(json.dumps(evidence["acceptance"], indent=2, sort_keys=True))
    return 0 if bool(require_mapping_value(evidence, "acceptance")["passed"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
