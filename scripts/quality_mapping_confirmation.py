from __future__ import annotations

import statistics

from dataclasses import dataclass
from itertools import pairwise
from typing import Mapping

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_quality_sweep import _candidate_record


@dataclass(frozen=True)
class MappingEvaluationSpec:
    experiment_id: str
    case_ids: tuple[str, ...]
    balanced_candidate_id: str
    maximum_candidate_id: str
    maximum_size_ratio: float
    selection_policy: str
    expected_record_count: int
    retained_anchor_artifacts: object
    downstream_checks: Mapping[str, Mapping[str, object]]


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


def _candidate_runs(
    case: Mapping[str, object],
    candidate_id: str,
    runs_per_candidate: int,
) -> list[Mapping[str, object]]:
    candidate = _candidate_record(case, candidate_id)
    if candidate is None:
        raise QualificationFailure(f"fresh receipt is missing candidate {candidate_id}.")
    runs = _array(candidate.get("runs"), f"{candidate_id} runs")
    if len(runs) != runs_per_candidate or any(not isinstance(run, Mapping) for run in runs):
        raise QualificationFailure(
            f"fresh receipt candidate {candidate_id} does not contain {runs_per_candidate} runs."
        )
    return sorted((run for run in runs if isinstance(run, Mapping)), key=lambda run: int(run["run_index"]))


def evaluate_mapping_confirmation(
    plan: object,
    evidence: Mapping[str, object],
    source_receipts: Mapping[str, object],
    spec: MappingEvaluationSpec,
) -> dict[str, object]:
    candidates = tuple(plan.candidates)
    thresholds = plan.thresholds
    raw_acceptance = _mapping(evidence.get("acceptance"), "fresh sweep acceptance")
    complete = raw_acceptance.get("complete") is True and raw_acceptance.get("full_quality_gated_corpus") is True
    cases = _array(evidence.get("cases"), "fresh sweep cases")
    if [case.get("id") for case in cases if isinstance(case, Mapping)] != list(spec.case_ids):
        raise QualificationFailure("fresh sweep case order or identity changed.")
    if evidence.get("candidates") != [
        {"id": candidate.candidate_id, "quality": candidate.quality} for candidate in candidates
    ]:
        raise QualificationFailure("fresh sweep candidate identity changed.")
    runs_per_candidate = spec.expected_record_count // (len(spec.case_ids) * len(candidates))
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
    for candidate in candidates:
        summary = _mapping(raw_summaries.get(candidate.candidate_id), f"{candidate.candidate_id} summary")
        failures: list[str] = []
        if summary.get("complete") is not True:
            failures.append("incomplete")
        if (
            _number(summary.get("maximum_repeat_ssim_spread"), "repeat SSIM spread")
            > thresholds.maximum_repeat_ssim_spread
        ):
            failures.append("repeat_ssim_spread")
        if (
            _number(summary.get("maximum_repeat_size_ratio_spread"), "repeat size spread")
            > thresholds.maximum_repeat_size_ratio_spread
        ):
            failures.append("repeat_size_ratio_spread")
        if (
            candidate.candidate_id == spec.maximum_candidate_id
            and _number(summary.get("output_size_ratio"), "maximum output size ratio") > spec.maximum_size_ratio
        ):
            failures.append(f"{spec.maximum_candidate_id}_size_cap")
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
                f"quality_delta_vs_{spec.balanced_candidate_id}": summary.get("quality_delta"),
                f"output_size_ratio_vs_{spec.balanced_candidate_id}": summary.get("output_size_ratio"),
                f"median_encode_time_ratio_vs_{spec.balanced_candidate_id}": summary.get("median_encode_time_ratio"),
                "maximum_repeat_ssim_spread": summary.get("maximum_repeat_ssim_spread"),
                "maximum_repeat_size_ratio_spread": summary.get("maximum_repeat_size_ratio_spread"),
                "minimum_eye_order_margin": summary.get("minimum_eye_order_margin"),
            }
        )

    boundaries: list[dict[str, object]] = []
    for lower, higher in pairwise(candidates):
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
            lower_runs = _candidate_runs(case, lower.candidate_id, runs_per_candidate)
            higher_runs = _candidate_runs(case, higher.candidate_id, runs_per_candidate)
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
                    "quality_non_inferiority_passed": quality_delta >= thresholds.minimum_case_median_ssim_delta,
                    "objective_quality_clear": quality_delta >= thresholds.real_case_ssim_threshold,
                    "real_case": "real_mvc" in tags,
                    "sensitive_case": case_id in thresholds.sensitive_case_ids,
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
            >= thresholds.minimum_case_median_storage_growth_ratio
            for case in per_case
        )
        noninferiority_passed = all(case.get("quality_non_inferiority_passed") is True for case in per_case)
        distinction_passed = (
            corpus_median >= thresholds.minimum_corpus_median_ssim_improvement
            and real_clear_count >= thresholds.minimum_real_case_clear_count
            and sensitive_clear_count >= thresholds.minimum_sensitive_case_clear_count
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
    candidate_ids = [candidate.candidate_id for candidate in candidates]
    balanced_index = candidate_ids.index(spec.balanced_candidate_id)
    selected = [spec.balanced_candidate_id] if spec.balanced_candidate_id in eligible_ids else []
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
        for candidate in candidates
    ]
    all_boundaries_passed = len(boundaries) == len(candidates) - 1 and all(
        boundary["boundary_passed"] is True for boundary in boundaries
    )
    all_candidates_selected = selected == candidate_ids
    technical_passed = len(eligible_ids) == len(candidates)
    objective_ready = (
        complete
        and record_count == spec.expected_record_count
        and raw_acceptance.get("execution_passed") is True
        and technical_passed
        and all_boundaries_passed
        and all_candidates_selected
    )
    size_cap_key = f"{spec.maximum_candidate_id}_size_cap_ratio_vs_{spec.balanced_candidate_id}"
    return {
        "schema_version": 1,
        "experiment_id": spec.experiment_id,
        "experiment_plan": {"path": plan.relative_path, "sha256": plan.sha256},
        "completed_at": evidence.get("updated_at"),
        "source_receipts": dict(source_receipts),
        "method": {
            "candidate_order": "ascending_even_runs_descending_odd_runs",
            "runs_per_candidate": runs_per_candidate,
            "quality_metric": "minimum decoded same-eye SSIM",
            "repeatability_limits": {
                "maximum_repeat_ssim_spread": thresholds.maximum_repeat_ssim_spread,
                "maximum_repeat_size_ratio_spread": thresholds.maximum_repeat_size_ratio_spread,
            },
            size_cap_key: spec.maximum_size_ratio,
            "adjacent_boundary_policy": {
                "minimum_case_median_storage_growth_ratio": thresholds.minimum_case_median_storage_growth_ratio,
                "minimum_case_median_ssim_delta": thresholds.minimum_case_median_ssim_delta,
                "minimum_corpus_median_ssim_improvement": thresholds.minimum_corpus_median_ssim_improvement,
                "real_case_ssim_threshold": thresholds.real_case_ssim_threshold,
                "minimum_real_case_clear_count": thresholds.minimum_real_case_clear_count,
                "sensitive_case_ids": list(thresholds.sensitive_case_ids),
                "minimum_sensitive_case_clear_count": thresholds.minimum_sensitive_case_clear_count,
                "failed_boundary_action": "collapse",
                "aliases_forbidden": True,
            },
            "selection_policy": spec.selection_policy,
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
            "contains_balanced": spec.balanced_candidate_id in selected_set,
            "selection_policy": spec.selection_policy,
        }
        if selected
        else None,
        "provisional_mappings": provisional_mappings,
        "artifact_policy": {
            "fresh_outputs_ephemeral": True,
            "fresh_output_sha256_recorded_for_every_run": record_count == spec.expected_record_count,
            "retained_full_length_anchor_artifacts": spec.retained_anchor_artifacts,
        },
        "downstream_checks": {name: dict(status) for name, status in spec.downstream_checks.items()},
        "acceptance": {
            "complete": complete,
            "finalized": complete,
            "source_receipts_verified": True,
            "record_count": record_count,
            "expected_record_count": spec.expected_record_count,
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
