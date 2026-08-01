#!/usr/bin/env python3

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import stat
import statistics
import subprocess
import sys

from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Mapping, Sequence

from bd_to_avp.modules.video_quality_defaults import AUTOMATIC_DIRECT_QUALITY
from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_quality_sweep import (
    DEFAULT_ENCODER,
    _candidate_record,
    _git_head_from_clean_worktree,
    _atomic_write,
    _refresh_summaries,
    _require_head_tracked_file,
    _validate_resume_cases,
    load_sweep_plan,
    run_quality_sweep,
)
from scripts.qualify_generated_mv_hevc_calibration import calibration_lock
from scripts.qualify_mv_hevc_corpus import load_manifest
from scripts.qualify_mv_hevc_quality_match import sha256_file


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIRMATION_PLAN = REPOSITORY_ROOT / "docs/qualification/direct-mv-hevc-quality-mapping-confirmation-v1.json"
EXPECTED_EXPERIMENT_ID = "direct-mv-hevc-quality-mapping-confirmation-v1"
EXPECTED_CASE_IDS = (
    "production-dark",
    "production-grain-rain",
    "production-snow-detail",
    "production-motion",
    "production-crop",
    "production-rate-override",
    "synthetic-animation",
)
EXPECTED_CANDIDATES = (
    ("space_saver", "q040", 0.4),
    ("compact", "q050", 0.5),
    ("efficient", "q060", 0.6),
    ("balanced", "q070", 0.7),
    ("detailed", "q075", 0.75),
    ("high_detail", "q080", 0.8),
    ("maximum_detail", "q085", 0.85),
)
EXPECTED_SOURCE_RECEIPT_IDS = ("coarse", "upper", "automated_full_length_anchor")


@dataclass(frozen=True)
class ConfirmationCandidate:
    step_id: str
    candidate_id: str
    quality: float


@dataclass(frozen=True)
class ConfirmationThresholds:
    maximum_repeat_ssim_spread: float
    maximum_repeat_size_ratio_spread: float
    maximum_q085_to_q070_size_ratio: float
    minimum_case_median_storage_growth_ratio: float
    minimum_case_median_ssim_delta: float
    minimum_corpus_median_ssim_improvement: float
    real_case_ssim_threshold: float
    minimum_real_case_clear_count: int
    sensitive_case_ids: tuple[str, ...]
    minimum_sensitive_case_clear_count: int


@dataclass(frozen=True)
class ConfirmationPlan:
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
    thresholds: ConfirmationThresholds


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationFailure(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QualificationFailure(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise QualificationFailure(f"{label} must be a non-empty string.")
    return value


def _number(value: object, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise QualificationFailure(f"{label} must be a number.")
    return float(value)


def _integer(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise QualificationFailure(f"{label} must be an integer.")
    return value


def _sha256(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise QualificationFailure(f"{label} must be a lowercase SHA-256 identity.")
    return digest


def _repository_path(value: object, label: str) -> Path:
    relative_path = Path(_string(value, label))
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise QualificationFailure(f"{label} must be a safe repository-relative path.")
    resolved = (REPOSITORY_ROOT / relative_path).resolve()
    try:
        resolved.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise QualificationFailure(f"{label} escapes the repository.") from error
    return resolved


def parse_confirmation_plan(raw: object) -> ConfirmationPlan:
    document = _mapping(raw, "confirmation plan")
    if document.get("schema_version") != 1:
        raise QualificationFailure("confirmation plan schema_version must be 1.")
    if document.get("experiment_id") != EXPECTED_EXPERIMENT_ID:
        raise QualificationFailure("confirmation plan experiment_id is unsupported.")
    if document.get("target_id") != "direct_mv_hevc":
        raise QualificationFailure("confirmation plan target_id must be direct_mv_hevc.")
    if document.get("purpose") != "fresh_objective_confirmation_not_public_mapping":
        raise QualificationFailure("confirmation plan purpose is unsupported.")

    raw_sweep = _mapping(document.get("raw_sweep_plan"), "raw_sweep_plan")
    raw_sweep_path = _repository_path(raw_sweep.get("path"), "raw_sweep_plan.path")
    raw_sweep_sha256 = _sha256(raw_sweep.get("sha256"), "raw_sweep_plan.sha256")
    raw_sweep_id = _string(raw_sweep.get("sweep_id"), "raw_sweep_plan.sweep_id")
    if raw_sweep_id != "direct-mv-hevc-quality-confirmation-v1":
        raise QualificationFailure("confirmation raw sweep ID changed.")

    raw_receipts = _mapping(document.get("source_receipts"), "source_receipts")
    if tuple(raw_receipts) != EXPECTED_SOURCE_RECEIPT_IDS:
        raise QualificationFailure("confirmation source receipt set changed.")
    source_receipts: dict[str, Mapping[str, object]] = {}
    for receipt_id in EXPECTED_SOURCE_RECEIPT_IDS:
        receipt = _mapping(raw_receipts.get(receipt_id), f"source_receipts.{receipt_id}")
        if receipt.get("required_file_mode") != "0444" or receipt.get("records_used_for_confirmation") is not False:
            raise QualificationFailure(f"source_receipts.{receipt_id} must remain frozen provenance only.")
        _sha256(receipt.get("sha256"), f"source_receipts.{receipt_id}.sha256")
        _mapping(receipt.get("identity"), f"source_receipts.{receipt_id}.identity")
        source_receipts[receipt_id] = receipt

    corpus = _mapping(document.get("corpus"), "corpus")
    corpus_path = _repository_path(corpus.get("path"), "corpus.path")
    corpus_id = _string(corpus.get("corpus_id"), "corpus.corpus_id")
    corpus_sha256 = _sha256(corpus.get("sha256"), "corpus.sha256")
    selected_case_ids = tuple(
        _string(case_id, "corpus.selected_case_ids")
        for case_id in _array(corpus.get("selected_case_ids"), "corpus.selected_case_ids")
    )
    if selected_case_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("confirmation quality-gated case set changed.")

    public = _mapping(document.get("public_contract_bindings"), "public_contract_bindings")
    if tuple(public) != ("ladder_manifest", "video_quality_swift"):
        raise QualificationFailure("confirmation public binding set changed.")
    public_bindings: dict[str, Mapping[str, str]] = {}
    for binding_id, expected_path in (
        ("ladder_manifest", "docs/qualification/video-quality-ladder-v1.json"),
        ("video_quality_swift", "macos/BluRayToVisionPro/Models/VideoQuality.swift"),
    ):
        binding = _mapping(public.get(binding_id), f"public_contract_bindings.{binding_id}")
        path = _string(binding.get("path"), f"public_contract_bindings.{binding_id}.path")
        if path != expected_path:
            raise QualificationFailure(f"public_contract_bindings.{binding_id}.path changed.")
        public_bindings[binding_id] = {
            "path": path,
            "sha256": _sha256(binding.get("sha256"), f"public_contract_bindings.{binding_id}.sha256"),
        }

    balanced = _mapping(document.get("balanced"), "balanced")
    if (
        balanced.get("candidate_id") != "q070"
        or balanced.get("quality") != AUTOMATIC_DIRECT_QUALITY
        or balanced.get("source") != "bd_to_avp.modules.video_quality_defaults.AUTOMATIC_DIRECT_QUALITY"
    ):
        raise QualificationFailure("confirmation Balanced must remain production q070.")
    if document.get("runs_per_candidate") != 3:
        raise QualificationFailure("confirmation requires exactly three runs per candidate.")

    candidates = tuple(
        ConfirmationCandidate(
            step_id=_string(_mapping(candidate, "candidate").get("step_id"), "candidate.step_id"),
            candidate_id=_string(_mapping(candidate, "candidate").get("id"), "candidate.id"),
            quality=_number(_mapping(candidate, "candidate").get("quality"), "candidate.quality"),
        )
        for candidate in _array(document.get("candidates"), "candidates")
    )
    if (
        tuple((candidate.step_id, candidate.candidate_id, candidate.quality) for candidate in candidates)
        != EXPECTED_CANDIDATES
    ):
        raise QualificationFailure("confirmation candidate mapping changed.")

    technical = _mapping(document.get("technical_eligibility"), "technical_eligibility")
    boundary = _mapping(document.get("adjacent_boundary_policy"), "adjacent_boundary_policy")
    thresholds = ConfirmationThresholds(
        maximum_repeat_ssim_spread=_number(technical.get("maximum_repeat_ssim_spread"), "maximum_repeat_ssim_spread"),
        maximum_repeat_size_ratio_spread=_number(
            technical.get("maximum_repeat_size_ratio_spread"), "maximum_repeat_size_ratio_spread"
        ),
        maximum_q085_to_q070_size_ratio=_number(
            technical.get("maximum_q085_to_q070_size_ratio"), "maximum_q085_to_q070_size_ratio"
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
        sensitive_case_ids=tuple(
            _string(case_id, "sensitive_case_ids")
            for case_id in _array(boundary.get("sensitive_case_ids"), "sensitive_case_ids")
        ),
        minimum_sensitive_case_clear_count=_integer(
            boundary.get("minimum_sensitive_case_clear_count"), "minimum_sensitive_case_clear_count"
        ),
    )
    if thresholds != ConfirmationThresholds(
        0.0002, 0.02, 4.9, 0.005, -0.0002, 0.0001, 0.0001, 2, ("production-grain-rain", "production-snow-detail"), 1
    ):
        raise QualificationFailure("confirmation thresholds changed.")
    if (
        technical.get("all_147_raw_records_required") is not True
        or technical.get("eye_order_case_minimums_required") is not True
        or boundary.get("required_boundary_count") != 6
        or boundary.get("strict_storage_increase_in_every_paired_repeat") is not True
        or any(
            boundary.get(key) is not True
            for key in (
                "threshold_changes_forbidden",
                "interpolation_forbidden",
                "aliases_forbidden",
                "post_hoc_candidates_forbidden",
            )
        )
        or boundary.get("failed_boundary_action") != "collapse"
    ):
        raise QualificationFailure("confirmation technical or boundary policy changed.")
    selection = _mapping(document.get("selection_policy"), "selection_policy")
    decision = _mapping(document.get("decision_policy"), "decision_policy")
    artifact = _mapping(document.get("artifact_policy"), "artifact_policy")
    if (
        selection.get("primary") != "fixed_contiguous_subset_containing_q070"
        or selection.get("all_seven_required_for_positive_confirmation") is not True
        or selection.get("candidate_step_assignments_fixed_before_run") is not True
        or selection.get("missing_slots") != "unsupported"
        or artifact.get("fresh_output_sha256_recorded_for_every_run") is not True
        or artifact.get("fresh_outputs_ephemeral") is not True
        or artifact.get("retained_full_length_anchor_artifacts_bound_from_source_receipt") != ["q040", "q070", "q085"]
        or decision.get("public_mapping_changes_forbidden") is not True
        or decision.get("ordinary_direct_objective_confirmation_only") is not True
        or any(
            decision.get(key) is not False
            for key in (
                "metalfx_direct_performed",
                "generated_fallback_parity_performed",
                "package_parity_performed",
                "perceptual_review_performed",
                "long_form_runtime_performed",
                "vision_pro_validation_performed",
                "signed_beta_performed",
            )
        )
    ):
        raise QualificationFailure("confirmation selection, artifact, or decision policy changed.")

    return ConfirmationPlan(
        relative_path=None,
        sha256=None,
        raw_sweep_path=raw_sweep_path,
        raw_sweep_sha256=raw_sweep_sha256,
        raw_sweep_id=raw_sweep_id,
        source_receipts=source_receipts,
        corpus_path=corpus_path,
        corpus_id=corpus_id,
        corpus_sha256=corpus_sha256,
        public_bindings=public_bindings,
        candidates=candidates,
        thresholds=thresholds,
    )


def load_confirmation_plan(path: Path) -> ConfirmationPlan:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure("confirmation plan must be inside the repository.") from error
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"could not read confirmation plan: {error}") from error
    plan = parse_confirmation_plan(raw)
    _require_head_tracked_file(resolved, "Direct mapping-confirmation plan")
    if sha256_file(plan.raw_sweep_path) != plan.raw_sweep_sha256:
        raise QualificationFailure("raw confirmation sweep plan SHA-256 changed.")
    raw_sweep, raw_sweep_sha256 = load_sweep_plan(plan.raw_sweep_path)
    if raw_sweep_sha256 != plan.raw_sweep_sha256 or raw_sweep.sweep_id != plan.raw_sweep_id:
        raise QualificationFailure("raw confirmation sweep plan identity changed.")
    if tuple((candidate.candidate_id, candidate.quality) for candidate in raw_sweep.candidates) != tuple(
        (candidate.candidate_id, candidate.quality) for candidate in plan.candidates
    ):
        raise QualificationFailure("raw confirmation sweep candidate set changed.")
    if raw_sweep.runs_per_candidate != 3 or raw_sweep.balanced_quality != AUTOMATIC_DIRECT_QUALITY:
        raise QualificationFailure("raw confirmation sweep execution contract changed.")
    if sha256_file(plan.corpus_path) != plan.corpus_sha256:
        raise QualificationFailure("confirmation corpus SHA-256 changed.")
    manifest = load_manifest(plan.corpus_path)
    gated_case_ids = tuple(case.case_id for case in manifest.cases if case.quality_gate)
    if manifest.corpus_id != plan.corpus_id or gated_case_ids != EXPECTED_CASE_IDS:
        raise QualificationFailure("confirmation corpus identity or gated cases changed.")
    for binding in plan.public_bindings.values():
        binding_path = _repository_path(binding["path"], "public binding path")
        _require_head_tracked_file(binding_path, "Direct mapping-confirmation public contract")
        if sha256_file(binding_path) != binding["sha256"]:
            raise QualificationFailure("a bound public contract changed before confirmation.")
    return ConfirmationPlan(
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
    )


def _read_frozen_json(path: Path, expected_sha256: str, label: str) -> Mapping[str, object]:
    _reject_symlink_components(path, label)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise QualificationFailure(f"could not open {label} safely.") from error
    with os.fdopen(descriptor, "rb") as receipt_file:
        receipt_stat = os.fstat(receipt_file.fileno())
        data = receipt_file.read()
    if not stat.S_ISREG(receipt_stat.st_mode) or stat.S_IMODE(receipt_stat.st_mode) != 0o444:
        raise QualificationFailure(f"{label} must be a regular file frozen at mode 0444.")
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise QualificationFailure(f"{label} SHA-256 does not match preregistration.")
    try:
        document = json.loads(data)
    except json.JSONDecodeError as error:
        raise QualificationFailure(f"{label} is not valid JSON.") from error
    return _mapping(document, label)


def _reject_symlink_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    if absolute.is_symlink() or absolute.parent.is_symlink():
        raise QualificationFailure(f"{label} must not use symlinks.")


def _verify_sweep_receipt(
    plan: ConfirmationPlan,
    receipt_id: str,
    path: Path,
) -> dict[str, object]:
    binding = plan.source_receipts[receipt_id]
    identity = _mapping(binding.get("identity"), f"{receipt_id} identity")
    receipt = _read_frozen_json(path, _sha256(binding.get("sha256"), f"{receipt_id} sha256"), receipt_id)
    source_plan_path = _repository_path(identity.get("plan_path"), f"{receipt_id} plan path")
    source_plan, source_plan_sha256 = load_sweep_plan(source_plan_path)
    if source_plan_sha256 != identity.get("plan_sha256"):
        raise QualificationFailure(f"{receipt_id} source plan SHA-256 changed.")
    if (
        receipt.get("schema_version") != identity.get("schema_version")
        or receipt.get("sweep_id") != identity.get("sweep_id")
        or receipt.get("source_tree_dirty") is not False
        or receipt.get("sweep_plan")
        != {
            "path": source_plan.relative_path,
            "sha256": source_plan_sha256,
        }
    ):
        raise QualificationFailure(f"{receipt_id} source receipt identity changed.")
    if receipt.get("selected_case_ids") != list(EXPECTED_CASE_IDS):
        raise QualificationFailure(f"{receipt_id} source receipt is not the full gated corpus.")
    expected_candidates = [
        {"id": candidate.candidate_id, "quality": candidate.quality} for candidate in source_plan.candidates
    ]
    if receipt.get("candidates") != expected_candidates:
        raise QualificationFailure(f"{receipt_id} source receipt candidate identity changed.")
    manifest = load_manifest(source_plan.corpus_path)
    definitions = {case.case_id: case for case in manifest.cases if case.quality_gate}
    _validate_resume_cases(receipt, source_plan, EXPECTED_CASE_IDS)
    recomputed = copy.deepcopy(dict(receipt))
    _refresh_summaries(recomputed, source_plan, definitions, all_gated_case_ids=set(definitions))
    for key in ("candidate_summaries", "monotonicity_warnings", "acceptance"):
        if recomputed.get(key) != receipt.get(key):
            raise QualificationFailure(f"{receipt_id} source receipt {key} contradicts raw records.")
    acceptance = _mapping(receipt.get("acceptance"), f"{receipt_id} acceptance")
    if (
        acceptance.get("passed") is not True
        or acceptance.get("ladder_evidence_ready") is not False
        or acceptance.get("ladder_mapping_selected") is not False
    ):
        raise QualificationFailure(f"{receipt_id} source receipt is not accepted exploratory evidence.")
    return {
        "sha256": binding["sha256"],
        "source_git_sha": receipt.get("source_git_sha"),
        "plan": receipt.get("sweep_plan"),
        "raw_record_count": len(EXPECTED_CASE_IDS) * source_plan.runs_per_candidate * len(source_plan.candidates),
        "records_used_for_confirmation": False,
        "verified": True,
    }


def _verify_anchor_receipt(plan: ConfirmationPlan, path: Path) -> dict[str, object]:
    binding = plan.source_receipts["automated_full_length_anchor"]
    identity = _mapping(binding.get("identity"), "anchor identity")
    receipt = _read_frozen_json(
        path,
        _sha256(binding.get("sha256"), "anchor sha256"),
        "automated full-length anchor receipt",
    )
    plan_path = _repository_path(identity.get("plan_path"), "anchor plan path")
    if sha256_file(plan_path) != identity.get("plan_sha256"):
        raise QualificationFailure("anchor source plan SHA-256 changed.")
    if (
        receipt.get("schema_version") != identity.get("schema_version")
        or receipt.get("qualification_id") != identity.get("qualification_id")
        or receipt.get("source_tree_dirty") is not False
        or receipt.get("plan") != {"path": identity.get("plan_path"), "sha256": identity.get("plan_sha256")}
    ):
        raise QualificationFailure("anchor source receipt identity changed.")
    acceptance = _mapping(receipt.get("acceptance"), "anchor acceptance")
    if (
        acceptance.get("complete") is not True
        or acceptance.get("automated_passed") is not True
        or acceptance.get("ladder_mapping_selected") is not False
        or acceptance.get("signed_package_evidence") is not False
    ):
        raise QualificationFailure("anchor source receipt is not accepted automated provenance.")
    artifacts: list[dict[str, object]] = []
    for candidate in _array(receipt.get("candidates"), "anchor candidates"):
        candidate_record = _mapping(candidate, "anchor candidate")
        artifact = _mapping(candidate_record.get("artifact"), "anchor artifact")
        validation = _mapping(artifact.get("validation"), "anchor artifact validation")
        if candidate_record.get("status") != "complete" or not all(value is True for value in validation.values()):
            raise QualificationFailure("anchor source receipt contains an invalid candidate artifact.")
        artifacts.append(
            {
                "candidate_id": candidate_record.get("id"),
                "quality": candidate_record.get("quality"),
                "sha256": _sha256(artifact.get("sha256"), "anchor artifact sha256"),
                "bytes": _integer(artifact.get("size_bytes"), "anchor artifact size_bytes"),
            }
        )
    if [(artifact["candidate_id"], artifact["quality"]) for artifact in artifacts] != [
        ("q070", 0.7),
        ("q085", 0.85),
        ("q040", 0.4),
    ]:
        raise QualificationFailure("anchor candidate identities changed.")
    return {
        "sha256": binding["sha256"],
        "source_git_sha": receipt.get("source_git_sha"),
        "plan": receipt.get("plan"),
        "retained_artifacts": artifacts,
        "records_used_for_confirmation": False,
        "verified": True,
    }


def verify_source_receipts(
    plan: ConfirmationPlan,
    coarse_receipt: Path,
    upper_receipt: Path,
    anchor_receipt: Path,
) -> dict[str, object]:
    return {
        "coarse": _verify_sweep_receipt(plan, "coarse", coarse_receipt),
        "upper": _verify_sweep_receipt(plan, "upper", upper_receipt),
        "automated_full_length_anchor": _verify_anchor_receipt(plan, anchor_receipt),
    }


def _candidate_runs(case: Mapping[str, object], candidate_id: str) -> list[Mapping[str, object]]:
    candidate = _candidate_record(case, candidate_id)
    if candidate is None:
        raise QualificationFailure(f"fresh receipt is missing candidate {candidate_id}.")
    runs = _array(candidate.get("runs"), f"{candidate_id} runs")
    if len(runs) != 3 or any(not isinstance(run, Mapping) for run in runs):
        raise QualificationFailure(f"fresh receipt candidate {candidate_id} does not contain three runs.")
    return sorted((run for run in runs if isinstance(run, Mapping)), key=lambda run: int(run["run_index"]))


def evaluate_confirmation(
    plan: ConfirmationPlan,
    evidence: Mapping[str, object],
    source_receipts: Mapping[str, object],
) -> dict[str, object]:
    raw_acceptance = _mapping(evidence.get("acceptance"), "fresh sweep acceptance")
    complete = raw_acceptance.get("complete") is True and raw_acceptance.get("full_quality_gated_corpus") is True
    cases = _array(evidence.get("cases"), "fresh sweep cases")
    if [case.get("id") for case in cases if isinstance(case, Mapping)] != list(EXPECTED_CASE_IDS):
        raise QualificationFailure("fresh sweep case order or identity changed.")
    if evidence.get("candidates") != [
        {"id": candidate.candidate_id, "quality": candidate.quality} for candidate in plan.candidates
    ]:
        raise QualificationFailure("fresh sweep candidate identity changed.")
    record_count = sum(
        len(_array(_mapping(candidate, "fresh candidate").get("runs"), "fresh candidate runs"))
        for case in cases
        for candidate in _array(_mapping(case, "fresh case").get("candidates"), "fresh case candidates")
    )

    raw_summaries = {
        str(summary.get("id")): summary
        for summary in _array(evidence.get("candidate_summaries"), "fresh candidate summaries")
        if isinstance(summary, Mapping)
    }
    candidate_summaries: list[dict[str, object]] = []
    eligible_ids: set[str] = set()
    for candidate in plan.candidates:
        summary = _mapping(raw_summaries.get(candidate.candidate_id), f"{candidate.candidate_id} summary")
        failures: list[str] = []
        if summary.get("complete") is not True:
            failures.append("incomplete")
        if (
            _number(summary.get("maximum_repeat_ssim_spread"), "repeat SSIM spread")
            > plan.thresholds.maximum_repeat_ssim_spread
        ):
            failures.append("repeat_ssim_spread")
        if (
            _number(summary.get("maximum_repeat_size_ratio_spread"), "repeat size spread")
            > plan.thresholds.maximum_repeat_size_ratio_spread
        ):
            failures.append("repeat_size_ratio_spread")
        if (
            candidate.candidate_id == "q085"
            and _number(summary.get("output_size_ratio"), "q085 output size ratio")
            > plan.thresholds.maximum_q085_to_q070_size_ratio
        ):
            failures.append("q085_size_cap")
        if raw_acceptance.get("eye_order_passed") is not True:
            failures.append("eye_order")
        eligible = complete and not failures
        if eligible:
            eligible_ids.add(candidate.candidate_id)
        candidate_summaries.append(
            {
                "step_id": candidate.step_id,
                "candidate_id": candidate.candidate_id,
                "quality": candidate.quality,
                "technically_eligible": eligible,
                "failure_reasons": failures,
                "quality_delta_vs_q070": summary.get("quality_delta"),
                "output_size_ratio_vs_q070": summary.get("output_size_ratio"),
                "median_encode_time_ratio_vs_q070": summary.get("median_encode_time_ratio"),
                "maximum_repeat_ssim_spread": summary.get("maximum_repeat_ssim_spread"),
                "maximum_repeat_size_ratio_spread": summary.get("maximum_repeat_size_ratio_spread"),
                "minimum_eye_order_margin": summary.get("minimum_eye_order_margin"),
            }
        )

    boundaries: list[dict[str, object]] = []
    for lower, higher in pairwise(plan.candidates):
        per_case: list[dict[str, object]] = []
        for case_value in cases:
            case = _mapping(case_value, "fresh case")
            case_id = _string(case.get("id"), "fresh case id")
            lower_candidate = _mapping(_candidate_record(case, lower.candidate_id), "lower candidate")
            higher_candidate = _mapping(_candidate_record(case, higher.candidate_id), "higher candidate")
            lower_summary = _mapping(lower_candidate.get("summary"), "lower summary")
            higher_summary = _mapping(higher_candidate.get("summary"), "higher summary")
            quality_delta = _number(higher_summary.get("median_min_same_eye_ssim"), "higher quality") - _number(
                lower_summary.get("median_min_same_eye_ssim"), "lower quality"
            )
            storage_growth = (
                _number(higher_summary.get("median_final_bytes"), "higher bytes")
                / _number(lower_summary.get("median_final_bytes"), "lower bytes")
                - 1
            )
            lower_runs = _candidate_runs(case, lower.candidate_id)
            higher_runs = _candidate_runs(case, higher.candidate_id)
            strict_storage = all(
                _integer(higher_run.get("final_bytes"), "higher paired bytes")
                > _integer(lower_run.get("final_bytes"), "lower paired bytes")
                for lower_run, higher_run in zip(lower_runs, higher_runs, strict=True)
            )
            tags = _array(case.get("tags"), "fresh case tags")
            per_case.append(
                {
                    "case_id": case_id,
                    "quality_delta": quality_delta,
                    "storage_growth_ratio": storage_growth,
                    "strict_paired_storage_growth": strict_storage,
                    "quality_non_inferiority_passed": quality_delta >= plan.thresholds.minimum_case_median_ssim_delta,
                    "objective_quality_clear": quality_delta >= plan.thresholds.real_case_ssim_threshold,
                    "real_case": "real_mvc" in tags,
                    "sensitive_case": case_id in plan.thresholds.sensitive_case_ids,
                }
            )
        quality_deltas = [_number(case.get("quality_delta"), "boundary quality delta") for case in per_case]
        storage_growths = [_number(case.get("storage_growth_ratio"), "boundary storage growth") for case in per_case]
        corpus_median = statistics.median(quality_deltas)
        real_clear_count = sum(
            case.get("real_case") is True and case.get("objective_quality_clear") is True for case in per_case
        )
        sensitive_clear_count = sum(
            case.get("sensitive_case") is True and case.get("objective_quality_clear") is True for case in per_case
        )
        storage_passed = all(
            case.get("strict_paired_storage_growth") is True
            and _number(case.get("storage_growth_ratio"), "boundary storage growth")
            >= plan.thresholds.minimum_case_median_storage_growth_ratio
            for case in per_case
        )
        noninferiority_passed = all(case.get("quality_non_inferiority_passed") is True for case in per_case)
        distinction_passed = (
            corpus_median >= plan.thresholds.minimum_corpus_median_ssim_improvement
            and real_clear_count >= plan.thresholds.minimum_real_case_clear_count
            and sensitive_clear_count >= plan.thresholds.minimum_sensitive_case_clear_count
        )
        failure_reasons = []
        if not storage_passed:
            failure_reasons.append("storage")
        if not noninferiority_passed:
            failure_reasons.append("quality_non_inferiority")
        if not distinction_passed:
            failure_reasons.append("objective_distinction")
        boundaries.append(
            {
                "lower_candidate_id": lower.candidate_id,
                "higher_candidate_id": higher.candidate_id,
                "minimum_case_quality_delta": min(quality_deltas),
                "corpus_median_quality_delta": corpus_median,
                "minimum_case_storage_growth_ratio": min(storage_growths),
                "real_case_clear_count": real_clear_count,
                "sensitive_case_clear_count": sensitive_clear_count,
                "storage_passed": storage_passed,
                "quality_non_inferiority_passed": noninferiority_passed,
                "objective_distinction_passed": distinction_passed,
                "boundary_passed": not failure_reasons,
                "failure_reasons": failure_reasons,
                "per_case": per_case,
            }
        )

    boundary_by_pair = {
        (str(boundary["lower_candidate_id"]), str(boundary["higher_candidate_id"])): boundary for boundary in boundaries
    }
    candidate_ids = [candidate.candidate_id for candidate in plan.candidates]
    balanced_index = candidate_ids.index("q070")
    selected = ["q070"] if "q070" in eligible_ids else []
    if selected:
        for index in range(balanced_index - 1, -1, -1):
            pair = (candidate_ids[index], candidate_ids[index + 1])
            if candidate_ids[index] not in eligible_ids or boundary_by_pair[pair]["boundary_passed"] is not True:
                break
            selected.insert(0, candidate_ids[index])
        for index in range(balanced_index + 1, len(candidate_ids)):
            pair = (candidate_ids[index - 1], candidate_ids[index])
            if candidate_ids[index] not in eligible_ids or boundary_by_pair[pair]["boundary_passed"] is not True:
                break
            selected.append(candidate_ids[index])
    selected_set = set(selected)
    provisional_mappings = [
        {
            "step_id": candidate.step_id,
            "status": "provisional_objective_confirmation" if candidate.candidate_id in selected_set else "unsupported",
            "candidate_id": candidate.candidate_id if candidate.candidate_id in selected_set else None,
            "values": {"quality": candidate.quality} if candidate.candidate_id in selected_set else None,
        }
        for candidate in plan.candidates
    ]
    all_boundaries_passed = len(boundaries) == 6 and all(boundary["boundary_passed"] is True for boundary in boundaries)
    all_candidates_selected = selected == candidate_ids
    technical_passed = len(eligible_ids) == len(plan.candidates)
    objective_ready = (
        complete
        and record_count == 147
        and raw_acceptance.get("execution_passed") is True
        and technical_passed
        and all_boundaries_passed
        and all_candidates_selected
    )
    return {
        "schema_version": 1,
        "experiment_id": EXPECTED_EXPERIMENT_ID,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan.sha256},
        "completed_at": evidence.get("updated_at"),
        "source_receipts": dict(source_receipts),
        "method": {
            "candidate_order": "ascending_even_runs_descending_odd_runs",
            "runs_per_candidate": 3,
            "quality_metric": "minimum decoded same-eye SSIM",
            "repeatability_limits": {
                "maximum_repeat_ssim_spread": plan.thresholds.maximum_repeat_ssim_spread,
                "maximum_repeat_size_ratio_spread": plan.thresholds.maximum_repeat_size_ratio_spread,
            },
            "q085_size_cap_ratio_vs_q070": plan.thresholds.maximum_q085_to_q070_size_ratio,
            "adjacent_boundary_policy": {
                "minimum_case_median_storage_growth_ratio": (plan.thresholds.minimum_case_median_storage_growth_ratio),
                "minimum_case_median_ssim_delta": plan.thresholds.minimum_case_median_ssim_delta,
                "minimum_corpus_median_ssim_improvement": (plan.thresholds.minimum_corpus_median_ssim_improvement),
                "real_case_ssim_threshold": plan.thresholds.real_case_ssim_threshold,
                "minimum_real_case_clear_count": plan.thresholds.minimum_real_case_clear_count,
                "sensitive_case_ids": list(plan.thresholds.sensitive_case_ids),
                "minimum_sensitive_case_clear_count": plan.thresholds.minimum_sensitive_case_clear_count,
                "failed_boundary_action": "collapse",
                "aliases_forbidden": True,
            },
            "selection_policy": "fixed_contiguous_subset_containing_q070",
            "all_seven_required_for_positive_confirmation": True,
        },
        "raw_sweep": {
            "plan": evidence.get("sweep_plan"),
            "sweep_id": evidence.get("sweep_id"),
            "source_git_sha": evidence.get("source_git_sha"),
            "record_count": record_count,
            "fresh_output_sha256_record_count": record_count,
            "acceptance": dict(raw_acceptance),
        },
        "candidate_summaries": candidate_summaries,
        "boundary_evaluations": boundaries,
        "selected_subset": {
            "candidate_ids": selected,
            "cardinality": len(selected),
            "contains_balanced": "q070" in selected_set,
            "selection_policy": "fixed_contiguous_subset_containing_q070",
        }
        if selected
        else None,
        "provisional_mappings": provisional_mappings,
        "artifact_policy": {
            "fresh_outputs_ephemeral": True,
            "fresh_output_sha256_recorded_for_every_run": record_count == 147,
            "retained_full_length_anchor_artifacts": source_receipts["automated_full_length_anchor"],
        },
        "downstream_checks": {
            name: {"status": "not_performed", "objective_stage_blocker": False}
            for name in (
                "metalfx_direct",
                "generated_fallback_parity",
                "package_parity",
                "perceptual_review",
                "long_form_runtime",
                "vision_pro_validation",
                "signed_beta",
            )
        },
        "acceptance": {
            "complete": complete,
            "finalized": complete,
            "source_receipts_verified": True,
            "record_count": record_count,
            "expected_record_count": 147,
            "technical_eligibility_passed": technical_passed,
            "adjacent_boundary_count": len(boundaries),
            "all_adjacent_boundaries_passed": all_boundaries_passed,
            "all_seven_candidates_selected": all_candidates_selected,
            "objective_decision_ready": objective_ready,
            "provisional_mappings_only": True,
            "public_mapping_changes_forbidden": True,
            "ladder_mapping_selected": False,
            "passed": objective_ready,
        },
    }


def _canonical_bytes(evidence: Mapping[str, object]) -> bytes:
    return (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode()


def _finalize_receipt(output_path: Path, evidence: dict[str, object], confirmation: Mapping[str, object]) -> None:
    _reject_symlink_components(output_path, "fresh confirmation receipt")
    output_stat = output_path.stat()
    if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_nlink != 1:
        raise QualificationFailure("fresh confirmation receipt must be one regular file.")
    existing = evidence.get("confirmation")
    if existing is not None and existing != confirmation:
        raise QualificationFailure("finalized confirmation contradicts the fresh raw records.")
    evidence["confirmation"] = dict(confirmation)
    canonical = _canonical_bytes(evidence)
    mode = stat.S_IMODE(output_path.stat().st_mode)
    if existing is not None:
        if output_path.read_bytes() != canonical:
            raise QualificationFailure("finalized confirmation receipt is not canonical JSON.")
        if mode not in {0o444, 0o644}:
            raise QualificationFailure("finalized confirmation receipt has an unsafe mode.")
        if mode == 0o644:
            output_path.chmod(0o444)
        return
    if not (mode & 0o200):
        raise QualificationFailure("fresh confirmation receipt is unexpectedly read-only before finalization.")
    _atomic_write(output_path, evidence)
    if output_path.read_bytes() != canonical or not stat.S_ISREG(output_path.stat().st_mode):
        raise QualificationFailure("fresh confirmation receipt finalization was not atomic and canonical.")
    output_path.chmod(0o444)


def run_confirmation(
    confirmation_plan_path: Path,
    coarse_receipt: Path,
    upper_receipt: Path,
    anchor_receipt: Path,
    output_path: Path,
    work_directory: Path,
    encoder_path: Path,
    *,
    resume: bool,
) -> dict[str, object]:
    with calibration_lock(output_path, work_directory):
        _reject_symlink_components(output_path, "fresh confirmation receipt")
        _reject_symlink_components(work_directory, "fresh confirmation work directory")
        initial_head = _git_head_from_clean_worktree()
        plan = load_confirmation_plan(confirmation_plan_path)
        source_receipts = verify_source_receipts(plan, coarse_receipt, upper_receipt, anchor_receipt)
        evidence = run_quality_sweep(
            plan.raw_sweep_path,
            output_path,
            work_directory,
            encoder_path,
            resume=resume,
        )
        raw_acceptance = _mapping(evidence.get("acceptance"), "fresh sweep acceptance")
        if raw_acceptance.get("complete") is not True:
            return evidence
        if _git_head_from_clean_worktree() != initial_head:
            raise QualificationFailure("repository identity changed during direct confirmation.")
        final_plan = load_confirmation_plan(confirmation_plan_path)
        final_sources = verify_source_receipts(final_plan, coarse_receipt, upper_receipt, anchor_receipt)
        if final_plan != plan or final_sources != source_receipts:
            raise QualificationFailure("confirmation plan or provenance changed before finalization.")
        confirmation = evaluate_confirmation(plan, evidence, source_receipts)
        _finalize_receipt(output_path, evidence, confirmation)
        return evidence


def exit_code_for_confirmation(evidence: Mapping[str, object]) -> int:
    confirmation = evidence.get("confirmation")
    if not isinstance(confirmation, Mapping):
        raw_acceptance = evidence.get("acceptance")
        if isinstance(raw_acceptance, Mapping) and raw_acceptance.get("complete") is True:
            return 1
        return 3
    acceptance = confirmation.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise QualificationFailure("confirmation acceptance is missing.")
    return 0 if acceptance.get("objective_decision_ready") is True else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the preregistered ordinary direct MV-HEVC mapping confirmation.")
    parser.add_argument("--confirmation-plan", type=Path, default=DEFAULT_CONFIRMATION_PLAN)
    parser.add_argument("--coarse-receipt", type=Path, required=True)
    parser.add_argument("--upper-receipt", type=Path, required=True)
    parser.add_argument("--anchor-receipt", type=Path, required=True)
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
            args.coarse_receipt.absolute(),
            args.upper_receipt.absolute(),
            args.anchor_receipt.absolute(),
            args.output.absolute(),
            args.work_directory.absolute(),
            args.encoder.resolve(),
            resume=args.resume,
        )
        return exit_code_for_confirmation(evidence)
    except KeyboardInterrupt:
        print("Direct MV-HEVC mapping confirmation interrupted; resume the saved checkpoint.", file=sys.stderr)
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
        print(f"Direct MV-HEVC mapping confirmation failed: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
