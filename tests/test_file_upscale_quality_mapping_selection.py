import copy
import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_mapping_selection import (
    DEFAULT_CONFIRMATION_PLAN,
    DEFAULT_SELECTION_PLAN,
    EXPECTED_CALIBRATED_NOISE,
    EXPECTED_CASE_IDS,
    EXPECTED_NOISE,
    EXPECTED_QUALITIES,
    MappingSelectionPlan,
    SourceReceiptBinding,
    _artifact_path,
    _candidate_order,
    _cleanup_completed_work_directory,
    _load_resume_evidence,
    _new_evidence,
    _public_contract_record,
    _read_frozen_source_receipt,
    _refresh_summaries,
    _retained_artifact_entry,
    _toolchain_record,
    _validate_clean_work_directory,
    assign_provisional_mappings,
    exit_code_for_evidence,
    load_mapping_selection_plan as _load_mapping_selection_plan,
    main as mapping_selection_main,
    materialized_case_orders,
    parse_mapping_corpus_binding,
    parse_mapping_selection_plan,
    recompute_calibration_noise_maxima,
    recompute_source_noise_maxima,
    select_provisional_subset,
    verify_source_response,
)
from scripts.qualify_mv_hevc_corpus import load_manifest
from scripts.qualify_mv_hevc_quality_match import sha256_file
from tests.test_file_upscale_quality_sweep import (
    _base_record as sweep_base_record,
    _bitrate_mbps as sweep_bitrate_mbps,
    _candidate_record as sweep_candidate_record,
    _case_record as sweep_case_record,
)


PUBLIC_LADDER_SHA256 = "04620e59e5380c88d3d5152f78712402675f31db6f1253c1d93224af585111dc"
VIDEO_QUALITY_SWIFT_SHA256 = "6f204564261d859590086ca41e9a27ac9f69bc0feb225137cf0abc4a98082dfa"


def load_mapping_selection_plan(path: Path):
    return _load_mapping_selection_plan(path, allow_historical_public_contracts=True)


def _source_noise_receipt() -> dict[str, object]:
    fields = {key: values[1] for key, values in EXPECTED_NOISE.items()}
    cases = []
    for case_index, case_id in enumerate(
        (
            "production-dark",
            "production-grain-rain",
            "production-crop",
            "production-rate-override",
            "synthetic-animation",
        )
    ):
        repeats = []
        for repeat_index in range(3):
            candidates = []
            for candidate_index, candidate_id in enumerate(("q065", "q075", "q085")):
                record: dict[str, object] = {"id": candidate_id}
                for field, source_maximum in fields.items():
                    baseline = 0.2 + case_index * 0.01 + candidate_index * 0.001
                    record[field] = (
                        baseline + source_maximum
                        if case_id == "production-dark" and candidate_id == "q065" and repeat_index == 1
                        else baseline
                    )
                candidates.append(record)
            repeats.append({"repeat_index": repeat_index, "candidates": candidates})
        cases.append({"id": case_id, "repeats": repeats})
    return {"cases": cases}


def _calibration_source_receipt(
    plan: MappingSelectionPlan,
    binding: Any,
    binding_sha256: str,
) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case_index, case_id in enumerate(EXPECTED_CASE_IDS):
        repeats: list[dict[str, object]] = []
        for repeat_index in range(5):
            candidate: dict[str, object] = {"id": "q075", "quality": 75}
            for field, (_, source_maximum, _, _) in EXPECTED_CALIBRATED_NOISE.items():
                baseline = 0.5 + case_index * 0.01
                candidate[field] = (
                    baseline + source_maximum
                    if case_id
                    == {
                        "min_same_eye_ssim": "production-snow-detail",
                        "final_to_base_size_ratio": "production-snow-detail",
                        "minimum_frame_same_eye_ssim": "production-motion",
                        "p05_frame_same_eye_ssim": "production-snow-detail",
                        "frame_ssim_standard_deviation": "production-motion",
                        "maximum_adjacent_frame_ssim_drop": "production-motion",
                        "min_eye_order_margin": "production-motion",
                    }[field]
                    and repeat_index == 4
                    else baseline
                )
            repeats.append({"repeat_index": repeat_index, "order": [75], "candidates": [candidate]})
        cases.append({"id": case_id, "repeats": repeats})

    recomputed = recompute_calibration_noise_maxima({"cases": cases})
    maxima = cast(dict[str, dict[str, object]], recomputed["metrics"])
    limit_by_field = {limit.record_field: limit for limit in plan.noise_limits}
    previous_limits = {
        "min_same_eye_ssim": 0.0002,
        "final_to_base_size_ratio": 0.02,
        "minimum_frame_same_eye_ssim": 0.0016,
        "p05_frame_same_eye_ssim": 0.0012,
        "frame_ssim_standard_deviation": 0.0002,
        "maximum_adjacent_frame_ssim_drop": 0.001,
        "min_eye_order_margin": 0.0011,
    }
    metrics = {
        field: {
            "record_field": field,
            "source": maxima[field]["source_group"],
            "previous_limit": previous_limits[field],
            "observed_maximum": maxima[field]["source_maximum"],
            "multiplier": 2,
            "quantum": limit_by_field[field].quantum,
            "derived_limit": limit_by_field[field].limit,
        }
        for field in EXPECTED_CALIBRATED_NOISE
    }
    scope = {
        "calibration_only": True,
        "selection_forbidden": True,
        "boundary_evaluation_forbidden": True,
        "provisional_outputs_forbidden": True,
        "public_contract_changes_forbidden": True,
        "later_confirmation_required": True,
    }
    derivation = {
        "source_records": "raw_q075_case_repeat_candidate_records_only",
        "group_by": ["case_id", "candidate_id"],
        "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
        "source_maximum_statistic": "maximum_across_cases",
        "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
        "multiplier": 2,
        "predecessor_receipt_records_forbidden": True,
        "summary_fields_as_source_forbidden": True,
    }
    return {
        "schema_version": 4,
        "experiment_id": "file-upscale-quality-repeatability-calibration-v2",
        "source_git_sha": "1f988fbf198595d52084eabc3055edd2f1d14221",
        "source_tree_dirty": False,
        "experiment_plan": {
            "path": "docs/qualification/file-upscale-quality-repeatability-calibration-v2.json",
            "sha256": "c4cf953bd868eadd04f4ed11a7ca4f2211c81f5ee72f375347f5f3d9cf14ecdb",
        },
        "corpus_binding": {
            "path": binding.relative_path,
            "binding_id": binding.binding_id,
            "sha256": binding_sha256,
        },
        "selected_case_ids": list(binding.selected_case_ids),
        "candidates": [
            {
                "id": "q075",
                "quality": 75,
                "quality_factor": "75/100",
                "bitrate_scaling_factor": "0.75",
            }
        ],
        "public_contract_bindings": _public_contract_record(plan),
        "toolchain": _toolchain_record(cast(Any, plan)),
        "method": {
            "stage": "repeatability_limit_calibration_only",
            "scope": scope,
            "derivation": derivation,
        },
        "predecessor": {
            "receipt": {
                "schema_version": 3,
                "experiment_id": "file-upscale-quality-mapping-selection-v1",
                "sha256": "c8e2478913a8c458657f0f7904720d6f76e8761b8ba1922e7c5dda5b916d2cef",
                "source_git_sha": "b93a9729a2396b3942e679a1a8db34967f9d4467",
                "file_mode": "0444",
                "provided_via": "--mapping-selection-receipt",
            },
            "plan": {
                "path": "docs/qualification/file-upscale-quality-mapping-selection-v1.json",
                "sha256": "3aa76c79adb81e72dd89f9fd548ef73698880eebf6332c149fe401c058d090ee",
                "schema_version": 1,
            },
            "accepted_complete_receipt_verified": True,
            "records_used_for_calibration": False,
        },
        "cases": cases,
        "repeatability_calibration": {
            "source_records": "raw_q075_case_repeat_candidate_records_only",
            "group_by": ["case_id", "candidate_id"],
            "within_case_statistic": "maximum_minus_minimum_across_five_repeats",
            "source_maximum_statistic": "maximum_across_cases",
            "formula": "max(previous_limit, ceil(2 * source_maximum / quantum) * quantum)",
            "multiplier": 2,
            "raw_record_count": 35,
            "case_repeat_ranges": recomputed["case_repeat_ranges"],
            "metrics": metrics,
        },
        "later_confirmation": {
            "required_before_public_contract_changes": True,
            "status": "not_performed",
        },
        "acceptance": {
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
        },
    }


def _definitions(plan: MappingSelectionPlan, selection_plan: Path = DEFAULT_SELECTION_PLAN):
    _, binding, _, _ = load_mapping_selection_plan(selection_plan)
    manifest = load_manifest(binding.source_manifest_path)
    by_id = {case.case_id: case for case in manifest.cases}
    return binding, {case_id: by_id[case_id] for case_id in EXPECTED_CASE_IDS}


def _retained_artifact_manifest(plan: MappingSelectionPlan) -> list[dict[str, object]]:
    artifacts: list[dict[str, object]] = []
    for case_id in plan.retained_case_ids:
        repeat = plan.retained_repeat_index + 1
        artifacts.append({"artifact_id": f"{case_id}-r{repeat}-base"})
        artifacts.extend(
            {"artifact_id": f"{case_id}-r{repeat}-{candidate.candidate_id}"} for candidate in plan.candidates
        )
    return artifacts


def _complete_evidence(
    *,
    selection_plan: Path = DEFAULT_SELECTION_PLAN,
    collapse_q100: bool = False,
    oversize_q045: bool = False,
    storage_tie_q045_q055: bool = False,
) -> tuple[MappingSelectionPlan, Any, Any, dict[str, Any]]:
    plan, binding, plan_sha256, binding_sha256 = load_mapping_selection_plan(selection_plan)
    _, definitions = _definitions(plan, selection_plan)
    evidence = cast(
        dict[str, Any],
        _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            {"verified": True},
            {"git_head": "f" * 40},
        ),
    )
    for case_index, case_id in enumerate(EXPECTED_CASE_IDS):
        definition = definitions[case_id]
        case = sweep_case_record(definition, binding)
        for repeat_index in range(plan.runs_per_candidate):
            base = sweep_base_record(definition, repeat_index)
            records = []
            for execution_ordinal, candidate in enumerate(_candidate_order(plan, case_id, repeat_index)):
                final_bytes = 1800 + EXPECTED_QUALITIES.index(candidate.quality) * 300
                if oversize_q045 and candidate.quality == 45:
                    final_bytes = 4200
                if storage_tie_q045_q055 and case_index == 0 and candidate.quality == 55:
                    final_bytes = 1800
                score_quality = 95 if collapse_q100 and candidate.quality == 100 else candidate.quality
                quality_score = 0.90 + case_index * 0.001 + (score_quality - 45) * 0.00005
                records.append(
                    sweep_candidate_record(
                        definition,
                        candidate.quality,
                        repeat_index,
                        execution_ordinal,
                        final_bytes=final_bytes,
                        quality_score=quality_score,
                    )
                )
            cast(list[object], case["repeats"]).append(
                {
                    "repeat_index": repeat_index,
                    "order": [candidate.quality for candidate in _candidate_order(plan, case_id, repeat_index)],
                    "base": base,
                    "candidates": records,
                }
            )
        cast(list[object], evidence["cases"]).append(case)
    evidence["retained_artifacts"] = _retained_artifact_manifest(plan)
    _refresh_summaries(evidence, plan, binding, definitions)
    return plan, binding, definitions, evidence


class FileUpscaleMappingPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_document = json.loads(DEFAULT_SELECTION_PLAN.read_text(encoding="utf-8"))
        self.confirmation_document = json.loads(DEFAULT_CONFIRMATION_PLAN.read_text(encoding="utf-8"))
        self.corpus_path = DEFAULT_SELECTION_PLAN.with_name("file-upscale-quality-corpus-v2.json")
        self.corpus_document = json.loads(self.corpus_path.read_text(encoding="utf-8"))

    def test_committed_plan_binds_exact_corpus_grid_tools_and_public_contracts(self) -> None:
        plan, binding, _, binding_sha256 = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)

        self.assertEqual(binding.selected_case_ids, EXPECTED_CASE_IDS)
        self.assertNotIn("itu-mvcds-2", binding.selected_case_ids)
        self.assertEqual(binding_sha256, self.plan_document["corpus_binding"]["sha256"])
        self.assertEqual([candidate.quality for candidate in plan.candidates], list(EXPECTED_QUALITIES))
        self.assertEqual(plan.balanced_quality, 75)
        self.assertEqual(plan.base_eye_bitrate_mbps, 20)
        self.assertEqual(plan.base_merge_quality, 75)
        self.assertEqual(plan.source_receipt.sha256, "d62f038afa796f7404bd47dabc6f84cfa47ba6e221b32a501ebc4314714c9bb6")
        self.assertEqual(plan.source_receipt.source_git_sha, "a96e6a0e21fc21e47dad6c9fec186725ef6166a3")
        self.assertEqual(plan.source_plan.sha256, "978323dccf106a1933c0e4809861d2278c882dfa5459e514e84eae4f1aa844f5")
        self.assertFalse(self.plan_document["decision_policy"]["ladder_mapping_selected"])
        self.assertTrue(self.plan_document["decision_policy"]["public_mapping_changes_forbidden"])

    def test_corpus_v2_reuses_v1_identities_and_adds_checked_segments(self) -> None:
        parsed = parse_mapping_corpus_binding(self.corpus_document)

        self.assertEqual(parsed.selected_case_ids, EXPECTED_CASE_IDS)
        snow = parsed.expected_case_sources["production-snow-detail"]
        motion = parsed.expected_case_sources["production-motion"]
        self.assertEqual(snow["start_seconds"], 1800.0)
        self.assertEqual(snow["segment_bytes"], 19493338)
        self.assertEqual(snow["segment_sha256"], "c2e3c1fd3dec3e27e91f49a99d72959b0a918aecfc227015443ff216ae289ba6")
        self.assertEqual(motion["start_seconds"], 4500.0)
        self.assertEqual(motion["segment_bytes"], 20585951)
        self.assertEqual(motion["segment_sha256"], "d2b655ce10831f8416f701c259a536c1fed815a53ca03be573f5cbcc61bcd76b")

    def test_schedule_is_exact_materialized_and_balanced(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        schedules = materialized_case_orders()

        self.assertEqual(plan.case_schedules, schedules)
        ordinal_counts = {quality: [0] * len(EXPECTED_QUALITIES) for quality in EXPECTED_QUALITIES}
        for schedule in schedules:
            for order in schedule.orders:
                self.assertEqual(set(order), set(EXPECTED_QUALITIES))
                for ordinal, quality in enumerate(order):
                    ordinal_counts[quality][ordinal] += 1
        self.assertTrue(all(counts == [3] * 7 for counts in ordinal_counts.values()))

    def test_q100_uses_canonical_factor_one(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        self.assertEqual(plan.candidates[-1].candidate_id, "q100")
        self.assertEqual(plan.candidates[-1].bitrate_scaling_factor, "1")

    def test_plan_rejects_schedule_threshold_or_public_decision_drift(self) -> None:
        changed = copy.deepcopy(self.plan_document)
        changed["execution_order"]["case_orders"][0]["orders"][0][0] = 55
        with self.assertRaisesRegex(QualificationFailure, "materialized"):
            parse_mapping_selection_plan(changed)

        changed = copy.deepcopy(self.plan_document)
        changed["source_response"]["noise_derivation"]["metrics"]["min_same_eye_ssim"]["limit"] = 0.0003
        with self.assertRaisesRegex(QualificationFailure, "quantum or limit"):
            parse_mapping_selection_plan(changed)

        changed = copy.deepcopy(self.plan_document)
        changed["decision_policy"]["ladder_mapping_selected"] = True
        with self.assertRaisesRegex(QualificationFailure, "ladder_mapping_selected"):
            parse_mapping_selection_plan(changed)

    def test_source_thresholds_recompute_only_from_raw_grouped_records(self) -> None:
        maxima = recompute_source_noise_maxima(_source_noise_receipt())
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)

        for limit in plan.noise_limits:
            source_maximum = maxima[limit.record_field]["source_maximum"]
            self.assertIsInstance(source_maximum, (int, float))
            self.assertAlmostEqual(
                float(cast(float, source_maximum)),
                limit.source_maximum,
                places=15,
            )
            self.assertEqual(limit.limit, EXPECTED_NOISE[limit.key][3])

    def test_historical_public_bindings_remain_pinned_after_route_table_v2(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        self.assertEqual(sha256_file(plan.ladder_manifest.path), PUBLIC_LADDER_SHA256)
        self.assertEqual(plan.video_quality_swift.sha256, VIDEO_QUALITY_SWIFT_SHA256)
        self.assertNotEqual(sha256_file(plan.video_quality_swift.path), VIDEO_QUALITY_SWIFT_SHA256)
        route_table = json.loads(
            (DEFAULT_SELECTION_PLAN.parents[2] / "docs/qualification/video-quality-route-table-v2.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(route_table["mapping_version"], 2)
        self.assertEqual(route_table["status"], "candidate_pending_qualification")

    def test_v2_plan_binds_exact_calibration_identity_and_thresholds(self) -> None:
        plan, binding, plan_sha256, _ = load_mapping_selection_plan(DEFAULT_CONFIRMATION_PLAN)

        self.assertEqual(plan_sha256, "c831add22aed97b629c53af76b60cd7eccf6654c088a6a73f1b5ba53b4095118")
        self.assertEqual(plan.experiment_id, "file-upscale-quality-mapping-confirmation-v2")
        self.assertEqual(plan.purpose, "objective_provisional_mapping_confirmation_not_public_ladder_mapping")
        self.assertEqual(plan.source_receipt.schema_version, 4)
        self.assertEqual(plan.source_receipt.experiment_id, "file-upscale-quality-repeatability-calibration-v2")
        self.assertEqual(plan.source_receipt.sha256, "6d44f4c23df142d3a819f0aba1b87f9fa688435485f4f1798a103ea94ccbe49e")
        self.assertEqual(plan.source_receipt.source_git_sha, "1f988fbf198595d52084eabc3055edd2f1d14221")
        self.assertEqual(plan.source_plan.sha256, "c4cf953bd868eadd04f4ed11a7ca4f2211c81f5ee72f375347f5f3d9cf14ecdb")
        self.assertEqual(binding.selected_case_ids, EXPECTED_CASE_IDS)
        self.assertEqual(
            {limit.key: limit.limit for limit in plan.noise_limits},
            {key: values[3] for key, values in EXPECTED_CALIBRATED_NOISE.items()},
        )
        self.assertEqual(len(_retained_artifact_manifest(plan)), 32)
        self.assertFalse(self.confirmation_document["decision_policy"]["ladder_mapping_selected"])

    def test_v2_threshold_dispatch_preserves_v1_and_storage_distinction(self) -> None:
        v1, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        v2, _, _, _ = load_mapping_selection_plan(DEFAULT_CONFIRMATION_PLAN)
        v1_limits = {limit.key: limit.limit for limit in v1.noise_limits}
        v2_limits = {limit.key: limit.limit for limit in v2.noise_limits}

        self.assertEqual(v1_limits, {key: values[3] for key, values in EXPECTED_NOISE.items()})
        self.assertEqual(v2_limits, {key: values[3] for key, values in EXPECTED_CALIBRATED_NOISE.items()})
        self.assertEqual(v1_limits["final_to_base_size_ratio"], 0.02)
        self.assertEqual(v2_limits["final_to_base_size_ratio"], 0.03)
        self.assertEqual(v2.minimum_case_median_storage_growth, 0.02)
        self.assertEqual(v2.minimum_minimum_frame_delta, -0.0054)
        self.assertEqual(v2.minimum_p05_delta, -0.0019)
        self.assertEqual(v2.maximum_adjacent_drop_increase, 0.0058)
        self.assertEqual(v2.real_case_minimum_frame_threshold, 0.0054)
        self.assertEqual(v2.real_case_p05_threshold, 0.0019)

    def test_v2_source_verifier_accepts_only_calibration_receipt_contract(self) -> None:
        plan, binding, _, binding_sha256 = load_mapping_selection_plan(DEFAULT_CONFIRMATION_PLAN)
        receipt = _calibration_source_receipt(plan, binding, binding_sha256)

        with patch(
            "scripts.qualify_file_upscale_quality_mapping_selection._read_frozen_source_receipt",
            return_value=receipt,
        ):
            verified = cast(dict[str, Any], verify_source_response(plan, Path("calibration-receipt.json")))

        self.assertEqual(verified["receipt"]["schema_version"], 4)
        self.assertTrue(verified["calibration_scope"]["calibration_only"])
        self.assertFalse(verified["predecessor_isolation"]["records_used_for_calibration"])
        self.assertEqual(verified["later_confirmation"]["status"], "not_performed")
        self.assertEqual(
            {key: metric["limit"] for key, metric in verified["noise_derivation"]["metrics"].items()},
            {key: values[3] for key, values in EXPECTED_CALIBRATED_NOISE.items()},
        )

    def test_v2_source_verifier_rejects_predecessor_outcomes_and_completed_confirmation(self) -> None:
        plan, binding, _, binding_sha256 = load_mapping_selection_plan(DEFAULT_CONFIRMATION_PLAN)
        receipt = _calibration_source_receipt(plan, binding, binding_sha256)
        cast(dict[str, object], receipt["predecessor"])["candidate_outcomes"] = []
        with patch(
            "scripts.qualify_file_upscale_quality_mapping_selection._read_frozen_source_receipt",
            return_value=receipt,
        ):
            with self.assertRaisesRegex(QualificationFailure, "invalid shape"):
                verify_source_response(plan, Path("calibration-receipt.json"))

        receipt = _calibration_source_receipt(plan, binding, binding_sha256)
        cast(dict[str, object], receipt["predecessor"])["records_used_for_calibration"] = True
        with patch(
            "scripts.qualify_file_upscale_quality_mapping_selection._read_frozen_source_receipt",
            return_value=receipt,
        ):
            with self.assertRaisesRegex(QualificationFailure, "predecessor isolation"):
                verify_source_response(plan, Path("calibration-receipt.json"))

        receipt = _calibration_source_receipt(plan, binding, binding_sha256)
        cast(dict[str, object], receipt["later_confirmation"])["status"] = "performed"
        with patch(
            "scripts.qualify_file_upscale_quality_mapping_selection._read_frozen_source_receipt",
            return_value=receipt,
        ):
            with self.assertRaisesRegex(QualificationFailure, "already performed"):
                verify_source_response(plan, Path("calibration-receipt.json"))

    def test_v1_new_evidence_receipt_shape_remains_frozen(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        evidence = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            {"verified": True},
            {"git_head": "f" * 40},
        )
        evidence.pop("created_at")
        evidence.pop("updated_at")
        canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()

        self.assertEqual(
            hashlib.sha256(canonical).hexdigest(), "87121a354f6fb0a48554beb40f6ce37584439fe7d84cd0f271731eae49019067"
        )


class FileUpscaleMappingAnalysisTests(unittest.TestCase):
    def test_complete_distinct_grid_selects_all_seven_and_exit_zero(self) -> None:
        _, _, _, evidence = _complete_evidence()

        self.assertEqual(
            evidence["selected_subset"]["candidate_ids"], [f"q{quality:03d}" for quality in EXPECTED_QUALITIES]
        )
        self.assertEqual(len(evidence["boundary_evaluations"]), 21)
        self.assertEqual(evidence["acceptance"]["selected_candidate_count"], 7)
        self.assertTrue(evidence["acceptance"]["objective_decision_ready"])
        self.assertEqual(exit_code_for_evidence(evidence), 0)
        mappings = {mapping["step_id"]: mapping for mapping in evidence["provisional_mappings"]}
        self.assertEqual(mappings["space_saver"]["candidate_id"], "q045")
        self.assertEqual(mappings["balanced"]["candidate_id"], "q075")
        self.assertEqual(mappings["maximum_detail"]["candidate_id"], "q100")
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_collapsed_boundary_produces_sparse_mapping_and_exit_one(self) -> None:
        _, _, _, evidence = _complete_evidence(collapse_q100=True)

        self.assertEqual(evidence["acceptance"]["selected_candidate_count"], 6)
        self.assertTrue(evidence["acceptance"]["collapsed_boundaries"])
        self.assertFalse(evidence["acceptance"]["objective_decision_ready"])
        self.assertEqual(exit_code_for_evidence(evidence), 1)
        supported = [
            mapping["candidate_id"]
            for mapping in evidence["provisional_mappings"]
            if mapping["status"] == "provisional_objective_selection"
        ]
        unsupported = [mapping for mapping in evidence["provisional_mappings"] if mapping["status"] == "unsupported"]
        self.assertEqual(len(supported), len(set(supported)))
        self.assertEqual(len(unsupported), 1)
        self.assertIsNone(unsupported[0]["candidate_id"])
        self.assertIsNone(unsupported[0]["values"])

    def test_size_cap_makes_candidate_ineligible_without_aliasing(self) -> None:
        _, _, _, evidence = _complete_evidence(oversize_q045=True)

        summary = next(item for item in evidence["candidate_summaries"] if item["id"] == "q045")
        self.assertFalse(summary["technically_eligible"])
        self.assertIn("size_cap", summary["eligibility_failures"])
        self.assertEqual(exit_code_for_evidence(evidence), 1)
        self.assertTrue(any(mapping["status"] == "unsupported" for mapping in evidence["provisional_mappings"]))

    def test_storage_tie_in_one_case_collapses_the_boundary(self) -> None:
        _, _, _, evidence = _complete_evidence(storage_tie_q045_q055=True)

        boundary = next(
            item
            for item in evidence["boundary_evaluations"]
            if item["lower_candidate_id"] == "q045" and item["higher_candidate_id"] == "q055"
        )
        self.assertFalse(boundary["storage_passed"])
        self.assertTrue(boundary["collapsed"])
        self.assertIn("storage", boundary["failure_reasons"])
        self.assertEqual(exit_code_for_evidence(evidence), 1)

    def test_summary_names_distinguish_within_case_and_cross_case_ranges(self) -> None:
        _, _, _, evidence = _complete_evidence()

        for summary in evidence["candidate_summaries"]:
            self.assertNotIn("repeat_ssim_spread", summary)
            self.assertIn("within_case_repeat_ranges", summary)
            self.assertIn("maximum_within_case_repeat_min_same_eye_ssim_range", summary)
            self.assertIn("cross_case_min_same_eye_ssim_range", summary)
            self.assertEqual(len(summary["within_case_repeat_ranges"]), 7)

    def test_downstream_checks_are_explicitly_not_objective_blockers(self) -> None:
        _, _, _, evidence = _complete_evidence()

        for check in evidence["downstream_checks"].values():
            self.assertEqual(check["status"], "not_performed")
            self.assertFalse(check["objective_stage_blocker"])
        self.assertFalse(evidence["acceptance"]["downstream_checks_block_objective_stage"])

    def test_mapping_assignment_leaves_missing_slots_unsupported(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        mappings = assign_provisional_mappings(
            plan,
            {"candidate_ids": ["q065", "q075", "q095"]},
        )

        by_step = {mapping["step_id"]: mapping for mapping in mappings}
        self.assertEqual(by_step["efficient"]["candidate_id"], "q065")
        self.assertEqual(by_step["balanced"]["candidate_id"], "q075")
        self.assertEqual(by_step["detailed"]["candidate_id"], "q095")
        self.assertEqual(by_step["space_saver"]["status"], "unsupported")
        self.assertEqual(by_step["maximum_detail"]["status"], "unsupported")

    def test_non_adjacent_failed_boundary_excludes_the_full_subset(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        eligible = ["q045", "q055", "q075"]
        boundaries = [
            {
                "lower_candidate_id": lower,
                "higher_candidate_id": higher,
                "boundary_passed": passed,
                "minimum_case_storage_coverage": 0.1,
                "objective_quality_margin": 0.1,
                "storage_margin": 0.1,
                "end_to_end_storage_coverage": 0.2,
            }
            for lower, higher, passed in (
                ("q045", "q055", True),
                ("q045", "q075", False),
                ("q055", "q075", True),
            )
        ]

        selected, _ = select_provisional_subset(plan, eligible, boundaries)

        self.assertIsNotNone(selected)
        selected = cast(dict[str, object], selected)
        self.assertEqual(selected["candidate_ids"], ["q055", "q075"])

    def test_exit_three_is_reserved_for_incomplete_resumable_evidence(self) -> None:
        self.assertEqual(
            exit_code_for_evidence(
                {
                    "acceptance": {
                        "objective_decision_ready": False,
                        "complete": False,
                        "planned_full_quality_gated_corpus": True,
                    }
                }
            ),
            3,
        )

    def test_v2_complete_confirmation_requires_all_seven_and_preserves_exit_contract(self) -> None:
        _, _, _, evidence = _complete_evidence(selection_plan=DEFAULT_CONFIRMATION_PLAN)

        self.assertEqual(
            evidence["selected_subset"]["candidate_ids"], [f"q{quality:03d}" for quality in EXPECTED_QUALITIES]
        )
        self.assertEqual(len(evidence["boundary_evaluations"]), 21)
        self.assertTrue(evidence["acceptance"]["all_seven_candidates_selected"])
        self.assertTrue(evidence["acceptance"]["balanced_technically_eligible"])
        self.assertTrue(evidence["acceptance"]["objective_decision_ready"])
        self.assertEqual(exit_code_for_evidence(evidence), 0)

        _, _, _, negative = _complete_evidence(
            selection_plan=DEFAULT_CONFIRMATION_PLAN,
            collapse_q100=True,
        )
        self.assertTrue(negative["acceptance"]["complete"])
        self.assertFalse(negative["acceptance"]["objective_decision_ready"])
        self.assertEqual(exit_code_for_evidence(negative), 1)

        plan, binding, plan_sha256, binding_sha256 = load_mapping_selection_plan(DEFAULT_CONFIRMATION_PLAN)
        incomplete = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            {"verified": True},
            {"git_head": "f" * 40},
        )
        self.assertEqual(exit_code_for_evidence(incomplete), 3)

    def test_v2_storage_growth_uses_strict_002_not_repeatability_003(self) -> None:
        plan, binding, definitions, evidence = _complete_evidence(selection_plan=DEFAULT_CONFIRMATION_PLAN)
        first_case = cast(dict[str, Any], evidence["cases"][0])
        for repeat in cast(list[dict[str, Any]], first_case["repeats"]):
            q055 = next(candidate for candidate in repeat["candidates"] if candidate["id"] == "q055")
            q055["final_bytes"] = 1834
            q055["final_to_base_size_ratio"] = 1.834
            q055["effective_bitrate_mbps"] = sweep_bitrate_mbps(1834, q055["duration_seconds"])
        _refresh_summaries(evidence, plan, binding, definitions)

        q055_summary = next(summary for summary in evidence["candidate_summaries"] if summary["id"] == "q055")
        boundary = next(
            item
            for item in evidence["boundary_evaluations"]
            if item["lower_candidate_id"] == "q045" and item["higher_candidate_id"] == "q055"
        )
        self.assertTrue(q055_summary["repeatability_passed"])
        self.assertFalse(boundary["storage_passed"])
        self.assertGreater(boundary["minimum_repeat_storage_growth_ratio"], 0.0)
        self.assertLess(boundary["minimum_case_storage_coverage"], 0.02)
        self.assertEqual(plan.noise_limits[1].limit, 0.03)
        self.assertEqual(plan.minimum_case_median_storage_growth, 0.02)
        self.assertEqual(exit_code_for_evidence(evidence), 1)

    def test_v2_cli_preserves_fatal_exit_two(self) -> None:
        with (
            patch(
                "scripts.qualify_file_upscale_quality_mapping_selection._configured_private_paths",
                return_value=(),
            ),
            patch(
                "scripts.qualify_file_upscale_quality_mapping_selection.run_mapping_selection",
                side_effect=QualificationFailure("invalid v2 source"),
            ),
        ):
            exit_code = mapping_selection_main(
                [
                    "--selection-plan",
                    str(DEFAULT_CONFIRMATION_PLAN),
                    "--source-receipt",
                    "calibration-receipt.json",
                ]
            )

        self.assertEqual(exit_code, 2)

    def test_cli_interrupt_returns_resumable_exit_three(self) -> None:
        with (
            patch(
                "scripts.qualify_file_upscale_quality_mapping_selection._configured_private_paths",
                return_value=(),
            ),
            patch(
                "scripts.qualify_file_upscale_quality_mapping_selection.run_mapping_selection",
                side_effect=KeyboardInterrupt,
            ),
        ):
            exit_code = mapping_selection_main(
                [
                    "--selection-plan",
                    str(DEFAULT_CONFIRMATION_PLAN),
                    "--source-receipt",
                    "calibration-receipt.json",
                ]
            )

        self.assertEqual(exit_code, 3)


class FileUpscaleMappingResumePrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, self.binding, self.plan_sha256, self.binding_sha256 = load_mapping_selection_plan(
            DEFAULT_SELECTION_PLAN
        )
        _, self.definitions = _definitions(self.plan)
        self.source_response = {"verified": True}
        self.environment = {"git_head": "f" * 40}

    def _partial_evidence(self) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            _new_evidence(
                self.plan,
                self.binding,
                self.plan_sha256,
                self.binding_sha256,
                self.source_response,
                self.environment,
            ),
        )

    def _load(self, root: Path, evidence: dict[str, Any]):
        output = root / "receipt.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.chmod(0o644)
        artifact_directory = root / "artifacts"
        artifact_directory.mkdir()
        return _load_resume_evidence(
            output,
            plan=self.plan,
            binding=self.binding,
            plan_sha256=self.plan_sha256,
            binding_sha256=self.binding_sha256,
            source_response=self.source_response,
            environment=self.environment,
            definitions=self.definitions,
            private_paths=(),
            artifact_directory=artifact_directory,
        )

    def test_resume_rejects_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._partial_evidence()
            evidence["schema_version"] = 99
            with self.assertRaisesRegex(QualificationFailure, "unsupported schema"):
                self._load(Path(temporary), evidence)

    def test_resume_rejects_materialized_order_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._partial_evidence()
            definition = self.definitions[EXPECTED_CASE_IDS[0]]
            case = sweep_case_record(definition, self.binding)
            cast(list[object], case["repeats"]).append(
                {
                    "repeat_index": 0,
                    "order": list(reversed(EXPECTED_QUALITIES)),
                    "base": None,
                    "candidates": [],
                }
            )
            cast(list[object], evidence["cases"]).append(case)
            with self.assertRaisesRegex(QualificationFailure, "repeat order changed"):
                self._load(Path(temporary), evidence)

    def test_resume_rejects_private_absolute_path_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            evidence = self._partial_evidence()
            evidence["case_summaries"] = [{"debug": "/Users/private/source.m2ts"}]
            with self.assertRaisesRegex(QualificationFailure, "private source information"):
                self._load(Path(temporary), evidence)

    def test_source_receipt_requires_regular_frozen_0444_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "source.json"
            data = b"{}\n"
            path.write_bytes(data)
            binding = SourceReceiptBinding(
                schema_version=2,
                experiment_id="file-upscale-quality-sweep-v1",
                sha256=hashlib.sha256(data).hexdigest(),
                source_git_sha="a" * 40,
                required_file_mode=0o444,
            )
            path.chmod(0o644)
            with self.assertRaisesRegex(QualificationFailure, "0444"):
                _read_frozen_source_receipt(path, binding)
            path.chmod(0o444)
            self.assertEqual(_read_frozen_source_receipt(path, binding), {})

    def test_artifact_paths_cannot_escape_or_record_private_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(QualificationFailure, "unsafe"):
                _artifact_path(root, "/Users/private/source.mov")
            with self.assertRaisesRegex(QualificationFailure, "unsafe"):
                _artifact_path(root, "../source.mov")
            safe = _artifact_path(root, "production-dark/repeat-1/q075-upscaled.mov")
            self.assertTrue(str(safe).startswith(str(root.resolve())))

            target = root / "target.mov"
            target.write_bytes(b"must-survive")
            symlink = root / "production-dark/repeat-1/q075-upscaled.mov"
            symlink.parent.mkdir(parents=True)
            symlink.symlink_to(target)
            with self.assertRaisesRegex(QualificationFailure, "must not use symlinks"):
                _artifact_path(root, "production-dark/repeat-1/q075-upscaled.mov")
            self.assertEqual(target.read_bytes(), b"must-survive")

    def test_incomplete_resume_discards_only_expected_unrecorded_crash_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._partial_evidence()
            output = root / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output.chmod(0o644)
            artifact_directory = root / "artifacts"
            expected = artifact_directory / "production-dark/repeat-1/generated-base.mov"
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"crash-window")

            loaded = _load_resume_evidence(
                output,
                plan=self.plan,
                binding=self.binding,
                plan_sha256=self.plan_sha256,
                binding_sha256=self.binding_sha256,
                source_response=self.source_response,
                environment=self.environment,
                definitions=self.definitions,
                private_paths=(),
                artifact_directory=artifact_directory,
            )

            self.assertEqual(loaded, evidence)
            self.assertFalse(expected.exists())
            (artifact_directory / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "unrecorded or missing media"):
                _load_resume_evidence(
                    output,
                    plan=self.plan,
                    binding=self.binding,
                    plan_sha256=self.plan_sha256,
                    binding_sha256=self.binding_sha256,
                    source_response=self.source_response,
                    environment=self.environment,
                    definitions=self.definitions,
                    private_paths=(),
                    artifact_directory=artifact_directory,
                )

    def test_incomplete_resume_rejects_expected_symlink_without_deleting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = self._partial_evidence()
            output = root / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output.chmod(0o644)
            artifact_directory = root / "artifacts"
            target = artifact_directory / "target.mov"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"must-survive")
            expected = artifact_directory / "production-dark/repeat-1/generated-base.mov"
            expected.parent.mkdir(parents=True)
            expected.symlink_to(target)

            with self.assertRaisesRegex(QualificationFailure, "must not use symlinks"):
                _load_resume_evidence(
                    output,
                    plan=self.plan,
                    binding=self.binding,
                    plan_sha256=self.plan_sha256,
                    binding_sha256=self.binding_sha256,
                    source_response=self.source_response,
                    environment=self.environment,
                    definitions=self.definitions,
                    private_paths=(),
                    artifact_directory=artifact_directory,
                )

            self.assertEqual(target.read_bytes(), b"must-survive")
            self.assertTrue(expected.is_symlink())

    def test_completed_writable_receipt_is_recovered_to_frozen_mode(self) -> None:
        for selection_plan in (DEFAULT_SELECTION_PLAN, DEFAULT_CONFIRMATION_PLAN):
            with self.subTest(selection_plan=selection_plan.name), tempfile.TemporaryDirectory() as temporary:
                plan, binding, definitions, evidence = _complete_evidence(selection_plan=selection_plan)
                _, _, plan_sha256, binding_sha256 = load_mapping_selection_plan(selection_plan)
                root = Path(temporary)
                artifact_directory = root / "artifacts"
                artifact_directory.mkdir()
                artifacts = []
                for case_id in plan.retained_case_ids:
                    case = next(case for case in evidence["cases"] if case["id"] == case_id)
                    repeat = case["repeats"][plan.retained_repeat_index]
                    base = repeat["base"]
                    base_data = case_id.encode().ljust(base["bytes"], b"b")
                    base_sha256 = hashlib.sha256(base_data).hexdigest()
                    base["sha256"] = base_sha256
                    artifacts.append(
                        {
                            "artifact_id": f"{case_id}-r1-base",
                            "case_id": case_id,
                            "repeat_index": 0,
                            "kind": "generated_base",
                            "candidate_id": None,
                            "path": f"{case_id}/repeat-1/generated-base.mov",
                            "bytes": len(base_data),
                            "sha256": base_sha256,
                        }
                    )
                    for candidate in repeat["candidates"]:
                        candidate["base_sha256"] = base_sha256
                        candidate["input_copy_sha256"] = base_sha256
                        candidate_data = candidate["id"].encode().ljust(candidate["final_bytes"], b"c")
                        candidate_sha256 = hashlib.sha256(candidate_data).hexdigest()
                        candidate["final_sha256"] = candidate_sha256
                        artifacts.append(
                            {
                                "artifact_id": f"{case_id}-r1-{candidate['id']}",
                                "case_id": case_id,
                                "repeat_index": 0,
                                "kind": "candidate_output",
                                "candidate_id": candidate["id"],
                                "path": f"{case_id}/repeat-1/{candidate['id']}-upscaled.mov",
                                "bytes": len(candidate_data),
                                "sha256": candidate_sha256,
                            }
                        )
                        artifact_path = artifact_directory / str(artifacts[-1]["path"])
                        artifact_path.parent.mkdir(parents=True, exist_ok=True)
                        artifact_path.write_bytes(candidate_data)
                    base_path = artifact_directory / str(artifacts[-(len(plan.candidates) + 1)]["path"])
                    base_path.parent.mkdir(parents=True, exist_ok=True)
                    base_path.write_bytes(base_data)
                evidence["retained_artifacts"] = artifacts
                _refresh_summaries(evidence, plan, binding, definitions)
                evidence["acceptance"]["finalized"] = True
                output = root / "receipt.json"
                output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                output.chmod(0o644)

                _load_resume_evidence(
                    output,
                    plan=plan,
                    binding=binding,
                    plan_sha256=plan_sha256,
                    binding_sha256=binding_sha256,
                    source_response=self.source_response,
                    environment=self.environment,
                    definitions=definitions,
                    private_paths=(),
                    artifact_directory=artifact_directory,
                )

                self.assertEqual(len(artifacts), 32)
                self.assertEqual(output.stat().st_mode & 0o777, 0o444)
                (artifact_directory / "orphan.mov").write_bytes(b"orphan")
                with self.assertRaisesRegex(QualificationFailure, "unrecorded or missing media"):
                    _load_resume_evidence(
                        output,
                        plan=plan,
                        binding=binding,
                        plan_sha256=plan_sha256,
                        binding_sha256=binding_sha256,
                        source_response=self.source_response,
                        environment=self.environment,
                        definitions=definitions,
                        private_paths=(),
                        artifact_directory=artifact_directory,
                    )

    def test_orphan_work_entry_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / ".bd-to-avp-file-upscale-quality-mapping-selection.json"
            marker.write_text("{}", encoding="utf-8")
            _validate_clean_work_directory(root)
            (root / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "orphaned"):
                _validate_clean_work_directory(root)

    def test_completed_work_cleanup_removes_case_state_and_rejects_other_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / ".bd-to-avp-file-upscale-quality-mapping-selection.json"
            marker.write_text("{}", encoding="utf-8")
            case_directory = root / "production-dark"
            case_directory.mkdir()
            (case_directory / "stale.mov").write_bytes(b"stale")

            _cleanup_completed_work_directory(root, ("production-dark",))

            self.assertFalse(case_directory.exists())
            (root / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "orphaned"):
                _cleanup_completed_work_directory(root, ("production-dark",))

    def test_retained_artifact_manifest_records_relative_hash_bound_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "candidate.mov"
            source.write_bytes(b"candidate-bytes")
            artifacts = root / "artifacts"
            artifacts.mkdir()

            entry = _retained_artifact_entry(
                artifact_directory=artifacts,
                source_path=source,
                case_id="production-dark",
                repeat_index=0,
                kind="candidate_output",
                candidate_id="q075",
                move=False,
            )

            self.assertEqual(entry["path"], "production-dark/repeat-1/q075-upscaled.mov")
            self.assertFalse(str(entry["path"]).startswith("/"))
            self.assertEqual(entry["sha256"], hashlib.sha256(b"candidate-bytes").hexdigest())
            self.assertEqual(entry["bytes"], len(b"candidate-bytes"))


if __name__ == "__main__":
    unittest.main()
