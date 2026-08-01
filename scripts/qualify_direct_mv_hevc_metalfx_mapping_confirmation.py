#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_DIRECT_UPSCALE_QUALITY
from bd_to_avp.worker.protocol import JobSpec, PROTOCOL_VERSION, WorkerProtocolError
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_mapping_confirmation import (
    ConfirmationCandidate,
    _array,
    _finalize_receipt,
    _mapping,
    _number,
    _read_frozen_json,
    _repository_path,
    _sha256,
    _string,
    exit_code_for_confirmation,
)
from scripts.qualify_direct_mv_hevc_quality_sweep import (
    DEFAULT_ENCODER,
    _git_head_from_clean_worktree,
    _require_head_tracked_file,
    load_sweep_plan,
    run_quality_sweep,
)
from scripts.qualify_generated_mv_hevc_calibration import calibration_lock
from scripts.qualify_mv_hevc_corpus import load_manifest
from scripts.qualify_mv_hevc_quality_match import sha256_file
from scripts.quality_mapping_confirmation import MappingEvaluationSpec, evaluate_mapping_confirmation


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIRMATION_PLAN = (
    REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-metalfx-quality-mapping-confirmation-v1.json"
)
EXPECTED_EXPERIMENT_ID = "direct-mv-hevc-metalfx-quality-mapping-confirmation-v1"
EXPECTED_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-snow-detail",
    "production-motion",
    "production-rate-override",
    "synthetic-animation",
    "synthetic-crop",
)
EXPECTED_PRODUCTION_CASE_IDS = EXPECTED_CASE_IDS[:5]
EXPECTED_CANDIDATES = (
    ("space_saver", "q030", 0.3),
    ("compact", "q040", 0.4),
    ("efficient", "q050", 0.5),
    ("balanced", "q060", 0.6),
    ("detailed", "q065", 0.65),
    ("high_detail", "q070", 0.7),
    ("maximum_detail", "q075", 0.75),
)
EXPECTED_SOURCE_RECEIPT_IDS = (
    "ordinary_direct_confirmation",
    "balanced_real_mvc_quality",
    "balanced_packaged_routes",
)


@dataclass(frozen=True)
class MetalFXThresholds:
    maximum_repeat_ssim_spread: float
    maximum_repeat_size_ratio_spread: float
    maximum_q075_to_q060_size_ratio: float
    minimum_case_median_storage_growth_ratio: float
    minimum_case_median_ssim_delta: float
    minimum_corpus_median_ssim_improvement: float
    real_case_ssim_threshold: float
    minimum_real_case_clear_count: int
    sensitive_case_ids: tuple[str, ...]
    minimum_sensitive_case_clear_count: int


@dataclass(frozen=True)
class MetalFXConfirmationPlan:
    relative_path: str | None
    sha256: str | None
    raw_sweep_path: Path
    raw_sweep_sha256: str
    raw_sweep_id: str
    source_receipts: Mapping[str, Mapping[str, object]]
    corpus_path: Path
    corpus_id: str
    corpus_sha256: str
    public_bindings: Mapping[str, Mapping[str, str]]
    candidates: tuple[ConfirmationCandidate, ...]
    thresholds: MetalFXThresholds
    fallback_policy: Mapping[str, object]


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise QualificationFailure(f"{label} must be an integer.")
    return value


def _parse_source_receipts(value: object) -> Mapping[str, Mapping[str, object]]:
    receipts = _mapping(value, "source_receipts")
    if tuple(receipts) != EXPECTED_SOURCE_RECEIPT_IDS:
        raise QualificationFailure("MetalFX source receipt identities changed.")
    expected_arguments = {
        "ordinary_direct_confirmation": "--ordinary-direct-receipt",
        "balanced_real_mvc_quality": "--balanced-quality-receipt",
        "balanced_packaged_routes": "--balanced-routes-receipt",
    }
    expected_fallback_use = {
        "ordinary_direct_confirmation": False,
        "balanced_real_mvc_quality": True,
        "balanced_packaged_routes": True,
    }
    parsed: dict[str, Mapping[str, object]] = {}
    for receipt_id in EXPECTED_SOURCE_RECEIPT_IDS:
        receipt = _mapping(receipts.get(receipt_id), f"source_receipts.{receipt_id}")
        _sha256(receipt.get("sha256"), f"source_receipts.{receipt_id}.sha256")
        identity = _mapping(receipt.get("identity"), f"source_receipts.{receipt_id}.identity")
        if (
            receipt.get("path_argument") != expected_arguments[receipt_id]
            or receipt.get("required_file_mode") != "0444"
            or receipt.get("records_used_for_metalfx_mapping_confirmation") is not False
            or receipt.get("records_used_for_fallback_policy_confirmation") is not expected_fallback_use[receipt_id]
            or not identity
        ):
            raise QualificationFailure(f"source receipt policy changed for {receipt_id}.")
        parsed[receipt_id] = receipt
    return parsed


def parse_confirmation_plan(raw: object) -> MetalFXConfirmationPlan:
    document = _mapping(raw, "MetalFX confirmation plan")
    if document.get("schema_version") != 1:
        raise QualificationFailure("MetalFX confirmation plan schema_version must be 1.")
    if document.get("experiment_id") != EXPECTED_EXPERIMENT_ID:
        raise QualificationFailure("MetalFX confirmation experiment_id is unsupported.")
    if document.get("target_id") != "direct_mv_hevc_metalfx_2x":
        raise QualificationFailure("MetalFX confirmation target_id changed.")

    raw_sweep = _mapping(document.get("raw_sweep_plan"), "raw_sweep_plan")
    raw_sweep_path = _repository_path(raw_sweep.get("path"), "raw_sweep_plan.path")
    raw_sweep_sha256 = _sha256(raw_sweep.get("sha256"), "raw_sweep_plan.sha256")
    raw_sweep_id = _string(raw_sweep.get("sweep_id"), "raw_sweep_plan.sweep_id")

    corpus = _mapping(document.get("corpus"), "corpus")
    corpus_path = _repository_path(corpus.get("path"), "corpus.path")
    corpus_id = _string(corpus.get("corpus_id"), "corpus.corpus_id")
    corpus_sha256 = _sha256(corpus.get("sha256"), "corpus.sha256")
    if tuple(_array(corpus.get("case_ids"), "corpus.case_ids")) != EXPECTED_CASE_IDS:
        raise QualificationFailure("MetalFX confirmation case identities changed.")

    bindings = _mapping(document.get("public_contract_bindings"), "public_contract_bindings")
    if tuple(bindings) != (
        "ladder_manifest",
        "video_quality_catalog",
        "worker_quality_contract",
        "route_resolution",
    ):
        raise QualificationFailure("MetalFX public contract bindings changed.")
    public_bindings: dict[str, Mapping[str, str]] = {}
    for binding_id, raw_binding in bindings.items():
        binding = _mapping(raw_binding, f"public_contract_bindings.{binding_id}")
        public_bindings[str(binding_id)] = {
            "path": _string(binding.get("path"), f"public_contract_bindings.{binding_id}.path"),
            "sha256": _sha256(binding.get("sha256"), f"public_contract_bindings.{binding_id}.sha256"),
        }

    candidates = tuple(
        ConfirmationCandidate(
            step_id=_string(candidate.get("step_id"), "candidate.step_id"),
            candidate_id=_string(candidate.get("candidate_id"), "candidate.candidate_id"),
            quality=_number(candidate.get("quality"), "candidate.quality"),
        )
        for candidate in (_mapping(value, "candidate") for value in _array(document.get("candidates"), "candidates"))
    )
    if tuple((candidate.step_id, candidate.candidate_id, candidate.quality) for candidate in candidates) != (
        EXPECTED_CANDIDATES
    ):
        raise QualificationFailure("MetalFX confirmation candidates changed.")

    technical = _mapping(document.get("technical_eligibility"), "technical_eligibility")
    boundary = _mapping(document.get("adjacent_boundary_policy"), "adjacent_boundary_policy")
    thresholds = MetalFXThresholds(
        maximum_repeat_ssim_spread=_number(technical.get("maximum_repeat_ssim_spread"), "maximum_repeat_ssim_spread"),
        maximum_repeat_size_ratio_spread=_number(
            technical.get("maximum_repeat_size_ratio_spread"), "maximum_repeat_size_ratio_spread"
        ),
        maximum_q075_to_q060_size_ratio=_number(
            technical.get("maximum_q075_to_q060_size_ratio"), "maximum_q075_to_q060_size_ratio"
        ),
        minimum_case_median_storage_growth_ratio=_number(
            boundary.get("minimum_case_median_storage_growth_ratio"),
            "minimum_case_median_storage_growth_ratio",
        ),
        minimum_case_median_ssim_delta=_number(
            boundary.get("minimum_case_median_ssim_delta"), "minimum_case_median_ssim_delta"
        ),
        minimum_corpus_median_ssim_improvement=_number(
            boundary.get("minimum_corpus_median_ssim_improvement"),
            "minimum_corpus_median_ssim_improvement",
        ),
        real_case_ssim_threshold=_number(boundary.get("real_case_ssim_threshold"), "real_case_ssim_threshold"),
        minimum_real_case_clear_count=_integer(
            boundary.get("minimum_real_case_clear_count"), "minimum_real_case_clear_count"
        ),
        sensitive_case_ids=tuple(_array(boundary.get("sensitive_case_ids"), "sensitive_case_ids")),
        minimum_sensitive_case_clear_count=_integer(
            boundary.get("minimum_sensitive_case_clear_count"), "minimum_sensitive_case_clear_count"
        ),
    )
    if thresholds != MetalFXThresholds(
        0.0002,
        0.02,
        4.9,
        0.005,
        -0.0002,
        0.0001,
        0.0001,
        2,
        (
            "production-grain-rain",
            "production-snow-detail",
        ),
        1,
    ):
        raise QualificationFailure("MetalFX confirmation thresholds changed.")
    if (
        technical.get("threshold_source")
        != "ordinary direct confirmation policy plus repeated production MetalFX Balanced evidence"
        or boundary.get("strict_paired_storage_growth_required") is not True
        or boundary.get("failed_boundary_action") != "collapse"
        or boundary.get("aliases_forbidden") is not True
    ):
        raise QualificationFailure("MetalFX confirmation threshold policy changed.")

    fallback = _mapping(document.get("fallback_policy"), "fallback_policy")
    expected_fallback = {
        "supported_ladder_step_ids": ["balanced"],
        "balanced_generated_values": {"eye_bitrate_mbps": 20, "merge_quality": 75},
        "balanced_file_upscale_quality": 75,
        "maximum_generated_quality_loss_vs_direct": 0.002,
        "matched_source_sha256": "da31e6ae9749897ca199f4a37a781b2be9a2d82076885efea4d0c156673bbcec",
        "required_route_reason": "direct_capability_unavailable",
        "required_fallback_reason": "metalfx_2x_mv_hevc_unavailable",
        "required_fallback_timing": "pre_input",
        "unsupported_step_action": "unavailable_not_balanced_alias",
        "custom_action": "preserve_exact_route_controls",
        "worker_contract_validation": "balanced_payload_parses_and_non_balanced_steps_reject",
        "aliases_forbidden": True,
    }
    if dict(fallback) != expected_fallback:
        raise QualificationFailure("MetalFX generated fallback policy changed.")

    selection = _mapping(document.get("selection_policy"), "selection_policy")
    artifact = _mapping(document.get("artifact_policy"), "artifact_policy")
    decision = _mapping(document.get("decision_policy"), "decision_policy")
    if (
        selection.get("primary") != "fixed_contiguous_subset_containing_q060"
        or selection.get("all_seven_required_for_positive_confirmation") is not True
        or selection.get("candidate_step_assignments_fixed_before_run") is not True
        or selection.get("missing_slots") != "unsupported"
        or artifact.get("fresh_output_sha256_recorded_for_every_run") is not True
        or artifact.get("fresh_outputs_ephemeral") is not True
        or artifact.get("retained_full_length_anchor_artifacts") != []
        or decision.get("public_mapping_changes_forbidden") is not True
        or decision.get("metalfx_objective_confirmation_only") is not True
        or decision.get("fallback_policy_confirmation_included") is not True
        or any(
            decision.get(key) is not False
            for key in (
                "package_parity_performed",
                "perceptual_review_performed",
                "long_form_runtime_performed",
                "vision_pro_validation_performed",
                "signed_beta_performed",
            )
        )
    ):
        raise QualificationFailure("MetalFX confirmation selection, artifact, or decision policy changed.")

    return MetalFXConfirmationPlan(
        relative_path=None,
        sha256=None,
        raw_sweep_path=raw_sweep_path,
        raw_sweep_sha256=raw_sweep_sha256,
        raw_sweep_id=raw_sweep_id,
        source_receipts=_parse_source_receipts(document.get("source_receipts")),
        corpus_path=corpus_path,
        corpus_id=corpus_id,
        corpus_sha256=corpus_sha256,
        public_bindings=public_bindings,
        candidates=candidates,
        thresholds=thresholds,
        fallback_policy=dict(fallback),
    )


def load_confirmation_plan(path: Path) -> MetalFXConfirmationPlan:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure("MetalFX confirmation plan must be inside the repository.") from error
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"could not read MetalFX confirmation plan: {error}") from error
    plan = parse_confirmation_plan(raw)
    _require_head_tracked_file(resolved, "MetalFX mapping-confirmation plan")
    if sha256_file(plan.raw_sweep_path) != plan.raw_sweep_sha256:
        raise QualificationFailure("MetalFX raw confirmation sweep plan SHA-256 changed.")
    raw_sweep, raw_sweep_sha256 = load_sweep_plan(plan.raw_sweep_path)
    if (
        raw_sweep_sha256 != plan.raw_sweep_sha256
        or raw_sweep.sweep_id != plan.raw_sweep_id
        or raw_sweep.target_id != "direct_mv_hevc_metalfx_2x"
        or raw_sweep.upscale_mode != "metalfx"
        or raw_sweep.comparison_scale != (1920, 1080)
    ):
        raise QualificationFailure("MetalFX raw confirmation sweep identity changed.")
    if tuple((candidate.candidate_id, candidate.quality) for candidate in raw_sweep.candidates) != tuple(
        (candidate.candidate_id, candidate.quality) for candidate in plan.candidates
    ):
        raise QualificationFailure("MetalFX raw confirmation sweep candidate set changed.")
    if raw_sweep.runs_per_candidate != 3 or raw_sweep.balanced_quality != AUTOMATIC_DIRECT_UPSCALE_QUALITY:
        raise QualificationFailure("MetalFX raw confirmation sweep execution contract changed.")
    if sha256_file(plan.corpus_path) != plan.corpus_sha256:
        raise QualificationFailure("MetalFX confirmation corpus SHA-256 changed.")
    manifest = load_manifest(plan.corpus_path)
    gated_case_ids = tuple(case.case_id for case in manifest.cases if case.quality_gate)
    if manifest.corpus_id != plan.corpus_id or gated_case_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("MetalFX confirmation corpus identity or gated cases changed.")
    if any(case.output_eye_width != 1920 or case.output_eye_height != 1080 for case in manifest.cases):
        raise QualificationFailure("MetalFX confirmation corpus contains an unsupported per-eye input size.")
    for binding in plan.public_bindings.values():
        binding_path = _repository_path(binding["path"], "public binding path")
        _require_head_tracked_file(binding_path, "MetalFX mapping-confirmation public contract")
        if sha256_file(binding_path) != binding["sha256"]:
            raise QualificationFailure("a MetalFX-bound public contract changed before confirmation.")
    return MetalFXConfirmationPlan(
        relative_path=relative_path,
        sha256=sha256_file(resolved),
        raw_sweep_path=plan.raw_sweep_path,
        raw_sweep_sha256=plan.raw_sweep_sha256,
        raw_sweep_id=plan.raw_sweep_id,
        source_receipts=plan.source_receipts,
        corpus_path=plan.corpus_path,
        corpus_id=plan.corpus_id,
        corpus_sha256=plan.corpus_sha256,
        public_bindings=plan.public_bindings,
        candidates=plan.candidates,
        thresholds=plan.thresholds,
        fallback_policy=plan.fallback_policy,
    )


def _receipt_sha(plan: MetalFXConfirmationPlan, receipt_id: str) -> str:
    receipt = _mapping(plan.source_receipts.get(receipt_id), f"source receipt {receipt_id}")
    return _sha256(receipt.get("sha256"), f"source receipt {receipt_id} SHA-256")


def _verify_ordinary_direct_receipt(plan: MetalFXConfirmationPlan, path: Path) -> dict[str, object]:
    document = _read_frozen_json(path, _receipt_sha(plan, "ordinary_direct_confirmation"), "ordinary direct receipt")
    confirmation = _mapping(document.get("confirmation"), "ordinary direct confirmation")
    acceptance = _mapping(confirmation.get("acceptance"), "ordinary direct acceptance")
    if (
        document.get("schema_version") != 1
        or document.get("source_git_sha") != "f44c2f0ecf1e772a00a4b3b623ea1d479ab9def3"
        or document.get("source_tree_dirty") is not False
        or confirmation.get("experiment_id") != "direct-mv-hevc-quality-mapping-confirmation-v1"
        or acceptance.get("objective_decision_ready") is not True
        or acceptance.get("public_mapping_changes_forbidden") is not True
        or acceptance.get("ladder_mapping_selected") is not False
    ):
        raise QualificationFailure("ordinary direct source receipt identity or acceptance changed.")
    production_sources: dict[str, object] = {}
    for raw_case in _array(document.get("cases"), "ordinary direct cases"):
        case = _mapping(raw_case, "ordinary direct case")
        case_id = _string(case.get("id"), "ordinary direct case id")
        if case_id in EXPECTED_PRODUCTION_CASE_IDS:
            production_sources[case_id] = dict(_mapping(case.get("source"), "ordinary direct case source"))
    if tuple(production_sources) != EXPECTED_PRODUCTION_CASE_IDS:
        raise QualificationFailure("ordinary direct source receipt omitted a required production case.")
    return {
        "sha256": _receipt_sha(plan, "ordinary_direct_confirmation"),
        "source_git_sha": document.get("source_git_sha"),
        "production_sources": production_sources,
        "objective_decision_ready": True,
    }


def _median(records: Sequence[Mapping[str, object]], key: str) -> float:
    return statistics.median(_number(record.get(key), key) for record in records)


def _verify_balanced_quality_receipt(plan: MetalFXConfirmationPlan, path: Path) -> dict[str, object]:
    document = _read_frozen_json(path, _receipt_sha(plan, "balanced_real_mvc_quality"), "Balanced quality receipt")
    source = _mapping(document.get("source"), "Balanced quality source")
    package = _mapping(document.get("package"), "Balanced quality package")
    acceptance = _mapping(document.get("acceptance"), "Balanced quality acceptance")
    thresholds = _mapping(document.get("thresholds"), "Balanced quality thresholds")
    direct_runs = tuple(
        _mapping(run, "Balanced direct run") for run in _array(document.get("direct_runs"), "direct_runs")
    )
    fallback_runs = tuple(
        _mapping(run, "Balanced generated fallback run")
        for run in _array(document.get("file_based_runs"), "file_based_runs")
    )
    if (
        document.get("schema_version") != 1
        or source.get("sha256") != plan.fallback_policy["matched_source_sha256"]
        or package.get("app_tree_sha256") != "7294f205d9d95d53f72d7fa30d977b2551c6d6ab8c0bdeddc444c354060fc801"
        or acceptance.get("passed") is not True
        or acceptance.get("repeated_full_routes") is not True
        or thresholds.get("runs_per_route") != 3
        or _number(thresholds.get("quality_tolerance"), "Balanced quality tolerance")
        != plan.fallback_policy["maximum_generated_quality_loss_vs_direct"]
        or len(direct_runs) != 3
        or len(fallback_runs) != 3
    ):
        raise QualificationFailure("Balanced matched-source quality receipt identity or acceptance changed.")
    direct_quality = _median(direct_runs, "min_same_eye_ssim")
    fallback_quality = _median(fallback_runs, "min_same_eye_ssim")
    quality_loss = direct_quality - fallback_quality
    quality_passed = quality_loss <= _number(
        plan.fallback_policy["maximum_generated_quality_loss_vs_direct"], "maximum fallback quality loss"
    )
    eye_order_passed = min(
        _number(run.get("min_eye_order_margin"), "eye-order margin") for run in (*direct_runs, *fallback_runs)
    ) >= _number(thresholds.get("minimum_eye_order_margin"), "minimum eye-order margin")
    if not quality_passed or not eye_order_passed:
        raise QualificationFailure("Balanced generated fallback does not preserve checked matched-source quality.")
    return {
        "sha256": _receipt_sha(plan, "balanced_real_mvc_quality"),
        "source_sha256": source.get("sha256"),
        "package_app_tree_sha256": package.get("app_tree_sha256"),
        "direct_median_min_same_eye_ssim": direct_quality,
        "generated_median_min_same_eye_ssim": fallback_quality,
        "generated_quality_loss_vs_direct": quality_loss,
        "maximum_generated_quality_loss_vs_direct": plan.fallback_policy["maximum_generated_quality_loss_vs_direct"],
        "generated_to_direct_size_ratio": _median(fallback_runs, "final_bytes") / _median(direct_runs, "final_bytes"),
        "generated_to_direct_elapsed_ratio": _median(fallback_runs, "elapsed_seconds")
        / _median(direct_runs, "elapsed_seconds"),
        "eye_order_passed": eye_order_passed,
        "quality_passed": quality_passed,
    }


def _verify_balanced_routes_receipt(plan: MetalFXConfirmationPlan, path: Path) -> dict[str, object]:
    document = _read_frozen_json(path, _receipt_sha(plan, "balanced_packaged_routes"), "Balanced routes receipt")
    source = _mapping(document.get("source"), "Balanced routes source")
    package = _mapping(document.get("package"), "Balanced routes package")
    acceptance = _mapping(document.get("acceptance"), "Balanced routes acceptance")
    metalfx = _mapping(document.get("metalfx_4k"), "metalfx_4k")
    direct = _mapping(metalfx.get("direct"), "metalfx direct")
    fallback = _mapping(metalfx.get("fallback"), "metalfx fallback")
    for mode in ("full", "preview"):
        direct_route = _mapping(_mapping(direct.get(mode), f"direct {mode}").get("route"), f"direct {mode} route")
        fallback_route = _mapping(
            _mapping(fallback.get(mode), f"fallback {mode}").get("route"), f"fallback {mode} route"
        )
        if direct_route != {
            "intent": "automatic",
            "quality": AUTOMATIC_DIRECT_UPSCALE_QUALITY,
            "rate_control": "quality",
            "reason": "direct_upscale_eligible",
            "selected": "direct_mv_hevc",
            "upscale_mode": "metalfx",
        }:
            raise QualificationFailure(f"Balanced direct MetalFX {mode} route changed.")
        if fallback_route != {
            "eye_bitrate_mbps": 20,
            "fallback_reason": plan.fallback_policy["required_fallback_reason"],
            "fallback_timing": plan.fallback_policy["required_fallback_timing"],
            "intent": "automatic",
            "merge_quality": 75,
            "reason": plan.fallback_policy["required_route_reason"],
            "selected": "generated_mv_hevc",
        }:
            raise QualificationFailure(f"Balanced generated fallback {mode} route changed.")
    required_acceptance = (
        "finalized_artifacts_valid",
        "metalfx_4k_direct_full_preview_parity",
        "metalfx_4k_fallback_full_preview_parity",
        "metalfx_4k_fallback_pre_input",
        "metalfx_4k_stage_contracts",
        "metalfx_4k_video_dimensions",
        "passed",
    )
    if (
        document.get("schema_version") != 3
        or _mapping(source.get("media"), "Balanced routes media").get("duration_seconds") != 65.649
        or source.get("sha256") != plan.fallback_policy["matched_source_sha256"]
        or package.get("app_tree_sha256") != "7294f205d9d95d53f72d7fa30d977b2551c6d6ab8c0bdeddc444c354060fc801"
        or any(acceptance.get(key) is not True for key in required_acceptance)
    ):
        raise QualificationFailure("Balanced packaged route receipt identity or acceptance changed.")
    return {
        "sha256": _receipt_sha(plan, "balanced_packaged_routes"),
        "source_sha256": source.get("sha256"),
        "package_app_tree_sha256": package.get("app_tree_sha256"),
        "full_preview_parity": True,
        "pre_input_fallback": True,
        "generated_values": dict(_mapping(plan.fallback_policy["balanced_generated_values"], "generated values")),
    }


def _worker_request(step_id: str) -> dict[str, object]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "type": "job.start",
        "job_id": "00000000-0000-0000-0000-000000000001",
        "operation": "convert_source",
        "source": {"kind": "direct_file", "path": "/tmp/metalfx-contract-source.mkv"},
        "destination": {"path": "/tmp/metalfx-contract-output"},
        "encoding": {
            "audio": {"mode": "convert_aac", "bitrate": 384, "preferred_language": None},
            "video": {
                "mode": "mv_hevc",
                "route_intent": "automatic",
                "quality_intent": {"mode": "ladder", "step": step_id, "mapping_version": 1},
                "direct_bitrate": {"mode": "automatic"},
                "generated_fallback": {
                    "eye_bitrate": {"mode": "automatic"},
                    "merge_quality": 75,
                },
            },
            "upscale": {"enabled": True, "quality": 75},
            "fov": 90,
            "frame_rate": "",
            "resolution": "",
            "crop_black_bars": False,
            "swap_eyes": False,
            "subtitles": {"mode": "off", "preferred_language": None},
        },
        "job": {
            "start_stage": 1,
            "keep_files": False,
            "overwrite": False,
            "remove_original": False,
            "continue_on_error": False,
            "software_encoder": False,
            "output_commands": False,
            "keep_awake": False,
        },
    }


def verify_worker_fallback_contract(plan: MetalFXConfirmationPlan) -> dict[str, object]:
    balanced_job = JobSpec.from_json_line(json.dumps(_worker_request("balanced")))
    encoding = balanced_job.encoding
    if encoding is None or encoding.video.generated_fallback is None:
        raise QualificationFailure("Balanced worker contract omitted generated fallback settings.")
    fallback = encoding.video.generated_fallback
    generated_values = _mapping(plan.fallback_policy["balanced_generated_values"], "Balanced generated values")
    if (
        fallback.eye_bitrate.mode.value != "automatic"
        or fallback.eye_bitrate.mbps is not None
        or fallback.merge_quality != generated_values["merge_quality"]
        or encoding.upscale.quality != plan.fallback_policy["balanced_file_upscale_quality"]
    ):
        raise QualificationFailure("Balanced worker fallback values changed.")
    rejected_step_ids: list[str] = []
    for step_id in (
        "space_saver",
        "compact",
        "efficient",
        "detailed",
        "high_detail",
        "maximum_detail",
    ):
        try:
            JobSpec.from_json_line(json.dumps(_worker_request(step_id)))
        except WorkerProtocolError:
            rejected_step_ids.append(step_id)
        else:
            raise QualificationFailure(f"Worker contract aliases unsupported quality step {step_id}.")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "mapping_version": 1,
        "supported_ladder_step_ids": ["balanced"],
        "generated_values": {
            "eye_bitrate_mbps": generated_values["eye_bitrate_mbps"],
            "merge_quality": fallback.merge_quality,
        },
        "file_upscale_quality": encoding.upscale.quality,
        "rejected_step_ids": rejected_step_ids,
        "unsupported_step_action": plan.fallback_policy["unsupported_step_action"],
        "aliases_forbidden": len(rejected_step_ids) == 6,
    }


def verify_source_receipts(
    plan: MetalFXConfirmationPlan,
    ordinary_direct_receipt: Path,
    balanced_quality_receipt: Path,
    balanced_routes_receipt: Path,
) -> dict[str, object]:
    ordinary = _verify_ordinary_direct_receipt(plan, ordinary_direct_receipt)
    quality = _verify_balanced_quality_receipt(plan, balanced_quality_receipt)
    routes = _verify_balanced_routes_receipt(plan, balanced_routes_receipt)
    worker_contract = verify_worker_fallback_contract(plan)
    if (
        quality["source_sha256"] != routes["source_sha256"]
        or quality["package_app_tree_sha256"] != routes["package_app_tree_sha256"]
    ):
        raise QualificationFailure("Balanced quality and route receipts do not bind the same package and source.")
    return {
        "ordinary_direct_confirmation": ordinary,
        "balanced_real_mvc_quality": quality,
        "balanced_packaged_routes": routes,
        "worker_fallback_contract": worker_contract,
    }


def _verify_fresh_production_sources(evidence: Mapping[str, object], source_receipts: Mapping[str, object]) -> None:
    ordinary = _mapping(source_receipts.get("ordinary_direct_confirmation"), "ordinary direct source receipt")
    expected_sources = _mapping(ordinary.get("production_sources"), "ordinary direct production sources")
    fresh_cases = {
        _string(case.get("id"), "fresh case id"): case
        for case in (_mapping(value, "fresh case") for value in _array(evidence.get("cases"), "fresh cases"))
    }
    for case_id in EXPECTED_PRODUCTION_CASE_IDS:
        fresh_case = _mapping(fresh_cases.get(case_id), f"fresh case {case_id}")
        fresh_source = _mapping(fresh_case.get("source"), "fresh source")
        if dict(fresh_source) != dict(_mapping(expected_sources.get(case_id), f"expected source {case_id}")):
            raise QualificationFailure(f"MetalFX fresh source identity changed for {case_id}.")


def evaluate_confirmation(
    plan: MetalFXConfirmationPlan,
    evidence: Mapping[str, object],
    source_receipts: Mapping[str, object],
) -> dict[str, object]:
    quality = _mapping(source_receipts.get("balanced_real_mvc_quality"), "Balanced quality source receipt")
    routes = _mapping(source_receipts.get("balanced_packaged_routes"), "Balanced routes source receipt")
    worker_contract = _mapping(source_receipts.get("worker_fallback_contract"), "worker fallback contract")
    fallback_ready = (
        quality.get("quality_passed") is True
        and quality.get("eye_order_passed") is True
        and routes.get("full_preview_parity") is True
        and routes.get("pre_input_fallback") is True
        and worker_contract.get("aliases_forbidden") is True
        and worker_contract.get("file_upscale_quality") == plan.fallback_policy["balanced_file_upscale_quality"]
    )
    result = evaluate_mapping_confirmation(
        plan,
        evidence,
        source_receipts,
        MappingEvaluationSpec(
            experiment_id=EXPECTED_EXPERIMENT_ID,
            case_ids=EXPECTED_CASE_IDS,
            balanced_candidate_id="q060",
            maximum_candidate_id="q075",
            maximum_size_ratio=plan.thresholds.maximum_q075_to_q060_size_ratio,
            selection_policy="fixed_contiguous_subset_containing_q060",
            expected_record_count=147,
            retained_anchor_artifacts={},
            downstream_checks={
                "generated_fallback_parity": {
                    "status": "completed",
                    "objective_stage_blocker": not fallback_ready,
                },
                "package_parity": {"status": "not_performed", "objective_stage_blocker": False},
                "perceptual_review": {"status": "not_performed", "objective_stage_blocker": False},
                "long_form_runtime": {"status": "not_performed", "objective_stage_blocker": False},
                "vision_pro_validation": {"status": "not_performed", "objective_stage_blocker": False},
                "signed_beta": {"status": "not_performed", "objective_stage_blocker": False},
            },
        ),
    )
    mapping_ready = (
        _mapping(result.get("acceptance"), "MetalFX mapping acceptance").get("objective_decision_ready") is True
    )
    result["target_id"] = "direct_mv_hevc_metalfx_2x"
    result["fallback_policy_confirmation"] = {
        "supported_ladder_step_ids": list(plan.fallback_policy["supported_ladder_step_ids"]),
        "unsupported_step_action": plan.fallback_policy["unsupported_step_action"],
        "aliases_forbidden": plan.fallback_policy["aliases_forbidden"],
        "custom_action": plan.fallback_policy["custom_action"],
        "matched_source_quality": dict(quality),
        "packaged_route_behavior": dict(routes),
        "worker_contract": dict(worker_contract),
        "decision_ready": fallback_ready,
    }
    acceptance = dict(_mapping(result.get("acceptance"), "MetalFX acceptance"))
    acceptance.update(
        {
            "mapping_objective_decision_ready": mapping_ready,
            "fallback_policy_decision_ready": fallback_ready,
            "objective_decision_ready": mapping_ready and fallback_ready,
            "passed": mapping_ready and fallback_ready,
        }
    )
    result["acceptance"] = acceptance
    return result


def run_confirmation(
    confirmation_plan_path: Path,
    ordinary_direct_receipt: Path,
    balanced_quality_receipt: Path,
    balanced_routes_receipt: Path,
    output_path: Path,
    work_directory: Path,
    encoder_path: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        initial_head = _git_head_from_clean_worktree()
        plan = load_confirmation_plan(confirmation_plan_path)
        source_receipts = verify_source_receipts(
            plan,
            ordinary_direct_receipt,
            balanced_quality_receipt,
            balanced_routes_receipt,
        )
        evidence = run_quality_sweep(
            plan.raw_sweep_path,
            output_path,
            work_directory,
            encoder_path,
            resume=resume,
        )
        raw_acceptance = _mapping(evidence.get("acceptance"), "fresh MetalFX sweep acceptance")
        if raw_acceptance.get("complete") is not True:
            return evidence
        _verify_fresh_production_sources(evidence, source_receipts)
        if _git_head_from_clean_worktree() != initial_head:
            raise QualificationFailure("repository identity changed during MetalFX confirmation.")
        final_plan = load_confirmation_plan(confirmation_plan_path)
        final_sources = verify_source_receipts(
            final_plan,
            ordinary_direct_receipt,
            balanced_quality_receipt,
            balanced_routes_receipt,
        )
        if final_plan != plan or final_sources != source_receipts:
            raise QualificationFailure("MetalFX confirmation plan or provenance changed before finalization.")
        confirmation = evaluate_confirmation(plan, evidence, source_receipts)
        _finalize_receipt(output_path, evidence, confirmation)
        return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the preregistered direct MetalFX 2x mapping confirmation.")
    parser.add_argument("--confirmation-plan", type=Path, default=DEFAULT_CONFIRMATION_PLAN)
    parser.add_argument("--ordinary-direct-receipt", type=Path, required=True)
    parser.add_argument("--balanced-quality-receipt", type=Path, required=True)
    parser.add_argument("--balanced-routes-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-directory", type=Path, required=True)
    parser.add_argument("--encoder", type=Path, default=DEFAULT_ENCODER)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_confirmation(
            args.confirmation_plan.resolve(),
            args.ordinary_direct_receipt.absolute(),
            args.balanced_quality_receipt.absolute(),
            args.balanced_routes_receipt.absolute(),
            args.output.absolute(),
            args.work_directory.absolute(),
            args.encoder.resolve(),
            resume=args.resume,
        )
        return exit_code_for_confirmation(evidence)
    except KeyboardInterrupt:
        print("Direct MetalFX mapping confirmation interrupted; resume the saved checkpoint.", file=sys.stderr)
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
        print(f"Direct MetalFX mapping confirmation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
