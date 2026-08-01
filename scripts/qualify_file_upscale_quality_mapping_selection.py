#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import platform
import shutil
import stat
import statistics
import subprocess
import sys

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from itertools import combinations
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from bd_to_avp.modules.video_quality_defaults import (
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
    DEFAULT_UPSCALE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_sweep import (
    EXPECTED_TOOL_KEYS,
    CorpusBinding,
    FileBinding,
    UpscaleCandidate,
    _array,
    _binding_record,
    _candidate_plan_record,
    _configured_private_paths,
    _environment_evidence,
    _file_binding,
    _git_head_from_clean_worktree,
    _integer,
    _loads_json_bytes,
    _mapping,
    _number,
    _paired_delta,
    _pinned_media_tool,
    _private_source_paths,
    _record_base,
    _record_candidate,
    _relative_repository_path,
    _repository_path,
    _require_head_tracked_file,
    _safe_error_message,
    _sha256_identity,
    _string,
    _toolchain_record,
    _validate_base_against_case,
    _validate_base_record,
    _validate_candidate_against_base,
    _validate_candidate_record,
    _validate_expected_case_sources,
    _validate_prepared_source,
    _validate_private_source_identity,
    load_sweep_plan,
)
from scripts.qualify_generated_mv_hevc_calibration import (
    _assert_private_values_absent,
    _atomic_write,
    _freeze_receipt,
    calibration_lock,
)
from scripts.qualify_mv_hevc_corpus import CorpusCase, PreparedCase, load_manifest, prepare_case
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTION_PLAN = REPOSITORY_ROOT / "docs/qualification/file-upscale-quality-mapping-selection-v1.json"
DEFAULT_CONFIRMATION_PLAN = REPOSITORY_ROOT / "docs/qualification/file-upscale-quality-mapping-confirmation-v2.json"
DEFAULT_SOURCE_RECEIPT = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-sweep-v1.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-mapping-selection-v1.json"
DEFAULT_WORK_DIRECTORY = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-mapping-selection-v1-work"
DEFAULT_ARTIFACT_DIRECTORY = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-mapping-selection-v1-artifacts"
EVIDENCE_SCHEMA_VERSION = 3
WORK_DIRECTORY_MARKER = ".bd-to-avp-file-upscale-quality-mapping-selection.json"
ARTIFACT_DIRECTORY_MARKER = ".bd-to-avp-file-upscale-quality-mapping-selection-artifacts.json"
V1_EXPERIMENT_ID = "file-upscale-quality-mapping-selection-v1"
V1_PURPOSE = "objective_provisional_mapping_selection_not_public_ladder_mapping"
V1_DESIGN = "fixed_integer_quality_grid_45_55_65_75_85_95_100_v1"
V1_DECISION_STAGE = "objective_provisional_mapping_selection_only"
V2_EXPERIMENT_ID = "file-upscale-quality-mapping-confirmation-v2"
V2_PURPOSE = "objective_provisional_mapping_confirmation_not_public_ladder_mapping"
V2_DESIGN = "fixed_integer_quality_grid_45_55_65_75_85_95_100_confirmation_v2"
V2_DECISION_STAGE = "objective_provisional_mapping_confirmation_only"
V2_CONFIRMATION_PLAN_SHA256 = "c831add22aed97b629c53af76b60cd7eccf6654c088a6a73f1b5ba53b4095118"
V2_SOURCE_EXPERIMENT_ID = "file-upscale-quality-repeatability-calibration-v2"
V2_SOURCE_RECEIPT_SHA256 = "6d44f4c23df142d3a819f0aba1b87f9fa688435485f4f1798a103ea94ccbe49e"
V2_SOURCE_GIT_SHA = "1f988fbf198595d52084eabc3055edd2f1d14221"
V2_SOURCE_PLAN_PATH = REPOSITORY_ROOT / "docs/qualification/file-upscale-quality-repeatability-calibration-v2.json"
V2_SOURCE_PLAN_SHA256 = "c4cf953bd868eadd04f4ed11a7ca4f2211c81f5ee72f375347f5f3d9cf14ecdb"
V2_PREDECESSOR_EXPERIMENT_ID = "file-upscale-quality-mapping-selection-v1"
V2_PREDECESSOR_RECEIPT_SHA256 = "c8e2478913a8c458657f0f7904720d6f76e8761b8ba1922e7c5dda5b916d2cef"
V2_PREDECESSOR_SOURCE_GIT_SHA = "b93a9729a2396b3942e679a1a8db34967f9d4467"
V2_PREDECESSOR_PLAN_SHA256 = "3aa76c79adb81e72dd89f9fd548ef73698880eebf6332c149fe401c058d090ee"
EXPECTED_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-snow-detail",
    "production-motion",
    "production-crop",
    "production-rate-override",
    "synthetic-animation",
)
EXPECTED_COVERAGE = (
    "real_mvc",
    "motion",
    "grain",
    "dark",
    "animation",
    "crop",
    "disparity",
    "frame_rate_override",
    "bit_depth_8",
)
EXPECTED_QUALITIES = (45, 55, 65, 75, 85, 95, 100)
EXPECTED_BASE_PERMUTATIONS = (
    (45, 55, 65, 75, 85, 95, 100),
    (100, 95, 85, 75, 65, 55, 45),
    (45, 100, 55, 95, 65, 85, 75),
)
EXPECTED_ROTATION_OFFSETS = (0, 2, 4)
EXPECTED_RETAINED_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-snow-detail",
    "production-motion",
)
SOURCE_RESPONSE_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-crop",
    "production-rate-override",
    "synthetic-animation",
)
EXPECTED_TIE_BREAKS = (
    "wider_minimum_case_storage_coverage",
    "larger_minimum_objective_quality_margin",
    "larger_minimum_storage_margin",
    "wider_end_to_end_storage_coverage",
    "lower_first_quality",
    "lexicographic_candidate_ids",
)
REPEATABILITY_FIELDS = (
    "min_same_eye_ssim",
    "final_to_base_size_ratio",
    "minimum_frame_same_eye_ssim",
    "p05_frame_same_eye_ssim",
    "frame_ssim_standard_deviation",
    "maximum_adjacent_frame_ssim_drop",
    "min_eye_order_margin",
)
SUMMARY_FIELDS = (
    "min_same_eye_ssim",
    "minimum_frame_same_eye_ssim",
    "p05_frame_same_eye_ssim",
    "frame_ssim_standard_deviation",
    "maximum_adjacent_frame_ssim_drop",
    "min_eye_order_margin",
    "final_to_base_size_ratio",
    "final_bytes",
)
EXPECTED_NOISE = {
    "min_same_eye_ssim": ("min_same_eye_ssim", 0.000081, 0.0001, 0.0002),
    "final_to_base_size_ratio": (
        "final_to_base_size_ratio",
        0.00797990891104794,
        0.01,
        0.02,
    ),
    "minimum_frame_same_eye_ssim": (
        "minimum_frame_same_eye_ssim",
        0.000778000000000056,
        0.0001,
        0.0016,
    ),
    "p05_frame_same_eye_ssim": (
        "p05_frame_same_eye_ssim",
        0.000556000000000001,
        0.0001,
        0.0012,
    ),
    "frame_ssim_standard_deviation": (
        "frame_ssim_standard_deviation",
        0.0000506638525244697,
        0.0001,
        0.0002,
    ),
    "maximum_adjacent_frame_ssim_drop": (
        "maximum_adjacent_frame_ssim_drop",
        0.000496000000000052,
        0.0001,
        0.001,
    ),
    "min_eye_order_margin": (
        "min_eye_order_margin",
        0.000516999999999879,
        0.0001,
        0.0011,
    ),
}
EXPECTED_CALIBRATED_NOISE = {
    "min_same_eye_ssim": ("min_same_eye_ssim", 0.00007699999999999374, 0.0001, 0.0002),
    "final_to_base_size_ratio": (
        "final_to_base_size_ratio",
        0.014467748802706737,
        0.01,
        0.03,
    ),
    "minimum_frame_same_eye_ssim": (
        "minimum_frame_same_eye_ssim",
        0.0026720000000000077,
        0.0001,
        0.0054,
    ),
    "p05_frame_same_eye_ssim": (
        "p05_frame_same_eye_ssim",
        0.0009209999999999496,
        0.0001,
        0.0019,
    ),
    "frame_ssim_standard_deviation": (
        "frame_ssim_standard_deviation",
        0.00008787075701880039,
        0.0001,
        0.0002,
    ),
    "maximum_adjacent_frame_ssim_drop": (
        "maximum_adjacent_frame_ssim_drop",
        0.002871000000000068,
        0.0001,
        0.0058,
    ),
    "min_eye_order_margin": (
        "min_eye_order_margin",
        0.00019099999999994122,
        0.0001,
        0.0011,
    ),
}
V2_PREVIOUS_LIMITS = {
    "min_same_eye_ssim": 0.0002,
    "final_to_base_size_ratio": 0.02,
    "minimum_frame_same_eye_ssim": 0.0016,
    "p05_frame_same_eye_ssim": 0.0012,
    "frame_ssim_standard_deviation": 0.0002,
    "maximum_adjacent_frame_ssim_drop": 0.001,
    "min_eye_order_margin": 0.0011,
}
V2_SOURCE_CASES = {
    "min_same_eye_ssim": "production-snow-detail",
    "final_to_base_size_ratio": "production-snow-detail",
    "minimum_frame_same_eye_ssim": "production-motion",
    "p05_frame_same_eye_ssim": "production-snow-detail",
    "frame_ssim_standard_deviation": "production-motion",
    "maximum_adjacent_frame_ssim_drop": "production-motion",
    "min_eye_order_margin": "production-motion",
}


@dataclass(frozen=True)
class SourceReceiptBinding:
    schema_version: int
    experiment_id: str
    sha256: str
    source_git_sha: str
    required_file_mode: int


@dataclass(frozen=True)
class SourcePlanBinding:
    path: Path
    sha256: str
    schema_version: int


@dataclass(frozen=True)
class NoiseLimit:
    key: str
    record_field: str
    source_maximum: float
    quantum: float
    limit: float


@dataclass(frozen=True)
class CaseSchedule:
    case_id: str
    case_index: int
    orders: tuple[tuple[int, ...], ...]


@dataclass(frozen=True)
class MappingSelectionContract:
    experiment_id: str
    purpose: str
    design: str
    decision_stage: str
    source_kind: str
    non_inferiority_values: tuple[float, float, float, float, float, float]
    real_case_minimum_frame_threshold: float
    real_case_p05_threshold: float


V1_SELECTION_CONTRACT = MappingSelectionContract(
    experiment_id=V1_EXPERIMENT_ID,
    purpose=V1_PURPOSE,
    design=V1_DESIGN,
    decision_stage=V1_DECISION_STAGE,
    source_kind="response_sweep_v1",
    non_inferiority_values=(-0.0002, -0.0016, -0.0012, 0.0002, 0.001, 0.0011),
    real_case_minimum_frame_threshold=0.0016,
    real_case_p05_threshold=0.0012,
)
V2_CONFIRMATION_CONTRACT = MappingSelectionContract(
    experiment_id=V2_EXPERIMENT_ID,
    purpose=V2_PURPOSE,
    design=V2_DESIGN,
    decision_stage=V2_DECISION_STAGE,
    source_kind="repeatability_calibration_v2",
    non_inferiority_values=(-0.0002, -0.0054, -0.0019, 0.0002, 0.0058, 0.0011),
    real_case_minimum_frame_threshold=0.0054,
    real_case_p05_threshold=0.0019,
)


@dataclass(frozen=True)
class MappingSelectionPlan:
    experiment_id: str
    binding_path: Path
    binding_id: str
    binding_sha256: str
    source_receipt: SourceReceiptBinding
    source_plan: SourcePlanBinding
    noise_limits: tuple[NoiseLimit, ...]
    ladder_manifest: FileBinding
    video_quality_swift: FileBinding
    balanced_quality: int
    base_eye_bitrate_mbps: int
    base_merge_quality: int
    runs_per_candidate: int
    case_schedules: tuple[CaseSchedule, ...]
    candidates: tuple[UpscaleCandidate, ...]
    ffmpeg_manifest: FileBinding
    fx_upscale_binary: FileBinding
    bundled_tools: Mapping[str, FileBinding]
    generated_encoder_contract: str
    file_upscale_command_contract: str
    metric_contract: str
    geometry_contract: str
    frame_rate_contract: str
    duration_tolerance_frames: int
    maximum_final_to_base_size_ratio: float
    minimum_case_median_storage_growth: float
    minimum_aggregate_delta: float
    minimum_minimum_frame_delta: float
    minimum_p05_delta: float
    maximum_frame_standard_deviation_increase: float
    maximum_adjacent_drop_increase: float
    maximum_eye_order_margin_loss: float
    minimum_corpus_median_aggregate_improvement: float
    real_case_clear_count: int
    real_case_aggregate_threshold: float
    real_case_minimum_frame_threshold: float
    real_case_p05_threshold: float
    required_sensitive_case_ids: tuple[str, ...]
    required_sensitive_case_clear_count: int
    selection_tie_breaks: tuple[str, ...]
    target_named_step_count: int
    retained_repeat_index: int
    retained_case_ids: tuple[str, ...]
    relative_path: str | None = None
    schema_version: int = 1
    target_id: str = "upscale_quality"
    purpose: str = "objective_provisional_mapping_selection_not_public_ladder_mapping"
    design: str = "fixed_integer_quality_grid_45_55_65_75_85_95_100_v1"
    execution_order_contract: str = "materialized_cross_case_rotations_v1"
    decision_stage: str = "objective_provisional_mapping_selection_only"


def _exact_keys(value: object, keys: Sequence[str], label: str) -> Mapping[str, object]:
    document = _mapping(value, label)
    expected = set(keys)
    if set(document) != expected:
        missing = sorted(expected - set(document))
        unexpected = sorted(set(document) - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise QualificationFailure(f"{label} has an invalid shape ({'; '.join(details)}).")
    return document


def _git_sha_identity(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 40 or any(character not in "0123456789abcdef" for character in digest):
        raise QualificationFailure(f"{label} must be a lowercase full Git SHA.")
    return digest


def _file_mode(value: object, label: str) -> int:
    text = _string(value, label)
    if text != "0444":
        raise QualificationFailure(f"{label} must be 0444.")
    return 0o444


def _rotate_left(values: tuple[int, ...], amount: int) -> tuple[int, ...]:
    offset = amount % len(values)
    return values[offset:] + values[:offset]


def materialized_case_orders(case_ids: Sequence[str] = EXPECTED_CASE_IDS) -> tuple[CaseSchedule, ...]:
    return tuple(
        CaseSchedule(
            case_id=case_id,
            case_index=case_index,
            orders=tuple(
                _rotate_left(permutation, case_index + EXPECTED_ROTATION_OFFSETS[repeat_index])
                for repeat_index, permutation in enumerate(EXPECTED_BASE_PERMUTATIONS)
            ),
        )
        for case_index, case_id in enumerate(case_ids)
    )


def _derived_limit(source_maximum: float, quantum: float) -> float:
    source = Decimal(str(source_maximum))
    step = Decimal(str(quantum))
    return float(((Decimal(2) * source / step).to_integral_value(rounding=ROUND_CEILING)) * step)


def _derived_calibrated_limit(previous_limit: float, source_maximum: float, quantum: float) -> float:
    previous = Decimal(str(previous_limit))
    source = Decimal(str(source_maximum))
    step = Decimal(str(quantum))
    observed_limit = ((Decimal(2) * source / step).to_integral_value(rounding=ROUND_CEILING)) * step
    return float(max(previous, observed_limit))


def parse_mapping_corpus_binding(raw: object) -> CorpusBinding:
    document = _exact_keys(
        raw,
        (
            "schema_version",
            "binding_id",
            "purpose",
            "source_manifest",
            "selected_case_ids",
            "private_source_identity",
            "expected_case_sources",
            "required_coverage",
        ),
        "file-upscale mapping corpus binding",
    )
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1, maximum=1)
    binding_id = _string(document.get("binding_id"), "binding_id")
    if binding_id != "file-upscale-quality-corpus-v2":
        raise QualificationFailure("file-upscale mapping corpus binding_id is unsupported.")
    purpose = _string(document.get("purpose"), "purpose")
    if purpose != "checked_file_upscale_mapping_selection_quality_gated_subset_not_public_mappings":
        raise QualificationFailure("file-upscale mapping corpus purpose is unsupported.")
    source_manifest = _exact_keys(
        document.get("source_manifest"),
        ("path", "corpus_id", "sha256"),
        "source_manifest",
    )
    source_manifest_path = _repository_path(
        _string(source_manifest.get("path"), "source_manifest.path"),
        "Source manifest path",
    )
    source_corpus_id = _string(source_manifest.get("corpus_id"), "source_manifest.corpus_id")
    source_manifest_sha256 = _sha256_identity(source_manifest.get("sha256"), "source_manifest.sha256")
    selected_case_ids = tuple(
        _string(case_id, f"selected_case_ids[{index}]")
        for index, case_id in enumerate(_array(document.get("selected_case_ids"), "selected_case_ids"))
    )
    if selected_case_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("file-upscale mapping corpus must select exactly the seven quality-gated cases.")
    expected_sources_document = _mapping(document.get("expected_case_sources"), "expected_case_sources")
    if set(expected_sources_document) != set(selected_case_ids):
        raise QualificationFailure("expected_case_sources must identify each selected mapping case exactly once.")
    expected_case_sources = {
        case_id: dict(_mapping(expected_sources_document[case_id], f"expected_case_sources.{case_id}"))
        for case_id in selected_case_ids
    }
    required_coverage = tuple(
        _string(tag, f"required_coverage[{index}]")
        for index, tag in enumerate(_array(document.get("required_coverage"), "required_coverage"))
    )
    if required_coverage != EXPECTED_COVERAGE:
        raise QualificationFailure("file-upscale mapping corpus must retain the checked coverage contract.")
    return CorpusBinding(
        schema_version=schema_version,
        binding_id=binding_id,
        purpose=purpose,
        source_manifest_path=source_manifest_path,
        source_corpus_id=source_corpus_id,
        source_manifest_sha256=source_manifest_sha256,
        selected_case_ids=selected_case_ids,
        expected_case_sources=expected_case_sources,
        required_coverage=required_coverage,
        private_source_identity=dict(_mapping(document.get("private_source_identity"), "private_source_identity")),
    )


def load_mapping_corpus_binding(path: Path) -> tuple[CorpusBinding, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "File-upscale mapping corpus binding")
    try:
        document = _loads_json_bytes(resolved_path.read_bytes(), "file-upscale mapping corpus binding")
    except OSError as error:
        raise QualificationFailure(f"Could not read file-upscale mapping corpus binding {path.name}.") from error
    parsed = parse_mapping_corpus_binding(document)
    if not parsed.source_manifest_path.is_file():
        raise QualificationFailure("The bound direct corpus manifest is unavailable.")
    if sha256_file(parsed.source_manifest_path) != parsed.source_manifest_sha256:
        raise QualificationFailure("The bound direct corpus manifest does not match its pinned SHA-256 identity.")
    manifest = load_manifest(parsed.source_manifest_path)
    if manifest.corpus_id != parsed.source_corpus_id:
        raise QualificationFailure("The bound direct corpus ID does not match the referenced manifest.")
    cases_by_id = {case.case_id: case for case in manifest.cases}
    manifest_quality_gated_ids = tuple(case.case_id for case in manifest.cases if case.quality_gate)
    if manifest_quality_gated_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("The direct corpus quality-gated case set or manifest order changed.")
    if any(case_id not in cases_by_id for case_id in parsed.selected_case_ids):
        raise QualificationFailure("The file-upscale mapping corpus references an unknown direct-corpus case.")
    observed_coverage = {tag for case_id in parsed.selected_case_ids for tag in cases_by_id[case_id].tags}
    if not set(parsed.required_coverage).issubset(observed_coverage):
        raise QualificationFailure("The file-upscale mapping corpus is missing required coverage.")
    _validate_expected_case_sources(parsed, cases_by_id)
    _validate_private_source_identity(parsed)
    return (
        CorpusBinding(
            binding_id=parsed.binding_id,
            source_manifest_path=parsed.source_manifest_path,
            source_corpus_id=parsed.source_corpus_id,
            source_manifest_sha256=parsed.source_manifest_sha256,
            selected_case_ids=parsed.selected_case_ids,
            expected_case_sources=parsed.expected_case_sources,
            required_coverage=parsed.required_coverage,
            private_source_identity=parsed.private_source_identity,
            relative_path=relative_path,
            schema_version=parsed.schema_version,
            purpose=parsed.purpose,
        ),
        sha256_file(resolved_path),
    )


def _parse_source_response_v1(
    document: Mapping[str, object],
) -> tuple[SourceReceiptBinding, SourcePlanBinding, tuple[NoiseLimit, ...]]:
    response = _exact_keys(document.get("source_response"), ("receipt", "plan", "noise_derivation"), "source_response")
    receipt = _exact_keys(
        response.get("receipt"),
        ("schema_version", "experiment_id", "sha256", "source_git_sha", "required_file_mode"),
        "source_response.receipt",
    )
    source_receipt = SourceReceiptBinding(
        schema_version=_integer(
            receipt.get("schema_version"), "source_response.receipt.schema_version", minimum=2, maximum=2
        ),
        experiment_id=_string(receipt.get("experiment_id"), "source_response.receipt.experiment_id"),
        sha256=_sha256_identity(receipt.get("sha256"), "source_response.receipt.sha256"),
        source_git_sha=_git_sha_identity(receipt.get("source_git_sha"), "source_response.receipt.source_git_sha"),
        required_file_mode=_file_mode(receipt.get("required_file_mode"), "source_response.receipt.required_file_mode"),
    )
    if source_receipt.experiment_id != "file-upscale-quality-sweep-v1":
        raise QualificationFailure("The source response experiment identity is unsupported.")
    if source_receipt.sha256 != "d62f038afa796f7404bd47dabc6f84cfa47ba6e221b32a501ebc4314714c9bb6":
        raise QualificationFailure("The source response receipt SHA-256 changed from the preregistration.")
    if source_receipt.source_git_sha != "a96e6a0e21fc21e47dad6c9fec186725ef6166a3":
        raise QualificationFailure("The source response Git identity changed from the preregistration.")

    raw_source_plan = _exact_keys(
        response.get("plan"),
        ("path", "sha256", "schema_version"),
        "source_response.plan",
    )
    source_plan = SourcePlanBinding(
        path=_repository_path(
            _string(raw_source_plan.get("path"), "source_response.plan.path"),
            "Source response plan",
        ),
        sha256=_sha256_identity(raw_source_plan.get("sha256"), "source_response.plan.sha256"),
        schema_version=_integer(
            raw_source_plan.get("schema_version"), "source_response.plan.schema_version", minimum=1, maximum=1
        ),
    )
    if source_plan.sha256 != "978323dccf106a1933c0e4809861d2278c882dfa5459e514e84eae4f1aa844f5":
        raise QualificationFailure("The source response plan SHA-256 changed from the preregistration.")

    derivation = _exact_keys(
        response.get("noise_derivation"),
        (
            "source_records",
            "group_by",
            "within_group_statistic",
            "source_maximum_statistic",
            "forbidden_source_field",
            "formula",
            "multiplier",
            "metrics",
        ),
        "source_response.noise_derivation",
    )
    expected_derivation = {
        "source_records": "raw_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_group_statistic": "maximum_minus_minimum_across_three_repeats",
        "source_maximum_statistic": "maximum_across_groups",
        "forbidden_source_field": "candidate_summaries[*].repeat_ssim_spread",
        "formula": "ceil(multiplier * source_maximum / quantum) * quantum",
        "multiplier": 2,
    }
    for key, expected in expected_derivation.items():
        if derivation.get(key) != expected:
            raise QualificationFailure(f"source_response.noise_derivation.{key} changed from the checked contract.")
    metrics = _mapping(derivation.get("metrics"), "source_response.noise_derivation.metrics")
    if set(metrics) != set(EXPECTED_NOISE):
        raise QualificationFailure("The source-noise metric set changed from the checked contract.")
    noise_limits: list[NoiseLimit] = []
    for key in REPEATABILITY_FIELDS:
        record_field, source_maximum, quantum, limit = EXPECTED_NOISE[key]
        metric = _exact_keys(
            metrics.get(key),
            ("record_field", "source_maximum", "quantum", "limit"),
            f"source_response.noise_derivation.metrics.{key}",
        )
        parsed = NoiseLimit(
            key=key,
            record_field=_string(metric.get("record_field"), f"noise metric {key}.record_field"),
            source_maximum=_number(metric.get("source_maximum"), f"noise metric {key}.source_maximum"),
            quantum=_number(metric.get("quantum"), f"noise metric {key}.quantum", positive=True),
            limit=_number(metric.get("limit"), f"noise metric {key}.limit", positive=True),
        )
        if parsed.record_field != record_field or not math.isclose(
            parsed.source_maximum, source_maximum, rel_tol=0.0, abs_tol=1e-18
        ):
            raise QualificationFailure(f"The preregistered source maximum for {key} changed.")
        if parsed.quantum != quantum or parsed.limit != limit:
            raise QualificationFailure(f"The preregistered quantum or limit for {key} changed.")
        if _derived_limit(parsed.source_maximum, parsed.quantum) != parsed.limit:
            raise QualificationFailure(f"The preregistered 2x ceil/quantum derivation for {key} is inconsistent.")
        noise_limits.append(parsed)
    return source_receipt, source_plan, tuple(noise_limits)


def _parse_source_response_v2(
    document: Mapping[str, object],
) -> tuple[SourceReceiptBinding, SourcePlanBinding, tuple[NoiseLimit, ...]]:
    response = _exact_keys(document.get("source_response"), ("receipt", "plan", "noise_derivation"), "source_response")
    receipt = _exact_keys(
        response.get("receipt"),
        ("schema_version", "experiment_id", "sha256", "source_git_sha", "required_file_mode"),
        "source_response.receipt",
    )
    source_receipt = SourceReceiptBinding(
        schema_version=_integer(
            receipt.get("schema_version"), "source_response.receipt.schema_version", minimum=4, maximum=4
        ),
        experiment_id=_string(receipt.get("experiment_id"), "source_response.receipt.experiment_id"),
        sha256=_sha256_identity(receipt.get("sha256"), "source_response.receipt.sha256"),
        source_git_sha=_git_sha_identity(receipt.get("source_git_sha"), "source_response.receipt.source_git_sha"),
        required_file_mode=_file_mode(receipt.get("required_file_mode"), "source_response.receipt.required_file_mode"),
    )
    if source_receipt.experiment_id != V2_SOURCE_EXPERIMENT_ID:
        raise QualificationFailure("The v2 source calibration experiment identity is unsupported.")
    if source_receipt.sha256 != V2_SOURCE_RECEIPT_SHA256:
        raise QualificationFailure("The v2 source calibration receipt SHA-256 changed from the preregistration.")
    if source_receipt.source_git_sha != V2_SOURCE_GIT_SHA:
        raise QualificationFailure("The v2 source calibration Git identity changed from the preregistration.")

    raw_source_plan = _exact_keys(
        response.get("plan"),
        ("path", "sha256", "schema_version"),
        "source_response.plan",
    )
    source_plan = SourcePlanBinding(
        path=_repository_path(
            _string(raw_source_plan.get("path"), "source_response.plan.path"),
            "Source response plan",
        ),
        sha256=_sha256_identity(raw_source_plan.get("sha256"), "source_response.plan.sha256"),
        schema_version=_integer(
            raw_source_plan.get("schema_version"), "source_response.plan.schema_version", minimum=1, maximum=1
        ),
    )
    if source_plan.path != V2_SOURCE_PLAN_PATH or source_plan.sha256 != V2_SOURCE_PLAN_SHA256:
        raise QualificationFailure("The v2 source calibration plan identity changed from the preregistration.")

    derivation = _exact_keys(
        response.get("noise_derivation"),
        (
            "source_records",
            "group_by",
            "within_group_statistic",
            "source_maximum_statistic",
            "forbidden_source_field",
            "formula",
            "multiplier",
            "metrics",
        ),
        "source_response.noise_derivation",
    )
    expected_derivation = {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_group_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "forbidden_source_field": "predecessor_receipt.cases[*].repeats[*].candidates[*]",
        "formula": "max(previous_limit, ceil(multiplier * source_maximum / quantum) * quantum)",
        "multiplier": 2,
    }
    for key, expected in expected_derivation.items():
        if derivation.get(key) != expected:
            raise QualificationFailure(f"source_response.noise_derivation.{key} changed from the v2 checked contract.")
    metrics = _mapping(derivation.get("metrics"), "source_response.noise_derivation.metrics")
    if set(metrics) != set(EXPECTED_CALIBRATED_NOISE):
        raise QualificationFailure("The calibrated source-noise metric set changed from the checked contract.")
    noise_limits: list[NoiseLimit] = []
    for key in REPEATABILITY_FIELDS:
        record_field, source_maximum, quantum, limit = EXPECTED_CALIBRATED_NOISE[key]
        metric = _exact_keys(
            metrics.get(key),
            ("record_field", "source_maximum", "quantum", "limit"),
            f"source_response.noise_derivation.metrics.{key}",
        )
        parsed = NoiseLimit(
            key=key,
            record_field=_string(metric.get("record_field"), f"noise metric {key}.record_field"),
            source_maximum=_number(metric.get("source_maximum"), f"noise metric {key}.source_maximum"),
            quantum=_number(metric.get("quantum"), f"noise metric {key}.quantum", positive=True),
            limit=_number(metric.get("limit"), f"noise metric {key}.limit", positive=True),
        )
        if parsed.record_field != record_field or not math.isclose(
            parsed.source_maximum, source_maximum, rel_tol=0.0, abs_tol=1e-18
        ):
            raise QualificationFailure(f"The preregistered calibrated source maximum for {key} changed.")
        if parsed.quantum != quantum or parsed.limit != limit:
            raise QualificationFailure(f"The preregistered calibrated quantum or limit for {key} changed.")
        if _derived_calibrated_limit(V2_PREVIOUS_LIMITS[key], parsed.source_maximum, parsed.quantum) != parsed.limit:
            raise QualificationFailure(f"The preregistered calibrated derivation for {key} is inconsistent.")
        noise_limits.append(parsed)
    return source_receipt, source_plan, tuple(noise_limits)


def _parse_source_response(
    document: Mapping[str, object],
    contract: MappingSelectionContract,
) -> tuple[SourceReceiptBinding, SourcePlanBinding, tuple[NoiseLimit, ...]]:
    if contract.source_kind == "response_sweep_v1":
        return _parse_source_response_v1(document)
    if contract.source_kind == "repeatability_calibration_v2":
        return _parse_source_response_v2(document)
    raise QualificationFailure("The mapping-selection source-response contract is unsupported.")


def _require_boolean(document: Mapping[str, object], key: str, expected: bool, label: str) -> None:
    if document.get(key) is not expected:
        raise QualificationFailure(f"{label}.{key} must be {str(expected).lower()}.")


def _parse_mapping_selection_plan_contract(
    raw: object,
    contract: MappingSelectionContract,
) -> MappingSelectionPlan:
    document = _exact_keys(
        raw,
        (
            "schema_version",
            "experiment_id",
            "target_id",
            "purpose",
            "design",
            "source_response",
            "corpus_binding",
            "public_contract_bindings",
            "balanced",
            "generated_base",
            "runs_per_candidate",
            "execution_order",
            "candidates",
            "toolchain",
            "technical_eligibility",
            "boundary_policy",
            "selection_policy",
            "artifact_retention",
            "decision_policy",
        ),
        "file-upscale mapping-selection plan",
    )
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1, maximum=1)
    experiment_id = _string(document.get("experiment_id"), "experiment_id")
    if experiment_id != contract.experiment_id:
        raise QualificationFailure("file-upscale mapping-selection experiment_id is unsupported.")
    target_id = _string(document.get("target_id"), "target_id")
    if target_id != "upscale_quality":
        raise QualificationFailure("file-upscale mapping-selection target_id must be upscale_quality.")
    purpose = _string(document.get("purpose"), "purpose")
    if purpose != contract.purpose:
        raise QualificationFailure("file-upscale mapping-selection purpose is unsupported.")
    design = _string(document.get("design"), "design")
    if design != contract.design:
        raise QualificationFailure("file-upscale mapping-selection design is unsupported.")

    source_receipt, source_plan, noise_limits = _parse_source_response(document, contract)

    corpus_binding = _exact_keys(
        document.get("corpus_binding"),
        ("path", "binding_id", "sha256"),
        "corpus_binding",
    )
    binding_path = _repository_path(
        _string(corpus_binding.get("path"), "corpus_binding.path"),
        "Corpus binding path",
    )
    binding_id = _string(corpus_binding.get("binding_id"), "corpus_binding.binding_id")
    binding_sha256 = _sha256_identity(corpus_binding.get("sha256"), "corpus_binding.sha256")
    if binding_id != "file-upscale-quality-corpus-v2":
        raise QualificationFailure("The mapping-selection corpus binding ID is unsupported.")

    public_bindings = _exact_keys(
        document.get("public_contract_bindings"),
        ("ladder_manifest", "video_quality_swift"),
        "public_contract_bindings",
    )
    ladder_manifest = _file_binding(
        public_bindings.get("ladder_manifest"),
        "public_contract_bindings.ladder_manifest",
    )
    video_quality_swift = _file_binding(
        public_bindings.get("video_quality_swift"),
        "public_contract_bindings.video_quality_swift",
    )
    if ladder_manifest.sha256 != "04620e59e5380c88d3d5152f78712402675f31db6f1253c1d93224af585111dc":
        raise QualificationFailure("The public ladder manifest binding changed from the preregistration.")
    if video_quality_swift.sha256 != "6f204564261d859590086ca41e9a27ac9f69bc0feb225137cf0abc4a98082dfa":
        raise QualificationFailure("The VideoQuality.swift binding changed from the preregistration.")

    balanced = _exact_keys(
        document.get("balanced"),
        ("candidate_id", "quality", "quality_source"),
        "balanced",
    )
    balanced_quality = _integer(balanced.get("quality"), "balanced.quality", minimum=0, maximum=100)
    if (
        balanced.get("candidate_id") != "q075"
        or balanced_quality != DEFAULT_UPSCALE_QUALITY
        or balanced.get("quality_source") != "bd_to_avp.modules.video_quality_defaults.DEFAULT_UPSCALE_QUALITY"
    ):
        raise QualificationFailure("Balanced must remain the exact production q075 default.")

    generated_base = _exact_keys(
        document.get("generated_base"),
        ("eye_bitrate_mbps", "eye_bitrate_source", "merge_quality", "merge_quality_source", "contract"),
        "generated_base",
    )
    base_eye_bitrate_mbps = _integer(
        generated_base.get("eye_bitrate_mbps"),
        "generated_base.eye_bitrate_mbps",
        minimum=1,
        maximum=500,
    )
    base_merge_quality = _integer(
        generated_base.get("merge_quality"),
        "generated_base.merge_quality",
        minimum=0,
        maximum=100,
    )
    if base_eye_bitrate_mbps != AUTOMATIC_GENERATED_EYE_BITRATE_MBPS:
        raise QualificationFailure("The generated base eye bitrate changed from the production default.")
    if base_merge_quality != AUTOMATIC_GENERATED_MERGE_QUALITY:
        raise QualificationFailure("The generated base merge quality changed from the production default.")
    if (
        generated_base.get("eye_bitrate_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_EYE_BITRATE_MBPS"
        or generated_base.get("merge_quality_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_MERGE_QUALITY"
        or generated_base.get("contract") != "production_generated_mv_hevc_v1"
    ):
        raise QualificationFailure("The generated base production contract changed.")

    runs_per_candidate = _integer(document.get("runs_per_candidate"), "runs_per_candidate", minimum=3, maximum=3)
    execution_order = _exact_keys(
        document.get("execution_order"),
        ("contract", "runtime_shuffle_forbidden", "base_permutations", "repeat_rotation_offsets", "case_orders"),
        "execution_order",
    )
    execution_order_contract = _string(execution_order.get("contract"), "execution_order.contract")
    if execution_order_contract != "materialized_cross_case_rotations_v1":
        raise QualificationFailure("The mapping-selection execution-order contract is unsupported.")
    _require_boolean(execution_order, "runtime_shuffle_forbidden", True, "execution_order")
    base_permutations = tuple(
        tuple(
            _integer(value, f"execution_order.base_permutations[{repeat_index}] value", minimum=0, maximum=100)
            for value in _array(raw_order, f"execution_order.base_permutations[{repeat_index}]")
        )
        for repeat_index, raw_order in enumerate(
            _array(execution_order.get("base_permutations"), "execution_order.base_permutations")
        )
    )
    if base_permutations != EXPECTED_BASE_PERMUTATIONS:
        raise QualificationFailure("The mapping-selection base permutations changed.")
    rotation_offsets = tuple(
        _integer(value, f"execution_order.repeat_rotation_offsets[{index}]", minimum=0, maximum=6)
        for index, value in enumerate(
            _array(execution_order.get("repeat_rotation_offsets"), "execution_order.repeat_rotation_offsets")
        )
    )
    if rotation_offsets != EXPECTED_ROTATION_OFFSETS:
        raise QualificationFailure("The mapping-selection repeat rotation offsets changed.")
    raw_case_orders = _array(execution_order.get("case_orders"), "execution_order.case_orders")
    case_schedules: list[CaseSchedule] = []
    for index, raw_schedule in enumerate(raw_case_orders):
        schedule = _exact_keys(
            raw_schedule, ("case_id", "case_index", "orders"), f"execution_order.case_orders[{index}]"
        )
        case_schedules.append(
            CaseSchedule(
                case_id=_string(schedule.get("case_id"), f"execution_order.case_orders[{index}].case_id"),
                case_index=_integer(
                    schedule.get("case_index"),
                    f"execution_order.case_orders[{index}].case_index",
                    minimum=0,
                    maximum=6,
                ),
                orders=tuple(
                    tuple(
                        _integer(
                            quality,
                            f"execution_order.case_orders[{index}].orders[{repeat_index}] value",
                            minimum=0,
                            maximum=100,
                        )
                        for quality in _array(
                            raw_order,
                            f"execution_order.case_orders[{index}].orders[{repeat_index}]",
                        )
                    )
                    for repeat_index, raw_order in enumerate(
                        _array(schedule.get("orders"), f"execution_order.case_orders[{index}].orders")
                    )
                ),
            )
        )
    if tuple(case_schedules) != materialized_case_orders():
        raise QualificationFailure("The materialized per-case/per-repeat execution schedule changed.")

    raw_candidates = _array(document.get("candidates"), "candidates")
    candidates: list[UpscaleCandidate] = []
    for index, raw_candidate in enumerate(raw_candidates):
        candidate = _exact_keys(raw_candidate, ("id", "quality"), f"candidates[{index}]")
        quality = _integer(candidate.get("quality"), f"candidates[{index}].quality", minimum=0, maximum=100)
        candidate_id = _string(candidate.get("id"), f"candidates[{index}].id")
        if candidate_id != f"q{quality:03d}":
            raise QualificationFailure("Each mapping candidate ID must be qNNN for its integer quality.")
        candidates.append(UpscaleCandidate(candidate_id=candidate_id, quality=quality))
    if tuple(candidate.quality for candidate in candidates) != EXPECTED_QUALITIES:
        raise QualificationFailure("The mapping-selection grid must be exactly 45, 55, 65, 75, 85, 95, 100.")
    if candidates[-1].bitrate_scaling_factor != "1":
        raise QualificationFailure("q100 must use the canonical bitrate-scaling factor 1.")

    toolchain = _exact_keys(
        document.get("toolchain"),
        (
            "ffmpeg_manifest",
            "generated_encoder_contract",
            "file_upscale_command_contract",
            "fx_upscale_binary",
            "bundled_tools",
            "metric_contract",
            "geometry_contract",
            "timing_contract",
        ),
        "toolchain",
    )
    ffmpeg_manifest = _file_binding(toolchain.get("ffmpeg_manifest"), "toolchain.ffmpeg_manifest")
    fx_upscale_binary = _file_binding(toolchain.get("fx_upscale_binary"), "toolchain.fx_upscale_binary")
    bundled_tools_document = _mapping(toolchain.get("bundled_tools"), "toolchain.bundled_tools")
    if tuple(sorted(bundled_tools_document)) != EXPECTED_TOOL_KEYS:
        raise QualificationFailure("The mapping-selection bundled-tool set changed.")
    bundled_tools = {
        key: _file_binding(bundled_tools_document[key], f"toolchain.bundled_tools.{key}") for key in EXPECTED_TOOL_KEYS
    }
    generated_encoder_contract = _string(
        toolchain.get("generated_encoder_contract"), "toolchain.generated_encoder_contract"
    )
    file_upscale_command_contract = _string(
        toolchain.get("file_upscale_command_contract"), "toolchain.file_upscale_command_contract"
    )
    metric_contract = _string(toolchain.get("metric_contract"), "toolchain.metric_contract")
    geometry_contract = _string(toolchain.get("geometry_contract"), "toolchain.geometry_contract")
    timing_contract = _exact_keys(
        toolchain.get("timing_contract"),
        ("frame_rate", "duration_tolerance_frames"),
        "toolchain.timing_contract",
    )
    frame_rate_contract = _string(timing_contract.get("frame_rate"), "toolchain.timing_contract.frame_rate")
    duration_tolerance_frames = _integer(
        timing_contract.get("duration_tolerance_frames"),
        "toolchain.timing_contract.duration_tolerance_frames",
        minimum=1,
        maximum=1,
    )
    if (
        generated_encoder_contract != "production_generated_mv_hevc_v1"
        or file_upscale_command_contract != "bd_to_avp.modules.video.fx_upscale_command_v1"
        or metric_contract != "ffmpeg_ssim_aggregate_and_per_frame_v1"
        or geometry_contract != "fx_upscale_2x_spatial_output_v1"
        or frame_rate_contract != "ffprobe_r_frame_rate_frame_count_duration_v2"
    ):
        raise QualificationFailure("The mapping-selection toolchain contract changed.")

    technical = _exact_keys(
        document.get("technical_eligibility"),
        (
            "all_planned_records_required",
            "structure_timing_geometry_eye_order_hash_provenance_required",
            "repeatability_limits_source",
            "maximum_final_to_base_size_ratio",
        ),
        "technical_eligibility",
    )
    _require_boolean(technical, "all_planned_records_required", True, "technical_eligibility")
    _require_boolean(
        technical,
        "structure_timing_geometry_eye_order_hash_provenance_required",
        True,
        "technical_eligibility",
    )
    if technical.get("repeatability_limits_source") != "source_response.noise_derivation.metrics":
        raise QualificationFailure("The technical repeatability source changed.")
    maximum_final_to_base_size_ratio = _number(
        technical.get("maximum_final_to_base_size_ratio"),
        "technical_eligibility.maximum_final_to_base_size_ratio",
        positive=True,
    )
    if maximum_final_to_base_size_ratio != 4.1:
        raise QualificationFailure("The technical size cap must remain 4.10.")

    boundary = _exact_keys(
        document.get("boundary_policy"),
        (
            "evaluate_every_ordered_lower_higher_pair",
            "storage",
            "quality_non_inferiority",
            "objective_distinction",
            "failed_boundary_action",
            "threshold_changes_forbidden",
            "interpolation_forbidden",
            "aliases_forbidden",
            "post_hoc_candidates_forbidden",
        ),
        "boundary_policy",
    )
    _require_boolean(boundary, "evaluate_every_ordered_lower_higher_pair", True, "boundary_policy")
    for key in (
        "threshold_changes_forbidden",
        "interpolation_forbidden",
        "aliases_forbidden",
        "post_hoc_candidates_forbidden",
    ):
        _require_boolean(boundary, key, True, "boundary_policy")
    if boundary.get("failed_boundary_action") != "collapse":
        raise QualificationFailure("Failed objective boundaries must collapse.")
    storage = _exact_keys(
        boundary.get("storage"),
        ("strict_increase_in_every_paired_repeat", "minimum_case_median_paired_growth_ratio"),
        "boundary_policy.storage",
    )
    _require_boolean(storage, "strict_increase_in_every_paired_repeat", True, "boundary_policy.storage")
    minimum_case_median_storage_growth = _number(
        storage.get("minimum_case_median_paired_growth_ratio"),
        "boundary_policy.storage.minimum_case_median_paired_growth_ratio",
        positive=True,
    )
    if minimum_case_median_storage_growth != 0.02:
        raise QualificationFailure("The boundary storage threshold must remain 0.02.")
    non_inferiority = _exact_keys(
        boundary.get("quality_non_inferiority"),
        (
            "minimum_aggregate_delta",
            "minimum_minimum_frame_delta",
            "minimum_p05_delta",
            "maximum_frame_standard_deviation_increase",
            "maximum_adjacent_drop_increase",
            "maximum_eye_order_margin_loss",
        ),
        "boundary_policy.quality_non_inferiority",
    )
    non_inferiority_values = (
        _number(non_inferiority.get("minimum_aggregate_delta"), "minimum_aggregate_delta"),
        _number(non_inferiority.get("minimum_minimum_frame_delta"), "minimum_minimum_frame_delta"),
        _number(non_inferiority.get("minimum_p05_delta"), "minimum_p05_delta"),
        _number(
            non_inferiority.get("maximum_frame_standard_deviation_increase"),
            "maximum_frame_standard_deviation_increase",
        ),
        _number(non_inferiority.get("maximum_adjacent_drop_increase"), "maximum_adjacent_drop_increase"),
        _number(non_inferiority.get("maximum_eye_order_margin_loss"), "maximum_eye_order_margin_loss"),
    )
    if non_inferiority_values != contract.non_inferiority_values:
        raise QualificationFailure("The quality non-inferiority thresholds changed.")

    distinction = _exact_keys(
        boundary.get("objective_distinction"),
        (
            "minimum_corpus_median_aggregate_improvement",
            "real_case_clear_count",
            "real_case_aggregate_threshold",
            "real_case_minimum_frame_threshold",
            "real_case_p05_threshold",
            "required_sensitive_case_ids",
            "required_sensitive_case_clear_count",
        ),
        "boundary_policy.objective_distinction",
    )
    minimum_corpus_median_aggregate_improvement = _number(
        distinction.get("minimum_corpus_median_aggregate_improvement"),
        "minimum_corpus_median_aggregate_improvement",
        positive=True,
    )
    real_case_clear_count = _integer(
        distinction.get("real_case_clear_count"), "real_case_clear_count", minimum=2, maximum=2
    )
    real_case_aggregate_threshold = _number(
        distinction.get("real_case_aggregate_threshold"), "real_case_aggregate_threshold", positive=True
    )
    real_case_minimum_frame_threshold = _number(
        distinction.get("real_case_minimum_frame_threshold"),
        "real_case_minimum_frame_threshold",
        positive=True,
    )
    real_case_p05_threshold = _number(
        distinction.get("real_case_p05_threshold"), "real_case_p05_threshold", positive=True
    )
    required_sensitive_case_ids = tuple(
        _string(value, "required_sensitive_case_ids value")
        for value in _array(distinction.get("required_sensitive_case_ids"), "required_sensitive_case_ids")
    )
    required_sensitive_case_clear_count = _integer(
        distinction.get("required_sensitive_case_clear_count"),
        "required_sensitive_case_clear_count",
        minimum=1,
        maximum=1,
    )
    if (
        minimum_corpus_median_aggregate_improvement != 0.0002
        or real_case_aggregate_threshold != 0.0002
        or real_case_minimum_frame_threshold != contract.real_case_minimum_frame_threshold
        or real_case_p05_threshold != contract.real_case_p05_threshold
        or required_sensitive_case_ids != ("production-grain-rain", "production-snow-detail")
    ):
        raise QualificationFailure("The objective-distinction contract changed.")

    selection = _exact_keys(
        document.get("selection_policy"),
        ("primary", "tie_breaks", "slot_assignment", "missing_slots", "target_named_step_count"),
        "selection_policy",
    )
    if selection.get("primary") != "maximum_cardinality_ordered_subset_containing_q075":
        raise QualificationFailure("The mapping-selection primary algorithm changed.")
    selection_tie_breaks = tuple(
        _string(value, "selection_policy.tie_breaks value")
        for value in _array(selection.get("tie_breaks"), "selection_policy.tie_breaks")
    )
    if selection_tie_breaks != EXPECTED_TIE_BREAKS:
        raise QualificationFailure("The mapping-selection tie-break order changed.")
    slot_assignment = _exact_keys(
        selection.get("slot_assignment"),
        ("balanced", "lower_outward", "higher_outward"),
        "selection_policy.slot_assignment",
    )
    if (
        slot_assignment.get("balanced") != "q075"
        or slot_assignment.get("lower_outward") != ["efficient", "compact", "space_saver"]
        or slot_assignment.get("higher_outward") != ["detailed", "high_detail", "maximum_detail"]
        or selection.get("missing_slots") != "unsupported"
    ):
        raise QualificationFailure("The provisional slot-assignment contract changed.")
    target_named_step_count = _integer(
        selection.get("target_named_step_count"), "selection_policy.target_named_step_count", minimum=7, maximum=7
    )

    retention = _exact_keys(
        document.get("artifact_retention"),
        (
            "repeat_index",
            "case_ids",
            "retain_generated_base",
            "retain_all_candidate_outputs",
            "record_relative_paths_only",
        ),
        "artifact_retention",
    )
    retained_repeat_index = _integer(
        retention.get("repeat_index"), "artifact_retention.repeat_index", minimum=0, maximum=2
    )
    retained_case_ids = tuple(
        _string(value, "artifact_retention.case_ids value")
        for value in _array(retention.get("case_ids"), "artifact_retention.case_ids")
    )
    if retained_repeat_index != 0 or retained_case_ids != EXPECTED_RETAINED_CASE_IDS:
        raise QualificationFailure("The preregistered artifact-retention repeat or case set changed.")
    for key in ("retain_generated_base", "retain_all_candidate_outputs", "record_relative_paths_only"):
        _require_boolean(retention, key, True, "artifact_retention")

    decision = _exact_keys(
        document.get("decision_policy"),
        (
            "stage",
            "post_hoc_thresholds_forbidden",
            "public_mapping_changes_forbidden",
            "ladder_mapping_selected",
            "perceptual_review_performed",
            "long_form_runtime_performed",
            "package_parity_performed",
            "vision_pro_validation_performed",
            "downstream_checks_block_objective_stage",
        ),
        "decision_policy",
    )
    decision_stage = _string(decision.get("stage"), "decision_policy.stage")
    if decision_stage != contract.decision_stage:
        raise QualificationFailure("The mapping-selection decision stage changed.")
    for key in ("post_hoc_thresholds_forbidden", "public_mapping_changes_forbidden"):
        _require_boolean(decision, key, True, "decision_policy")
    for key in (
        "ladder_mapping_selected",
        "perceptual_review_performed",
        "long_form_runtime_performed",
        "package_parity_performed",
        "vision_pro_validation_performed",
        "downstream_checks_block_objective_stage",
    ):
        _require_boolean(decision, key, False, "decision_policy")

    return MappingSelectionPlan(
        schema_version=schema_version,
        experiment_id=experiment_id,
        target_id=target_id,
        purpose=purpose,
        design=design,
        binding_path=binding_path,
        binding_id=binding_id,
        binding_sha256=binding_sha256,
        source_receipt=source_receipt,
        source_plan=source_plan,
        noise_limits=noise_limits,
        ladder_manifest=ladder_manifest,
        video_quality_swift=video_quality_swift,
        balanced_quality=balanced_quality,
        base_eye_bitrate_mbps=base_eye_bitrate_mbps,
        base_merge_quality=base_merge_quality,
        runs_per_candidate=runs_per_candidate,
        case_schedules=tuple(case_schedules),
        candidates=tuple(candidates),
        ffmpeg_manifest=ffmpeg_manifest,
        fx_upscale_binary=fx_upscale_binary,
        bundled_tools=bundled_tools,
        generated_encoder_contract=generated_encoder_contract,
        file_upscale_command_contract=file_upscale_command_contract,
        metric_contract=metric_contract,
        geometry_contract=geometry_contract,
        frame_rate_contract=frame_rate_contract,
        duration_tolerance_frames=duration_tolerance_frames,
        maximum_final_to_base_size_ratio=maximum_final_to_base_size_ratio,
        minimum_case_median_storage_growth=minimum_case_median_storage_growth,
        minimum_aggregate_delta=non_inferiority_values[0],
        minimum_minimum_frame_delta=non_inferiority_values[1],
        minimum_p05_delta=non_inferiority_values[2],
        maximum_frame_standard_deviation_increase=non_inferiority_values[3],
        maximum_adjacent_drop_increase=non_inferiority_values[4],
        maximum_eye_order_margin_loss=non_inferiority_values[5],
        minimum_corpus_median_aggregate_improvement=minimum_corpus_median_aggregate_improvement,
        real_case_clear_count=real_case_clear_count,
        real_case_aggregate_threshold=real_case_aggregate_threshold,
        real_case_minimum_frame_threshold=real_case_minimum_frame_threshold,
        real_case_p05_threshold=real_case_p05_threshold,
        required_sensitive_case_ids=required_sensitive_case_ids,
        required_sensitive_case_clear_count=required_sensitive_case_clear_count,
        selection_tie_breaks=selection_tie_breaks,
        target_named_step_count=target_named_step_count,
        retained_repeat_index=retained_repeat_index,
        retained_case_ids=retained_case_ids,
        execution_order_contract=execution_order_contract,
        decision_stage=decision_stage,
    )


def _parse_mapping_selection_plan_v1(raw: object) -> MappingSelectionPlan:
    return _parse_mapping_selection_plan_contract(raw, V1_SELECTION_CONTRACT)


def _parse_mapping_selection_plan_v2(raw: object) -> MappingSelectionPlan:
    return _parse_mapping_selection_plan_contract(raw, V2_CONFIRMATION_CONTRACT)


def parse_mapping_selection_plan(raw: object) -> MappingSelectionPlan:
    document = _mapping(raw, "file-upscale mapping-selection plan")
    identity = (document.get("experiment_id"), document.get("purpose"))
    if identity == (V1_EXPERIMENT_ID, V1_PURPOSE):
        return _parse_mapping_selection_plan_v1(document)
    if identity == (V2_EXPERIMENT_ID, V2_PURPOSE):
        return _parse_mapping_selection_plan_v2(document)
    raise QualificationFailure("The file-upscale mapping-selection experiment ID and purpose are unsupported.")


def _validate_public_ladder(path: Path) -> None:
    try:
        document = _loads_json_bytes(path.read_bytes(), "video-quality ladder manifest")
    except OSError as error:
        raise QualificationFailure("Could not read the bound video-quality ladder manifest.") from error
    targets = _array(document.get("calibration_targets"), "video-quality ladder calibration_targets")
    target = next(
        (
            _mapping(raw_target, "video-quality ladder target")
            for raw_target in targets
            if isinstance(raw_target, Mapping) and raw_target.get("id") == "upscale_quality"
        ),
        None,
    )
    if target is None or target.get("ladder_exposure") != "pending_calibration":
        raise QualificationFailure("The public upscale-quality ladder exposure changed before selection.")
    mappings = _array(target.get("mappings"), "upscale-quality ladder mappings")
    expected_steps = (
        "space_saver",
        "compact",
        "efficient",
        "balanced",
        "detailed",
        "high_detail",
        "maximum_detail",
    )
    if tuple(_mapping(mapping, "upscale-quality mapping").get("step_id") for mapping in mappings) != expected_steps:
        raise QualificationFailure("The public upscale-quality ladder steps changed before selection.")
    for raw_mapping in mappings:
        mapping = _mapping(raw_mapping, "upscale-quality mapping")
        if mapping.get("step_id") == "balanced":
            if mapping.get("status") != "baseline_anchor" or mapping.get("values") != {"upscale_quality": 75}:
                raise QualificationFailure("The public upscale-quality Balanced anchor changed before selection.")
        elif mapping.get("status") != "needs_calibration" or mapping.get("values") is not None:
            raise QualificationFailure("A public upscale-quality mapping was selected before this objective stage.")


def _validate_v2_source_plan(plan: MappingSelectionPlan, binding: CorpusBinding) -> None:
    if plan.source_plan.path != V2_SOURCE_PLAN_PATH or plan.source_plan.sha256 != V2_SOURCE_PLAN_SHA256:
        raise QualificationFailure("The v2 source calibration plan binding is inconsistent.")
    try:
        data = plan.source_plan.path.read_bytes()
    except OSError as error:
        raise QualificationFailure("Could not read the bound v2 source calibration plan.") from error
    if hashlib.sha256(data).hexdigest() != V2_SOURCE_PLAN_SHA256:
        raise QualificationFailure("The bound v2 source calibration plan does not match its pinned SHA-256 identity.")
    document = _loads_json_bytes(data, "v2 source calibration plan")
    if (
        document.get("schema_version") != plan.source_plan.schema_version
        or document.get("experiment_id") != V2_SOURCE_EXPERIMENT_ID
        or document.get("target_id") != "upscale_quality"
        or document.get("purpose") != "calibrate_q075_repeatability_limits_only_not_mapping_selection"
    ):
        raise QualificationFailure("The bound v2 source calibration plan identity is inconsistent.")
    source_binding = _mapping(document.get("corpus_binding"), "v2 source calibration corpus_binding")
    if (
        source_binding.get("path") != binding.relative_path
        or source_binding.get("binding_id") != binding.binding_id
        or source_binding.get("sha256") != plan.binding_sha256
        or source_binding.get("selected_case_ids") != list(binding.selected_case_ids)
    ):
        raise QualificationFailure("The v2 source calibration corpus binding is inconsistent.")
    predecessor = _mapping(document.get("predecessor"), "v2 source calibration predecessor")
    predecessor_receipt = _mapping(predecessor.get("receipt"), "v2 source calibration predecessor receipt")
    predecessor_plan = _mapping(predecessor.get("plan"), "v2 source calibration predecessor plan")
    if (
        predecessor_receipt.get("schema_version") != 3
        or predecessor_receipt.get("experiment_id") != V2_PREDECESSOR_EXPERIMENT_ID
        or predecessor_receipt.get("sha256") != V2_PREDECESSOR_RECEIPT_SHA256
        or predecessor_receipt.get("source_git_sha") != V2_PREDECESSOR_SOURCE_GIT_SHA
        or predecessor_receipt.get("required_file_mode") != "0444"
        or predecessor_plan.get("path") != "docs/qualification/file-upscale-quality-mapping-selection-v1.json"
        or predecessor_plan.get("sha256") != V2_PREDECESSOR_PLAN_SHA256
        or predecessor_plan.get("schema_version") != 1
    ):
        raise QualificationFailure("The v2 source calibration predecessor binding is inconsistent.")
    derivation = _mapping(document.get("derivation"), "v2 source calibration derivation")
    if (
        derivation.get("source_records") != "raw_q075_case_repeat_candidate_records_only"
        or derivation.get("predecessor_receipt_records_forbidden") is not True
        or derivation.get("summary_fields_as_source_forbidden") is not True
    ):
        raise QualificationFailure("The v2 source calibration derivation does not isolate raw q075 records.")
    scope = _mapping(document.get("scope"), "v2 source calibration scope")
    expected_scope = {
        "calibration_only": True,
        "selection_forbidden": True,
        "boundary_evaluation_forbidden": True,
        "provisional_outputs_forbidden": True,
        "public_contract_changes_forbidden": True,
        "later_confirmation_required": True,
    }
    if scope != expected_scope:
        raise QualificationFailure("The v2 source calibration-only scope changed.")


def load_mapping_selection_plan(
    path: Path,
    *,
    allow_historical_public_contracts: bool = False,
) -> tuple[MappingSelectionPlan, CorpusBinding, str, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "File-upscale mapping-selection plan")
    try:
        document = _loads_json_bytes(resolved_path.read_bytes(), "file-upscale mapping-selection plan")
    except OSError as error:
        raise QualificationFailure(f"Could not read file-upscale mapping-selection plan {path.name}.") from error
    parsed = parse_mapping_selection_plan(document)
    binding, binding_sha256 = load_mapping_corpus_binding(parsed.binding_path)
    if binding_sha256 != parsed.binding_sha256 or binding.binding_id != parsed.binding_id:
        raise QualificationFailure("The mapping-selection corpus binding does not match its pinned identity.")
    plan_sha256 = sha256_file(resolved_path)
    if parsed.experiment_id == V1_EXPERIMENT_ID:
        source_sweep, _, source_plan_sha256, _ = load_sweep_plan(parsed.source_plan.path)
        if (
            source_plan_sha256 != parsed.source_plan.sha256
            or source_sweep.schema_version != parsed.source_plan.schema_version
            or source_sweep.experiment_id != parsed.source_receipt.experiment_id
        ):
            raise QualificationFailure("The bound response-characterization plan identity is inconsistent.")
    elif parsed.experiment_id == V2_EXPERIMENT_ID:
        if relative_path != "docs/qualification/file-upscale-quality-mapping-confirmation-v2.json":
            raise QualificationFailure("The v2 confirmation must use its exact repository plan path.")
        if plan_sha256 != V2_CONFIRMATION_PLAN_SHA256:
            raise QualificationFailure("The v2 confirmation plan changed from its preregistered SHA-256 identity.")
        _validate_v2_source_plan(parsed, binding)
    else:
        raise QualificationFailure("The mapping-selection plan contract is unsupported.")
    public_contract_labels = {"video-quality ladder manifest", "VideoQuality.swift"}
    for label, file_binding in (
        ("FFmpeg vendor manifest", parsed.ffmpeg_manifest),
        ("FX Upscale binary", parsed.fx_upscale_binary),
        ("video-quality ladder manifest", parsed.ladder_manifest),
        ("VideoQuality.swift", parsed.video_quality_swift),
        *((f"bundled tool {key}", tool) for key, tool in parsed.bundled_tools.items()),
    ):
        current_matches = file_binding.path.is_file() and sha256_file(file_binding.path) == file_binding.sha256
        if not current_matches and not (
            allow_historical_public_contracts and label in public_contract_labels and file_binding.path.is_file()
        ):
            raise QualificationFailure(f"{label} does not match its pinned SHA-256 identity.")
    _validate_public_ladder(parsed.ladder_manifest.path)
    return (
        MappingSelectionPlan(
            **{
                **parsed.__dict__,
                "relative_path": relative_path,
            }
        ),
        binding,
        plan_sha256,
        binding_sha256,
    )


def recompute_source_noise_maxima(receipt: Mapping[str, object]) -> dict[str, dict[str, object]]:
    cases = _array(receipt.get("cases"), "source response cases")
    if (
        tuple(_string(_mapping(case, "source response case").get("id"), "source response case.id") for case in cases)
        != SOURCE_RESPONSE_CASE_IDS
    ):
        raise QualificationFailure("The source response raw case set or order changed.")
    groups: dict[tuple[str, str], dict[str, list[float]]] = {}
    repeat_indices: dict[tuple[str, str], set[int]] = {}
    for raw_case in cases:
        case = _mapping(raw_case, "source response case")
        case_id = _string(case.get("id"), "source response case.id")
        repeats = _array(case.get("repeats"), f"source response case {case_id} repeats")
        if len(repeats) != 3:
            raise QualificationFailure(f"Source response case {case_id} must contain exactly three repeats.")
        for raw_repeat in repeats:
            repeat = _mapping(raw_repeat, f"source response case {case_id} repeat")
            repeat_index = _integer(
                repeat.get("repeat_index"),
                f"source response case {case_id} repeat_index",
                minimum=0,
                maximum=2,
            )
            candidates = _array(repeat.get("candidates"), f"source response case {case_id} candidates")
            if tuple(
                sorted(
                    _string(_mapping(candidate, "source response candidate").get("id"), "candidate.id")
                    for candidate in candidates
                )
            ) != ("q065", "q075", "q085"):
                raise QualificationFailure("The source response raw candidate set changed.")
            for raw_candidate in candidates:
                candidate = _mapping(raw_candidate, "source response candidate")
                candidate_id = _string(candidate.get("id"), "source response candidate.id")
                group_key = (case_id, candidate_id)
                if repeat_index in repeat_indices.setdefault(group_key, set()):
                    raise QualificationFailure("The source response contains a duplicate grouped repeat.")
                repeat_indices[group_key].add(repeat_index)
                values = groups.setdefault(group_key, {field: [] for field in REPEATABILITY_FIELDS})
                for field in REPEATABILITY_FIELDS:
                    values[field].append(_number(candidate.get(field), f"source response candidate.{field}"))
    expected_group_count = len(SOURCE_RESPONSE_CASE_IDS) * 3
    if len(groups) != expected_group_count or any(indices != {0, 1, 2} for indices in repeat_indices.values()):
        raise QualificationFailure("The source response grouped-repeat matrix is incomplete.")

    maxima: dict[str, dict[str, object]] = {}
    for field in REPEATABILITY_FIELDS:
        ranked = sorted(
            (
                (max(values[field]) - min(values[field]), case_id, candidate_id)
                for (case_id, candidate_id), values in groups.items()
            ),
            key=lambda item: (-item[0], item[1], item[2]),
        )
        source_maximum, case_id, candidate_id = ranked[0]
        maxima[field] = {
            "record_field": field,
            "source_maximum": source_maximum,
            "source_group": {"case_id": case_id, "candidate_id": candidate_id},
            "group_count": len(ranked),
        }
    return maxima


def recompute_calibration_noise_maxima(receipt: Mapping[str, object]) -> dict[str, object]:
    cases = _array(receipt.get("cases"), "source calibration cases")
    if (
        tuple(
            _string(_mapping(case, "source calibration case").get("id"), "source calibration case.id") for case in cases
        )
        != EXPECTED_CASE_IDS
    ):
        raise QualificationFailure("The source calibration raw case set or order changed.")
    values_by_case: dict[str, dict[str, list[float]]] = {}
    case_repeat_ranges: list[dict[str, object]] = []
    for raw_case in cases:
        case = _mapping(raw_case, "source calibration case")
        case_id = _string(case.get("id"), "source calibration case.id")
        repeats = _array(case.get("repeats"), f"source calibration case {case_id} repeats")
        if len(repeats) != 5:
            raise QualificationFailure(f"Source calibration case {case_id} must contain exactly five repeats.")
        values: dict[str, list[float]] = {field: [] for field in REPEATABILITY_FIELDS}
        for expected_index, raw_repeat in enumerate(repeats):
            repeat = _mapping(raw_repeat, f"source calibration case {case_id} repeat")
            if repeat.get("repeat_index") != expected_index or repeat.get("order") != [75]:
                raise QualificationFailure(f"Source calibration case {case_id} repeat schedule changed.")
            candidates = _array(repeat.get("candidates"), f"source calibration case {case_id} candidates")
            if len(candidates) != 1:
                raise QualificationFailure(f"Source calibration case {case_id} must contain q075 only.")
            candidate = _mapping(candidates[0], f"source calibration case {case_id} q075")
            if candidate.get("id") != "q075" or candidate.get("quality") != 75:
                raise QualificationFailure(f"Source calibration case {case_id} candidate is not q075.")
            for field in REPEATABILITY_FIELDS:
                values[field].append(_number(candidate.get(field), f"source calibration q075.{field}"))
        values_by_case[case_id] = values
        case_repeat_ranges.append(
            {
                "case_id": case_id,
                "candidate_id": "q075",
                "repeat_count": 5,
                "ranges": {field: max(field_values) - min(field_values) for field, field_values in values.items()},
            }
        )

    maxima: dict[str, dict[str, object]] = {}
    for field in REPEATABILITY_FIELDS:
        ranked = sorted(
            (
                (
                    max(values_by_case[case_id][field]) - min(values_by_case[case_id][field]),
                    case_index,
                    case_id,
                )
                for case_index, case_id in enumerate(EXPECTED_CASE_IDS)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        source_maximum, _, case_id = ranked[0]
        maxima[field] = {
            "record_field": field,
            "source_maximum": source_maximum,
            "source_group": {"case_id": case_id, "candidate_id": "q075"},
            "group_count": len(ranked),
        }
    return {
        "raw_record_count": len(EXPECTED_CASE_IDS) * 5,
        "case_repeat_ranges": case_repeat_ranges,
        "metrics": maxima,
    }


def _read_frozen_source_receipt(path: Path, binding: SourceReceiptBinding) -> Mapping[str, object]:
    resolved = path.resolve()
    if path.is_symlink():
        raise QualificationFailure("The source response receipt is unavailable or unsafe.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise QualificationFailure("The source response receipt is unavailable or unsafe.") from error
    with os.fdopen(descriptor, "rb") as receipt_file:
        receipt_stat = os.fstat(receipt_file.fileno())
        if not stat.S_ISREG(receipt_stat.st_mode):
            raise QualificationFailure("The source response receipt is not a regular file.")
        data = receipt_file.read()
    if stat.S_IMODE(receipt_stat.st_mode) != binding.required_file_mode:
        raise QualificationFailure("The source response receipt must be frozen read-only at mode 0444.")
    if hashlib.sha256(data).hexdigest() != binding.sha256:
        raise QualificationFailure("The source response receipt does not match its pinned SHA-256 identity.")
    receipt = _loads_json_bytes(data, "source response receipt")
    _assert_private_values_absent(receipt, ())
    return receipt


def _verify_source_response_v1(
    plan: MappingSelectionPlan,
    source_receipt_path: Path,
) -> dict[str, object]:
    receipt = _read_frozen_source_receipt(source_receipt_path, plan.source_receipt)
    source_plan, source_binding, source_plan_sha256, source_binding_sha256 = load_sweep_plan(plan.source_plan.path)
    if source_plan_sha256 != plan.source_plan.sha256:
        raise QualificationFailure("The source response plan does not match its pinned SHA-256 identity.")
    if (
        receipt.get("schema_version") != plan.source_receipt.schema_version
        or receipt.get("experiment_id") != plan.source_receipt.experiment_id
        or receipt.get("source_git_sha") != plan.source_receipt.source_git_sha
        or receipt.get("source_tree_dirty") is not False
    ):
        raise QualificationFailure("The source response receipt identity is inconsistent.")
    expected_plan_record = {
        "path": _relative_repository_path(plan.source_plan.path, "Source response plan"),
        "sha256": plan.source_plan.sha256,
    }
    if receipt.get("experiment_plan") != expected_plan_record:
        raise QualificationFailure("The source response receipt does not bind the checked response plan.")
    expected_binding_record = {
        "path": source_binding.relative_path,
        "binding_id": source_binding.binding_id,
        "sha256": source_binding_sha256,
    }
    if receipt.get("corpus_binding") != expected_binding_record:
        raise QualificationFailure("The source response receipt corpus binding is inconsistent.")
    if receipt.get("selected_case_ids") != list(source_binding.selected_case_ids):
        raise QualificationFailure("The source response receipt selected case set changed.")
    if receipt.get("candidates") != [_candidate_plan_record(candidate) for candidate in source_plan.candidates]:
        raise QualificationFailure("The source response receipt candidate grid changed.")
    acceptance = _mapping(receipt.get("acceptance"), "source response acceptance")
    required_acceptance = {
        "complete": True,
        "finalized": True,
        "planned_full_stress_subset": True,
        "structural_passed": True,
        "execution_passed": True,
        "decision_ready": True,
        "post_hoc_thresholds_forbidden": True,
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
        "thresholds_selected": False,
    }
    for key, expected in required_acceptance.items():
        if acceptance.get(key) is not expected:
            raise QualificationFailure(f"The source response acceptance field {key} is inconsistent.")

    recomputed = recompute_source_noise_maxima(receipt)
    verification: dict[str, object] = {}
    for limit in plan.noise_limits:
        observed = _mapping(recomputed.get(limit.record_field), f"recomputed source noise {limit.record_field}")
        observed_maximum = _number(observed.get("source_maximum"), f"observed {limit.record_field} source maximum")
        if not math.isclose(observed_maximum, limit.source_maximum, rel_tol=0.0, abs_tol=1e-15):
            raise QualificationFailure(
                f"The raw grouped source response maximum for {limit.record_field} changed from the preregistration."
            )
        if _derived_limit(observed_maximum, limit.quantum) != limit.limit:
            raise QualificationFailure(
                f"The raw grouped source response derivation for {limit.record_field} no longer yields its limit."
            )
        verification[limit.key] = {
            "record_field": limit.record_field,
            "observed_source_maximum": observed_maximum,
            "source_group": dict(_mapping(observed.get("source_group"), "source noise source_group")),
            "group_count": observed.get("group_count"),
            "multiplier": 2,
            "quantum": limit.quantum,
            "limit": limit.limit,
            "verified": True,
        }
    return {
        "receipt": {
            "schema_version": plan.source_receipt.schema_version,
            "experiment_id": plan.source_receipt.experiment_id,
            "sha256": plan.source_receipt.sha256,
            "source_git_sha": plan.source_receipt.source_git_sha,
            "file_mode": "0444",
        },
        "plan": {
            "path": expected_plan_record["path"],
            "sha256": plan.source_plan.sha256,
            "schema_version": plan.source_plan.schema_version,
        },
        "noise_derivation": {
            "source_records": "raw_case_repeat_candidate_records_only",
            "group_by": ["case_id", "candidate_id"],
            "within_group_statistic": "maximum_minus_minimum_across_three_repeats",
            "source_maximum_statistic": "maximum_across_groups",
            "forbidden_source_field": "candidate_summaries[*].repeat_ssim_spread",
            "formula": "ceil(multiplier * source_maximum / quantum) * quantum",
            "metrics": verification,
        },
    }


def _verify_source_response_v2(
    plan: MappingSelectionPlan,
    source_receipt_path: Path,
) -> dict[str, object]:
    receipt = _read_frozen_source_receipt(source_receipt_path, plan.source_receipt)
    binding, binding_sha256 = load_mapping_corpus_binding(plan.binding_path)
    _validate_v2_source_plan(plan, binding)
    if (
        receipt.get("schema_version") != plan.source_receipt.schema_version
        or receipt.get("experiment_id") != plan.source_receipt.experiment_id
        or receipt.get("source_git_sha") != plan.source_receipt.source_git_sha
        or receipt.get("source_tree_dirty") is not False
    ):
        raise QualificationFailure("The v2 source calibration receipt identity is inconsistent.")
    expected_plan_record = {
        "path": _relative_repository_path(plan.source_plan.path, "V2 source calibration plan"),
        "sha256": plan.source_plan.sha256,
    }
    if receipt.get("experiment_plan") != expected_plan_record:
        raise QualificationFailure("The v2 source receipt does not bind the checked calibration plan.")
    expected_binding_record = {
        "path": binding.relative_path,
        "binding_id": binding.binding_id,
        "sha256": binding_sha256,
    }
    if receipt.get("corpus_binding") != expected_binding_record:
        raise QualificationFailure("The v2 source calibration receipt corpus binding is inconsistent.")
    if receipt.get("selected_case_ids") != list(binding.selected_case_ids):
        raise QualificationFailure("The v2 source calibration selected case set changed.")
    expected_candidates = [_candidate_plan_record(UpscaleCandidate(candidate_id="q075", quality=75))]
    if receipt.get("candidates") != expected_candidates:
        raise QualificationFailure("The v2 source calibration candidate set must contain q075 only.")
    if receipt.get("public_contract_bindings") != _public_contract_record(plan):
        raise QualificationFailure("The v2 source calibration public contract bindings changed.")
    if receipt.get("toolchain") != _toolchain_record(cast(Any, plan)):
        raise QualificationFailure("The v2 source calibration toolchain binding changed.")

    acceptance = _exact_keys(
        receipt.get("acceptance"),
        (
            "complete",
            "finalized",
            "planned_full_quality_gated_corpus",
            "predecessor_verified",
            "expected_record_count",
            "record_count",
            "structural_timing_geometry_hash_provenance_passed",
            "eye_order_passed",
            "size_cap_passed",
            "retained_artifacts_complete",
            "derived_limits_complete",
            "calibration_receipt_valid",
            "calibration_only",
            "public_contract_changes_forbidden",
            "later_confirmation_required",
            "passed",
        ),
        "v2 source calibration acceptance",
    )
    expected_acceptance = {
        "complete": True,
        "finalized": True,
        "planned_full_quality_gated_corpus": True,
        "predecessor_verified": True,
        "expected_record_count": 35,
        "record_count": 35,
        "structural_timing_geometry_hash_provenance_passed": True,
        "eye_order_passed": True,
        "size_cap_passed": True,
        "retained_artifacts_complete": True,
        "derived_limits_complete": True,
        "calibration_receipt_valid": True,
        "calibration_only": True,
        "public_contract_changes_forbidden": True,
        "later_confirmation_required": True,
        "passed": True,
    }
    if acceptance != expected_acceptance:
        raise QualificationFailure("The v2 source calibration acceptance record is inconsistent.")

    method = _mapping(receipt.get("method"), "v2 source calibration method")
    if method.get("stage") != "repeatability_limit_calibration_only":
        raise QualificationFailure("The v2 source receipt is not a calibration-only stage.")
    scope = _mapping(method.get("scope"), "v2 source calibration method scope")
    expected_scope = {
        "calibration_only": True,
        "selection_forbidden": True,
        "boundary_evaluation_forbidden": True,
        "provisional_outputs_forbidden": True,
        "public_contract_changes_forbidden": True,
        "later_confirmation_required": True,
    }
    if scope != expected_scope:
        raise QualificationFailure("The v2 source receipt calibration-only scope changed.")
    derivation = _mapping(method.get("derivation"), "v2 source calibration method derivation")
    expected_method_derivation = {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
        "multiplier": 2,
        "predecessor_receipt_records_forbidden": True,
        "summary_fields_as_source_forbidden": True,
    }
    if derivation != expected_method_derivation:
        raise QualificationFailure("The v2 source receipt derivation contract changed.")

    predecessor = _exact_keys(
        receipt.get("predecessor"),
        ("receipt", "plan", "accepted_complete_receipt_verified", "records_used_for_calibration"),
        "v2 source calibration predecessor",
    )
    predecessor_receipt = _exact_keys(
        predecessor.get("receipt"),
        ("schema_version", "experiment_id", "sha256", "source_git_sha", "file_mode", "provided_via"),
        "v2 source calibration predecessor receipt",
    )
    predecessor_plan = _exact_keys(
        predecessor.get("plan"),
        ("path", "sha256", "schema_version"),
        "v2 source calibration predecessor plan",
    )
    expected_predecessor_receipt = {
        "schema_version": 3,
        "experiment_id": V2_PREDECESSOR_EXPERIMENT_ID,
        "sha256": V2_PREDECESSOR_RECEIPT_SHA256,
        "source_git_sha": V2_PREDECESSOR_SOURCE_GIT_SHA,
        "file_mode": "0444",
        "provided_via": "--mapping-selection-receipt",
    }
    expected_predecessor_plan = {
        "path": "docs/qualification/file-upscale-quality-mapping-selection-v1.json",
        "sha256": V2_PREDECESSOR_PLAN_SHA256,
        "schema_version": 1,
    }
    if (
        predecessor_receipt != expected_predecessor_receipt
        or predecessor_plan != expected_predecessor_plan
        or predecessor.get("accepted_complete_receipt_verified") is not True
        or predecessor.get("records_used_for_calibration") is not False
    ):
        raise QualificationFailure("The v2 source calibration predecessor isolation is inconsistent.")

    later_confirmation = _exact_keys(
        receipt.get("later_confirmation"),
        ("required_before_public_contract_changes", "status"),
        "v2 source calibration later_confirmation",
    )
    if later_confirmation != {"required_before_public_contract_changes": True, "status": "not_performed"}:
        raise QualificationFailure("The v2 source calibration later confirmation was already performed or changed.")

    recomputed = recompute_calibration_noise_maxima(receipt)
    calibration = _exact_keys(
        receipt.get("repeatability_calibration"),
        (
            "source_records",
            "group_by",
            "within_case_statistic",
            "source_maximum_statistic",
            "formula",
            "multiplier",
            "raw_record_count",
            "case_repeat_ranges",
            "metrics",
        ),
        "v2 source repeatability_calibration",
    )
    expected_calibration_metadata = {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
        "multiplier": 2,
        "raw_record_count": 35,
    }
    for key, expected in expected_calibration_metadata.items():
        if calibration.get(key) != expected:
            raise QualificationFailure(f"The v2 source calibration field {key} is inconsistent.")
    if calibration.get("case_repeat_ranges") != recomputed["case_repeat_ranges"]:
        raise QualificationFailure("The v2 source calibration case-repeat ranges do not match the raw q075 records.")
    calibration_metrics = _mapping(calibration.get("metrics"), "v2 source calibration metrics")
    recomputed_metrics = _mapping(recomputed.get("metrics"), "recomputed v2 source calibration metrics")
    if set(calibration_metrics) != set(REPEATABILITY_FIELDS):
        raise QualificationFailure("The v2 source calibration metric set changed.")
    verification: dict[str, object] = {}
    limit_by_field = {limit.record_field: limit for limit in plan.noise_limits}
    for field in REPEATABILITY_FIELDS:
        limit = limit_by_field[field]
        metric = _exact_keys(
            calibration_metrics.get(field),
            (
                "record_field",
                "source",
                "previous_limit",
                "observed_maximum",
                "multiplier",
                "quantum",
                "derived_limit",
            ),
            f"v2 source calibration metrics.{field}",
        )
        observed = _mapping(recomputed_metrics.get(field), f"recomputed v2 source calibration {field}")
        observed_maximum = _number(observed.get("source_maximum"), f"recomputed {field} source maximum")
        source_group = dict(_mapping(observed.get("source_group"), f"recomputed {field} source group"))
        if not math.isclose(observed_maximum, limit.source_maximum, rel_tol=0.0, abs_tol=1e-15):
            raise QualificationFailure(f"The raw v2 calibration maximum for {field} changed from the preregistration.")
        if source_group != {"case_id": V2_SOURCE_CASES[field], "candidate_id": "q075"}:
            raise QualificationFailure(f"The raw v2 calibration source case for {field} changed.")
        if (
            metric.get("record_field") != field
            or metric.get("source") != source_group
            or not math.isclose(
                _number(metric.get("observed_maximum"), f"v2 source calibration {field}.observed_maximum"),
                observed_maximum,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
            or metric.get("previous_limit") != V2_PREVIOUS_LIMITS[field]
            or metric.get("multiplier") != 2
            or metric.get("quantum") != limit.quantum
            or metric.get("derived_limit") != limit.limit
            or _derived_calibrated_limit(V2_PREVIOUS_LIMITS[field], observed_maximum, limit.quantum) != limit.limit
        ):
            raise QualificationFailure(f"The v2 source calibration metric {field} is inconsistent.")
        verification[field] = {
            "record_field": field,
            "observed_source_maximum": observed_maximum,
            "source_group": source_group,
            "group_count": observed.get("group_count"),
            "previous_limit": V2_PREVIOUS_LIMITS[field],
            "multiplier": 2,
            "quantum": limit.quantum,
            "limit": limit.limit,
            "verified": True,
        }
    return {
        "receipt": {
            "schema_version": plan.source_receipt.schema_version,
            "experiment_id": plan.source_receipt.experiment_id,
            "sha256": plan.source_receipt.sha256,
            "source_git_sha": plan.source_receipt.source_git_sha,
            "file_mode": "0444",
        },
        "plan": {
            "path": expected_plan_record["path"],
            "sha256": plan.source_plan.sha256,
            "schema_version": plan.source_plan.schema_version,
        },
        "noise_derivation": {
            "source_records": "raw_q075_case_repeat_candidate_records_only",
            "group_by": ["case_id", "candidate_id"],
            "within_group_statistic": "maximum_minus_minimum_across_five_repeats",
            "source_maximum_statistic": "maximum_across_cases",
            "forbidden_source_field": "predecessor_receipt.cases[*].repeats[*].candidates[*]",
            "formula": "max(previous_limit, ceil(multiplier * source_maximum / quantum) * quantum)",
            "metrics": verification,
        },
        "calibration_scope": dict(scope),
        "predecessor_isolation": {
            "accepted_complete_receipt_verified": True,
            "records_used_for_calibration": False,
        },
        "later_confirmation": dict(later_confirmation),
    }


def verify_source_response(
    plan: MappingSelectionPlan,
    source_receipt_path: Path,
) -> dict[str, object]:
    identity = (plan.experiment_id, plan.purpose)
    if identity == (V1_EXPERIMENT_ID, V1_PURPOSE):
        return _verify_source_response_v1(plan, source_receipt_path)
    if identity == (V2_EXPERIMENT_ID, V2_PURPOSE):
        return _verify_source_response_v2(plan, source_receipt_path)
    raise QualificationFailure("The mapping-selection source verifier contract is unsupported.")


def _prepare_owned_directory(
    directory: Path,
    marker_name: str,
    marker_identity: Mapping[str, object],
    label: str,
) -> Path:
    resolved = directory.resolve()
    if directory.is_symlink():
        raise QualificationFailure(f"{label} must not be a symlink.")
    dangerous = {Path("/").resolve(), Path.home().resolve(), REPOSITORY_ROOT}
    if resolved in dangerous:
        raise QualificationFailure(f"{label} must be a dedicated non-root directory.")
    marker = resolved / marker_name
    expected_marker = {"schema_version": 1, **marker_identity}
    if resolved.exists():
        if not resolved.is_dir():
            raise QualificationFailure(f"{label} must be a directory.")
        if marker.is_file():
            try:
                observed = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise QualificationFailure(f"Could not validate {label} ownership.") from error
            if observed != expected_marker:
                raise QualificationFailure(f"{label} belongs to a different experiment.")
        elif any(resolved.iterdir()):
            raise QualificationFailure(f"{label} is non-empty and has no ownership marker.")
        else:
            _atomic_write(marker, expected_marker, ())
    else:
        resolved.mkdir(parents=True)
        _atomic_write(marker, expected_marker, ())
    return resolved


def _owned_case_directory(work_directory: Path, case_id: str) -> Path:
    relative = Path(case_id)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise QualificationFailure(f"Unsafe mapping-selection case work path: {case_id}")
    path = work_directory / relative
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise QualificationFailure(f"Unsafe mapping-selection case work directory: {case_id}")
    if not (work_directory / WORK_DIRECTORY_MARKER).is_file():
        raise QualificationFailure("The mapping-selection work-directory ownership marker is missing.")
    return path


def _reset_case_directory(work_directory: Path, case_id: str) -> Path:
    path = _owned_case_directory(work_directory, case_id)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _artifact_path(artifact_directory: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise QualificationFailure("Retained artifact path is unsafe.")
    lexical_path = artifact_directory
    for part in relative.parts:
        lexical_path /= part
        if lexical_path.is_symlink():
            raise QualificationFailure("Retained artifact path must not use symlinks.")
    destination = lexical_path.resolve()
    try:
        destination.relative_to(artifact_directory.resolve())
    except ValueError as error:
        raise QualificationFailure("Retained artifact path escapes its owned directory.") from error
    return destination


def _retained_artifact_entry(
    *,
    artifact_directory: Path,
    source_path: Path,
    case_id: str,
    repeat_index: int,
    kind: str,
    candidate_id: str | None,
    move: bool,
) -> dict[str, object]:
    file_name = "generated-base.mov" if candidate_id is None else f"{candidate_id}-upscaled.mov"
    relative_path = f"{case_id}/repeat-{repeat_index + 1}/{file_name}"
    destination = _artifact_path(artifact_directory, relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not destination.is_file():
            raise QualificationFailure("Retained artifact destination is not a regular file.")
        destination.unlink()
    if move:
        shutil.move(source_path, destination)
    else:
        shutil.copyfile(source_path, destination)
    digest = sha256_file(destination)
    return {
        "artifact_id": f"{case_id}-r{repeat_index + 1}-{'base' if candidate_id is None else candidate_id}",
        "case_id": case_id,
        "repeat_index": repeat_index,
        "kind": kind,
        "candidate_id": candidate_id,
        "path": relative_path,
        "bytes": destination.stat().st_size,
        "sha256": digest,
    }


def _upsert_retained_artifact(evidence: dict[str, object], entry: Mapping[str, object]) -> None:
    artifacts = evidence.get("retained_artifacts")
    if not isinstance(artifacts, list):
        raise QualificationFailure("Retained artifact manifest is invalid.")
    artifact_id = entry.get("artifact_id")
    artifacts[:] = [
        artifact
        for artifact in artifacts
        if not isinstance(artifact, Mapping) or artifact.get("artifact_id") != artifact_id
    ]
    artifacts.append(dict(entry))
    artifacts.sort(key=lambda artifact: str(_mapping(artifact, "retained artifact").get("artifact_id")))


def _validate_retained_artifacts(
    evidence: Mapping[str, object],
    plan: MappingSelectionPlan,
    artifact_directory: Path,
) -> None:
    artifacts = _array(evidence.get("retained_artifacts"), "retained_artifacts")
    by_id: dict[str, Mapping[str, object]] = {}
    recorded_identities: dict[str, tuple[int, str]] = {}
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        raise QualificationFailure("Evidence cases are invalid while checking retained artifacts.")
    expected: set[str] = set()
    for raw_case in cases:
        if not isinstance(raw_case, Mapping) or raw_case.get("id") not in plan.retained_case_ids:
            continue
        case_id = str(raw_case["id"])
        repeat = _repeat_record(raw_case, plan.retained_repeat_index)
        if repeat is None:
            continue
        base = repeat.get("base")
        if isinstance(base, Mapping):
            artifact_id = f"{case_id}-r{plan.retained_repeat_index + 1}-base"
            expected.add(artifact_id)
            recorded_identities[artifact_id] = (
                _integer(base.get("bytes"), "retained base bytes", minimum=1, maximum=10**15),
                _sha256_identity(base.get("sha256"), "retained base sha256"),
            )
        candidates = repeat.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, Mapping) or not isinstance(candidate.get("id"), str):
                    continue
                artifact_id = f"{case_id}-r{plan.retained_repeat_index + 1}-{candidate['id']}"
                expected.add(artifact_id)
                recorded_identities[artifact_id] = (
                    _integer(candidate.get("final_bytes"), "retained candidate bytes", minimum=1, maximum=10**15),
                    _sha256_identity(candidate.get("final_sha256"), "retained candidate sha256"),
                )
    for raw_artifact in artifacts:
        artifact = _exact_keys(
            raw_artifact,
            ("artifact_id", "case_id", "repeat_index", "kind", "candidate_id", "path", "bytes", "sha256"),
            "retained artifact",
        )
        artifact_id = _string(artifact.get("artifact_id"), "retained artifact.artifact_id")
        if artifact_id in by_id:
            raise QualificationFailure("Retained artifact IDs must be unique.")
        case_id = _string(artifact.get("case_id"), "retained artifact.case_id")
        repeat_index = _integer(artifact.get("repeat_index"), "retained artifact.repeat_index", minimum=0, maximum=2)
        if case_id not in plan.retained_case_ids or repeat_index != plan.retained_repeat_index:
            raise QualificationFailure("Retained artifact is outside the preregistered case/repeat set.")
        path = _artifact_path(artifact_directory, _string(artifact.get("path"), "retained artifact.path"))
        if not path.is_file():
            raise QualificationFailure("A retained artifact is missing.")
        expected_bytes = _integer(artifact.get("bytes"), "retained artifact.bytes", minimum=1, maximum=10**15)
        expected_sha256 = _sha256_identity(artifact.get("sha256"), "retained artifact.sha256")
        if recorded_identities.get(artifact_id) != (expected_bytes, expected_sha256):
            raise QualificationFailure("A retained artifact identity contradicts its recorded output.")
        if path.stat().st_size != expected_bytes or sha256_file(path) != expected_sha256:
            raise QualificationFailure("A retained artifact changed after it was recorded.")
        by_id[artifact_id] = artifact
    if set(by_id) != expected:
        raise QualificationFailure("The retained artifact manifest does not match the recorded preregistered outputs.")
    expected_paths = {_string(artifact.get("path"), "retained artifact.path") for artifact in by_id.values()}
    observed_paths = {
        path.relative_to(artifact_directory).as_posix() for path in artifact_directory.rglob("*.mov") if path.is_file()
    }
    if observed_paths != expected_paths:
        raise QualificationFailure("The retained artifact directory contains unrecorded or missing media.")


def _discard_unrecorded_expected_artifacts(
    evidence: Mapping[str, object],
    plan: MappingSelectionPlan,
    artifact_directory: Path,
) -> None:
    artifact_root = artifact_directory.resolve()
    artifacts = _array(evidence.get("retained_artifacts"), "retained_artifacts")
    recorded_paths = {
        _string(_mapping(artifact, "retained artifact").get("path"), "retained artifact.path") for artifact in artifacts
    }
    for case_id in plan.retained_case_ids:
        case = _case_record(evidence, case_id)
        repeat = _repeat_record(case, plan.retained_repeat_index) if case is not None else None
        expected: list[tuple[str, bool]] = [
            (
                f"{case_id}/repeat-{plan.retained_repeat_index + 1}/generated-base.mov",
                repeat is not None and isinstance(repeat.get("base"), Mapping),
            )
        ]
        expected.extend(
            (
                f"{case_id}/repeat-{plan.retained_repeat_index + 1}/{candidate.candidate_id}-upscaled.mov",
                repeat is not None and _candidate_record(repeat, candidate.candidate_id) is not None,
            )
            for candidate in plan.candidates
        )
        for relative_path, raw_recorded in expected:
            if raw_recorded or relative_path in recorded_paths:
                continue
            relative = Path(relative_path)
            lexical_path = artifact_directory
            for part in relative.parts:
                lexical_path /= part
                if lexical_path.is_symlink():
                    raise QualificationFailure("An unrecorded retained-artifact crash path must not use symlinks.")
            path = artifact_root.joinpath(*relative.parts)
            if not path.exists():
                continue
            if not path.is_file():
                raise QualificationFailure("An unrecorded retained-artifact crash remnant is not a regular file.")
            path.unlink()
            parent = path.parent
            while parent != artifact_root and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent


def _validate_clean_work_directory(work_directory: Path) -> None:
    marker = work_directory / WORK_DIRECTORY_MARKER
    unexpected = [path for path in work_directory.rglob("*") if path != marker]
    if unexpected:
        raise QualificationFailure("The completed mapping-selection work directory contains orphaned artifacts.")


def _cleanup_completed_work_directory(work_directory: Path, case_ids: Sequence[str]) -> None:
    for case_id in case_ids:
        case_work = _owned_case_directory(work_directory, case_id)
        if case_work.exists():
            shutil.rmtree(case_work)
    _validate_clean_work_directory(work_directory)


def _schedule_for_case(plan: MappingSelectionPlan, case_id: str) -> CaseSchedule:
    try:
        return next(schedule for schedule in plan.case_schedules if schedule.case_id == case_id)
    except StopIteration as error:
        raise QualificationFailure(f"No checked execution schedule exists for case {case_id}.") from error


def _candidate_for_quality(plan: MappingSelectionPlan, quality: int) -> UpscaleCandidate:
    try:
        return next(candidate for candidate in plan.candidates if candidate.quality == quality)
    except StopIteration as error:
        raise QualificationFailure(f"No checked mapping candidate exists for quality {quality}.") from error


def _candidate_order(
    plan: MappingSelectionPlan,
    case_id: str,
    repeat_index: int,
) -> tuple[UpscaleCandidate, ...]:
    if not 0 <= repeat_index < plan.runs_per_candidate:
        raise QualificationFailure("Mapping-selection repeat index is outside the checked schedule.")
    return tuple(
        _candidate_for_quality(plan, quality) for quality in _schedule_for_case(plan, case_id).orders[repeat_index]
    )


def _candidate_record(repeat: Mapping[str, object], candidate_id: str) -> dict[str, object] | None:
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == candidate_id:
            return candidate
    return None


def _repeat_record(case: Mapping[str, object], repeat_index: int) -> dict[str, object] | None:
    repeats = case.get("repeats")
    if not isinstance(repeats, list):
        return None
    for repeat in repeats:
        if isinstance(repeat, dict) and repeat.get("repeat_index") == repeat_index:
            return repeat
    return None


def _case_record(evidence: Mapping[str, object], case_id: str) -> dict[str, object] | None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return None
    for case in cases:
        if isinstance(case, dict) and case.get("id") == case_id:
            return case
    return None


def _validate_candidate_prefix(
    repeat: Mapping[str, object],
    plan: MappingSelectionPlan,
    case_id: str,
    repeat_index: int,
) -> None:
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        raise QualificationFailure(f"Case {case_id} repeat {repeat_index} candidates must be an array.")
    observed_ids: list[str] = []
    for record in candidates:
        if not isinstance(record, Mapping) or not isinstance(record.get("id"), str):
            raise QualificationFailure(f"Case {case_id} repeat {repeat_index} contains an invalid candidate.")
        observed_ids.append(str(record["id"]))
    expected_ids = [candidate.candidate_id for candidate in _candidate_order(plan, case_id, repeat_index)]
    if observed_ids != expected_ids[: len(observed_ids)]:
        raise QualificationFailure(f"Case {case_id} repeat {repeat_index} violates the materialized schedule.")


def _candidate_complete(
    repeat: Mapping[str, object],
    plan: MappingSelectionPlan,
    case_id: str,
    candidate: UpscaleCandidate,
    repeat_index: int,
) -> bool:
    record = _candidate_record(repeat, candidate.candidate_id)
    if record is None:
        return False
    base = repeat.get("base")
    if not isinstance(base, Mapping):
        raise QualificationFailure("A mapping candidate record exists without its generated base.")
    execution_ordinal = _schedule_for_case(plan, case_id).orders[repeat_index].index(candidate.quality)
    _validate_candidate_record(record, candidate, repeat_index, execution_ordinal)
    _validate_candidate_against_base(record, base, candidate)
    return True


def _repeat_complete(
    repeat: Mapping[str, object],
    plan: MappingSelectionPlan,
    case_id: str,
    repeat_index: int,
) -> bool:
    base = repeat.get("base")
    if not isinstance(base, Mapping):
        return False
    _validate_base_record(base, cast(Any, plan), repeat_index)
    candidates = repeat.get("candidates")
    if not isinstance(candidates, list):
        return False
    _validate_candidate_prefix(repeat, plan, case_id, repeat_index)
    expected_order = _candidate_order(plan, case_id, repeat_index)
    if len(candidates) != len(expected_order):
        return False
    return all(_candidate_complete(repeat, plan, case_id, candidate, repeat_index) for candidate in expected_order)


def _case_complete(case: Mapping[str, object], plan: MappingSelectionPlan) -> bool:
    case_id = case.get("id")
    if not isinstance(case_id, str):
        return False
    repeats = case.get("repeats")
    if not isinstance(repeats, list) or len(repeats) != plan.runs_per_candidate:
        return False
    return all(
        (repeat := _repeat_record(case, repeat_index)) is not None
        and _repeat_complete(repeat, plan, case_id, repeat_index)
        for repeat_index in range(plan.runs_per_candidate)
    )


def _case_record_template(
    definition: CorpusCase,
    prepared: PreparedCase,
) -> dict[str, object]:
    return {
        "id": definition.case_id,
        "tags": list(definition.tags),
        "quality_gate": definition.quality_gate,
        "source": dict(prepared.source_evidence),
        "prepared": {
            "duration_seconds": prepared.duration_seconds,
            "frame_count": prepared.frame_count,
            "eye_width": definition.output_eye_width,
            "eye_height": definition.output_eye_height,
            "frame_rate": definition.output_frame_rate,
            "source_sha256": sha256_file(prepared.source_path),
        },
        "repeats": [],
    }


def _repeat_record_template(
    plan: MappingSelectionPlan,
    case_id: str,
    repeat_index: int,
) -> dict[str, object]:
    return {
        "repeat_index": repeat_index,
        "order": list(_schedule_for_case(plan, case_id).orders[repeat_index]),
        "base": None,
        "candidates": [],
    }


def _cases_selected_from_binding(binding: CorpusBinding) -> tuple[CorpusCase, ...]:
    manifest = load_manifest(binding.source_manifest_path)
    cases_by_id = {case.case_id: case for case in manifest.cases}
    return tuple(cases_by_id[case_id] for case_id in binding.selected_case_ids)


def _validate_case_record(
    case: Mapping[str, object],
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    definition: CorpusCase,
) -> None:
    case_id = definition.case_id
    if (
        case.get("id") != case_id
        or case.get("tags") != list(definition.tags)
        or case.get("quality_gate") is not definition.quality_gate
        or case.get("source") != dict(binding.expected_case_sources[case_id])
    ):
        raise QualificationFailure(f"Resume evidence case {case_id} metadata changed.")
    prepared = _mapping(case.get("prepared"), f"resume evidence case {case_id} prepared")
    frame_count = prepared.get("frame_count")
    if (
        prepared.get("eye_width") != definition.output_eye_width
        or prepared.get("eye_height") != definition.output_eye_height
        or prepared.get("frame_rate") != definition.output_frame_rate
        or type(frame_count) is not int
        or frame_count <= 0
    ):
        raise QualificationFailure(f"Resume evidence case {case_id} prepared metadata changed.")
    _number(prepared.get("duration_seconds"), f"resume evidence case {case_id} duration", positive=True)
    _sha256_identity(prepared.get("source_sha256"), f"resume evidence case {case_id} source_sha256")
    repeats = _array(case.get("repeats"), f"resume evidence case {case_id} repeats")
    if len(repeats) > plan.runs_per_candidate:
        raise QualificationFailure(f"Resume evidence case {case_id} has too many repeats.")
    observed_repeat_indices: list[int] = []
    for raw_repeat in repeats:
        repeat = _mapping(raw_repeat, f"resume evidence case {case_id} repeat")
        repeat_index = _integer(
            repeat.get("repeat_index"),
            f"resume evidence case {case_id} repeat_index",
            minimum=0,
            maximum=plan.runs_per_candidate - 1,
        )
        observed_repeat_indices.append(repeat_index)
        if repeat.get("order") != list(_schedule_for_case(plan, case_id).orders[repeat_index]):
            raise QualificationFailure(f"Resume evidence case {case_id} repeat order changed.")
        base = repeat.get("base")
        if base is not None:
            base_record = _mapping(base, "resume repeat base")
            _validate_base_record(base_record, cast(Any, plan), repeat_index)
            _validate_base_against_case(base_record, definition, prepared)
        candidates = _array(repeat.get("candidates"), "resume repeat candidates")
        if len(candidates) > len(plan.candidates):
            raise QualificationFailure(f"Resume evidence case {case_id} has too many candidates.")
        _validate_candidate_prefix(repeat, plan, case_id, repeat_index)
        if candidates and not isinstance(base, Mapping):
            raise QualificationFailure(f"Resume evidence case {case_id} has candidates without a base.")
        for execution_ordinal, candidate_record in enumerate(candidates):
            candidate = _candidate_order(plan, case_id, repeat_index)[execution_ordinal]
            candidate_mapping = _mapping(candidate_record, "resume candidate")
            _validate_candidate_record(candidate_mapping, candidate, repeat_index, execution_ordinal)
            _validate_candidate_against_base(candidate_mapping, _mapping(base, "resume base"), candidate)
    if observed_repeat_indices != list(range(len(observed_repeat_indices))):
        raise QualificationFailure(f"Resume evidence case {case_id} repeats violate the execution prefix.")


def _case_candidate_records(case: Mapping[str, object], candidate_id: str) -> list[Mapping[str, object]]:
    records: list[Mapping[str, object]] = []
    repeats = case.get("repeats")
    if not isinstance(repeats, list):
        return records
    for repeat_index in range(len(repeats)):
        repeat = _repeat_record(case, repeat_index)
        if repeat is None:
            continue
        record = _candidate_record(repeat, candidate_id)
        if record is not None:
            records.append(record)
    return records


def _numeric_range(records: Sequence[Mapping[str, object]], field: str) -> float:
    values = [_number(record.get(field), f"candidate.{field}") for record in records]
    return max(values) - min(values)


def _median_field(records: Sequence[Mapping[str, object]], field: str) -> float:
    return float(statistics.median(_number(record.get(field), f"candidate.{field}") for record in records))


def _update_paired_deltas(evidence: Mapping[str, object], plan: MappingSelectionPlan) -> None:
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        return
    balanced_id = f"q{plan.balanced_quality:03d}"
    for raw_case in cases:
        if not isinstance(raw_case, Mapping):
            continue
        repeats = raw_case.get("repeats")
        if not isinstance(repeats, list):
            continue
        for raw_repeat in repeats:
            if not isinstance(raw_repeat, Mapping):
                continue
            balanced = _candidate_record(raw_repeat, balanced_id)
            candidates = raw_repeat.get("candidates")
            if balanced is None or not isinstance(candidates, list):
                continue
            for raw_candidate in candidates:
                if isinstance(raw_candidate, dict):
                    raw_candidate["paired_delta_to_q075"] = _paired_delta(raw_candidate, balanced)


def _case_candidate_summary(
    records: Sequence[Mapping[str, object]],
    plan: MappingSelectionPlan,
    definition: CorpusCase,
    candidate: UpscaleCandidate,
) -> dict[str, object]:
    complete = len(records) == plan.runs_per_candidate
    summary: dict[str, object] = {
        "id": candidate.candidate_id,
        "quality": candidate.quality,
        "run_count": len(records),
        "complete": complete,
    }
    if not records:
        return summary
    for field in SUMMARY_FIELDS:
        summary[f"median_{field}"] = _median_field(records, field)
    summary["repeat_ranges"] = {field: _numeric_range(records, field) for field in REPEATABILITY_FIELDS}
    summary["size_cap_passed"] = all(
        _number(record.get("final_to_base_size_ratio"), "candidate.final_to_base_size_ratio")
        <= plan.maximum_final_to_base_size_ratio
        for record in records
    )
    summary["eye_order_passed"] = all(
        _number(record.get("min_eye_order_margin"), "candidate.min_eye_order_margin")
        >= definition.minimum_eye_order_margin
        for record in records
    )
    return summary


def _candidate_summary(
    evidence: Mapping[str, object],
    plan: MappingSelectionPlan,
    definitions: Mapping[str, CorpusCase],
    candidate: UpscaleCandidate,
) -> dict[str, object]:
    cases = _array(evidence.get("cases"), "evidence cases")
    expected_record_count = len(definitions) * plan.runs_per_candidate
    all_records: list[Mapping[str, object]] = []
    within_case_ranges: list[dict[str, object]] = []
    case_medians: list[dict[str, object]] = []
    all_case_groups_complete = True
    size_cap_passed = True
    eye_order_passed = True
    for case_id in EXPECTED_CASE_IDS:
        raw_case = next(
            (case for case in cases if isinstance(case, Mapping) and case.get("id") == case_id),
            None,
        )
        records = _case_candidate_records(
            _mapping(raw_case, f"case {case_id}") if raw_case else {}, candidate.candidate_id
        )
        all_records.extend(records)
        complete = len(records) == plan.runs_per_candidate
        all_case_groups_complete = all_case_groups_complete and complete
        ranges = {field: _numeric_range(records, field) for field in REPEATABILITY_FIELDS} if records else {}
        within_case_ranges.append(
            {
                "case_id": case_id,
                "repeat_count": len(records),
                "complete": complete,
                "ranges": ranges,
            }
        )
        if records:
            case_medians.append(
                {
                    "case_id": case_id,
                    "medians": {field: _median_field(records, field) for field in SUMMARY_FIELDS},
                }
            )
            size_cap_passed = size_cap_passed and all(
                _number(record.get("final_to_base_size_ratio"), "candidate.final_to_base_size_ratio")
                <= plan.maximum_final_to_base_size_ratio
                for record in records
            )
            definition = definitions[case_id]
            eye_order_passed = eye_order_passed and all(
                _number(record.get("min_eye_order_margin"), "candidate.min_eye_order_margin")
                >= definition.minimum_eye_order_margin
                for record in records
            )
    complete = len(all_records) == expected_record_count and all_case_groups_complete
    maximum_ranges = {
        field: max(
            (
                _number(
                    _mapping(item.get("ranges"), "within-case ranges").get(field, 0.0),
                    f"within-case {field} range",
                )
                for item in within_case_ranges
            ),
            default=0.0,
        )
        for field in REPEATABILITY_FIELDS
    }
    cross_case_median_ranges = {
        field: (
            max(
                _number(_mapping(item["medians"], "case medians")[field], f"case median {field}")
                for item in case_medians
            )
            - min(
                _number(_mapping(item["medians"], "case medians")[field], f"case median {field}")
                for item in case_medians
            )
            if case_medians
            else 0.0
        )
        for field in SUMMARY_FIELDS
    }
    limit_by_field = {limit.record_field: limit.limit for limit in plan.noise_limits}
    repeatability_passed = complete and all(
        maximum_ranges[field] <= limit_by_field[field] for field in REPEATABILITY_FIELDS
    )
    failures: list[str] = []
    if not complete:
        failures.append("incomplete_records")
    if not repeatability_passed:
        failures.append("repeatability")
    if not size_cap_passed:
        failures.append("size_cap")
    if not eye_order_passed:
        failures.append("eye_order")
    technically_eligible = complete and repeatability_passed and size_cap_passed and eye_order_passed
    return {
        "id": candidate.candidate_id,
        "quality": candidate.quality,
        "quality_factor": f"{candidate.quality}/100",
        "bitrate_scaling_factor": candidate.bitrate_scaling_factor,
        "run_count": len(all_records),
        "expected_run_count": expected_record_count,
        "complete": complete,
        "within_case_repeat_ranges": within_case_ranges,
        "maximum_within_case_repeat_ranges": maximum_ranges,
        "maximum_within_case_repeat_min_same_eye_ssim_range": maximum_ranges["min_same_eye_ssim"],
        "cross_case_median_ranges": cross_case_median_ranges,
        "cross_case_min_same_eye_ssim_range": cross_case_median_ranges["min_same_eye_ssim"],
        "repeatability_passed": repeatability_passed,
        "size_cap_passed": size_cap_passed,
        "eye_order_passed": eye_order_passed,
        "structure_timing_geometry_hash_provenance_passed": complete,
        "technically_eligible": technically_eligible,
        "eligibility_failures": failures,
    }


def _paired_case_evaluation(
    plan: MappingSelectionPlan,
    case: Mapping[str, object],
    lower_id: str,
    higher_id: str,
) -> dict[str, object]:
    case_id = _string(case.get("id"), "boundary case.id")
    deltas_by_field: dict[str, list[float]] = {
        field: []
        for field in (
            "min_same_eye_ssim",
            "minimum_frame_same_eye_ssim",
            "p05_frame_same_eye_ssim",
            "frame_ssim_standard_deviation",
            "maximum_adjacent_frame_ssim_drop",
            "min_eye_order_margin",
        )
    }
    storage_growth: list[float] = []
    lower_bytes_total = 0
    higher_bytes_total = 0
    for repeat_index in range(plan.runs_per_candidate):
        repeat = _repeat_record(case, repeat_index)
        if repeat is None:
            raise QualificationFailure(f"Boundary case {case_id} is missing repeat {repeat_index}.")
        lower = _candidate_record(repeat, lower_id)
        higher = _candidate_record(repeat, higher_id)
        if lower is None or higher is None:
            raise QualificationFailure(f"Boundary case {case_id} is missing a paired candidate record.")
        lower_bytes = _integer(lower.get("final_bytes"), "lower final_bytes", minimum=1, maximum=10**15)
        higher_bytes = _integer(higher.get("final_bytes"), "higher final_bytes", minimum=1, maximum=10**15)
        lower_bytes_total += lower_bytes
        higher_bytes_total += higher_bytes
        storage_growth.append(higher_bytes / lower_bytes - 1.0)
        for field in deltas_by_field:
            deltas_by_field[field].append(
                _number(higher.get(field), f"higher {field}") - _number(lower.get(field), f"lower {field}")
            )
    medians = {field: float(statistics.median(values)) for field, values in deltas_by_field.items()}
    median_storage_growth = float(statistics.median(storage_growth))
    strict_storage_increase = all(value > 0 for value in storage_growth)
    storage_distinct = strict_storage_increase and median_storage_growth >= plan.minimum_case_median_storage_growth
    non_inferiority_checks = {
        "aggregate": medians["min_same_eye_ssim"] >= plan.minimum_aggregate_delta,
        "minimum_frame": medians["minimum_frame_same_eye_ssim"] >= plan.minimum_minimum_frame_delta,
        "p05": medians["p05_frame_same_eye_ssim"] >= plan.minimum_p05_delta,
        "frame_standard_deviation": (
            medians["frame_ssim_standard_deviation"] <= plan.maximum_frame_standard_deviation_increase
        ),
        "adjacent_drop": medians["maximum_adjacent_frame_ssim_drop"] <= plan.maximum_adjacent_drop_increase,
        "eye_order_margin": medians["min_eye_order_margin"] >= -plan.maximum_eye_order_margin_loss,
    }
    objective_clearance_margin = max(
        medians["min_same_eye_ssim"] - plan.real_case_aggregate_threshold,
        medians["minimum_frame_same_eye_ssim"] - plan.real_case_minimum_frame_threshold,
        medians["p05_frame_same_eye_ssim"] - plan.real_case_p05_threshold,
    )
    real_case = case_id != "synthetic-animation"
    objective_threshold_cleared = real_case and objective_clearance_margin >= 0
    return {
        "case_id": case_id,
        "paired_repeat_count": plan.runs_per_candidate,
        "storage_growth_ratios": storage_growth,
        "minimum_paired_storage_growth_ratio": min(storage_growth),
        "median_paired_storage_growth_ratio": median_storage_growth,
        "aggregate_storage_growth_ratio": higher_bytes_total / lower_bytes_total - 1.0,
        "strict_storage_increase": strict_storage_increase,
        "storage_distinct": storage_distinct,
        "median_paired_deltas": medians,
        "quality_non_inferiority_checks": non_inferiority_checks,
        "quality_non_inferiority_passed": all(non_inferiority_checks.values()),
        "real_case": real_case,
        "objective_clearance_margin": objective_clearance_margin,
        "objective_threshold_cleared": objective_threshold_cleared,
        "sensitive_case": case_id in plan.required_sensitive_case_ids,
    }


def evaluate_boundaries(
    plan: MappingSelectionPlan,
    evidence: Mapping[str, object],
    eligible_candidate_ids: Sequence[str],
) -> list[dict[str, object]]:
    cases = [_mapping(case, "boundary case") for case in _array(evidence.get("cases"), "boundary cases")]
    candidate_by_id = {candidate.candidate_id: candidate for candidate in plan.candidates}
    boundaries: list[dict[str, object]] = []
    for lower_id, higher_id in combinations(eligible_candidate_ids, 2):
        lower = candidate_by_id[lower_id]
        higher = candidate_by_id[higher_id]
        if lower.quality >= higher.quality:
            raise QualificationFailure("Eligible mapping candidates are not strictly quality-ordered.")
        case_evaluations = [_paired_case_evaluation(plan, case, lower_id, higher_id) for case in cases]
        corpus_median_aggregate_improvement = float(
            statistics.median(
                _number(
                    _mapping(item["median_paired_deltas"], "boundary medians")["min_same_eye_ssim"],
                    "boundary aggregate improvement",
                )
                for item in case_evaluations
            )
        )
        real_case_margins = sorted(
            (
                _number(item["objective_clearance_margin"], "objective clearance margin")
                for item in case_evaluations
                if item["real_case"] is True
            ),
            reverse=True,
        )
        sensitive_margins = [
            _number(item["objective_clearance_margin"], "sensitive objective clearance margin")
            for item in case_evaluations
            if item["sensitive_case"] is True
        ]
        real_case_clear_count = sum(item["objective_threshold_cleared"] is True for item in case_evaluations)
        sensitive_case_clear_count = sum(
            item["objective_threshold_cleared"] is True and item["sensitive_case"] is True for item in case_evaluations
        )
        objective_quality_margin = min(
            corpus_median_aggregate_improvement - plan.minimum_corpus_median_aggregate_improvement,
            real_case_margins[plan.real_case_clear_count - 1],
            max(sensitive_margins),
        )
        objective_distinct = (
            corpus_median_aggregate_improvement >= plan.minimum_corpus_median_aggregate_improvement
            and real_case_clear_count >= plan.real_case_clear_count
            and sensitive_case_clear_count >= plan.required_sensitive_case_clear_count
        )
        storage_passed = all(item["storage_distinct"] is True for item in case_evaluations)
        quality_non_inferiority_passed = all(
            item["quality_non_inferiority_passed"] is True for item in case_evaluations
        )
        minimum_case_storage_coverage = min(
            _number(item["median_paired_storage_growth_ratio"], "median paired storage growth")
            for item in case_evaluations
        )
        minimum_repeat_storage_growth = min(
            _number(item["minimum_paired_storage_growth_ratio"], "minimum paired storage growth")
            for item in case_evaluations
        )
        storage_margin = min(
            minimum_repeat_storage_growth,
            minimum_case_storage_coverage - plan.minimum_case_median_storage_growth,
        )
        lower_total = sum(
            _integer(record.get("final_bytes"), "lower final_bytes", minimum=1, maximum=10**15)
            for case in cases
            for record in _case_candidate_records(case, lower_id)
        )
        higher_total = sum(
            _integer(record.get("final_bytes"), "higher final_bytes", minimum=1, maximum=10**15)
            for case in cases
            for record in _case_candidate_records(case, higher_id)
        )
        boundary_passed = storage_passed and quality_non_inferiority_passed and objective_distinct
        failure_reasons: list[str] = []
        if not storage_passed:
            failure_reasons.append("storage")
        if not quality_non_inferiority_passed:
            failure_reasons.append("quality_non_inferiority")
        if not objective_distinct:
            failure_reasons.append("objective_distinction")
        boundaries.append(
            {
                "lower_candidate_id": lower_id,
                "higher_candidate_id": higher_id,
                "lower_quality": lower.quality,
                "higher_quality": higher.quality,
                "case_evaluations": case_evaluations,
                "minimum_case_storage_coverage": minimum_case_storage_coverage,
                "minimum_repeat_storage_growth_ratio": minimum_repeat_storage_growth,
                "storage_margin": storage_margin,
                "end_to_end_storage_coverage": higher_total / lower_total - 1.0,
                "storage_passed": storage_passed,
                "quality_non_inferiority_passed": quality_non_inferiority_passed,
                "corpus_median_aggregate_improvement": corpus_median_aggregate_improvement,
                "real_case_clear_count": real_case_clear_count,
                "sensitive_case_clear_count": sensitive_case_clear_count,
                "objective_quality_margin": objective_quality_margin,
                "objective_distinct": objective_distinct,
                "boundary_passed": boundary_passed,
                "collapsed": not boundary_passed,
                "failure_reasons": failure_reasons,
            }
        )
    return boundaries


def select_provisional_subset(
    plan: MappingSelectionPlan,
    eligible_candidate_ids: Sequence[str],
    boundaries: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    balanced_id = f"q{plan.balanced_quality:03d}"
    if balanced_id not in eligible_candidate_ids:
        return None, []
    candidate_by_id = {candidate.candidate_id: candidate for candidate in plan.candidates}
    boundary_by_pair = {
        (str(boundary["lower_candidate_id"]), str(boundary["higher_candidate_id"])): boundary for boundary in boundaries
    }
    valid_subsets: list[dict[str, object]] = []
    for subset_size in range(1, len(eligible_candidate_ids) + 1):
        for candidate_tuple in combinations(eligible_candidate_ids, subset_size):
            if balanced_id not in candidate_tuple:
                continue
            subset_boundaries = [boundary_by_pair[pair] for pair in combinations(candidate_tuple, 2)]
            if not all(boundary.get("boundary_passed") is True for boundary in subset_boundaries):
                continue
            if len(candidate_tuple) > 1:
                coverage = boundary_by_pair[(candidate_tuple[0], candidate_tuple[-1])]
                minimum_case_storage_coverage = _number(
                    coverage["minimum_case_storage_coverage"], "minimum case storage coverage"
                )
                end_to_end_storage_coverage = _number(
                    coverage["end_to_end_storage_coverage"], "end-to-end storage coverage"
                )
                minimum_objective_quality_margin = min(
                    _number(boundary["objective_quality_margin"], "objective quality margin")
                    for boundary in subset_boundaries
                )
                minimum_storage_margin = min(
                    _number(boundary["storage_margin"], "storage margin") for boundary in subset_boundaries
                )
            else:
                minimum_case_storage_coverage = 0.0
                end_to_end_storage_coverage = 0.0
                minimum_objective_quality_margin = 0.0
                minimum_storage_margin = 0.0
            valid_subsets.append(
                {
                    "candidate_ids": list(candidate_tuple),
                    "qualities": [candidate_by_id[candidate_id].quality for candidate_id in candidate_tuple],
                    "cardinality": len(candidate_tuple),
                    "minimum_case_storage_coverage": minimum_case_storage_coverage,
                    "minimum_objective_quality_margin": minimum_objective_quality_margin,
                    "minimum_storage_margin": minimum_storage_margin,
                    "end_to_end_storage_coverage": end_to_end_storage_coverage,
                    "first_quality": candidate_by_id[candidate_tuple[0]].quality,
                }
            )

    def subset_sort_key(subset: Mapping[str, object]) -> tuple[object, ...]:
        return (
            -_integer(subset.get("cardinality"), "subset cardinality", minimum=1, maximum=7),
            -_number(subset.get("minimum_case_storage_coverage"), "subset storage coverage"),
            -_number(subset.get("minimum_objective_quality_margin"), "subset objective margin"),
            -_number(subset.get("minimum_storage_margin"), "subset storage margin"),
            -_number(subset.get("end_to_end_storage_coverage"), "subset end-to-end coverage"),
            _integer(subset.get("first_quality"), "subset first quality", minimum=0, maximum=100),
            tuple(str(candidate_id) for candidate_id in _array(subset.get("candidate_ids"), "subset IDs")),
        )

    valid_subsets.sort(key=subset_sort_key)
    if not valid_subsets:
        return None, []
    selected = dict(valid_subsets[0])
    selected["contains_balanced"] = balanced_id in _array(selected.get("candidate_ids"), "selected IDs")
    selected["selection_policy"] = {
        "primary": "maximum_cardinality_ordered_subset_containing_q075",
        "tie_breaks": list(plan.selection_tie_breaks),
    }
    return selected, valid_subsets


def assign_provisional_mappings(
    plan: MappingSelectionPlan,
    selected: Mapping[str, object] | None,
) -> list[dict[str, object]]:
    slots = (
        "space_saver",
        "compact",
        "efficient",
        "balanced",
        "detailed",
        "high_detail",
        "maximum_detail",
    )
    assignments: dict[str, str] = {}
    if selected is not None:
        candidate_ids = [str(value) for value in _array(selected.get("candidate_ids"), "selected candidate_ids")]
        lower = [candidate_id for candidate_id in candidate_ids if int(candidate_id[1:]) < plan.balanced_quality]
        higher = [candidate_id for candidate_id in candidate_ids if int(candidate_id[1:]) > plan.balanced_quality]
        assignments["balanced"] = f"q{plan.balanced_quality:03d}"
        for slot, candidate_id in zip(("efficient", "compact", "space_saver"), reversed(lower), strict=False):
            assignments[slot] = candidate_id
        for slot, candidate_id in zip(("detailed", "high_detail", "maximum_detail"), higher, strict=False):
            assignments[slot] = candidate_id
    candidate_by_id = {candidate.candidate_id: candidate for candidate in plan.candidates}
    mappings: list[dict[str, object]] = []
    for slot in slots:
        assigned_candidate_id = assignments.get(slot)
        if assigned_candidate_id is None:
            mappings.append(
                {
                    "step_id": slot,
                    "status": "unsupported",
                    "candidate_id": None,
                    "values": None,
                }
            )
        else:
            mappings.append(
                {
                    "step_id": slot,
                    "status": "provisional_objective_selection",
                    "candidate_id": assigned_candidate_id,
                    "values": {"upscale_quality": candidate_by_id[assigned_candidate_id].quality},
                }
            )
    return mappings


def _noise_limit_record(plan: MappingSelectionPlan) -> dict[str, object]:
    return {
        limit.key: {
            "record_field": limit.record_field,
            "source_maximum": limit.source_maximum,
            "multiplier": 2,
            "quantum": limit.quantum,
            "limit": limit.limit,
        }
        for limit in plan.noise_limits
    }


def _public_contract_record(plan: MappingSelectionPlan) -> dict[str, object]:
    return {
        "ladder_manifest": _binding_record(plan.ladder_manifest),
        "video_quality_swift": _binding_record(plan.video_quality_swift),
    }


def _method_record(plan: MappingSelectionPlan) -> dict[str, object]:
    return {
        "design": plan.design,
        "decision_stage": plan.decision_stage,
        "runs_per_candidate": plan.runs_per_candidate,
        "balanced_quality": plan.balanced_quality,
        "generated_base": {
            "eye_bitrate_mbps": plan.base_eye_bitrate_mbps,
            "merge_quality": plan.base_merge_quality,
            "target_total_eye_bitrate_mbps": plan.base_eye_bitrate_mbps * 2,
        },
        "pairing": "one fresh generated base per case/repeat; every candidate receives an exact verified copy",
        "execution_order": {
            "contract": plan.execution_order_contract,
            "runtime_shuffle_forbidden": True,
            "case_orders": [
                {
                    "case_id": schedule.case_id,
                    "case_index": schedule.case_index,
                    "orders": [list(order) for order in schedule.orders],
                }
                for schedule in plan.case_schedules
            ],
        },
        "repeatability_limits": _noise_limit_record(plan),
        "technical_eligibility": {
            "all_planned_records_required": True,
            "structure_timing_geometry_eye_order_hash_provenance_required": True,
            "maximum_final_to_base_size_ratio": plan.maximum_final_to_base_size_ratio,
        },
        "boundary_policy": {
            "evaluate_every_ordered_lower_higher_pair": True,
            "minimum_case_median_paired_storage_growth_ratio": plan.minimum_case_median_storage_growth,
            "quality_non_inferiority": {
                "minimum_aggregate_delta": plan.minimum_aggregate_delta,
                "minimum_minimum_frame_delta": plan.minimum_minimum_frame_delta,
                "minimum_p05_delta": plan.minimum_p05_delta,
                "maximum_frame_standard_deviation_increase": plan.maximum_frame_standard_deviation_increase,
                "maximum_adjacent_drop_increase": plan.maximum_adjacent_drop_increase,
                "maximum_eye_order_margin_loss": plan.maximum_eye_order_margin_loss,
            },
            "objective_distinction": {
                "minimum_corpus_median_aggregate_improvement": (plan.minimum_corpus_median_aggregate_improvement),
                "real_case_clear_count": plan.real_case_clear_count,
                "real_case_aggregate_threshold": plan.real_case_aggregate_threshold,
                "real_case_minimum_frame_threshold": plan.real_case_minimum_frame_threshold,
                "real_case_p05_threshold": plan.real_case_p05_threshold,
                "required_sensitive_case_ids": list(plan.required_sensitive_case_ids),
                "required_sensitive_case_clear_count": plan.required_sensitive_case_clear_count,
            },
            "failed_boundary_action": "collapse",
            "threshold_changes_forbidden": True,
            "interpolation_forbidden": True,
            "aliases_forbidden": True,
            "post_hoc_candidates_forbidden": True,
        },
        "selection_policy": {
            "primary": "maximum_cardinality_ordered_subset_containing_q075",
            "tie_breaks": list(plan.selection_tie_breaks),
            "target_named_step_count": plan.target_named_step_count,
            "missing_slots": "unsupported",
        },
        "artifact_retention": {
            "repeat_index": plan.retained_repeat_index,
            "case_ids": list(plan.retained_case_ids),
            "relative_paths_only": True,
        },
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
    }


def _new_evidence(
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    source_response: Mapping[str, object],
    environment: Mapping[str, object],
) -> dict[str, object]:
    if plan.relative_path is None or binding.relative_path is None:
        raise QualificationFailure("Mapping-selection inputs are not repository-bound.")
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "experiment_id": plan.experiment_id,
        "created_at": now,
        "updated_at": now,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "source_response": dict(source_response),
        "corpus_binding": {
            "path": binding.relative_path,
            "binding_id": binding.binding_id,
            "sha256": binding_sha256,
        },
        "manifest": {
            "path": _relative_repository_path(binding.source_manifest_path, "Bound direct corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "private_source_identity": dict(binding.private_source_identity),
        "public_contract_bindings": _public_contract_record(plan),
        "toolchain": _toolchain_record(cast(Any, plan)),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": list(binding.selected_case_ids),
        "candidates": [_candidate_plan_record(candidate) for candidate in plan.candidates],
        "cases": [],
        "case_summaries": [],
        "candidate_summaries": [],
        "boundary_evaluations": [],
        "valid_subsets": [],
        "selected_subset": None,
        "provisional_mappings": assign_provisional_mappings(plan, None),
        "retained_artifacts": [],
        "downstream_checks": {
            "perceptual_review": {"status": "not_performed", "objective_stage_blocker": False},
            "long_form_runtime": {"status": "not_performed", "objective_stage_blocker": False},
            "package_parity": {"status": "not_performed", "objective_stage_blocker": False},
            "vision_pro_validation": {"status": "not_performed", "objective_stage_blocker": False},
        },
        "acceptance": {
            "complete": False,
            "finalized": False,
            "planned_full_quality_gated_corpus": True,
            "source_response_verified": True,
            "structural_passed": False,
            "retained_artifacts_complete": False,
            "technical_eligibility_complete": False,
            "balanced_technically_eligible": False,
            "provisional_selection_available": False,
            "selected_candidate_count": 0,
            "target_candidate_count": plan.target_named_step_count,
            "all_seven_candidates_selected": False,
            "collapsed_boundaries": True,
            "objective_selection_ambiguous": True,
            "objective_decision_ready": False,
            "public_mapping_changes_forbidden": True,
            "ladder_mapping_selected": False,
            "perceptual_review_performed": False,
            "long_form_runtime_performed": False,
            "package_parity_performed": False,
            "vision_pro_validation_performed": False,
            "downstream_checks_block_objective_stage": False,
            "passed": False,
        },
    }


def _retained_manifest_complete(evidence: Mapping[str, object], plan: MappingSelectionPlan) -> bool:
    artifacts = evidence.get("retained_artifacts")
    if not isinstance(artifacts, list):
        return False
    expected_ids = {f"{case_id}-r{plan.retained_repeat_index + 1}-base" for case_id in plan.retained_case_ids}
    expected_ids.update(
        f"{case_id}-r{plan.retained_repeat_index + 1}-{candidate.candidate_id}"
        for case_id in plan.retained_case_ids
        for candidate in plan.candidates
    )
    observed_ids = {
        str(artifact.get("artifact_id"))
        for artifact in artifacts
        if isinstance(artifact, Mapping) and isinstance(artifact.get("artifact_id"), str)
    }
    return observed_ids == expected_ids


def _refresh_summaries(
    evidence: dict[str, object],
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    definitions: Mapping[str, CorpusCase],
) -> None:
    _update_paired_deltas(evidence, plan)
    cases = evidence.get("cases")
    if not isinstance(cases, list):
        raise QualificationFailure("Mapping-selection evidence cases are invalid.")
    case_summaries: list[dict[str, object]] = []
    for case_id in binding.selected_case_ids:
        case = _case_record(evidence, case_id)
        if case is None:
            continue
        definition = definitions[case_id]
        case_summaries.append(
            {
                "id": case_id,
                "complete": _case_complete(case, plan),
                "candidates": [
                    _case_candidate_summary(
                        _case_candidate_records(case, candidate.candidate_id),
                        plan,
                        definition,
                        candidate,
                    )
                    for candidate in plan.candidates
                ],
            }
        )
    evidence["case_summaries"] = case_summaries

    candidate_summaries = [_candidate_summary(evidence, plan, definitions, candidate) for candidate in plan.candidates]
    evidence["candidate_summaries"] = candidate_summaries
    complete = (
        len(cases) == len(binding.selected_case_ids)
        and [case.get("id") for case in cases if isinstance(case, Mapping)] == list(binding.selected_case_ids)
        and all(isinstance(case, Mapping) and _case_complete(case, plan) for case in cases)
    )
    eligible_candidate_ids = [
        str(summary["id"]) for summary in candidate_summaries if summary.get("technically_eligible") is True
    ]
    boundaries = evaluate_boundaries(plan, evidence, eligible_candidate_ids) if complete else []
    selected, valid_subsets = (
        select_provisional_subset(plan, eligible_candidate_ids, boundaries) if complete else (None, [])
    )
    evidence["boundary_evaluations"] = boundaries
    evidence["valid_subsets"] = valid_subsets
    evidence["selected_subset"] = selected
    evidence["provisional_mappings"] = assign_provisional_mappings(plan, selected)

    selected_count = (
        _integer(selected.get("cardinality"), "selected cardinality", minimum=1, maximum=7)
        if selected is not None
        else 0
    )
    all_seven_selected = selected_count == plan.target_named_step_count
    balanced_id = f"q{plan.balanced_quality:03d}"
    balanced_eligible = balanced_id in eligible_candidate_ids
    all_boundaries_passed = (
        complete
        and len(boundaries) == math.comb(len(plan.candidates), 2)
        and all(boundary.get("boundary_passed") is True for boundary in boundaries)
    )
    retained_artifacts_complete = complete and _retained_manifest_complete(evidence, plan)
    objective_selection_ambiguous = complete and selected is None
    collapsed_boundaries = complete and not all_boundaries_passed
    objective_decision_ready = (
        complete
        and evidence.get("selected_case_ids") == list(binding.selected_case_ids)
        and evidence.get("source_response") is not None
        and retained_artifacts_complete
        and len(eligible_candidate_ids) == len(plan.candidates)
        and balanced_eligible
        and all_seven_selected
        and all_boundaries_passed
        and not objective_selection_ambiguous
    )
    previous_acceptance = evidence.get("acceptance")
    finalized = isinstance(previous_acceptance, Mapping) and previous_acceptance.get("finalized") is True and complete
    evidence["acceptance"] = {
        "complete": complete,
        "finalized": finalized,
        "planned_full_quality_gated_corpus": evidence.get("selected_case_ids") == list(binding.selected_case_ids),
        "source_response_verified": True,
        "structural_passed": complete,
        "retained_artifacts_complete": retained_artifacts_complete,
        "technical_eligibility_complete": complete and len(eligible_candidate_ids) == len(plan.candidates),
        "balanced_technically_eligible": balanced_eligible,
        "provisional_selection_available": selected is not None,
        "selected_candidate_count": selected_count,
        "target_candidate_count": plan.target_named_step_count,
        "all_seven_candidates_selected": all_seven_selected,
        "collapsed_boundaries": collapsed_boundaries,
        "objective_selection_ambiguous": objective_selection_ambiguous,
        "objective_decision_ready": objective_decision_ready,
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
        "perceptual_review_performed": False,
        "long_form_runtime_performed": False,
        "package_parity_performed": False,
        "vision_pro_validation_performed": False,
        "downstream_checks_block_objective_stage": False,
        "passed": objective_decision_ready,
    }


def _expected_resume_identity(
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    source_response: Mapping[str, object],
    environment: Mapping[str, object],
) -> dict[str, object]:
    return {
        "experiment_id": plan.experiment_id,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "source_response": dict(source_response),
        "corpus_binding": {
            "path": binding.relative_path,
            "binding_id": binding.binding_id,
            "sha256": binding_sha256,
        },
        "manifest": {
            "path": _relative_repository_path(binding.source_manifest_path, "Bound direct corpus manifest"),
            "corpus_id": binding.source_corpus_id,
            "sha256": binding.source_manifest_sha256,
        },
        "private_source_identity": dict(binding.private_source_identity),
        "public_contract_bindings": _public_contract_record(plan),
        "toolchain": _toolchain_record(cast(Any, plan)),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": list(binding.selected_case_ids),
        "candidates": [_candidate_plan_record(candidate) for candidate in plan.candidates],
        "downstream_checks": {
            "perceptual_review": {"status": "not_performed", "objective_stage_blocker": False},
            "long_form_runtime": {"status": "not_performed", "objective_stage_blocker": False},
            "package_parity": {"status": "not_performed", "objective_stage_blocker": False},
            "vision_pro_validation": {"status": "not_performed", "objective_stage_blocker": False},
        },
    }


def _completed_resume_is_consistent(
    evidence: dict[str, object],
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    definitions: Mapping[str, CorpusCase],
) -> bool:
    cases = evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != len(definitions):
        return False
    if not all(isinstance(case, Mapping) and _case_complete(case, plan) for case in cases):
        return False
    refreshed = copy.deepcopy(evidence)
    _refresh_summaries(refreshed, plan, binding, definitions)
    for key in (
        "case_summaries",
        "candidate_summaries",
        "boundary_evaluations",
        "valid_subsets",
        "selected_subset",
        "provisional_mappings",
        "acceptance",
    ):
        if refreshed.get(key) != evidence.get(key):
            raise QualificationFailure("Completed resume evidence summaries contradict the recorded runs.")
    return True


def _load_resume_evidence(
    output_path: Path,
    *,
    plan: MappingSelectionPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    source_response: Mapping[str, object],
    environment: Mapping[str, object],
    definitions: Mapping[str, CorpusCase],
    private_paths: Sequence[Path],
    artifact_directory: Path,
) -> dict[str, object]:
    if output_path.is_symlink():
        raise QualificationFailure("Resume evidence must not be a symlink.")
    try:
        raw_bytes = output_path.read_bytes()
        raw_text = raw_bytes.decode("utf-8")
        evidence = dict(_loads_json_bytes(raw_bytes, "mapping-selection resume evidence"))
    except (OSError, UnicodeDecodeError) as error:
        raise QualificationFailure("Could not read mapping-selection resume evidence.") from error
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise QualificationFailure("Mapping-selection resume evidence has an unsupported schema.")
    for key, expected in _expected_resume_identity(
        plan,
        binding,
        plan_sha256,
        binding_sha256,
        source_response,
        environment,
    ).items():
        if evidence.get(key) != expected:
            raise QualificationFailure(f"Mapping-selection resume evidence {key} changed.")
    cases = _array(evidence.get("cases"), "mapping-selection resume cases")
    observed_ids = [
        _string(_mapping(case, "mapping-selection resume case").get("id"), "resume case.id") for case in cases
    ]
    if observed_ids != list(binding.selected_case_ids[: len(observed_ids)]):
        raise QualificationFailure("Mapping-selection resume cases violate the manifest-order prefix.")
    for raw_case in cases:
        case = _mapping(raw_case, "mapping-selection resume case")
        case_id = _string(case.get("id"), "mapping-selection resume case.id")
        _validate_case_record(case, plan, binding, definitions[case_id])
    acceptance = _mapping(evidence.get("acceptance"), "mapping-selection resume acceptance")
    complete = acceptance.get("complete") is True
    if not complete:
        _discard_unrecorded_expected_artifacts(evidence, plan, artifact_directory)
    _validate_retained_artifacts(evidence, plan, artifact_directory)
    _assert_private_values_absent(evidence, private_paths)
    finalized = acceptance.get("finalized") is True
    if complete and not _completed_resume_is_consistent(evidence, plan, binding, definitions):
        raise QualificationFailure("Completed resume evidence contradicts its raw records.")
    mode = stat.S_IMODE(output_path.stat().st_mode)
    writable = bool(mode & 0o222)
    if complete:
        canonical = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
        if raw_text != canonical:
            raise QualificationFailure("Completed mapping-selection evidence is not canonical JSON.")
        if not finalized:
            raise QualificationFailure("Completed mapping-selection evidence must be finalized and frozen 0444.")
        if writable:
            if mode != 0o644:
                raise QualificationFailure("Writable completed mapping-selection evidence has an unsafe mode.")
            _freeze_receipt(output_path)
            mode = stat.S_IMODE(output_path.stat().st_mode)
        if mode != 0o444:
            raise QualificationFailure("Completed mapping-selection evidence must be finalized and frozen 0444.")
    elif finalized or not writable:
        raise QualificationFailure("Incomplete mapping-selection evidence must remain writable and unfinalized.")
    return evidence


def _ensure_tracked_inputs(plan: MappingSelectionPlan, binding: CorpusBinding, plan_path: Path) -> None:
    tracked_inputs = (
        (plan_path, "Mapping-selection plan"),
        (plan.binding_path, "Mapping-selection corpus binding"),
        (binding.source_manifest_path, "Bound direct corpus manifest"),
        (
            _repository_path(str(binding.private_source_identity["source"]), "Direct anchor plan"),
            "Direct anchor plan",
        ),
        (plan.source_plan.path, "Source response plan"),
        (plan.ladder_manifest.path, "Video-quality ladder manifest"),
        (plan.video_quality_swift.path, "VideoQuality.swift"),
        (plan.ffmpeg_manifest.path, "FFmpeg vendor manifest"),
        (plan.fx_upscale_binary.path, "FX Upscale binary"),
        *((tool.path, f"bundled tool {key}") for key, tool in plan.bundled_tools.items()),
    )
    for path, label in tracked_inputs:
        _require_head_tracked_file(path, label)
    for tool in (plan.fx_upscale_binary.path, *(plan.bundled_tools[key].path for key in EXPECTED_TOOL_KEYS)):
        if not tool.is_file() or not os.access(tool, os.X_OK):
            raise QualificationFailure(f"Required bundled tool is unavailable or not executable: {tool.name}")


def _artifact_entry(
    evidence: Mapping[str, object],
    artifact_id: str,
) -> Mapping[str, object] | None:
    artifacts = evidence.get("retained_artifacts")
    if not isinstance(artifacts, list):
        return None
    for artifact in artifacts:
        if isinstance(artifact, Mapping) and artifact.get("artifact_id") == artifact_id:
            return artifact
    return None


def _run_mapping_selection_unlocked(
    selection_plan_path: Path,
    source_receipt_path: Path,
    output_path: Path,
    work_directory: Path,
    artifact_directory: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("File-upscale mapping selection requires macOS arm64.")
    source_git_sha = _git_head_from_clean_worktree()
    plan, binding, plan_sha256, binding_sha256 = load_mapping_selection_plan(selection_plan_path)
    if _require_head_tracked_file(selection_plan_path, "Mapping-selection plan") != plan.relative_path:
        raise QualificationFailure("Mapping-selection plan repository identity changed during validation.")
    _ensure_tracked_inputs(plan, binding, selection_plan_path)
    source_response = verify_source_response(plan, source_receipt_path)
    planned_cases = _cases_selected_from_binding(binding)
    if tuple(case.case_id for case in planned_cases) != EXPECTED_CASE_IDS:
        raise QualificationFailure("The mapping-selection decision corpus changed.")
    definitions = {case.case_id: case for case in planned_cases}

    ffmpeg = _pinned_media_tool(cast(Any, plan), "ffmpeg")
    ffprobe = _pinned_media_tool(cast(Any, plan), "ffprobe")
    environment = _environment_evidence(cast(Any, plan), ffmpeg, ffprobe, source_git_sha)
    if environment.get("git_head") != source_git_sha:
        raise QualificationFailure("Mapping-selection environment Git identity changed during preflight.")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    marker_identity = {
        "experiment_id": plan.experiment_id,
        "experiment_plan_sha256": plan_sha256,
        "receipt_name": output_path.name,
    }
    work_directory = _prepare_owned_directory(
        work_directory,
        WORK_DIRECTORY_MARKER,
        marker_identity,
        "Mapping-selection work directory",
    )
    artifact_directory = _prepare_owned_directory(
        artifact_directory,
        ARTIFACT_DIRECTORY_MARKER,
        marker_identity,
        "Mapping-selection artifact directory",
    )
    private_paths = _private_source_paths(planned_cases)
    if output_path.exists():
        if not resume:
            raise QualificationFailure(
                "Mapping-selection output already exists; use --resume or choose a new output path."
            )
        evidence = _load_resume_evidence(
            output_path,
            plan=plan,
            binding=binding,
            plan_sha256=plan_sha256,
            binding_sha256=binding_sha256,
            source_response=source_response,
            environment=environment,
            definitions=definitions,
            private_paths=private_paths,
            artifact_directory=artifact_directory,
        )
        acceptance = _mapping(evidence.get("acceptance"), "mapping-selection acceptance")
        if acceptance.get("complete") is True and acceptance.get("finalized") is True:
            _cleanup_completed_work_directory(work_directory, binding.selected_case_ids)
            return evidence
    else:
        evidence = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            source_response,
            environment,
        )
        _atomic_write(output_path, evidence, private_paths)

    for definition in planned_cases:
        existing_case = _case_record(evidence, definition.case_id)
        if existing_case is not None and _case_complete(existing_case, plan):
            case_work = _owned_case_directory(work_directory, definition.case_id)
            if case_work.exists():
                shutil.rmtree(case_work)
            continue
        case_work = (
            _owned_case_directory(work_directory, definition.case_id)
            if existing_case is not None
            else _reset_case_directory(work_directory, definition.case_id)
        )
        case_work.mkdir(parents=True, exist_ok=True)
        try:
            prepared = prepare_case(definition, case_work, ffmpeg=ffmpeg, ffprobe=ffprobe)
            _validate_prepared_source(binding, prepared)
        except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
            raise QualificationFailure(_safe_error_message(error, private_paths)) from None
        expected_case = _case_record_template(definition, prepared)
        prepared_record = _mapping(expected_case["prepared"], "prepared case metadata")
        if existing_case is None:
            existing_case = expected_case
            _array(evidence["cases"], "evidence cases").append(existing_case)
            evidence["updated_at"] = datetime.now(UTC).isoformat()
            _atomic_write(output_path, evidence, private_paths)
        else:
            for key in ("source", "prepared", "tags", "quality_gate"):
                if existing_case.get(key) != expected_case[key]:
                    raise QualificationFailure(
                        f"Prepared source identity changed while resuming case {definition.case_id}."
                    )

        repeats = _array(existing_case.get("repeats"), f"case {definition.case_id} repeats")
        for repeat_index in range(plan.runs_per_candidate):
            repeat = _repeat_record(existing_case, repeat_index)
            if repeat is None:
                repeat = _repeat_record_template(plan, definition.case_id, repeat_index)
                repeats.append(repeat)
                repeats.sort(
                    key=lambda item: _integer(
                        _mapping(item, "repeat").get("repeat_index"),
                        "repeat index",
                        minimum=0,
                        maximum=2,
                    )
                )
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
            if _repeat_complete(repeat, plan, definition.case_id, repeat_index):
                continue
            repeat_directory = case_work / f"repeat-{repeat_index + 1}"
            repeat_directory.mkdir(parents=True, exist_ok=True)
            base_path = repeat_directory / "generated-base.mov"
            base = repeat.get("base")
            if base is None:
                try:
                    base_path.unlink(missing_ok=True)
                    base = _record_base(
                        ffmpeg,
                        ffprobe,
                        prepared,
                        cast(Any, plan),
                        repeat_index,
                        base_path,
                    )
                except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
                    raise QualificationFailure(_safe_error_message(error, private_paths)) from None
                _validate_base_against_case(base, definition, prepared_record)
                repeat["base"] = base
            else:
                base = _mapping(base, "repeat.base")
                _validate_base_record(base, cast(Any, plan), repeat_index)
                _validate_base_against_case(base, definition, prepared_record)
                if not base_path.is_file() or sha256_file(base_path) != base["sha256"]:
                    if any(
                        _candidate_record(repeat, candidate.candidate_id) is not None for candidate in plan.candidates
                    ):
                        raise QualificationFailure(
                            "Resume requires the recorded base artifact for an incomplete repeat."
                        )
                    base_path.unlink(missing_ok=True)
                    base = _record_base(
                        ffmpeg,
                        ffprobe,
                        prepared,
                        cast(Any, plan),
                        repeat_index,
                        base_path,
                    )
                    _validate_base_against_case(base, definition, prepared_record)
                    repeat["base"] = base

            retain_repeat = definition.case_id in plan.retained_case_ids and repeat_index == plan.retained_repeat_index
            base_artifact_id = f"{definition.case_id}-r{repeat_index + 1}-base"
            if retain_repeat and _artifact_entry(evidence, base_artifact_id) is None:
                entry = _retained_artifact_entry(
                    artifact_directory=artifact_directory,
                    source_path=base_path,
                    case_id=definition.case_id,
                    repeat_index=repeat_index,
                    kind="generated_base",
                    candidate_id=None,
                    move=False,
                )
                if entry["sha256"] != base["sha256"]:
                    raise QualificationFailure("The retained generated base hash contradicts its record.")
                _upsert_retained_artifact(evidence, entry)
            evidence["updated_at"] = datetime.now(UTC).isoformat()
            _atomic_write(output_path, evidence, private_paths)

            candidates = _array(repeat.get("candidates"), "repeat candidates")
            _validate_candidate_prefix(repeat, plan, definition.case_id, repeat_index)
            for execution_ordinal, candidate in enumerate(_candidate_order(plan, definition.case_id, repeat_index)):
                if _candidate_complete(repeat, plan, definition.case_id, candidate, repeat_index):
                    continue
                try:
                    record = _record_candidate(
                        ffmpeg,
                        ffprobe,
                        prepared,
                        cast(Any, plan),
                        candidate,
                        repeat_index,
                        execution_ordinal,
                        base_path,
                        base,
                        repeat_directory,
                    )
                except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
                    raise QualificationFailure(_safe_error_message(error, private_paths)) from None
                candidates.append(record)
                final_path = repeat_directory / f"{candidate.candidate_id}-upscaled.mov"
                if retain_repeat:
                    entry = _retained_artifact_entry(
                        artifact_directory=artifact_directory,
                        source_path=final_path,
                        case_id=definition.case_id,
                        repeat_index=repeat_index,
                        kind="candidate_output",
                        candidate_id=candidate.candidate_id,
                        move=True,
                    )
                    if entry["sha256"] != record["final_sha256"]:
                        raise QualificationFailure("A retained candidate hash contradicts its record.")
                    _upsert_retained_artifact(evidence, entry)
                _refresh_summaries(evidence, plan, binding, definitions)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
                (repeat_directory / f"{candidate.candidate_id}-input.mov").unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
            if _repeat_complete(repeat, plan, definition.case_id, repeat_index):
                shutil.rmtree(repeat_directory, ignore_errors=True)
                _refresh_summaries(evidence, plan, binding, definitions)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
        if _case_complete(existing_case, plan):
            shutil.rmtree(case_work, ignore_errors=True)

    if _git_head_from_clean_worktree() != source_git_sha:
        raise QualificationFailure("Mapping-selection Git identity changed before final receipt freeze.")
    final_plan, final_binding, final_plan_sha256, final_binding_sha256 = load_mapping_selection_plan(
        selection_plan_path
    )
    final_source_response = verify_source_response(final_plan, source_receipt_path)
    if (
        final_plan != plan
        or final_binding != binding
        or final_plan_sha256 != plan_sha256
        or final_binding_sha256 != binding_sha256
        or final_source_response != source_response
    ):
        raise QualificationFailure("Mapping-selection plan, corpus, or source response changed before freeze.")
    final_environment = _environment_evidence(cast(Any, plan), ffmpeg, ffprobe, source_git_sha)
    if final_environment != environment:
        raise QualificationFailure("Mapping-selection environment changed before final receipt freeze.")
    _cleanup_completed_work_directory(work_directory, binding.selected_case_ids)
    _validate_retained_artifacts(evidence, plan, artifact_directory)
    _refresh_summaries(evidence, plan, binding, definitions)
    final_acceptance = evidence.get("acceptance")
    if not isinstance(final_acceptance, dict):
        raise QualificationFailure("Mapping-selection acceptance is invalid before freeze.")
    if final_acceptance.get("complete") is True:
        final_acceptance["finalized"] = True
    evidence["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(output_path, evidence, private_paths)
    if final_acceptance.get("finalized") is True:
        _freeze_receipt(output_path)
    return evidence


def run_mapping_selection(
    selection_plan_path: Path,
    source_receipt_path: Path,
    output_path: Path,
    work_directory: Path,
    artifact_directory: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        return _run_mapping_selection_unlocked(
            selection_plan_path,
            source_receipt_path,
            output_path,
            work_directory,
            artifact_directory,
            resume=resume,
        )


def exit_code_for_evidence(evidence: Mapping[str, object]) -> int:
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise QualificationFailure("Mapping-selection acceptance record is missing.")
    if acceptance.get("objective_decision_ready") is True:
        return 0
    if acceptance.get("complete") is True and acceptance.get("planned_full_quality_gated_corpus") is True:
        return 1
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the preregistered objective file-upscale mapping-selection stage without changing public mappings."
        )
    )
    parser.add_argument("--selection-plan", type=Path, default=DEFAULT_SELECTION_PLAN)
    parser.add_argument("--source-receipt", type=Path, default=DEFAULT_SOURCE_RECEIPT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-directory", type=Path, default=DEFAULT_WORK_DIRECTORY)
    parser.add_argument("--artifact-directory", type=Path, default=DEFAULT_ARTIFACT_DIRECTORY)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    private_paths: tuple[Path, ...] = ()
    try:
        private_paths = _configured_private_paths()
        evidence = run_mapping_selection(
            args.selection_plan.resolve(),
            args.source_receipt.absolute(),
            args.output.absolute(),
            args.work_directory.absolute(),
            args.artifact_directory.absolute(),
            resume=args.resume,
        )
        return exit_code_for_evidence(evidence)
    except KeyboardInterrupt:
        print("File-upscale mapping selection interrupted; resume the saved checkpoint.", file=sys.stderr)
        return 3
    except (
        AssertionError,
        KeyError,
        OSError,
        TypeError,
        subprocess.SubprocessError,
        QualificationFailure,
        ValueError,
    ) as error:
        message = _safe_error_message(error, private_paths)
        print(f"File-upscale mapping selection failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
