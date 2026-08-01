#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import shutil
import stat
import subprocess
import sys

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence, cast

from bd_to_avp.modules.video_quality_defaults import (
    AUTOMATIC_GENERATED_EYE_BITRATE_MBPS,
    AUTOMATIC_GENERATED_MERGE_QUALITY,
    DEFAULT_UPSCALE_QUALITY,
)
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_mapping_selection import (
    EXPECTED_CASE_IDS,
    EXPECTED_QUALITIES as PREDECESSOR_QUALITIES,
    EXPECTED_RETAINED_CASE_IDS,
    REPEATABILITY_FIELDS,
    CaseSchedule,
    SourcePlanBinding,
    SourceReceiptBinding,
    _artifact_entry,
    _candidate_complete,
    _candidate_record,
    _case_complete,
    _case_record,
    _case_record_template,
    _cases_selected_from_binding,
    _exact_keys,
    _file_mode,
    _git_sha_identity,
    _prepare_owned_directory,
    _public_contract_record,
    _read_frozen_source_receipt,
    _repeat_complete,
    _repeat_record,
    _repeat_record_template,
    _require_boolean,
    _retained_artifact_entry,
    _upsert_retained_artifact,
    _validate_candidate_prefix,
    _validate_case_record,
    _validate_prepared_source,
    _validate_retained_artifacts,
    load_mapping_corpus_binding,
    load_mapping_selection_plan,
)
from scripts.qualify_file_upscale_quality_sweep import (
    EXPECTED_TOOL_KEYS,
    CorpusBinding,
    FileBinding,
    UpscaleCandidate,
    _array,
    _candidate_plan_record,
    _configured_private_paths,
    _environment_evidence,
    _file_binding,
    _git_head_from_clean_worktree,
    _integer,
    _loads_json_bytes,
    _mapping,
    _number,
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
)
from scripts.qualify_generated_mv_hevc_calibration import (
    _assert_private_values_absent,
    _atomic_write,
    _freeze_receipt,
    calibration_lock,
)
from scripts.qualify_mv_hevc_corpus import CorpusCase, prepare_case
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CALIBRATION_PLAN = REPOSITORY_ROOT / "docs/qualification/file-upscale-quality-repeatability-calibration-v2.json"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-repeatability-calibration-v2.json"
DEFAULT_WORK_DIRECTORY = REPOSITORY_ROOT / "build/qualification/file-upscale-quality-repeatability-calibration-v2-work"
DEFAULT_ARTIFACT_DIRECTORY = (
    REPOSITORY_ROOT / "build/qualification/file-upscale-quality-repeatability-calibration-v2-artifacts"
)
EVIDENCE_SCHEMA_VERSION = 4
WORK_DIRECTORY_MARKER = ".bd-to-avp-file-upscale-quality-repeatability-calibration-v2.json"
ARTIFACT_DIRECTORY_MARKER = ".bd-to-avp-file-upscale-quality-repeatability-calibration-v2-artifacts.json"
EXPECTED_EXPERIMENT_ID = "file-upscale-quality-repeatability-calibration-v2"
EXPECTED_CORPUS_SHA256 = "b515d21f4958f263214f674528e4a07f0b6ffa258c891be5533cb23f16103b91"
EXPECTED_PREDECESSOR_PLAN_SHA256 = "3aa76c79adb81e72dd89f9fd548ef73698880eebf6332c149fe401c058d090ee"
EXPECTED_PREDECESSOR_RECEIPT_SHA256 = "c8e2478913a8c458657f0f7904720d6f76e8761b8ba1922e7c5dda5b916d2cef"
EXPECTED_PREDECESSOR_SOURCE_GIT_SHA = "b93a9729a2396b3942e679a1a8db34967f9d4467"
EXPECTED_PUBLIC_LADDER_SHA256 = "04620e59e5380c88d3d5152f78712402675f31db6f1253c1d93224af585111dc"
EXPECTED_VIDEO_QUALITY_SWIFT_SHA256 = "6f204564261d859590086ca41e9a27ac9f69bc0feb225137cf0abc4a98082dfa"
EXPECTED_RUNS_PER_CASE = 5
EXPECTED_QUALITY = 75
EXPECTED_PREVIOUS_LIMITS = {
    "min_same_eye_ssim": (0.0001, 0.0002),
    "final_to_base_size_ratio": (0.01, 0.02),
    "minimum_frame_same_eye_ssim": (0.0001, 0.0016),
    "p05_frame_same_eye_ssim": (0.0001, 0.0012),
    "frame_ssim_standard_deviation": (0.0001, 0.0002),
    "maximum_adjacent_frame_ssim_drop": (0.0001, 0.001),
    "min_eye_order_margin": (0.0001, 0.0011),
}


@dataclass(frozen=True)
class PreviousRepeatabilityLimit:
    key: str
    record_field: str
    quantum: float
    previous_limit: float


@dataclass(frozen=True)
class RepeatabilityCalibrationPlan:
    experiment_id: str
    binding_path: Path
    binding_id: str
    binding_sha256: str
    predecessor_receipt: SourceReceiptBinding
    predecessor_plan: SourcePlanBinding
    previous_limits: tuple[PreviousRepeatabilityLimit, ...]
    ladder_manifest: FileBinding
    video_quality_swift: FileBinding
    balanced_quality: int
    balanced_quality_source: str
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
    retained_repeat_index: int
    retained_case_ids: tuple[str, ...]
    relative_path: str | None = None
    schema_version: int = 1
    target_id: str = "upscale_quality"
    purpose: str = "calibrate_q075_repeatability_limits_only_not_mapping_selection"
    design: str = "q075_five_repeat_full_quality_gated_corpus_v2"
    execution_order_contract: str = "materialized_manifest_order_q075_five_repeat_v2"


def materialized_case_orders(case_ids: Sequence[str] = EXPECTED_CASE_IDS) -> tuple[CaseSchedule, ...]:
    return tuple(
        CaseSchedule(
            case_id=case_id,
            case_index=case_index,
            orders=tuple((EXPECTED_QUALITY,) for _ in range(EXPECTED_RUNS_PER_CASE)),
        )
        for case_index, case_id in enumerate(case_ids)
    )


def _parse_previous_limits(value: object) -> tuple[PreviousRepeatabilityLimit, ...]:
    document = _exact_keys(value, ("source", "metrics"), "predecessor.previous_repeatability")
    if document.get("source") != "source_response.noise_derivation.metrics":
        raise QualificationFailure("The previous repeatability-limit source changed from the checked plan.")
    metrics = _mapping(document.get("metrics"), "predecessor.previous_repeatability.metrics")
    if set(metrics) != set(REPEATABILITY_FIELDS):
        raise QualificationFailure("The previous repeatability metric set changed from the checked plan.")
    limits: list[PreviousRepeatabilityLimit] = []
    for field in REPEATABILITY_FIELDS:
        metric = _exact_keys(
            metrics.get(field),
            ("record_field", "quantum", "previous_limit"),
            f"predecessor.previous_repeatability.metrics.{field}",
        )
        record_field = _string(metric.get("record_field"), f"previous repeatability {field}.record_field")
        quantum = _number(metric.get("quantum"), f"previous repeatability {field}.quantum", positive=True)
        previous_limit = _number(
            metric.get("previous_limit"), f"previous repeatability {field}.previous_limit", positive=True
        )
        expected_quantum, expected_limit = EXPECTED_PREVIOUS_LIMITS[field]
        if record_field != field or quantum != expected_quantum or previous_limit != expected_limit:
            raise QualificationFailure(f"The frozen previous quantum or limit for {field} changed.")
        limits.append(
            PreviousRepeatabilityLimit(
                key=field,
                record_field=record_field,
                quantum=quantum,
                previous_limit=previous_limit,
            )
        )
    return tuple(limits)


def _parse_case_schedules(value: object, runs_per_candidate: int) -> tuple[CaseSchedule, ...]:
    execution = _exact_keys(
        value,
        ("contract", "runtime_shuffle_forbidden", "case_orders"),
        "execution_order",
    )
    if execution.get("contract") != "materialized_manifest_order_q075_five_repeat_v2":
        raise QualificationFailure("The repeatability execution-order contract changed.")
    _require_boolean(execution, "runtime_shuffle_forbidden", True, "execution_order")
    schedules: list[CaseSchedule] = []
    for index, raw_schedule in enumerate(_array(execution.get("case_orders"), "execution_order.case_orders")):
        schedule = _exact_keys(raw_schedule, ("case_id", "case_index", "orders"), f"case schedule {index}")
        case_id = _string(schedule.get("case_id"), f"case schedule {index}.case_id")
        case_index = _integer(schedule.get("case_index"), f"case schedule {index}.case_index", minimum=0, maximum=6)
        orders = tuple(
            tuple(
                _integer(quality, f"case schedule {index} quality", minimum=EXPECTED_QUALITY, maximum=EXPECTED_QUALITY)
                for quality in _array(raw_order, f"case schedule {index} order")
            )
            for raw_order in _array(schedule.get("orders"), f"case schedule {index}.orders")
        )
        if len(orders) != runs_per_candidate or any(order != (EXPECTED_QUALITY,) for order in orders):
            raise QualificationFailure("Every checked repeat must materialize q075 exactly once.")
        schedules.append(CaseSchedule(case_id=case_id, case_index=case_index, orders=orders))
    parsed = tuple(schedules)
    if parsed != materialized_case_orders():
        raise QualificationFailure("The materialized seven-case, five-repeat q075 schedule changed.")
    return parsed


def parse_repeatability_calibration_plan(raw: object) -> RepeatabilityCalibrationPlan:
    document = _exact_keys(
        raw,
        (
            "schema_version",
            "experiment_id",
            "target_id",
            "purpose",
            "design",
            "predecessor",
            "corpus_binding",
            "public_contract_bindings",
            "balanced",
            "generated_base",
            "runs_per_candidate",
            "execution_order",
            "toolchain",
            "technical_eligibility",
            "derivation",
            "artifact_retention",
            "scope",
        ),
        "file-upscale repeatability-calibration plan",
    )
    schema_version = _integer(document.get("schema_version"), "schema_version", minimum=1, maximum=1)
    experiment_id = _string(document.get("experiment_id"), "experiment_id")
    if experiment_id != EXPECTED_EXPERIMENT_ID:
        raise QualificationFailure("The repeatability-calibration experiment identity changed.")
    if document.get("target_id") != "upscale_quality":
        raise QualificationFailure("The repeatability-calibration target must remain upscale_quality.")
    if document.get("purpose") != "calibrate_q075_repeatability_limits_only_not_mapping_selection":
        raise QualificationFailure("The repeatability-calibration purpose changed.")
    if document.get("design") != "q075_five_repeat_full_quality_gated_corpus_v2":
        raise QualificationFailure("The repeatability-calibration design changed.")

    predecessor = _exact_keys(
        document.get("predecessor"),
        ("receipt", "plan", "previous_repeatability"),
        "predecessor",
    )
    receipt = _exact_keys(
        predecessor.get("receipt"),
        (
            "schema_version",
            "experiment_id",
            "sha256",
            "source_git_sha",
            "required_file_mode",
            "required_cli_argument",
        ),
        "predecessor.receipt",
    )
    predecessor_receipt = SourceReceiptBinding(
        schema_version=_integer(
            receipt.get("schema_version"), "predecessor.receipt.schema_version", minimum=3, maximum=3
        ),
        experiment_id=_string(receipt.get("experiment_id"), "predecessor.receipt.experiment_id"),
        sha256=_sha256_identity(receipt.get("sha256"), "predecessor.receipt.sha256"),
        source_git_sha=_git_sha_identity(receipt.get("source_git_sha"), "predecessor.receipt.source_git_sha"),
        required_file_mode=_file_mode(receipt.get("required_file_mode"), "predecessor.receipt.required_file_mode"),
    )
    if (
        predecessor_receipt.experiment_id != "file-upscale-quality-mapping-selection-v1"
        or predecessor_receipt.sha256 != EXPECTED_PREDECESSOR_RECEIPT_SHA256
        or predecessor_receipt.source_git_sha != EXPECTED_PREDECESSOR_SOURCE_GIT_SHA
        or receipt.get("required_cli_argument") != "--mapping-selection-receipt"
    ):
        raise QualificationFailure("The accepted predecessor receipt binding changed.")
    raw_predecessor_plan = _exact_keys(
        predecessor.get("plan"),
        ("path", "sha256", "schema_version"),
        "predecessor.plan",
    )
    predecessor_plan = SourcePlanBinding(
        path=_repository_path(
            _string(raw_predecessor_plan.get("path"), "predecessor.plan.path"),
            "Predecessor mapping-selection plan",
        ),
        sha256=_sha256_identity(raw_predecessor_plan.get("sha256"), "predecessor.plan.sha256"),
        schema_version=_integer(
            raw_predecessor_plan.get("schema_version"), "predecessor.plan.schema_version", minimum=1, maximum=1
        ),
    )
    if (
        _relative_repository_path(predecessor_plan.path, "Predecessor mapping-selection plan")
        != "docs/qualification/file-upscale-quality-mapping-selection-v1.json"
        or predecessor_plan.sha256 != EXPECTED_PREDECESSOR_PLAN_SHA256
    ):
        raise QualificationFailure("The predecessor mapping-selection plan binding changed.")
    previous_limits = _parse_previous_limits(predecessor.get("previous_repeatability"))

    corpus = _exact_keys(
        document.get("corpus_binding"),
        ("path", "binding_id", "sha256", "selected_case_ids"),
        "corpus_binding",
    )
    binding_path = _repository_path(
        _string(corpus.get("path"), "corpus_binding.path"),
        "File-upscale repeatability corpus binding",
    )
    binding_id = _string(corpus.get("binding_id"), "corpus_binding.binding_id")
    binding_sha256 = _sha256_identity(corpus.get("sha256"), "corpus_binding.sha256")
    selected_case_ids = tuple(
        _string(case_id, f"corpus_binding.selected_case_ids[{index}]")
        for index, case_id in enumerate(_array(corpus.get("selected_case_ids"), "corpus_binding.selected_case_ids"))
    )
    if (
        _relative_repository_path(binding_path, "File-upscale repeatability corpus binding")
        != "docs/qualification/file-upscale-quality-corpus-v2.json"
        or binding_id != "file-upscale-quality-corpus-v2"
        or binding_sha256 != EXPECTED_CORPUS_SHA256
        or selected_case_ids != EXPECTED_CASE_IDS
    ):
        raise QualificationFailure("The checked corpus-v2 identity or manifest-order case set changed.")

    public = _exact_keys(
        document.get("public_contract_bindings"),
        ("ladder_manifest", "video_quality_swift"),
        "public_contract_bindings",
    )
    ladder_manifest = _file_binding(public.get("ladder_manifest"), "public_contract_bindings.ladder_manifest")
    video_quality_swift = _file_binding(
        public.get("video_quality_swift"), "public_contract_bindings.video_quality_swift"
    )
    if (
        ladder_manifest.sha256 != EXPECTED_PUBLIC_LADDER_SHA256
        or video_quality_swift.sha256 != EXPECTED_VIDEO_QUALITY_SWIFT_SHA256
    ):
        raise QualificationFailure("The public ladder or VideoQuality.swift binding changed.")

    balanced = _exact_keys(
        document.get("balanced"),
        ("candidate_id", "quality", "quality_source"),
        "balanced",
    )
    balanced_quality = _integer(balanced.get("quality"), "balanced.quality", minimum=75, maximum=75)
    balanced_quality_source = _string(balanced.get("quality_source"), "balanced.quality_source")
    if (
        balanced.get("candidate_id") != "q075"
        or balanced_quality != DEFAULT_UPSCALE_QUALITY
        or balanced_quality_source != "bd_to_avp.modules.video_quality_defaults.DEFAULT_UPSCALE_QUALITY"
    ):
        raise QualificationFailure("The production Balanced q075 binding changed.")

    generated_base = _exact_keys(
        document.get("generated_base"),
        (
            "contract",
            "eye_bitrate_mbps",
            "eye_bitrate_source",
            "merge_quality",
            "merge_quality_source",
            "fresh_base_per_case_repeat",
            "exact_copy_per_candidate",
        ),
        "generated_base",
    )
    base_eye_bitrate_mbps = _integer(
        generated_base.get("eye_bitrate_mbps"), "generated_base.eye_bitrate_mbps", minimum=20, maximum=20
    )
    base_merge_quality = _integer(
        generated_base.get("merge_quality"), "generated_base.merge_quality", minimum=75, maximum=75
    )
    if (
        generated_base.get("contract") != "production_generated_mv_hevc_v1"
        or base_eye_bitrate_mbps != AUTOMATIC_GENERATED_EYE_BITRATE_MBPS
        or base_merge_quality != AUTOMATIC_GENERATED_MERGE_QUALITY
        or generated_base.get("eye_bitrate_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_EYE_BITRATE_MBPS"
        or generated_base.get("merge_quality_source")
        != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_GENERATED_MERGE_QUALITY"
    ):
        raise QualificationFailure("The production generated-base contract changed.")
    _require_boolean(generated_base, "fresh_base_per_case_repeat", True, "generated_base")
    _require_boolean(generated_base, "exact_copy_per_candidate", True, "generated_base")

    runs_per_candidate = _integer(
        document.get("runs_per_candidate"),
        "runs_per_candidate",
        minimum=EXPECTED_RUNS_PER_CASE,
        maximum=EXPECTED_RUNS_PER_CASE,
    )
    case_schedules = _parse_case_schedules(document.get("execution_order"), runs_per_candidate)

    toolchain = _exact_keys(
        document.get("toolchain"),
        (
            "ffmpeg_manifest",
            "fx_upscale_binary",
            "bundled_tools",
            "generated_encoder_contract",
            "file_upscale_command_contract",
            "metric_contract",
            "geometry_contract",
            "timing_contract",
        ),
        "toolchain",
    )
    ffmpeg_manifest = _file_binding(toolchain.get("ffmpeg_manifest"), "toolchain.ffmpeg_manifest")
    fx_upscale_binary = _file_binding(toolchain.get("fx_upscale_binary"), "toolchain.fx_upscale_binary")
    raw_bundled_tools = _mapping(toolchain.get("bundled_tools"), "toolchain.bundled_tools")
    if set(raw_bundled_tools) != set(EXPECTED_TOOL_KEYS):
        raise QualificationFailure("The bundled file-upscale tool set changed.")
    bundled_tools = {
        key: _file_binding(raw_bundled_tools[key], f"toolchain.bundled_tools.{key}") for key in EXPECTED_TOOL_KEYS
    }
    generated_encoder_contract = _string(
        toolchain.get("generated_encoder_contract"), "toolchain.generated_encoder_contract"
    )
    file_upscale_command_contract = _string(
        toolchain.get("file_upscale_command_contract"), "toolchain.file_upscale_command_contract"
    )
    metric_contract = _string(toolchain.get("metric_contract"), "toolchain.metric_contract")
    geometry_contract = _string(toolchain.get("geometry_contract"), "toolchain.geometry_contract")
    timing = _exact_keys(
        toolchain.get("timing_contract"),
        ("frame_rate", "duration_tolerance_frames"),
        "toolchain.timing_contract",
    )
    frame_rate_contract = _string(timing.get("frame_rate"), "toolchain.timing_contract.frame_rate")
    duration_tolerance_frames = _integer(
        timing.get("duration_tolerance_frames"),
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
        raise QualificationFailure("The production file-upscale toolchain contract changed.")

    technical = _exact_keys(
        document.get("technical_eligibility"),
        (
            "all_planned_records_required",
            "structure_timing_geometry_eye_order_hash_provenance_required",
            "maximum_final_to_base_size_ratio",
            "repeatability_metric_fields",
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
    maximum_final_to_base_size_ratio = _number(
        technical.get("maximum_final_to_base_size_ratio"),
        "technical_eligibility.maximum_final_to_base_size_ratio",
        positive=True,
    )
    metric_fields = tuple(
        _string(field, f"technical_eligibility.repeatability_metric_fields[{index}]")
        for index, field in enumerate(
            _array(technical.get("repeatability_metric_fields"), "technical_eligibility.repeatability_metric_fields")
        )
    )
    if maximum_final_to_base_size_ratio != 4.1 or metric_fields != REPEATABILITY_FIELDS:
        raise QualificationFailure("The mapping-selection size cap or repeatability metric fields changed.")

    derivation = _exact_keys(
        document.get("derivation"),
        (
            "source_records",
            "group_by",
            "within_case_statistic",
            "source_maximum_statistic",
            "formula",
            "multiplier",
            "predecessor_receipt_records_forbidden",
            "summary_fields_as_source_forbidden",
        ),
        "derivation",
    )
    expected_derivation = {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
        "multiplier": 2,
    }
    for key, expected in expected_derivation.items():
        if derivation.get(key) != expected:
            raise QualificationFailure(f"derivation.{key} changed from the checked calibration contract.")
    _require_boolean(derivation, "predecessor_receipt_records_forbidden", True, "derivation")
    _require_boolean(derivation, "summary_fields_as_source_forbidden", True, "derivation")

    retention = _exact_keys(
        document.get("artifact_retention"),
        (
            "repeat_index",
            "case_ids",
            "retain_generated_base",
            "retain_q075_output",
            "record_relative_paths_only",
            "exact_mov_count",
        ),
        "artifact_retention",
    )
    retained_repeat_index = _integer(
        retention.get("repeat_index"), "artifact_retention.repeat_index", minimum=0, maximum=0
    )
    retained_case_ids = tuple(
        _string(case_id, f"artifact_retention.case_ids[{index}]")
        for index, case_id in enumerate(_array(retention.get("case_ids"), "artifact_retention.case_ids"))
    )
    if retained_case_ids != EXPECTED_RETAINED_CASE_IDS or retention.get("exact_mov_count") != 8:
        raise QualificationFailure("The exact eight-MOV retention set changed.")
    for key in ("retain_generated_base", "retain_q075_output", "record_relative_paths_only"):
        _require_boolean(retention, key, True, "artifact_retention")

    scope = _exact_keys(
        document.get("scope"),
        (
            "calibration_only",
            "selection_forbidden",
            "boundary_evaluation_forbidden",
            "provisional_outputs_forbidden",
            "public_contract_changes_forbidden",
            "later_confirmation_required",
        ),
        "scope",
    )
    for key in scope:
        _require_boolean(scope, key, True, "scope")

    return RepeatabilityCalibrationPlan(
        experiment_id=experiment_id,
        binding_path=binding_path,
        binding_id=binding_id,
        binding_sha256=binding_sha256,
        predecessor_receipt=predecessor_receipt,
        predecessor_plan=predecessor_plan,
        previous_limits=previous_limits,
        ladder_manifest=ladder_manifest,
        video_quality_swift=video_quality_swift,
        balanced_quality=balanced_quality,
        balanced_quality_source=balanced_quality_source,
        base_eye_bitrate_mbps=base_eye_bitrate_mbps,
        base_merge_quality=base_merge_quality,
        runs_per_candidate=runs_per_candidate,
        case_schedules=case_schedules,
        candidates=(UpscaleCandidate(candidate_id="q075", quality=balanced_quality),),
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
        retained_repeat_index=retained_repeat_index,
        retained_case_ids=retained_case_ids,
        schema_version=schema_version,
    )


def load_repeatability_calibration_plan(
    path: Path,
) -> tuple[RepeatabilityCalibrationPlan, CorpusBinding, str, str]:
    resolved_path = path.resolve()
    relative_path = _relative_repository_path(resolved_path, "File-upscale repeatability-calibration plan")
    try:
        document = _loads_json_bytes(resolved_path.read_bytes(), "file-upscale repeatability-calibration plan")
    except OSError as error:
        raise QualificationFailure(f"Could not read repeatability-calibration plan {path.name}.") from error
    parsed = parse_repeatability_calibration_plan(document)
    binding, binding_sha256 = load_mapping_corpus_binding(parsed.binding_path)
    if (
        binding_sha256 != parsed.binding_sha256
        or binding.binding_id != parsed.binding_id
        or binding.selected_case_ids != EXPECTED_CASE_IDS
    ):
        raise QualificationFailure("The repeatability-calibration corpus binding does not match its pinned identity.")
    predecessor, predecessor_binding, predecessor_sha256, _ = load_mapping_selection_plan(parsed.predecessor_plan.path)
    if (
        predecessor_sha256 != parsed.predecessor_plan.sha256
        or predecessor.schema_version != parsed.predecessor_plan.schema_version
        or predecessor.experiment_id != parsed.predecessor_receipt.experiment_id
        or predecessor_binding != binding
    ):
        raise QualificationFailure("The bound predecessor mapping-selection plan identity is inconsistent.")
    previous_by_field = {limit.record_field: limit for limit in parsed.previous_limits}
    for prior_limit in predecessor.noise_limits:
        current = previous_by_field.get(prior_limit.record_field)
        if current is None or current.quantum != prior_limit.quantum or current.previous_limit != prior_limit.limit:
            raise QualificationFailure("The calibration plan does not preserve the predecessor limits and quanta.")
    if (
        parsed.ladder_manifest != predecessor.ladder_manifest
        or parsed.video_quality_swift != predecessor.video_quality_swift
        or parsed.base_eye_bitrate_mbps != predecessor.base_eye_bitrate_mbps
        or parsed.base_merge_quality != predecessor.base_merge_quality
        or parsed.ffmpeg_manifest != predecessor.ffmpeg_manifest
        or parsed.fx_upscale_binary != predecessor.fx_upscale_binary
        or parsed.bundled_tools != predecessor.bundled_tools
        or parsed.generated_encoder_contract != predecessor.generated_encoder_contract
        or parsed.file_upscale_command_contract != predecessor.file_upscale_command_contract
        or parsed.metric_contract != predecessor.metric_contract
        or parsed.geometry_contract != predecessor.geometry_contract
        or parsed.frame_rate_contract != predecessor.frame_rate_contract
        or parsed.duration_tolerance_frames != predecessor.duration_tolerance_frames
        or parsed.maximum_final_to_base_size_ratio != predecessor.maximum_final_to_base_size_ratio
    ):
        raise QualificationFailure("The production tool, public contract, or technical check binding changed.")
    for label, file_binding in (
        ("FFmpeg vendor manifest", parsed.ffmpeg_manifest),
        ("FX Upscale binary", parsed.fx_upscale_binary),
        ("video-quality ladder manifest", parsed.ladder_manifest),
        ("VideoQuality.swift", parsed.video_quality_swift),
        *((f"bundled tool {key}", tool) for key, tool in parsed.bundled_tools.items()),
    ):
        if not file_binding.path.is_file() or sha256_file(file_binding.path) != file_binding.sha256:
            raise QualificationFailure(f"{label} does not match its pinned SHA-256 identity.")
    return (
        RepeatabilityCalibrationPlan(**{**parsed.__dict__, "relative_path": relative_path}),
        binding,
        sha256_file(resolved_path),
        binding_sha256,
    )


def verify_predecessor_receipt(
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    receipt_path: Path,
) -> dict[str, object]:
    receipt = _read_frozen_source_receipt(receipt_path, plan.predecessor_receipt)
    if (
        receipt.get("schema_version") != plan.predecessor_receipt.schema_version
        or receipt.get("experiment_id") != plan.predecessor_receipt.experiment_id
        or receipt.get("source_git_sha") != plan.predecessor_receipt.source_git_sha
        or receipt.get("source_tree_dirty") is not False
    ):
        raise QualificationFailure("The predecessor mapping-selection receipt identity is inconsistent.")
    expected_plan = {
        "path": _relative_repository_path(plan.predecessor_plan.path, "Predecessor mapping-selection plan"),
        "sha256": plan.predecessor_plan.sha256,
    }
    if receipt.get("experiment_plan") != expected_plan:
        raise QualificationFailure("The predecessor receipt does not bind the checked mapping-selection plan.")
    expected_binding = {
        "path": binding.relative_path,
        "binding_id": binding.binding_id,
        "sha256": plan.binding_sha256,
    }
    if receipt.get("corpus_binding") != expected_binding:
        raise QualificationFailure("The predecessor receipt corpus binding changed.")
    if receipt.get("selected_case_ids") != list(EXPECTED_CASE_IDS):
        raise QualificationFailure("The predecessor receipt selected case set changed.")
    expected_candidates = [
        _candidate_plan_record(UpscaleCandidate(candidate_id=f"q{quality:03d}", quality=quality))
        for quality in PREDECESSOR_QUALITIES
    ]
    if receipt.get("candidates") != expected_candidates:
        raise QualificationFailure("The predecessor receipt candidate grid changed.")
    acceptance = _mapping(receipt.get("acceptance"), "predecessor receipt acceptance")
    required_acceptance = {
        "complete": True,
        "finalized": True,
        "planned_full_quality_gated_corpus": True,
        "source_response_verified": True,
        "structural_passed": True,
        "retained_artifacts_complete": True,
        "public_mapping_changes_forbidden": True,
        "ladder_mapping_selected": False,
    }
    for key, expected in required_acceptance.items():
        if acceptance.get(key) is not expected:
            raise QualificationFailure(f"The predecessor receipt acceptance field {key} is inconsistent.")
    return {
        "receipt": {
            "schema_version": plan.predecessor_receipt.schema_version,
            "experiment_id": plan.predecessor_receipt.experiment_id,
            "sha256": plan.predecessor_receipt.sha256,
            "source_git_sha": plan.predecessor_receipt.source_git_sha,
            "file_mode": "0444",
            "provided_via": "--mapping-selection-receipt",
        },
        "plan": {**expected_plan, "schema_version": plan.predecessor_plan.schema_version},
        "accepted_complete_receipt_verified": True,
        "records_used_for_calibration": False,
    }


def _previous_limit_record(plan: RepeatabilityCalibrationPlan) -> dict[str, object]:
    return {
        limit.key: {
            "record_field": limit.record_field,
            "quantum": limit.quantum,
            "previous_limit": limit.previous_limit,
        }
        for limit in plan.previous_limits
    }


def _method_record(plan: RepeatabilityCalibrationPlan) -> dict[str, object]:
    return {
        "design": plan.design,
        "stage": "repeatability_limit_calibration_only",
        "runs_per_candidate": plan.runs_per_candidate,
        "balanced": {
            "candidate_id": "q075",
            "quality": plan.balanced_quality,
            "quality_source": plan.balanced_quality_source,
        },
        "generated_base": {
            "eye_bitrate_mbps": plan.base_eye_bitrate_mbps,
            "merge_quality": plan.base_merge_quality,
            "target_total_eye_bitrate_mbps": plan.base_eye_bitrate_mbps * 2,
        },
        "pairing": "one fresh generated base per case/repeat; q075 receives one exact verified copy",
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
        "previous_repeatability_limits": _previous_limit_record(plan),
        "derivation": {
            "source_records": "raw_q075_case_repeat_candidate_records_only",
            "group_by": ["case_id", "candidate_id"],
            "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
            "source_maximum_statistic": "maximum_across_cases",
            "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
            "multiplier": 2,
            "predecessor_receipt_records_forbidden": True,
            "summary_fields_as_source_forbidden": True,
        },
        "technical_eligibility": {
            "all_planned_records_required": True,
            "structure_timing_geometry_eye_order_hash_provenance_required": True,
            "maximum_final_to_base_size_ratio": plan.maximum_final_to_base_size_ratio,
            "repeatability_metric_fields": list(REPEATABILITY_FIELDS),
        },
        "artifact_retention": {
            "repeat_index": plan.retained_repeat_index,
            "case_ids": list(plan.retained_case_ids),
            "exact_mov_count": 8,
            "relative_paths_only": True,
        },
        "scope": {
            "calibration_only": True,
            "selection_forbidden": True,
            "boundary_evaluation_forbidden": True,
            "provisional_outputs_forbidden": True,
            "public_contract_changes_forbidden": True,
            "later_confirmation_required": True,
        },
    }


def _validate_candidate_technical(
    record: Mapping[str, object],
    base: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    definition: CorpusCase,
    repeat_index: int,
) -> None:
    candidate = plan.candidates[0]
    _validate_candidate_record(record, candidate, repeat_index, 0)
    _validate_candidate_against_base(record, base, candidate)
    if (
        _number(record.get("final_to_base_size_ratio"), "candidate.final_to_base_size_ratio", positive=True)
        > plan.maximum_final_to_base_size_ratio
    ):
        raise QualificationFailure(f"Case {definition.case_id} q075 output exceeds the checked size cap.")
    if (
        _number(record.get("min_eye_order_margin"), "candidate.min_eye_order_margin")
        < definition.minimum_eye_order_margin
    ):
        raise QualificationFailure(f"Case {definition.case_id} q075 output fails the checked eye-order margin.")


def _validate_case_technical(
    case: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    definition: CorpusCase,
) -> None:
    for repeat_index in range(plan.runs_per_candidate):
        repeat = _repeat_record(case, repeat_index)
        if repeat is None:
            continue
        record = _candidate_record(repeat, "q075")
        if record is None:
            continue
        base = _mapping(repeat.get("base"), "q075 repeat base")
        _validate_candidate_technical(record, base, plan, definition, repeat_index)


def _q075_candidate_complete(
    repeat: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    definition: CorpusCase,
    repeat_index: int,
) -> bool:
    if not _candidate_complete(repeat, cast(Any, plan), definition.case_id, plan.candidates[0], repeat_index):
        return False
    _validate_case_technical({"repeats": [repeat]}, plan, definition)
    return True


def _q075_repeat_complete(
    repeat: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    definition: CorpusCase,
    repeat_index: int,
) -> bool:
    if not _repeat_complete(repeat, cast(Any, plan), definition.case_id, repeat_index):
        return False
    return _q075_candidate_complete(repeat, plan, definition, repeat_index)


def _q075_case_complete(
    case: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    definition: CorpusCase,
) -> bool:
    if not _case_complete(case, cast(Any, plan)):
        return False
    _validate_case_technical(case, plan, definition)
    return True


def derive_calibrated_limit(previous_limit: float, source_maximum: float, quantum: float) -> float:
    previous = Decimal(str(previous_limit))
    source = Decimal(str(source_maximum))
    step = Decimal(str(quantum))
    observed_limit = ((Decimal(2) * source / step).to_integral_value(rounding=ROUND_CEILING)) * step
    return float(max(previous, observed_limit))


def derive_repeatability_limits(
    evidence: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
) -> dict[str, object]:
    cases = _array(evidence.get("cases"), "repeatability calibration raw cases")
    observed_case_ids = [
        _string(_mapping(case, "repeatability calibration raw case").get("id"), "raw case.id") for case in cases
    ]
    if observed_case_ids != list(EXPECTED_CASE_IDS):
        raise QualificationFailure("Repeatability limits require all seven raw cases in manifest order.")
    grouped: list[dict[str, object]] = []
    values_by_case: dict[str, dict[str, list[float]]] = {}
    for raw_case in cases:
        case = _mapping(raw_case, "repeatability calibration raw case")
        case_id = _string(case.get("id"), "raw case.id")
        repeats = _array(case.get("repeats"), f"raw case {case_id} repeats")
        if len(repeats) != EXPECTED_RUNS_PER_CASE:
            raise QualificationFailure(f"Raw case {case_id} must contain exactly five repeats.")
        field_values: dict[str, list[float]] = {field: [] for field in REPEATABILITY_FIELDS}
        for expected_index, raw_repeat in enumerate(repeats):
            repeat = _mapping(raw_repeat, f"raw case {case_id} repeat")
            if repeat.get("repeat_index") != expected_index or repeat.get("order") != [EXPECTED_QUALITY]:
                raise QualificationFailure(f"Raw case {case_id} repeat schedule changed.")
            candidates = _array(repeat.get("candidates"), f"raw case {case_id} repeat candidates")
            if len(candidates) != 1:
                raise QualificationFailure(f"Raw case {case_id} repeat must contain q075 only.")
            candidate = _mapping(candidates[0], f"raw case {case_id} q075")
            if candidate.get("id") != "q075" or candidate.get("quality") != EXPECTED_QUALITY:
                raise QualificationFailure(f"Raw case {case_id} repeat candidate is not q075.")
            for field in REPEATABILITY_FIELDS:
                field_values[field].append(_number(candidate.get(field), f"raw q075 candidate.{field}"))
        values_by_case[case_id] = field_values
        grouped.append(
            {
                "case_id": case_id,
                "candidate_id": "q075",
                "repeat_count": EXPECTED_RUNS_PER_CASE,
                "ranges": {field: max(values) - min(values) for field, values in field_values.items()},
            }
        )
    metrics: dict[str, object] = {}
    previous_by_field = {limit.record_field: limit for limit in plan.previous_limits}
    for field in REPEATABILITY_FIELDS:
        ranked = sorted(
            (
                (max(values_by_case[case_id][field]) - min(values_by_case[case_id][field]), case_index, case_id)
                for case_index, case_id in enumerate(EXPECTED_CASE_IDS)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        source_maximum, _, source_case_id = ranked[0]
        previous = previous_by_field[field]
        metrics[field] = {
            "record_field": field,
            "source": {"case_id": source_case_id, "candidate_id": "q075"},
            "previous_limit": previous.previous_limit,
            "observed_maximum": source_maximum,
            "multiplier": 2,
            "quantum": previous.quantum,
            "derived_limit": derive_calibrated_limit(
                previous.previous_limit,
                source_maximum,
                previous.quantum,
            ),
        }
    return {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
        "multiplier": 2,
        "raw_record_count": len(EXPECTED_CASE_IDS) * EXPECTED_RUNS_PER_CASE,
        "case_repeat_ranges": grouped,
        "metrics": metrics,
    }


def _retained_manifest_complete(evidence: Mapping[str, object], plan: RepeatabilityCalibrationPlan) -> bool:
    artifacts = evidence.get("retained_artifacts")
    if not isinstance(artifacts, list):
        return False
    expected = {f"{case_id}-r1-base" for case_id in plan.retained_case_ids}
    expected.update(f"{case_id}-r1-q075" for case_id in plan.retained_case_ids)
    observed = {
        str(artifact.get("artifact_id"))
        for artifact in artifacts
        if isinstance(artifact, Mapping) and isinstance(artifact.get("artifact_id"), str)
    }
    return observed == expected and len(artifacts) == 8


def _refresh_calibration(
    evidence: dict[str, object],
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    definitions: Mapping[str, CorpusCase],
) -> None:
    cases = _array(evidence.get("cases"), "repeatability calibration cases")
    observed_ids = [
        _string(_mapping(case, "repeatability calibration case").get("id"), "calibration case.id") for case in cases
    ]
    if observed_ids != list(binding.selected_case_ids[: len(observed_ids)]):
        raise QualificationFailure("Repeatability calibration cases violate the manifest-order prefix.")
    for raw_case in cases:
        case = _mapping(raw_case, "repeatability calibration case")
        case_id = _string(case.get("id"), "repeatability calibration case.id")
        _validate_case_record(case, cast(Any, plan), binding, definitions[case_id])
        _validate_case_technical(case, plan, definitions[case_id])
    structural_passed = len(cases) == len(definitions) and all(
        isinstance(case, Mapping) and _q075_case_complete(case, plan, definitions[str(case.get("id"))])
        for case in cases
    )
    calibration = derive_repeatability_limits(evidence, plan) if structural_passed else None
    retained_complete = _retained_manifest_complete(evidence, plan)
    complete = structural_passed and retained_complete
    derived_complete = isinstance(calibration, Mapping) and set(
        _mapping(calibration.get("metrics"), "derived repeatability metrics")
    ) == set(REPEATABILITY_FIELDS)
    existing_acceptance = evidence.get("acceptance")
    finalized = isinstance(existing_acceptance, Mapping) and existing_acceptance.get("finalized") is True
    valid = complete and derived_complete
    evidence["repeatability_calibration"] = calibration
    evidence["acceptance"] = {
        "complete": complete,
        "finalized": finalized,
        "planned_full_quality_gated_corpus": True,
        "predecessor_verified": True,
        "expected_record_count": len(EXPECTED_CASE_IDS) * EXPECTED_RUNS_PER_CASE,
        "record_count": sum(
            1
            for raw_case in cases
            for raw_repeat in _array(
                _mapping(raw_case, "partial calibration case").get("repeats"),
                "partial calibration repeats",
            )
            if _candidate_record(_mapping(raw_repeat, "partial calibration repeat"), "q075") is not None
        ),
        "structural_timing_geometry_hash_provenance_passed": structural_passed,
        "eye_order_passed": structural_passed,
        "size_cap_passed": structural_passed,
        "retained_artifacts_complete": retained_complete,
        "derived_limits_complete": derived_complete,
        "calibration_receipt_valid": valid,
        "calibration_only": True,
        "public_contract_changes_forbidden": True,
        "later_confirmation_required": True,
        "passed": valid,
    }


def _new_evidence(
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    predecessor: Mapping[str, object],
    environment: Mapping[str, object],
) -> dict[str, object]:
    if plan.relative_path is None or binding.relative_path is None:
        raise QualificationFailure("Repeatability-calibration inputs are not repository-bound.")
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "experiment_id": plan.experiment_id,
        "created_at": now,
        "updated_at": now,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "predecessor": dict(predecessor),
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
        "public_contract_bindings": _public_contract_record(cast(Any, plan)),
        "toolchain": _toolchain_record(cast(Any, plan)),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": list(binding.selected_case_ids),
        "candidates": [_candidate_plan_record(plan.candidates[0])],
        "cases": [],
        "repeatability_calibration": None,
        "retained_artifacts": [],
        "later_confirmation": {
            "status": "not_performed",
            "required_before_public_contract_changes": True,
        },
        "acceptance": {
            "complete": False,
            "finalized": False,
            "planned_full_quality_gated_corpus": True,
            "predecessor_verified": True,
            "expected_record_count": len(EXPECTED_CASE_IDS) * EXPECTED_RUNS_PER_CASE,
            "record_count": 0,
            "structural_timing_geometry_hash_provenance_passed": False,
            "eye_order_passed": False,
            "size_cap_passed": False,
            "retained_artifacts_complete": False,
            "derived_limits_complete": False,
            "calibration_receipt_valid": False,
            "calibration_only": True,
            "public_contract_changes_forbidden": True,
            "later_confirmation_required": True,
            "passed": False,
        },
    }


def _expected_resume_identity(
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    predecessor: Mapping[str, object],
    environment: Mapping[str, object],
) -> dict[str, object]:
    return {
        "experiment_id": plan.experiment_id,
        "source_git_sha": environment["git_head"],
        "source_tree_dirty": False,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "predecessor": dict(predecessor),
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
        "public_contract_bindings": _public_contract_record(cast(Any, plan)),
        "toolchain": _toolchain_record(cast(Any, plan)),
        "method": _method_record(plan),
        "environment": dict(environment),
        "selected_case_ids": list(binding.selected_case_ids),
        "candidates": [_candidate_plan_record(plan.candidates[0])],
        "later_confirmation": {
            "status": "not_performed",
            "required_before_public_contract_changes": True,
        },
    }


def _validate_exact_retained_artifacts(
    evidence: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
    artifact_directory: Path,
) -> None:
    expected_specs: dict[str, dict[str, object]] = {}
    for case_id in plan.retained_case_ids:
        case = _case_record(evidence, case_id)
        if case is None:
            continue
        repeat = _repeat_record(case, plan.retained_repeat_index)
        if repeat is None:
            continue
        if isinstance(repeat.get("base"), Mapping):
            artifact_id = f"{case_id}-r1-base"
            expected_specs[artifact_id] = {
                "case_id": case_id,
                "repeat_index": 0,
                "kind": "generated_base",
                "candidate_id": None,
                "path": f"{case_id}/repeat-1/generated-base.mov",
            }
        if _candidate_record(repeat, "q075") is not None:
            artifact_id = f"{case_id}-r1-q075"
            expected_specs[artifact_id] = {
                "case_id": case_id,
                "repeat_index": 0,
                "kind": "candidate_output",
                "candidate_id": "q075",
                "path": f"{case_id}/repeat-1/q075-upscaled.mov",
            }
    artifacts = _array(evidence.get("retained_artifacts"), "retained_artifacts")
    observed_ids: set[str] = set()
    for raw_artifact in artifacts:
        artifact = _mapping(raw_artifact, "retained artifact")
        artifact_id = _string(artifact.get("artifact_id"), "retained artifact.artifact_id")
        expected = expected_specs.get(artifact_id)
        if expected is None or any(artifact.get(key) != value for key, value in expected.items()):
            raise QualificationFailure("A retained artifact path or identity is outside the exact eight-MOV contract.")
        if artifact_id in observed_ids:
            raise QualificationFailure("Retained artifact IDs must be unique.")
        observed_ids.add(artifact_id)
    if observed_ids != set(expected_specs):
        raise QualificationFailure("The retained artifact manifest does not match the recorded q075 outputs.")
    _validate_retained_artifacts(evidence, cast(Any, plan), artifact_directory)
    expected_paths = {str(spec["path"]) for spec in expected_specs.values()}
    observed_paths = {
        path.relative_to(artifact_directory).as_posix()
        for path in artifact_directory.rglob("*")
        if path.is_file() and path.name != ARTIFACT_DIRECTORY_MARKER
    }
    if observed_paths != expected_paths:
        raise QualificationFailure("The retained artifact directory contains unrecorded or missing media.")


def _discard_unrecorded_expected_artifacts(
    evidence: Mapping[str, object],
    plan: RepeatabilityCalibrationPlan,
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
        candidates = (
            (
                (f"{case_id}/repeat-1/generated-base.mov", isinstance(repeat.get("base"), Mapping)),
                (f"{case_id}/repeat-1/q075-upscaled.mov", _candidate_record(repeat, "q075") is not None),
            )
            if repeat is not None
            else (
                (f"{case_id}/repeat-1/generated-base.mov", False),
                (f"{case_id}/repeat-1/q075-upscaled.mov", False),
            )
        )
        for relative_path, raw_recorded in candidates:
            if raw_recorded or relative_path in recorded_paths:
                continue
            relative = Path(relative_path)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts:
                raise QualificationFailure("An unrecorded retained-artifact crash path is unsafe.")
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


def _completed_resume_is_consistent(
    evidence: dict[str, object],
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    definitions: Mapping[str, CorpusCase],
) -> bool:
    cases = evidence.get("cases")
    if not isinstance(cases, list) or len(cases) != len(definitions):
        return False
    refreshed = copy.deepcopy(evidence)
    _refresh_calibration(refreshed, plan, binding, definitions)
    if refreshed.get("repeatability_calibration") != evidence.get("repeatability_calibration"):
        raise QualificationFailure("Completed calibration limits contradict the raw q075 records.")
    if refreshed.get("acceptance") != evidence.get("acceptance"):
        raise QualificationFailure("Completed calibration acceptance contradicts the raw q075 records.")
    return _mapping(evidence.get("acceptance"), "completed calibration acceptance").get("complete") is True


def _load_resume_evidence(
    output_path: Path,
    *,
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    plan_sha256: str,
    binding_sha256: str,
    predecessor: Mapping[str, object],
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
        evidence = dict(_loads_json_bytes(raw_bytes, "repeatability-calibration resume evidence"))
    except (OSError, UnicodeDecodeError) as error:
        raise QualificationFailure("Could not read repeatability-calibration resume evidence.") from error
    expected_top_level = {
        "schema_version",
        "experiment_id",
        "created_at",
        "updated_at",
        "source_git_sha",
        "source_tree_dirty",
        "experiment_plan",
        "predecessor",
        "corpus_binding",
        "manifest",
        "private_source_identity",
        "public_contract_bindings",
        "toolchain",
        "method",
        "environment",
        "selected_case_ids",
        "candidates",
        "cases",
        "repeatability_calibration",
        "retained_artifacts",
        "later_confirmation",
        "acceptance",
    }
    if set(evidence) != expected_top_level:
        raise QualificationFailure("Repeatability-calibration resume evidence has an invalid top-level shape.")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
        raise QualificationFailure("Repeatability-calibration resume evidence has an unsupported schema.")
    _string(evidence.get("created_at"), "resume evidence.created_at")
    _string(evidence.get("updated_at"), "resume evidence.updated_at")
    for key, expected in _expected_resume_identity(
        plan,
        binding,
        plan_sha256,
        binding_sha256,
        predecessor,
        environment,
    ).items():
        if evidence.get(key) != expected:
            raise QualificationFailure(f"Repeatability-calibration resume evidence {key} changed.")
    cases = _array(evidence.get("cases"), "repeatability-calibration resume cases")
    observed_ids = [
        _string(_mapping(case, "repeatability-calibration resume case").get("id"), "resume case.id") for case in cases
    ]
    if observed_ids != list(binding.selected_case_ids[: len(observed_ids)]):
        raise QualificationFailure("Repeatability-calibration resume cases violate the manifest-order prefix.")
    for raw_case in cases:
        case = _mapping(raw_case, "repeatability-calibration resume case")
        case_id = _string(case.get("id"), "repeatability-calibration resume case.id")
        _validate_case_record(case, cast(Any, plan), binding, definitions[case_id])
        _validate_case_technical(case, plan, definitions[case_id])
    acceptance = _mapping(evidence.get("acceptance"), "repeatability-calibration resume acceptance")
    complete = acceptance.get("complete") is True
    if not complete:
        _discard_unrecorded_expected_artifacts(evidence, plan, artifact_directory)
    _validate_exact_retained_artifacts(evidence, plan, artifact_directory)
    _assert_private_values_absent(evidence, private_paths)
    canonical = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    if raw_text != canonical:
        raise QualificationFailure("Repeatability-calibration resume evidence is not canonical JSON.")
    finalized = acceptance.get("finalized") is True
    mode = stat.S_IMODE(output_path.stat().st_mode)
    writable = bool(mode & 0o222)
    if complete:
        if not _completed_resume_is_consistent(evidence, plan, binding, definitions):
            raise QualificationFailure("Completed repeatability-calibration evidence contradicts its raw records.")
        if acceptance.get("calibration_receipt_valid") is not True:
            raise QualificationFailure("Completed repeatability-calibration evidence is not structurally valid.")
        if finalized:
            if writable:
                if mode != 0o644:
                    raise QualificationFailure("Writable completed calibration evidence has an unsafe mode.")
                _freeze_receipt(output_path)
                mode = stat.S_IMODE(output_path.stat().st_mode)
            if mode != 0o444:
                raise QualificationFailure("Completed calibration evidence must be frozen 0444.")
        elif mode != 0o644:
            raise QualificationFailure("Completed unfinalized calibration evidence must remain writable 0644.")
    elif finalized or mode != 0o644:
        raise QualificationFailure("Incomplete calibration evidence must remain writable 0644 and unfinalized.")
    return evidence


def _owned_case_directory(work_directory: Path, case_id: str) -> Path:
    relative = Path(case_id)
    if relative.is_absolute() or len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise QualificationFailure(f"Unsafe repeatability-calibration case work path: {case_id}")
    path = work_directory / relative
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise QualificationFailure(f"Unsafe repeatability-calibration case work directory: {case_id}")
    if not (work_directory / WORK_DIRECTORY_MARKER).is_file():
        raise QualificationFailure("The repeatability-calibration work-directory ownership marker is missing.")
    return path


def _reset_case_directory(work_directory: Path, case_id: str) -> Path:
    path = _owned_case_directory(work_directory, case_id)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    return path


def _validate_clean_work_directory(work_directory: Path) -> None:
    marker = work_directory / WORK_DIRECTORY_MARKER
    unexpected = [path for path in work_directory.rglob("*") if path != marker]
    if unexpected:
        raise QualificationFailure("The completed repeatability-calibration work directory contains orphaned state.")


def _cleanup_completed_work_directory(work_directory: Path, case_ids: Sequence[str]) -> None:
    for case_id in case_ids:
        case_work = _owned_case_directory(work_directory, case_id)
        if case_work.exists():
            shutil.rmtree(case_work)
    _validate_clean_work_directory(work_directory)


def _ensure_tracked_inputs(
    plan: RepeatabilityCalibrationPlan,
    binding: CorpusBinding,
    plan_path: Path,
) -> None:
    tracked_inputs = (
        (plan_path, "Repeatability-calibration plan"),
        (plan.binding_path, "Repeatability-calibration corpus binding"),
        (binding.source_manifest_path, "Bound direct corpus manifest"),
        (
            _repository_path(str(binding.private_source_identity["source"]), "Direct anchor plan"),
            "Direct anchor plan",
        ),
        (plan.predecessor_plan.path, "Predecessor mapping-selection plan"),
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


def _run_repeatability_calibration_unlocked(
    calibration_plan_path: Path,
    predecessor_receipt_path: Path,
    output_path: Path,
    work_directory: Path,
    artifact_directory: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise QualificationFailure("File-upscale repeatability calibration requires macOS arm64.")
    source_git_sha = _git_head_from_clean_worktree()
    plan, binding, plan_sha256, binding_sha256 = load_repeatability_calibration_plan(calibration_plan_path)
    if _require_head_tracked_file(calibration_plan_path, "Repeatability-calibration plan") != plan.relative_path:
        raise QualificationFailure("Repeatability-calibration plan repository identity changed during validation.")
    _ensure_tracked_inputs(plan, binding, calibration_plan_path)
    predecessor = verify_predecessor_receipt(plan, binding, predecessor_receipt_path)
    planned_cases = _cases_selected_from_binding(binding)
    if tuple(case.case_id for case in planned_cases) != EXPECTED_CASE_IDS:
        raise QualificationFailure("The repeatability-calibration corpus changed.")
    definitions = {case.case_id: case for case in planned_cases}

    ffmpeg = _pinned_media_tool(cast(Any, plan), "ffmpeg")
    ffprobe = _pinned_media_tool(cast(Any, plan), "ffprobe")
    environment = _environment_evidence(cast(Any, plan), ffmpeg, ffprobe, source_git_sha)
    if environment.get("git_head") != source_git_sha:
        raise QualificationFailure("Repeatability-calibration environment Git identity changed during preflight.")

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
        "Repeatability-calibration work directory",
    )
    artifact_directory = _prepare_owned_directory(
        artifact_directory,
        ARTIFACT_DIRECTORY_MARKER,
        marker_identity,
        "Repeatability-calibration artifact directory",
    )
    private_paths = _private_source_paths(planned_cases)
    if output_path.exists():
        if not resume:
            raise QualificationFailure(
                "Repeatability-calibration output already exists; use --resume or choose a new output path."
            )
        evidence = _load_resume_evidence(
            output_path,
            plan=plan,
            binding=binding,
            plan_sha256=plan_sha256,
            binding_sha256=binding_sha256,
            predecessor=predecessor,
            environment=environment,
            definitions=definitions,
            private_paths=private_paths,
            artifact_directory=artifact_directory,
        )
        acceptance = _mapping(evidence.get("acceptance"), "repeatability-calibration acceptance")
        if acceptance.get("complete") is True and acceptance.get("finalized") is True:
            _cleanup_completed_work_directory(work_directory, binding.selected_case_ids)
            return evidence
    else:
        evidence = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            predecessor,
            environment,
        )
        _atomic_write(output_path, evidence, private_paths)

    for definition in planned_cases:
        existing_case = _case_record(evidence, definition.case_id)
        if existing_case is not None and _q075_case_complete(existing_case, plan, definition):
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
                repeat = _repeat_record_template(cast(Any, plan), definition.case_id, repeat_index)
                repeats.append(repeat)
                repeats.sort(
                    key=lambda item: _integer(
                        _mapping(item, "repeat").get("repeat_index"),
                        "repeat index",
                        minimum=0,
                        maximum=plan.runs_per_candidate - 1,
                    )
                )
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
            if _q075_repeat_complete(repeat, plan, definition, repeat_index):
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
                    if _candidate_record(repeat, "q075") is not None:
                        raise QualificationFailure("Resume requires the recorded base for an incomplete q075 repeat.")
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
            base_artifact_id = f"{definition.case_id}-r1-base"
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
            _validate_candidate_prefix(repeat, cast(Any, plan), definition.case_id, repeat_index)
            if not _q075_candidate_complete(repeat, plan, definition, repeat_index):
                candidate = plan.candidates[0]
                try:
                    record = _record_candidate(
                        ffmpeg,
                        ffprobe,
                        prepared,
                        cast(Any, plan),
                        candidate,
                        repeat_index,
                        0,
                        base_path,
                        base,
                        repeat_directory,
                    )
                    _validate_candidate_technical(record, base, plan, definition, repeat_index)
                except (OSError, subprocess.SubprocessError, QualificationFailure, ValueError) as error:
                    raise QualificationFailure(_safe_error_message(error, private_paths)) from None
                candidates.append(record)
                final_path = repeat_directory / "q075-upscaled.mov"
                if retain_repeat:
                    entry = _retained_artifact_entry(
                        artifact_directory=artifact_directory,
                        source_path=final_path,
                        case_id=definition.case_id,
                        repeat_index=repeat_index,
                        kind="candidate_output",
                        candidate_id="q075",
                        move=True,
                    )
                    if entry["sha256"] != record["final_sha256"]:
                        raise QualificationFailure("The retained q075 output hash contradicts its record.")
                    _upsert_retained_artifact(evidence, entry)
                _refresh_calibration(evidence, plan, binding, definitions)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
                (repeat_directory / "q075-input.mov").unlink(missing_ok=True)
                final_path.unlink(missing_ok=True)
            if _q075_repeat_complete(repeat, plan, definition, repeat_index):
                shutil.rmtree(repeat_directory, ignore_errors=True)
                _refresh_calibration(evidence, plan, binding, definitions)
                evidence["updated_at"] = datetime.now(UTC).isoformat()
                _atomic_write(output_path, evidence, private_paths)
        if _q075_case_complete(existing_case, plan, definition):
            shutil.rmtree(case_work, ignore_errors=True)

    if _git_head_from_clean_worktree() != source_git_sha:
        raise QualificationFailure("Repeatability-calibration Git identity changed before final receipt freeze.")
    final_plan, final_binding, final_plan_sha256, final_binding_sha256 = load_repeatability_calibration_plan(
        calibration_plan_path
    )
    final_predecessor = verify_predecessor_receipt(final_plan, final_binding, predecessor_receipt_path)
    if (
        final_plan != plan
        or final_binding != binding
        or final_plan_sha256 != plan_sha256
        or final_binding_sha256 != binding_sha256
        or final_predecessor != predecessor
    ):
        raise QualificationFailure("Repeatability-calibration plan, corpus, or predecessor changed before freeze.")
    final_environment = _environment_evidence(cast(Any, plan), ffmpeg, ffprobe, source_git_sha)
    if final_environment != environment:
        raise QualificationFailure("Repeatability-calibration environment changed before final receipt freeze.")
    _cleanup_completed_work_directory(work_directory, binding.selected_case_ids)
    _validate_exact_retained_artifacts(evidence, plan, artifact_directory)
    _refresh_calibration(evidence, plan, binding, definitions)
    acceptance = _mapping(evidence.get("acceptance"), "repeatability-calibration final acceptance")
    if acceptance.get("complete") is not True or acceptance.get("calibration_receipt_valid") is not True:
        raise QualificationFailure("Repeatability calibration did not produce a complete structurally valid receipt.")
    cast(dict[str, object], evidence["acceptance"])["finalized"] = True
    evidence["updated_at"] = datetime.now(UTC).isoformat()
    _atomic_write(output_path, evidence, private_paths)
    _freeze_receipt(output_path)
    return evidence


def run_repeatability_calibration(
    calibration_plan_path: Path,
    predecessor_receipt_path: Path,
    output_path: Path,
    work_directory: Path,
    artifact_directory: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        return _run_repeatability_calibration_unlocked(
            calibration_plan_path,
            predecessor_receipt_path,
            output_path,
            work_directory,
            artifact_directory,
            resume=resume,
        )


def exit_code_for_evidence(evidence: Mapping[str, object]) -> int:
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise QualificationFailure("Repeatability-calibration acceptance record is missing.")
    if acceptance.get("complete") is True:
        if (
            acceptance.get("finalized") is True
            and acceptance.get("calibration_receipt_valid") is True
            and acceptance.get("derived_limits_complete") is True
        ):
            return 0
        raise QualificationFailure("Complete repeatability-calibration evidence is invalid or unfinalized.")
    if acceptance.get("finalized") is True:
        raise QualificationFailure("Incomplete repeatability-calibration evidence cannot be finalized.")
    return 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the checked q075-only file-upscale repeatability-limit calibration stage."
    )
    parser.add_argument("--calibration-plan", type=Path, default=DEFAULT_CALIBRATION_PLAN)
    parser.add_argument("--mapping-selection-receipt", type=Path, required=True)
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
        evidence = run_repeatability_calibration(
            args.calibration_plan.resolve(),
            args.mapping_selection_receipt.absolute(),
            args.output.absolute(),
            args.work_directory.absolute(),
            args.artifact_directory.absolute(),
            resume=args.resume,
        )
        return exit_code_for_evidence(evidence)
    except KeyboardInterrupt:
        print("File-upscale repeatability calibration interrupted; resume the saved checkpoint.", file=sys.stderr)
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
        print(f"File-upscale repeatability calibration failed: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
