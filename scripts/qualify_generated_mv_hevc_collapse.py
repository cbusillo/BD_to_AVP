#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import stat
import statistics
import subprocess
import sys

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import combinations, pairwise
from pathlib import Path
from typing import Iterator, Mapping, Sequence

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_generated_mv_hevc_calibration import (
    REPOSITORY_ROOT,
    _assert_private_values_absent,
    _atomic_write,
    _freeze_receipt,
    _git_head_from_clean_worktree,
    _require_head_tracked_file,
)


DEFAULT_ANALYSIS_PLAN = REPOSITORY_ROOT / "docs/qualification/generated-mv-hevc-collapse-analysis-v1.json"
OUTPUT_SCHEMA_VERSION = 1
LOCK_SUFFIX = ".bd-to-avp-generated-mv-hevc-collapse.lock"
EXPECTED_TIE_BREAKS = (
    "wider_minimum_case_storage_coverage",
    "larger_minimum_boundary_quality_margin",
    "larger_minimum_boundary_storage_margin",
    "lower_first_merge_quality",
    "lexicographic_cell_ids",
)


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
class BalancedRequirement:
    cell_id: str
    eye_bitrate_mbps: int
    merge_quality: int


@dataclass(frozen=True)
class SourceCorpusBinding:
    path: Path
    sha256: str
    binding_id: str
    selected_case_ids: tuple[str, ...]


@dataclass(frozen=True)
class CollapsePlan:
    analysis_id: str
    source_receipt: SourceReceiptBinding
    source_plan: SourcePlanBinding
    balanced: BalancedRequirement
    target_named_step_count: int
    relative_path: str | None = None


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationFailure(f"{label} must be an object.")
    return value


def _array(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise QualificationFailure(f"{label} must be an array.")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationFailure(f"{label} must be a non-empty string.")
    return value.strip()


def _integer(value: object, label: str, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise QualificationFailure(f"{label} must be an integer between {minimum} and {maximum}.")
    return value


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise QualificationFailure(f"{label} must be numeric.")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise QualificationFailure(f"{label} must be finite.")
    return parsed


def _sha256_identity(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise QualificationFailure(f"{label} must be a lowercase SHA-256 identity.")
    return digest


def _git_sha_identity(value: object, label: str) -> str:
    digest = _string(value, label)
    if len(digest) != 40 or any(character not in "0123456789abcdef" for character in digest):
        raise QualificationFailure(f"{label} must be a lowercase full Git SHA.")
    return digest


def _repository_path(value: object, label: str) -> Path:
    relative_path = _string(value, label)
    path = (REPOSITORY_ROOT / relative_path).resolve()
    try:
        path.relative_to(REPOSITORY_ROOT)
    except ValueError as error:
        raise QualificationFailure(f"{label} escapes the repository.") from error
    return path


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise QualificationFailure(f"JSON contains duplicate key: {key}")
        document[key] = value
    return document


def _loads_json(data: bytes, label: str) -> Mapping[str, object]:
    try:
        return _mapping(
            json.loads(data.decode("utf-8"), object_pairs_hook=_strict_object),
            label,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationFailure(f"Could not parse {label}.") from error


def parse_analysis_plan(raw: object) -> CollapsePlan:
    document = _mapping(raw, "collapse analysis plan")
    if document.get("schema_version") != 1:
        raise QualificationFailure("collapse analysis plan schema_version must be 1.")
    analysis_id = _string(document.get("analysis_id"), "analysis_id")
    if analysis_id != "generated-mv-hevc-collapse-analysis-v1":
        raise QualificationFailure("collapse analysis_id is unsupported.")

    source_receipt = _mapping(document.get("source_receipt"), "source_receipt")
    mode_value = _string(source_receipt.get("required_file_mode"), "source_receipt.required_file_mode")
    if mode_value != "0444":
        raise QualificationFailure("source_receipt.required_file_mode must be '0444'.")
    receipt_binding = SourceReceiptBinding(
        schema_version=_integer(
            source_receipt.get("schema_version"),
            "source_receipt.schema_version",
            minimum=1,
            maximum=1,
        ),
        experiment_id=_string(source_receipt.get("experiment_id"), "source_receipt.experiment_id"),
        sha256=_sha256_identity(source_receipt.get("sha256"), "source_receipt.sha256"),
        source_git_sha=_git_sha_identity(source_receipt.get("source_git_sha"), "source_receipt.source_git_sha"),
        required_file_mode=0o444,
    )
    if receipt_binding.experiment_id != "generated-mv-hevc-merge-refinement-v1":
        raise QualificationFailure("source_receipt.experiment_id is unsupported.")

    source_plan = _mapping(document.get("source_plan"), "source_plan")
    plan_binding = SourcePlanBinding(
        path=_repository_path(source_plan.get("path"), "source_plan.path"),
        sha256=_sha256_identity(source_plan.get("sha256"), "source_plan.sha256"),
        schema_version=_integer(source_plan.get("schema_version"), "source_plan.schema_version", minimum=2, maximum=2),
    )

    balanced = _mapping(document.get("balanced_required"), "balanced_required")
    balanced_requirement = BalancedRequirement(
        cell_id=_string(balanced.get("cell_id"), "balanced_required.cell_id"),
        eye_bitrate_mbps=_integer(
            balanced.get("eye_bitrate_mbps"),
            "balanced_required.eye_bitrate_mbps",
            minimum=1,
            maximum=500,
        ),
        merge_quality=_integer(
            balanced.get("merge_quality"),
            "balanced_required.merge_quality",
            minimum=0,
            maximum=100,
        ),
    )
    if balanced_requirement != BalancedRequirement("b020-m075", 20, 75):
        raise QualificationFailure("balanced_required must identify production Balanced 20/75.")

    candidate_policy = _mapping(document.get("candidate_policy"), "candidate_policy")
    required_candidate_policy = {
        "use_only_technically_eligible_cells": True,
        "require_balanced_cell": True,
        "boundary_quality_threshold": "pre_registered_thresholds.aggregate_quality_distinguishability",
        "boundary_storage_threshold": "pre_registered_thresholds.storage_distinguishability_ratio",
        "every_case_required": True,
        "non_adjacent_boundaries_allowed": True,
        "no_new_encodes": True,
        "ladder_mapping_selected": False,
    }
    if dict(candidate_policy) != required_candidate_policy:
        raise QualificationFailure("candidate_policy does not match the checked collapse contract.")

    selection_policy = _mapping(document.get("selection_policy"), "selection_policy")
    if selection_policy.get("primary") != "maximum_cardinality_ordered_subset":
        raise QualificationFailure("selection_policy.primary is unsupported.")
    tie_breaks = tuple(
        _string(item, "selection_policy.tie_breaks value")
        for item in _array(selection_policy.get("tie_breaks"), "selection_policy.tie_breaks")
    )
    if tie_breaks != EXPECTED_TIE_BREAKS:
        raise QualificationFailure("selection_policy.tie_breaks do not match the checked order.")
    target_named_step_count = _integer(
        selection_policy.get("target_named_step_count"),
        "selection_policy.target_named_step_count",
        minimum=2,
        maximum=20,
    )
    if target_named_step_count != 7:
        raise QualificationFailure("selection_policy.target_named_step_count must remain 7.")
    return CollapsePlan(
        analysis_id=analysis_id,
        source_receipt=receipt_binding,
        source_plan=plan_binding,
        balanced=balanced_requirement,
        target_named_step_count=target_named_step_count,
    )


def load_analysis_plan(path: Path) -> tuple[CollapsePlan, str]:
    resolved = path.resolve()
    try:
        relative_path = resolved.relative_to(REPOSITORY_ROOT).as_posix()
    except ValueError as error:
        raise QualificationFailure("Collapse analysis plan must be inside the repository.") from error
    try:
        data = resolved.read_bytes()
    except OSError as error:
        raise QualificationFailure(f"Could not read collapse analysis plan: {error}") from error
    raw = _loads_json(data, "collapse analysis plan")
    parsed = parse_analysis_plan(raw)
    return CollapsePlan(
        analysis_id=parsed.analysis_id,
        source_receipt=parsed.source_receipt,
        source_plan=parsed.source_plan,
        balanced=parsed.balanced,
        target_named_step_count=parsed.target_named_step_count,
        relative_path=relative_path,
    ), hashlib.sha256(data).hexdigest()


def _load_source_plan(plan: CollapsePlan) -> Mapping[str, object]:
    path = plan.source_plan.path
    if not path.is_file():
        raise QualificationFailure("The source refinement plan is unavailable.")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise QualificationFailure("Could not read the source refinement plan.") from error
    if hashlib.sha256(data).hexdigest() != plan.source_plan.sha256:
        raise QualificationFailure("The source refinement plan does not match its pinned SHA-256 identity.")
    document = _loads_json(data, "source refinement plan")
    if document.get("schema_version") != plan.source_plan.schema_version:
        raise QualificationFailure("The source refinement plan schema does not match the collapse plan.")
    if document.get("experiment_id") != plan.source_receipt.experiment_id:
        raise QualificationFailure("The source refinement plan experiment ID is inconsistent.")
    if document.get("target_id") != "generated_mv_hevc":
        raise QualificationFailure("The source refinement plan target is inconsistent.")
    balanced = _mapping(document.get("balanced"), "source refinement plan balanced")
    if (
        balanced.get("eye_bitrate_mbps") != plan.balanced.eye_bitrate_mbps
        or balanced.get("merge_quality") != plan.balanced.merge_quality
    ):
        raise QualificationFailure("The source refinement plan Balanced cell is inconsistent.")
    decision_policy = _mapping(document.get("decision_policy"), "source refinement plan decision_policy")
    if (
        decision_policy.get("stage") != "merge_response_refinement_only"
        or decision_policy.get("ladder_mapping_selected") is not False
    ):
        raise QualificationFailure("The source refinement plan decision policy is inconsistent.")
    _mapping(document.get("pre_registered_thresholds"), "source refinement plan thresholds")
    return document


def _load_source_corpus_binding(source_plan: Mapping[str, object]) -> SourceCorpusBinding:
    reference = _mapping(source_plan.get("corpus_binding"), "source refinement corpus_binding")
    path = _repository_path(reference.get("path"), "source refinement corpus_binding.path")
    expected_sha256 = _sha256_identity(
        reference.get("sha256"),
        "source refinement corpus_binding.sha256",
    )
    expected_binding_id = _string(
        reference.get("binding_id"),
        "source refinement corpus_binding.binding_id",
    )
    try:
        data = path.read_bytes()
    except OSError as error:
        raise QualificationFailure("Could not read the source corpus binding.") from error
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise QualificationFailure("The source corpus binding does not match its pinned SHA-256 identity.")
    document = _loads_json(data, "source corpus binding")
    if document.get("binding_id") != expected_binding_id:
        raise QualificationFailure("The source corpus binding ID is inconsistent.")
    selected_case_ids = tuple(
        _string(value, "source corpus selected_case_ids value")
        for value in _array(document.get("selected_case_ids"), "source corpus selected_case_ids")
    )
    if not selected_case_ids or len(set(selected_case_ids)) != len(selected_case_ids):
        raise QualificationFailure("The source corpus selected case IDs are invalid.")
    return SourceCorpusBinding(
        path=path,
        sha256=expected_sha256,
        binding_id=expected_binding_id,
        selected_case_ids=selected_case_ids,
    )


def _cell_id(eye_bitrate_mbps: int, merge_quality: int) -> str:
    return f"b{eye_bitrate_mbps:03d}-m{merge_quality:03d}"


def _validate_source_receipt(
    plan: CollapsePlan,
    receipt: Mapping[str, object],
    source_plan: Mapping[str, object],
    source_binding: SourceCorpusBinding,
) -> tuple[list[dict[str, object]], list[str], list[Mapping[str, object]], Mapping[str, object]]:
    if receipt.get("schema_version") != plan.source_receipt.schema_version:
        raise QualificationFailure("The source refinement receipt schema is unsupported.")
    if (
        receipt.get("experiment_id") != plan.source_receipt.experiment_id
        or receipt.get("source_git_sha") != plan.source_receipt.source_git_sha
        or receipt.get("source_tree_dirty") is not False
    ):
        raise QualificationFailure("The source refinement receipt identity is inconsistent.")
    experiment_plan = _mapping(receipt.get("experiment_plan"), "source receipt experiment_plan")
    expected_plan_path = plan.source_plan.path.relative_to(REPOSITORY_ROOT).as_posix()
    if experiment_plan != {"path": expected_plan_path, "sha256": plan.source_plan.sha256}:
        raise QualificationFailure("The source receipt refinement plan identity is inconsistent.")
    receipt_binding = _mapping(receipt.get("corpus_binding"), "source receipt corpus_binding")
    expected_binding_path = source_binding.path.relative_to(REPOSITORY_ROOT).as_posix()
    if receipt_binding != {
        "path": expected_binding_path,
        "binding_id": source_binding.binding_id,
        "sha256": source_binding.sha256,
    }:
        raise QualificationFailure("The source receipt corpus binding identity is inconsistent.")

    source_thresholds = _mapping(source_plan.get("pre_registered_thresholds"), "source plan thresholds")
    receipt_thresholds = _mapping(receipt.get("pre_registered_thresholds"), "source receipt thresholds")
    method = _mapping(receipt.get("method"), "source receipt method")
    method_thresholds = _mapping(method.get("pre_registered_thresholds"), "source receipt method thresholds")
    if receipt_thresholds != source_thresholds or method_thresholds != source_thresholds:
        raise QualificationFailure("The source receipt thresholds differ from the checked refinement plan.")
    if method.get("decision_stage") != "merge_response_refinement_only":
        raise QualificationFailure("The source receipt decision stage is inconsistent.")
    method_balanced = _mapping(method.get("balanced"), "source receipt method balanced")
    if method_balanced != {
        "eye_bitrate_mbps": plan.balanced.eye_bitrate_mbps,
        "merge_quality": plan.balanced.merge_quality,
    }:
        raise QualificationFailure("The source receipt method Balanced cell is inconsistent.")

    acceptance = _mapping(receipt.get("acceptance"), "source receipt acceptance")
    for key in (
        "complete",
        "experiment_complete",
        "execution_passed",
        "objective_validation_passed",
        "eye_order_passed",
        "planned_stress_corpus",
        "thresholds_pre_registered",
        "thresholds_evaluated",
        "refinement_evidence_ready",
    ):
        if acceptance.get(key) is not True:
            raise QualificationFailure(f"The source receipt acceptance gate {key} did not pass.")
    for key in ("refinement_decision_ready", "ladder_evidence_ready", "ladder_mapping_selected"):
        if acceptance.get(key) is not False:
            raise QualificationFailure(f"The source receipt acceptance gate {key} must remain false.")
    if (
        _integer(
            acceptance.get("ambiguous_adjacent_count"), "acceptance.ambiguous_adjacent_count", minimum=1, maximum=100
        )
        < 1
    ):
        raise QualificationFailure("The source receipt does not require collapse analysis.")

    source_axes = _mapping(source_plan.get("axes"), "source refinement plan axes")
    eye_bitrates = _array(source_axes.get("eye_bitrate_mbps"), "source refinement eye bitrate axis")
    merge_qualities = [
        _integer(value, "merge quality", minimum=0, maximum=100)
        for value in _array(source_axes.get("merge_quality"), "source refinement merge-quality axis")
    ]
    if eye_bitrates != [plan.balanced.eye_bitrate_mbps]:
        raise QualificationFailure("The source refinement eye-bitrate axis is inconsistent.")
    expected_cells = [
        {
            "id": _cell_id(plan.balanced.eye_bitrate_mbps, value),
            "eye_bitrate_mbps": plan.balanced.eye_bitrate_mbps,
            "merge_quality": value,
        }
        for value in merge_qualities
    ]
    cells = _array(receipt.get("cells"), "source receipt cells")
    if cells != expected_cells:
        raise QualificationFailure("The source receipt cell order differs from the checked refinement plan.")

    raw_evaluations = _array(receipt.get("refinement_cell_evaluations"), "source receipt cell evaluations")
    evaluations = [_mapping(value, "source receipt cell evaluation") for value in raw_evaluations]
    if len(evaluations) != len(expected_cells):
        raise QualificationFailure("The source receipt cell evaluation set is incomplete.")
    evaluation_by_id: dict[str, Mapping[str, object]] = {}
    for evaluation in evaluations:
        cell_id = _string(evaluation.get("cell_id"), "cell evaluation cell_id")
        if cell_id in evaluation_by_id or evaluation.get("complete") is not True:
            raise QualificationFailure("The source receipt contains invalid cell evaluations.")
        evaluation_by_id[cell_id] = evaluation
    if set(evaluation_by_id) != {str(cell["id"]) for cell in expected_cells}:
        raise QualificationFailure("The source receipt cell evaluations do not match the checked cells.")
    eligible_ids = [
        str(cell["id"])
        for cell in expected_cells
        if evaluation_by_id[str(cell["id"])].get("candidate_constraints_passed") is True
    ]
    if len(eligible_ids) != acceptance.get("technically_eligible_cell_count"):
        raise QualificationFailure("The source receipt eligible-cell count is inconsistent.")
    if plan.balanced.cell_id not in eligible_ids:
        raise QualificationFailure("Production Balanced is not technically eligible in the source receipt.")

    selected_case_ids = [
        _string(value, "selected_case_ids value")
        for value in _array(receipt.get("selected_case_ids"), "source receipt selected_case_ids")
    ]
    if tuple(selected_case_ids) != source_binding.selected_case_ids:
        raise QualificationFailure("The source receipt case IDs differ from the checked corpus binding.")
    cases = [_mapping(value, "source receipt case") for value in _array(receipt.get("cases"), "source receipt cases")]
    if [case.get("id") for case in cases] != selected_case_ids:
        raise QualificationFailure("The source receipt case order is inconsistent.")
    for case in cases:
        case_cells = _array(case.get("cells"), "source receipt case cells")
        case_cell_ids = [
            _string(_mapping(cell, "source receipt case cell").get("id"), "source receipt case cell id")
            for cell in case_cells
        ]
        if case_cell_ids != [str(cell["id"]) for cell in expected_cells]:
            raise QualificationFailure("A source receipt case has an inconsistent cell set.")
        for raw_cell in case_cells:
            summary = _mapping(_mapping(raw_cell, "source receipt case cell").get("summary"), "cell summary")
            _number(summary.get("median_min_same_eye_ssim"), "median_min_same_eye_ssim")
            median_final_bytes = _number(summary.get("median_final_bytes"), "median_final_bytes")
            if median_final_bytes <= 0:
                raise QualificationFailure("median_final_bytes must be positive.")
    return expected_cells, eligible_ids, cases, source_thresholds


def _load_source_receipt(
    plan: CollapsePlan,
    receipt_path: Path,
    source_plan: Mapping[str, object],
    source_binding: SourceCorpusBinding,
) -> tuple[Mapping[str, object], list[dict[str, object]], list[str], list[Mapping[str, object]], Mapping[str, object]]:
    resolved = receipt_path.resolve()
    if receipt_path.is_symlink():
        raise QualificationFailure("The source refinement receipt is unavailable or unsafe.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as error:
        raise QualificationFailure("The source refinement receipt is unavailable or unsafe.") from error
    with os.fdopen(descriptor, "rb") as receipt_file:
        receipt_stat = os.fstat(receipt_file.fileno())
        if not stat.S_ISREG(receipt_stat.st_mode):
            raise QualificationFailure("The source refinement receipt is not a regular file.")
        data = receipt_file.read()
    if stat.S_IMODE(receipt_stat.st_mode) != plan.source_receipt.required_file_mode:
        raise QualificationFailure("The source refinement receipt must be frozen read-only.")
    if hashlib.sha256(data).hexdigest() != plan.source_receipt.sha256:
        raise QualificationFailure("The source refinement receipt does not match its pinned SHA-256 identity.")
    receipt = _loads_json(data, "source refinement receipt")
    _assert_private_values_absent(receipt, ())
    cells, eligible_ids, cases, thresholds = _validate_source_receipt(
        plan,
        receipt,
        source_plan,
        source_binding,
    )
    return receipt, cells, eligible_ids, cases, thresholds


def _case_cell_summary(case: Mapping[str, object], cell_id: str) -> Mapping[str, object]:
    for raw_cell in _array(case.get("cells"), "source receipt case cells"):
        cell = _mapping(raw_cell, "source receipt case cell")
        if cell.get("id") == cell_id:
            return _mapping(cell.get("summary"), "source receipt cell summary")
    raise QualificationFailure(f"Source receipt case is missing cell {cell_id}.")


def evaluate_boundaries(
    cells: Sequence[Mapping[str, object]],
    eligible_ids: Sequence[str],
    cases: Sequence[Mapping[str, object]],
    thresholds: Mapping[str, object],
) -> list[dict[str, object]]:
    quality_threshold = _number(
        thresholds.get("aggregate_quality_distinguishability"),
        "aggregate quality distinguishability threshold",
    )
    storage_threshold = _number(
        thresholds.get("storage_distinguishability_ratio"),
        "storage distinguishability threshold",
    )
    cell_by_id = {str(cell["id"]): cell for cell in cells}
    boundaries: list[dict[str, object]] = []
    for lower_id, higher_id in combinations(eligible_ids, 2):
        lower = cell_by_id[lower_id]
        higher = cell_by_id[higher_id]
        if int(lower["merge_quality"]) >= int(higher["merge_quality"]):
            raise QualificationFailure("Eligible cells are not strictly ordered by merge quality.")
        case_evaluations: list[dict[str, object]] = []
        for case in cases:
            lower_summary = _case_cell_summary(case, lower_id)
            higher_summary = _case_cell_summary(case, higher_id)
            quality_separation = _number(
                higher_summary.get("median_min_same_eye_ssim"),
                "higher median_min_same_eye_ssim",
            ) - _number(lower_summary.get("median_min_same_eye_ssim"), "lower median_min_same_eye_ssim")
            lower_bytes = _number(lower_summary.get("median_final_bytes"), "lower median_final_bytes")
            higher_bytes = _number(higher_summary.get("median_final_bytes"), "higher median_final_bytes")
            storage_growth_ratio = higher_bytes / lower_bytes - 1.0
            case_evaluations.append(
                {
                    "case_id": case["id"],
                    "quality_separation": quality_separation,
                    "storage_growth_ratio": storage_growth_ratio,
                    "quality_distinct": quality_separation >= quality_threshold,
                    "storage_distinct": storage_growth_ratio >= storage_threshold,
                }
            )
        minimum_quality_separation = min(float(item["quality_separation"]) for item in case_evaluations)
        minimum_storage_growth_ratio = min(float(item["storage_growth_ratio"]) for item in case_evaluations)
        quality_distinct = all(item["quality_distinct"] is True for item in case_evaluations)
        storage_distinct = all(item["storage_distinct"] is True for item in case_evaluations)
        boundaries.append(
            {
                "lower_cell_id": lower_id,
                "higher_cell_id": higher_id,
                "case_evaluations": case_evaluations,
                "minimum_quality_separation": minimum_quality_separation,
                "median_quality_separation": statistics.median(
                    float(item["quality_separation"]) for item in case_evaluations
                ),
                "minimum_storage_growth_ratio": minimum_storage_growth_ratio,
                "median_storage_growth_ratio": statistics.median(
                    float(item["storage_growth_ratio"]) for item in case_evaluations
                ),
                "minimum_quality_margin": minimum_quality_separation - quality_threshold,
                "minimum_storage_margin": minimum_storage_growth_ratio - storage_threshold,
                "quality_distinct": quality_distinct,
                "storage_distinct": storage_distinct,
                "response_separable": quality_distinct and storage_distinct,
            }
        )
    return boundaries


def select_collapsed_subset(
    cells: Sequence[Mapping[str, object]],
    eligible_ids: Sequence[str],
    boundaries: Sequence[Mapping[str, object]],
    balanced_cell_id: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    if balanced_cell_id not in eligible_ids:
        raise QualificationFailure("Balanced is absent from the eligible collapse candidates.")
    cell_by_id = {str(cell["id"]): cell for cell in cells}
    boundary_by_pair = {
        (str(boundary["lower_cell_id"]), str(boundary["higher_cell_id"])): boundary for boundary in boundaries
    }
    valid_subsets: list[dict[str, object]] = []
    for subset_size in range(1, len(eligible_ids) + 1):
        for candidate_tuple in combinations(eligible_ids, subset_size):
            if balanced_cell_id not in candidate_tuple:
                continue
            successive = [boundary_by_pair[pair] for pair in pairwise(candidate_tuple)]
            if not all(boundary.get("response_separable") is True for boundary in successive):
                continue
            if len(candidate_tuple) > 1:
                coverage_boundary = boundary_by_pair[(candidate_tuple[0], candidate_tuple[-1])]
                storage_coverage = float(coverage_boundary["minimum_storage_growth_ratio"])
                minimum_quality_margin = min(float(boundary["minimum_quality_margin"]) for boundary in successive)
                minimum_storage_margin = min(float(boundary["minimum_storage_margin"]) for boundary in successive)
            else:
                storage_coverage = 0.0
                minimum_quality_margin = 0.0
                minimum_storage_margin = 0.0
            valid_subsets.append(
                {
                    "cell_ids": list(candidate_tuple),
                    "cardinality": len(candidate_tuple),
                    "minimum_case_storage_coverage": storage_coverage,
                    "minimum_boundary_quality_margin": minimum_quality_margin,
                    "minimum_boundary_storage_margin": minimum_storage_margin,
                    "first_merge_quality": int(cell_by_id[candidate_tuple[0]]["merge_quality"]),
                }
            )
    if not valid_subsets:
        raise QualificationFailure("No valid collapsed subset contains production Balanced.")
    valid_subsets.sort(
        key=lambda subset: (
            -int(subset["cardinality"]),
            -float(subset["minimum_case_storage_coverage"]),
            -float(subset["minimum_boundary_quality_margin"]),
            -float(subset["minimum_boundary_storage_margin"]),
            int(subset["first_merge_quality"]),
            tuple(str(cell_id) for cell_id in subset["cell_ids"]),
        )
    )
    selected = dict(valid_subsets[0])
    selected["contains_balanced"] = balanced_cell_id in selected["cell_ids"]
    selected["selection_policy"] = {
        "primary": "maximum_cardinality_ordered_subset",
        "tie_breaks": list(EXPECTED_TIE_BREAKS),
    }
    return selected, valid_subsets


def build_analysis_receipt(
    plan: CollapsePlan,
    plan_sha256: str,
    analyzer_git_sha: str,
    source_binding: SourceCorpusBinding,
    cells: Sequence[Mapping[str, object]],
    eligible_ids: Sequence[str],
    thresholds: Mapping[str, object],
    boundaries: Sequence[Mapping[str, object]],
    selected: Mapping[str, object],
    valid_subsets: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    if plan.relative_path is None:
        raise QualificationFailure("Collapse analysis plan is not repository-bound.")
    target_met = int(selected["cardinality"]) >= plan.target_named_step_count
    cell_by_id = {str(cell["id"]): cell for cell in cells}
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "analysis_id": plan.analysis_id,
        "created_at": datetime.now(UTC).isoformat(),
        "analysis_source_git_sha": analyzer_git_sha,
        "analysis_source_tree_dirty": False,
        "analysis_plan": {"path": plan.relative_path, "sha256": plan_sha256},
        "source_receipt": {
            "schema_version": plan.source_receipt.schema_version,
            "experiment_id": plan.source_receipt.experiment_id,
            "sha256": plan.source_receipt.sha256,
            "source_git_sha": plan.source_receipt.source_git_sha,
            "file_mode": "0444",
        },
        "source_plan": {
            "path": plan.source_plan.path.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": plan.source_plan.sha256,
            "schema_version": plan.source_plan.schema_version,
        },
        "source_corpus_binding": {
            "path": source_binding.path.relative_to(REPOSITORY_ROOT).as_posix(),
            "sha256": source_binding.sha256,
            "binding_id": source_binding.binding_id,
            "selected_case_ids": list(source_binding.selected_case_ids),
        },
        "thresholds": dict(thresholds),
        "eligible_cells": [dict(cell_by_id[cell_id]) for cell_id in eligible_ids],
        "boundary_evaluations": [dict(boundary) for boundary in boundaries],
        "valid_subsets": [dict(subset) for subset in valid_subsets],
        "selected_subset": dict(selected),
        "acceptance": {
            "analysis_complete": True,
            "source_receipt_verified": True,
            "source_plan_verified": True,
            "thresholds_unchanged": True,
            "non_adjacent_pairwise_evaluation_used": True,
            "balanced_included": selected.get("contains_balanced") is True,
            "selected_chain_valid": True,
            "target_named_step_count": plan.target_named_step_count,
            "selected_step_count": selected["cardinality"],
            "target_step_count_met": target_met,
            "bitrate_search_ready": target_met,
            "product_decision_required": not target_met,
            "ladder_evidence_ready": False,
            "ladder_mapping_selected": False,
        },
    }


@contextmanager
def collapse_lock(output_path: Path) -> Iterator[None]:
    lock_path = Path(f"{output_path}{LOCK_SUFFIX}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualificationFailure("Generated collapse analysis is already running for this output.") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def run_analysis(analysis_plan_path: Path, source_receipt_path: Path, output_path: Path) -> dict[str, object]:
    analyzer_git_sha = _git_head_from_clean_worktree()
    plan, plan_sha256 = load_analysis_plan(analysis_plan_path)
    if _require_head_tracked_file(analysis_plan_path, "Collapse analysis plan") != plan.relative_path:
        raise QualificationFailure("Collapse analysis plan repository identity changed during validation.")
    _require_head_tracked_file(plan.source_plan.path, "Source refinement plan")
    source_plan = _load_source_plan(plan)
    source_binding = _load_source_corpus_binding(source_plan)
    _require_head_tracked_file(source_binding.path, "Source corpus binding")
    _, cells, eligible_ids, cases, thresholds = _load_source_receipt(
        plan,
        source_receipt_path,
        source_plan,
        source_binding,
    )
    boundaries = evaluate_boundaries(cells, eligible_ids, cases, thresholds)
    selected, valid_subsets = select_collapsed_subset(
        cells,
        eligible_ids,
        boundaries,
        plan.balanced.cell_id,
    )
    evidence = build_analysis_receipt(
        plan,
        plan_sha256,
        analyzer_git_sha,
        source_binding,
        cells,
        eligible_ids,
        thresholds,
        boundaries,
        selected,
        valid_subsets,
    )
    _assert_private_values_absent(evidence, ())
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with collapse_lock(output_path):
        if output_path.exists() or output_path.is_symlink():
            raise QualificationFailure("Collapse analysis output already exists; choose a new output path.")
        _atomic_write(output_path, evidence, ())
        _freeze_receipt(output_path)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collapse generated MV-HEVC merge candidates using a checked immutable refinement receipt."
    )
    parser.add_argument("--analysis-plan", type=Path, default=DEFAULT_ANALYSIS_PLAN)
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        evidence = run_analysis(
            args.analysis_plan.resolve(),
            args.source_receipt.absolute(),
            args.output.absolute(),
        )
    except (
        AssertionError,
        KeyError,
        OSError,
        TypeError,
        subprocess.SubprocessError,
        QualificationFailure,
        ValueError,
    ) as error:
        print(f"Generated MV-HEVC collapse analysis failed: {error}", file=sys.stderr)
        return 2
    acceptance = evidence.get("acceptance")
    if not isinstance(acceptance, Mapping) or acceptance.get("analysis_complete") is not True:
        print("Generated MV-HEVC collapse analysis failed: acceptance record is missing.", file=sys.stderr)
        return 2
    return 0 if acceptance.get("target_step_count_met") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
