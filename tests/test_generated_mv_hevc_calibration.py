import copy
import hashlib
import json
import stat
import tempfile
import unittest

from dataclasses import replace
from itertools import product
from pathlib import Path
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import CURRENT_REQUIRED_BOX_TYPES, QualificationFailure
from scripts.qualify_generated_mv_hevc_calibration import (
    DEFAULT_EXPERIMENT_PLAN,
    DEFAULT_BITRATE_SEARCH_PLAN,
    DEFAULT_REFINEMENT_PLAN,
    CorpusBinding,
    ExperimentCell,
    ExperimentPlan,
    _assert_private_values_absent,
    _cell_order,
    _completed_resume_is_consistent,
    _freeze_receipt,
    _load_resume_evidence,
    load_corpus_binding,
    _new_evidence,
    _prepare_owned_work_directory,
    _refresh_summaries,
    _reset_case_directory,
    _summarize_measurement_runs,
    _threshold_record,
    _validate_run_record,
    _verify_bitrate_search_source_receipts,
    _verify_refinement_source_receipt,
    calibration_lock,
    load_experiment_plan,
    main,
    parse_corpus_binding,
    parse_experiment_plan,
)
from scripts.qualify_mv_hevc_corpus import (
    CorpusCase,
    PreparedCase,
    _encode_generated,
    _measure_output,
    summarize_frame_quality,
)


class GeneratedCalibrationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_EXPERIMENT_PLAN.read_text(encoding="utf-8"))
        self.refinement_document = json.loads(DEFAULT_REFINEMENT_PLAN.read_text(encoding="utf-8"))
        self.bitrate_search_document = json.loads(DEFAULT_BITRATE_SEARCH_PLAN.read_text(encoding="utf-8"))
        self.binding_document = json.loads(
            DEFAULT_EXPERIMENT_PLAN.with_name("generated-mv-hevc-corpus-v1.json").read_text(encoding="utf-8")
        )

    def test_committed_binding_and_plan_are_valid_and_exploratory(self) -> None:
        plan, binding, plan_digest, binding_digest = load_experiment_plan(DEFAULT_EXPERIMENT_PLAN)

        self.assertEqual(
            binding.selected_case_ids,
            (
                "production-dark",
                "production-grain-rain",
                "production-motion",
                "synthetic-animation",
            ),
        )
        self.assertEqual(plan.balanced_eye_bitrate_mbps, 20)
        self.assertEqual(plan.balanced_merge_quality, 75)
        self.assertEqual(plan.runs_per_cell, 3)
        self.assertEqual(plan.eye_bitrates_mbps, (16, 20, 24))
        self.assertEqual(plan.merge_qualities, (65, 75, 85))
        self.assertEqual(len(plan.cells), 9)
        self.assertEqual(plan.cells[4].cell_id, "b020-m075")
        self.assertEqual(len(plan_digest), 64)
        self.assertEqual(len(binding_digest), 64)

    def test_rejects_balanced_values_that_diverge_from_production_defaults(self) -> None:
        document = copy.deepcopy(self.document)
        document["balanced"]["eye_bitrate_mbps"] = 21

        with self.assertRaisesRegex(QualificationFailure, "production generated default"):
            parse_experiment_plan(document)

    def test_rejects_balanced_value_outside_center_level(self) -> None:
        document = copy.deepcopy(self.document)
        document["axes"]["eye_bitrate_mbps"] = [16, 19, 20]

        with self.assertRaisesRegex(QualificationFailure, "center level"):
            parse_experiment_plan(document)

    def test_rejects_policy_that_selects_ladder_mapping(self) -> None:
        document = copy.deepcopy(self.document)
        document["decision_policy"]["ladder_mapping_selected"] = True

        with self.assertRaisesRegex(QualificationFailure, "must not select"):
            parse_experiment_plan(document)

    def test_committed_refinement_plan_pins_thresholds_without_selecting_mapping(self) -> None:
        plan, binding, plan_digest, binding_digest = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)

        self.assertEqual(plan.schema_version, 2)
        self.assertEqual(plan.eye_bitrates_mbps, (20,))
        self.assertEqual(plan.merge_qualities, (65, 68, 71, 75, 79, 82, 85))
        self.assertEqual(plan.cells[3].cell_id, "b020-m075")
        self.assertEqual(plan.decision_stage, "merge_response_refinement_only")
        self.assertIsNotNone(plan.source_evidence)
        self.assertIsNotNone(plan.thresholds)
        assert plan.source_evidence is not None
        assert plan.thresholds is not None
        self.assertEqual(
            plan.source_evidence.receipt_sha256,
            "fe3c81e96771f9d0f4dc1f6461556d5fdf22c95034776b044f268978d68bd07f",
        )
        self.assertEqual(plan.thresholds.aggregate_quality_non_inferiority_margin, 0.0006)
        self.assertEqual(binding.binding_id, "generated-mv-hevc-stress-v1")
        self.assertEqual(len(plan_digest), 64)
        self.assertEqual(len(binding_digest), 64)

    def test_refinement_rejects_changed_merge_levels(self) -> None:
        document = copy.deepcopy(self.refinement_document)
        document["axes"]["merge_quality"][1] = 69

        with self.assertRaisesRegex(QualificationFailure, "checked seven-level"):
            parse_experiment_plan(document)

    def test_refinement_rejects_threshold_below_checked_noise_floor(self) -> None:
        document = copy.deepcopy(self.refinement_document)
        document["pre_registered_thresholds"]["aggregate_quality_distinguishability"] = 0.0005

        with self.assertRaisesRegex(QualificationFailure, "checked rounded 2x noise derivation"):
            parse_experiment_plan(document)

    def test_refinement_rejects_output_cap_below_distinguishable_growth(self) -> None:
        document = copy.deepcopy(self.refinement_document)
        document["pre_registered_thresholds"]["maximum_output_size_ratio"] = 1.0

        with self.assertRaisesRegex(QualificationFailure, "checked rounded 2x noise derivation"):
            parse_experiment_plan(document)

    def test_refinement_rejects_more_permissive_threshold(self) -> None:
        document = copy.deepcopy(self.refinement_document)
        document["pre_registered_thresholds"]["repeat_ssim_spread_limit"] = 0.001

        with self.assertRaisesRegex(QualificationFailure, "checked rounded 2x noise derivation"):
            parse_experiment_plan(document)

    def test_committed_bitrate_search_plan_covers_exact_integer_frontiers(self) -> None:
        plan, binding, plan_digest, binding_digest = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)

        self.assertEqual(plan.schema_version, 3)
        self.assertEqual(plan.eye_bitrates_mbps, tuple(range(1, 21)))
        self.assertEqual(plan.merge_qualities, (65, 75))
        self.assertEqual(len(plan.cells), 40)
        self.assertEqual(plan.decision_stage, "same_tier_bitrate_minimization_only")
        self.assertIsNotNone(plan.bitrate_search)
        self.assertIsNotNone(plan.thresholds)
        assert plan.bitrate_search is not None
        self.assertEqual(plan.bitrate_search.accepted_merge_qualities, (65, 75))
        self.assertEqual(plan.bitrate_search.guided_anchor_count, 2)
        self.assertTrue(plan.bitrate_search.custom_exact_retained)
        self.assertEqual(binding.binding_id, "generated-mv-hevc-stress-v1")
        self.assertEqual(len(plan_digest), 64)
        self.assertEqual(len(binding_digest), 64)

    def test_bitrate_search_rejects_incomplete_integer_grid(self) -> None:
        document = copy.deepcopy(self.bitrate_search_document)
        document["axes"]["eye_bitrate_mbps"].remove(11)

        with self.assertRaisesRegex(QualificationFailure, "exactly 20 levels"):
            parse_experiment_plan(document)

    def test_bitrate_search_rejects_changed_product_decision(self) -> None:
        document = copy.deepcopy(self.bitrate_search_document)
        document["bitrate_search"]["accepted_merge_qualities"] = [65, 71, 75]

        with self.assertRaisesRegex(QualificationFailure, r"must be \[65, 75\]"):
            parse_experiment_plan(document)

    def test_bitrate_search_rejects_post_hoc_storage_threshold(self) -> None:
        document = copy.deepcopy(self.bitrate_search_document)
        document["bitrate_search"]["minimum_aggregate_storage_reduction_ratio"] = 0.01

        with self.assertRaisesRegex(QualificationFailure, "must be 2%"):
            parse_experiment_plan(document)

    def test_bitrate_search_latin_order_balances_every_cell_across_execution_windows(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        orders = [_cell_order(plan, run_index) for run_index in range(plan.runs_per_cell)]
        window_bounds = ((14, 27), (13, 26), (13, 27))

        for cell in plan.cells:
            windows: list[str] = []
            for order, (early_end, middle_end) in zip(orders, window_bounds, strict=True):
                position = order.index(cell)
                windows.append("early" if position < early_end else "middle" if position < middle_end else "late")
            self.assertEqual(set(windows), {"early", "middle", "late"})

        for anchor_id in ("b020-m065", "b020-m075"):
            anchor = next(cell for cell in plan.cells if cell.cell_id == anchor_id)
            self.assertEqual(
                [order.index(anchor) for order in orders],
                [
                    38 if anchor_id.endswith("065") else 39,
                    24 if anchor_id.endswith("065") else 25,
                    11 if anchor_id.endswith("065") else 12,
                ],
            )

    def test_plan_and_binding_loaders_reject_duplicate_json_keys(self) -> None:
        build_directory = DEFAULT_EXPERIMENT_PLAN.parents[2] / "build"
        build_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_directory) as temporary_directory:
            root = Path(temporary_directory)
            plan_path = root / "plan.json"
            binding_path = root / "binding.json"
            plan_path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
            binding_path.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "duplicate key"):
                load_experiment_plan(plan_path)
            with self.assertRaisesRegex(QualificationFailure, "duplicate key"):
                load_corpus_binding(binding_path)

    def test_rejects_duplicate_binding_cases(self) -> None:
        document = copy.deepcopy(self.binding_document)
        document["selected_case_ids"].append(document["selected_case_ids"][0])

        with self.assertRaisesRegex(QualificationFailure, "non-empty and unique"):
            parse_corpus_binding(document)


class GeneratedCalibrationSummaryTests(unittest.TestCase):
    @staticmethod
    def _plan(*, runs_per_cell: int = 2) -> ExperimentPlan:
        eye_bitrates = (16, 20, 24)
        merge_qualities = (65, 75, 85)
        cells = tuple(
            ExperimentCell(f"b{bitrate:03d}-m{merge:03d}", bitrate, merge)
            for bitrate, merge in product(eye_bitrates, merge_qualities)
        )
        return ExperimentPlan(
            experiment_id="test",
            binding_path=DEFAULT_EXPERIMENT_PLAN.with_name("generated-mv-hevc-corpus-v1.json"),
            binding_id="binding",
            binding_sha256="a" * 64,
            balanced_eye_bitrate_mbps=20,
            balanced_merge_quality=75,
            runs_per_cell=runs_per_cell,
            eye_bitrates_mbps=eye_bitrates,
            merge_qualities=merge_qualities,
            cells=cells,
            ffmpeg_manifest_path=DEFAULT_EXPERIMENT_PLAN.parents[2] / "vendor/ffmpeg-macos-arm64.toml",
            ffmpeg_manifest_sha256="f" * 64,
            generated_encoder_contract="production_generated_mv_hevc_v1",
            metric_contract="ffmpeg_ssim_aggregate_and_per_frame_v1",
            relative_path="docs/test-plan.json",
        )

    @staticmethod
    def _binding(*case_ids: str) -> CorpusBinding:
        return CorpusBinding(
            binding_id="binding",
            source_manifest_path=DEFAULT_EXPERIMENT_PLAN.with_name("direct-mv-hevc-corpus-v1.json"),
            source_corpus_id="corpus",
            source_manifest_sha256="b" * 64,
            selected_case_ids=case_ids,
            expected_case_sources={
                case_id: {
                    "filter_sha256": "a" * 64,
                    "kind": "synthetic",
                    "requested_duration_seconds": 4.0,
                }
                for case_id in case_ids
            },
            required_coverage=("animation",),
            relative_path="docs/test-binding.json",
        )

    @staticmethod
    def _run(
        cell: ExperimentCell,
        run_index: int,
        quality: float,
        size: int,
        elapsed: float,
    ) -> dict[str, object]:
        left_match = quality
        right_match = quality + 0.0001
        left_cross = quality - 0.2
        right_cross = quality - 0.1999
        return {
            "codec_name": "hevc",
            "codec_tag_string": "hvc1",
            "duration_seconds": 4.0,
            "effective_bitrate_mbps": size / 100,
            "elapsed_seconds": elapsed,
            "eye_bitrate_mbps": cell.eye_bitrate_mbps,
            "final_bytes": size,
            "frame_count": 96,
            "frame_rate": "24",
            "frame_quality_sample_count": 96,
            "frame_ssim_standard_deviation": 0.001,
            "left_cross_ssim": left_cross,
            "left_match_ssim": left_match,
            "merge_quality": cell.merge_quality,
            "maximum_adjacent_frame_ssim_drop": 0.002,
            "median_frame_same_eye_ssim": quality,
            "min_eye_order_margin": min(left_match - left_cross, right_match - right_cross),
            "min_same_eye_ssim": min(left_match, right_match),
            "minimum_frame_same_eye_ssim": quality - 0.01,
            "observed_box_types": sorted(CURRENT_REQUIRED_BOX_TYPES),
            "p05_frame_same_eye_ssim": quality - 0.005,
            "right_cross_ssim": right_cross,
            "right_match_ssim": right_match,
            "run_index": run_index,
            "sha256": "c" * 64,
            "system_cpu_seconds": 0.2,
            "target_total_eye_bitrate_mbps": cell.eye_bitrate_mbps * 2,
            "user_cpu_seconds": 0.4,
        }

    @classmethod
    def _case_record(cls, plan: ExperimentPlan, case_id: str = "case-a") -> dict[str, object]:
        cells: list[dict[str, object]] = []
        for bitrate_index, bitrate in enumerate(plan.eye_bitrates_mbps):
            for merge_index, merge in enumerate(plan.merge_qualities):
                cell = next(
                    candidate
                    for candidate in plan.cells
                    if candidate.eye_bitrate_mbps == bitrate and candidate.merge_quality == merge
                )
                base_quality = 0.95 + bitrate_index * 0.01 + merge_index * 0.001
                base_size = 1_000 + bitrate_index * 200 + merge_index * 20
                runs = [
                    cls._run(cell, run_index, base_quality + run_index * 0.0001, base_size + run_index, 2 + run_index)
                    for run_index in range(plan.runs_per_cell)
                ]
                cells.append(
                    {
                        "id": cell.cell_id,
                        "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                        "merge_quality": cell.merge_quality,
                        "runs": runs,
                        "summary": None,
                    }
                )
        return {"id": case_id, "cells": cells}

    @classmethod
    def _bitrate_case_record(
        cls,
        plan: ExperimentPlan,
        case_id: str = "case-a",
        *,
        frontiers: dict[int, int] | None = None,
        storage_benefit: bool = True,
    ) -> dict[str, object]:
        frontier_by_merge = frontiers or {65: 5, 75: 8}
        cells: list[dict[str, object]] = []
        for cell in plan.cells:
            anchor_quality = 0.95 if cell.merge_quality == 65 else 0.96
            if cell.eye_bitrate_mbps == 20:
                quality = anchor_quality
            elif cell.eye_bitrate_mbps >= frontier_by_merge[cell.merge_quality]:
                quality = anchor_quality - 0.0002
            else:
                quality = anchor_quality - 0.002
            anchor_size = 10_000 if cell.merge_quality == 65 else 20_000
            if storage_benefit:
                size = int(anchor_size * (0.5 + 0.5 * cell.eye_bitrate_mbps / 20))
            else:
                size = anchor_size
            runs = [
                cls._run(cell, run_index, quality + run_index * 0.00005, size + run_index, 2 + run_index)
                for run_index in range(plan.runs_per_cell)
            ]
            cells.append(
                {
                    "id": cell.cell_id,
                    "eye_bitrate_mbps": cell.eye_bitrate_mbps,
                    "merge_quality": cell.merge_quality,
                    "runs": runs,
                    "summary": None,
                }
            )
        return {"id": case_id, "cells": cells}

    @staticmethod
    def _definition(case_id: str = "case-a") -> CorpusCase:
        return CorpusCase(
            case_id=case_id,
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )

    def test_summarize_runs_reports_per_eye_medians_and_repeat_spread(self) -> None:
        plan = self._plan()
        cell = plan.cells[0]
        runs = [self._run(cell, 0, 0.95, 1000, 2), self._run(cell, 1, 0.96, 1100, 4)]

        summary = _summarize_measurement_runs(runs)

        self.assertAlmostEqual(summary["median_min_same_eye_ssim"], 0.955)
        self.assertAlmostEqual(summary["median_left_match_ssim"], 0.955)
        self.assertAlmostEqual(summary["repeat_ssim_spread"], 0.01)
        self.assertEqual(summary["median_final_bytes"], 1050)

    def test_refresh_summaries_anchors_balanced_and_records_interactions(self) -> None:
        plan = self._plan()
        binding = self._binding("case-a")
        evidence: dict[str, object] = {"cases": [self._case_record(plan)]}
        definition = self._definition()

        _refresh_summaries(evidence, plan, binding, {"case-a": definition})

        balanced = next(summary for summary in evidence["cell_summaries"] if summary["id"] == "b020-m075")
        self.assertEqual(balanced["worst_case_quality_delta"], 0.0)
        self.assertEqual(balanced["worst_case_output_size_ratio"], 1.0)
        self.assertEqual(evidence["baseline_repeatability"]["case_count"], 1)
        self.assertEqual(len(evidence["interaction_observations"]), 4)
        self.assertTrue(evidence["acceptance"]["experiment_complete"])
        self.assertFalse(evidence["acceptance"]["thresholds_selected"])
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_completed_schema_one_receipt_tolerates_absent_refinement_fields(self) -> None:
        plan = self._plan()
        binding = self._binding("case-a")
        definition = self._definition()
        evidence: dict[str, object] = {"cases": [self._case_record(plan)]}
        _refresh_summaries(evidence, plan, binding, {"case-a": definition})
        evidence.pop("refinement_cell_evaluations")
        evidence.pop("refinement_adjacent_evaluations")
        acceptance = evidence["acceptance"]
        for key in (
            "thresholds_pre_registered",
            "thresholds_evaluated",
            "refinement_evidence_ready",
            "refinement_decision_ready",
            "technically_eligible_cell_count",
            "ambiguous_adjacent_count",
        ):
            acceptance.pop(key)

        self.assertTrue(_completed_resume_is_consistent(evidence, plan, binding, {"case-a": definition}))

    def test_subset_execution_does_not_claim_planned_stress_corpus(self) -> None:
        plan = self._plan()
        binding = self._binding("case-a", "case-b")
        evidence: dict[str, object] = {"cases": [self._case_record(plan)]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        self.assertTrue(evidence["acceptance"]["execution_passed"])
        self.assertFalse(evidence["acceptance"]["planned_stress_corpus"])
        self.assertFalse(evidence["acceptance"]["experiment_complete"])

    def test_partial_cells_do_not_claim_eye_order_pass(self) -> None:
        plan = self._plan()
        binding = self._binding("case-a")
        case = self._case_record(plan)
        case["cells"][0]["runs"] = []
        evidence: dict[str, object] = {"cases": [case]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        self.assertFalse(evidence["acceptance"]["complete"])
        self.assertFalse(evidence["acceptance"]["eye_order_passed"])

    def test_axis_findings_report_quality_reversal_without_selecting_winner(self) -> None:
        plan = self._plan()
        binding = self._binding("case-a")
        case = self._case_record(plan)
        reversed_cell = next(cell for cell in case["cells"] if cell["id"] == "b024-m075")
        for run in reversed_cell["runs"]:
            run["left_match_ssim"] = 0.90
            run["right_match_ssim"] = 0.9001
            run["left_cross_ssim"] = 0.70
            run["right_cross_ssim"] = 0.7001
            run["min_same_eye_ssim"] = 0.90
            run["min_eye_order_margin"] = 0.2
        evidence: dict[str, object] = {"cases": [case]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        self.assertTrue(any(finding["code"] == "quality_reversal" for finding in evidence["axis_findings"]))
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_cell_order_alternates_to_reduce_order_bias(self) -> None:
        plan = self._plan()

        self.assertEqual(_cell_order(plan, 0), plan.cells)
        self.assertEqual(_cell_order(plan, 1), plan.cells[3:] + plan.cells[:3])
        self.assertEqual(_cell_order(plan, 2), plan.cells[6:] + plan.cells[:6])

    def test_refinement_cell_order_rotates_seven_cells_across_repeats(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)

        orders = [tuple(cell.cell_id for cell in _cell_order(plan, index)) for index in range(3)]

        self.assertEqual([order[0] for order in orders], ["b020-m065", "b020-m071", "b020-m079"])
        self.assertEqual({cell_id for order in orders for cell_id in order}, {cell.cell_id for cell in plan.cells})

    def test_refinement_refresh_evaluates_thresholds_without_selecting_mapping(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        binding = self._binding("case-a")
        evidence: dict[str, object] = {"cases": [self._case_record(plan)]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        self.assertEqual(len(evidence["refinement_cell_evaluations"]), 7)
        self.assertEqual(len(evidence["refinement_adjacent_evaluations"]), 6)
        self.assertTrue(evidence["acceptance"]["thresholds_pre_registered"])
        self.assertTrue(evidence["acceptance"]["thresholds_evaluated"])
        self.assertTrue(evidence["acceptance"]["refinement_evidence_ready"])
        self.assertFalse(evidence["acceptance"]["ladder_evidence_ready"])
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_refinement_allows_intentional_quality_loss_for_lower_merge_tier(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        binding = self._binding("case-a")
        evidence: dict[str, object] = {"cases": [self._case_record(plan)]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluation = next(item for item in evidence["refinement_cell_evaluations"] if item["cell_id"] == "b020-m065")
        case_evaluation = evaluation["case_evaluations"][0]
        self.assertFalse(case_evaluation["quality_non_inferiority_passed"])
        self.assertTrue(evaluation["candidate_constraints_passed"])

    def test_refinement_disqualifies_quality_regression_for_higher_merge_tier(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        binding = self._binding("case-a")
        case = self._case_record(plan)
        higher = next(cell for cell in case["cells"] if cell["id"] == "b020-m079")
        for run_index, run in enumerate(higher["runs"]):
            run.update(
                {
                    "left_match_ssim": 0.90,
                    "right_match_ssim": 0.9001,
                    "left_cross_ssim": 0.70,
                    "right_cross_ssim": 0.7001,
                    "min_same_eye_ssim": 0.90,
                    "min_eye_order_margin": 0.2,
                    "minimum_frame_same_eye_ssim": 0.89,
                    "p05_frame_same_eye_ssim": 0.895,
                    "median_frame_same_eye_ssim": 0.90,
                    "final_bytes": 1300 + run_index,
                }
            )
        evidence: dict[str, object] = {"cases": [case]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluation = next(item for item in evidence["refinement_cell_evaluations"] if item["cell_id"] == "b020-m079")
        self.assertFalse(evaluation["case_evaluations"][0]["quality_non_inferiority_passed"])
        self.assertFalse(evaluation["candidate_constraints_passed"])

    def test_refinement_marks_pair_ambiguous_when_one_case_is_not_distinct(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        binding = self._binding("case-a", "case-b", "case-c")
        cases = [self._case_record(plan, case_id) for case_id in binding.selected_case_ids]
        ambiguous_case = cases[-1]
        lower = next(cell for cell in ambiguous_case["cells"] if cell["id"] == "b020-m065")
        higher = next(cell for cell in ambiguous_case["cells"] if cell["id"] == "b020-m068")
        for lower_run, higher_run in zip(lower["runs"], higher["runs"], strict=True):
            for key in (
                "left_match_ssim",
                "right_match_ssim",
                "min_same_eye_ssim",
                "minimum_frame_same_eye_ssim",
                "p05_frame_same_eye_ssim",
                "median_frame_same_eye_ssim",
            ):
                higher_run[key] = lower_run[key]
        definitions = {case_id: self._definition(case_id) for case_id in binding.selected_case_ids}
        evidence: dict[str, object] = {"cases": cases}

        _refresh_summaries(evidence, plan, binding, definitions)

        evaluation = next(
            item
            for item in evidence["refinement_adjacent_evaluations"]
            if item["lower_cell_id"] == "b020-m065" and item["higher_cell_id"] == "b020-m068"
        )
        self.assertFalse(evaluation["quality_distinct"])
        self.assertTrue(evaluation["requires_collapse_or_blinded_review"])

    def test_refinement_eye_order_failure_disqualifies_cell(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        binding = self._binding("case-a")
        case = self._case_record(plan)
        failed_cell = next(cell for cell in case["cells"] if cell["id"] == "b020-m079")
        for run in failed_cell["runs"]:
            run["min_eye_order_margin"] = 0.0
        evidence: dict[str, object] = {"cases": [case]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluation = next(item for item in evidence["refinement_cell_evaluations"] if item["cell_id"] == "b020-m079")
        self.assertFalse(evaluation["candidate_constraints_passed"])
        self.assertFalse(evidence["acceptance"]["eye_order_passed"])

    def test_bitrate_search_selects_exact_same_tier_integer_frontiers(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        binding = self._binding("case-a")
        evidence: dict[str, object] = {"cases": [self._bitrate_case_record(plan)]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluations = {item["merge_quality"]: item for item in evidence["bitrate_tier_evaluations"]}
        self.assertEqual(evaluations[65]["core_minimum_cell_id"], "b005-m065")
        self.assertEqual(evaluations[65]["selected_cell_id"], "b005-m065")
        self.assertEqual(evaluations[75]["core_minimum_cell_id"], "b008-m075")
        self.assertEqual(evaluations[75]["selected_cell_id"], "b008-m075")
        self.assertTrue(evaluations[65]["frontier_monotone"])
        self.assertTrue(evaluations[75]["minimization_adopted"])
        self.assertTrue(evidence["acceptance"]["bitrate_search_ready"])
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_bitrate_search_fails_closed_on_non_monotone_frontier(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        binding = self._binding("case-a")
        case = self._bitrate_case_record(plan)
        failed_cell = next(cell for cell in case["cells"] if cell["id"] == "b006-m065")
        for run in failed_cell["runs"]:
            quality = 0.948
            run["left_match_ssim"] = quality
            run["right_match_ssim"] = quality + 0.0001
            run["left_cross_ssim"] = quality - 0.2
            run["right_cross_ssim"] = quality - 0.1999
            run["min_same_eye_ssim"] = quality
            run["minimum_frame_same_eye_ssim"] = quality - 0.01
            run["p05_frame_same_eye_ssim"] = quality - 0.005
            run["median_frame_same_eye_ssim"] = quality
        evidence: dict[str, object] = {"cases": [case]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluation = next(item for item in evidence["bitrate_tier_evaluations"] if item["merge_quality"] == 65)
        self.assertFalse(evaluation["frontier_monotone"])
        self.assertFalse(evaluation["decision_ready"])
        self.assertFalse(evidence["acceptance"]["bitrate_search_ready"])

    def test_bitrate_search_keeps_storage_benefit_out_of_quality_frontier(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        binding = self._binding("case-a")
        evidence: dict[str, object] = {"cases": [self._bitrate_case_record(plan, storage_benefit=False)]}

        _refresh_summaries(evidence, plan, binding, {"case-a": self._definition()})

        evaluations = {item["merge_quality"]: item for item in evidence["bitrate_tier_evaluations"]}
        self.assertTrue(evaluations[65]["frontier_monotone"])
        self.assertTrue(evaluations[65]["decision_ready"])
        self.assertFalse(evaluations[65]["minimization_adopted"])
        self.assertEqual(evaluations[65]["selected_cell_id"], "b020-m065")
        self.assertTrue(evidence["acceptance"]["bitrate_search_ready"])

    def test_bitrate_search_storage_benefit_uses_total_case_bytes(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        binding = self._binding("case-a", "case-b")
        first = self._bitrate_case_record(plan, "case-a", storage_benefit=False)
        second = self._bitrate_case_record(plan, "case-b", storage_benefit=False)

        def set_sizes(case: dict[str, object], cell_id: str, size: int) -> None:
            cell = next(item for item in case["cells"] if item["id"] == cell_id)
            for run_index, run in enumerate(cell["runs"]):
                run["final_bytes"] = size + run_index

        for bitrate in plan.eye_bitrates_mbps:
            set_sizes(first, f"b{bitrate:03d}-m065", 1_000)
            set_sizes(second, f"b{bitrate:03d}-m065", 100_000)
        set_sizes(first, "b005-m065", 500)
        set_sizes(second, "b005-m065", 99_000)
        definitions = {case_id: self._definition(case_id) for case_id in binding.selected_case_ids}
        evidence: dict[str, object] = {"cases": [first, second]}

        _refresh_summaries(evidence, plan, binding, definitions)

        tier = next(item for item in evidence["bitrate_tier_evaluations"] if item["merge_quality"] == 65)
        candidate = next(item for item in tier["cell_evaluations"] if item["cell_id"] == "b005-m065")
        self.assertAlmostEqual(candidate["aggregate_size_ratio_vs_tier_anchor"], 99_502 / 101_002)
        self.assertFalse(candidate["storage_benefit_passed"])
        self.assertEqual(tier["selected_cell_id"], "b020-m065")

    def test_run_validation_rejects_contradictory_eye_metrics(self) -> None:
        plan = self._plan()
        cell = plan.cells[0]
        run = self._run(cell, 0, 0.95, 1000, 2)
        run["min_same_eye_ssim"] = 0.5

        with self.assertRaisesRegex(QualificationFailure, "contradicts eye metrics"):
            _validate_run_record(run, cell, 0)


class GeneratedCalibrationCliTests(unittest.TestCase):
    @staticmethod
    def _args() -> list[str]:
        return ["--output", "receipt.json", "--work-directory", "work"]

    def test_refinement_returns_one_when_decision_requires_review(self) -> None:
        evidence = {
            "method": {"decision_stage": "merge_response_refinement_only"},
            "acceptance": {"execution_passed": True, "refinement_decision_ready": False},
        }

        with patch("scripts.qualify_generated_mv_hevc_calibration.run_calibration", return_value=evidence):
            self.assertEqual(main(self._args()), 1)

    def test_refinement_returns_zero_only_when_decision_is_ready(self) -> None:
        evidence = {
            "method": {"decision_stage": "merge_response_refinement_only"},
            "acceptance": {"execution_passed": True, "refinement_decision_ready": True},
        }

        with patch("scripts.qualify_generated_mv_hevc_calibration.run_calibration", return_value=evidence):
            self.assertEqual(main(self._args()), 0)

    def test_bitrate_search_returns_one_when_frontier_is_not_ready(self) -> None:
        evidence = {
            "method": {"decision_stage": "same_tier_bitrate_minimization_only"},
            "acceptance": {"execution_passed": True, "bitrate_search_ready": False},
        }

        with patch("scripts.qualify_generated_mv_hevc_calibration.run_calibration", return_value=evidence):
            self.assertEqual(main(self._args()), 1)

    def test_bitrate_search_returns_zero_only_when_frontiers_are_ready(self) -> None:
        evidence = {
            "method": {"decision_stage": "same_tier_bitrate_minimization_only"},
            "acceptance": {"execution_passed": True, "bitrate_search_ready": True},
        }

        with patch("scripts.qualify_generated_mv_hevc_calibration.run_calibration", return_value=evidence):
            self.assertEqual(main(self._args()), 0)


class GeneratedCalibrationReceiptTests(unittest.TestCase):
    @staticmethod
    def _write_frozen_document(path: Path, document: object) -> str:
        data = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()
        path.write_bytes(data)
        path.chmod(0o444)
        return hashlib.sha256(data).hexdigest()

    def test_refinement_requires_pinned_source_receipt(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)

        with self.assertRaisesRegex(QualificationFailure, "requires --source-evidence-receipt"):
            _verify_refinement_source_receipt(plan, None)

    def test_refinement_rejects_wrong_source_receipt_hash(self) -> None:
        plan, _, _, _ = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = Path(temporary_directory) / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")
            receipt.chmod(0o444)

            with self.assertRaisesRegex(QualificationFailure, "pinned SHA-256"):
                _verify_refinement_source_receipt(plan, receipt)

    def test_bitrate_search_verifies_both_frozen_source_receipts(self) -> None:
        plan, binding, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        assert plan.bitrate_search is not None
        assert plan.thresholds is not None
        refinement_plan_record = {
            "path": "docs/qualification/generated-mv-hevc-merge-refinement-v1.json",
            "sha256": "a2dabc8afc72356e67f40ef9416a087103db3674cb12359fd8632959f5bee5d4",
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            refinement_path = root / "refinement.json"
            refinement_document = {
                "schema_version": 1,
                "experiment_id": plan.bitrate_search.merge_refinement_receipt.evidence_id,
                "source_git_sha": plan.bitrate_search.merge_refinement_receipt.source_git_sha,
                "source_tree_dirty": False,
                "experiment_plan": refinement_plan_record,
                "corpus_binding": {
                    "path": binding.relative_path,
                    "binding_id": binding.binding_id,
                    "sha256": plan.binding_sha256,
                },
                "pre_registered_thresholds": _threshold_record(plan.thresholds),
                "cells": [{"id": "b020-m065"}, {"id": "b020-m075"}],
                "acceptance": {
                    "experiment_complete": True,
                    "refinement_evidence_ready": True,
                    "thresholds_pre_registered": True,
                    "ladder_mapping_selected": False,
                },
            }
            refinement_sha = self._write_frozen_document(refinement_path, refinement_document)
            policy = replace(
                plan.bitrate_search,
                merge_refinement_receipt=replace(
                    plan.bitrate_search.merge_refinement_receipt,
                    sha256=refinement_sha,
                ),
            )

            collapse_path = root / "collapse.json"
            collapse_document = {
                "schema_version": 1,
                "analysis_id": policy.collapse_receipt.evidence_id,
                "analysis_source_git_sha": policy.collapse_receipt.source_git_sha,
                "analysis_source_tree_dirty": False,
                "analysis_plan": {
                    "path": policy.collapse_plan.path.relative_to(DEFAULT_BITRATE_SEARCH_PLAN.parents[2]).as_posix(),
                    "sha256": policy.collapse_plan.sha256,
                },
                "source_receipt": {
                    "schema_version": 1,
                    "experiment_id": policy.merge_refinement_receipt.evidence_id,
                    "sha256": refinement_sha,
                    "source_git_sha": policy.merge_refinement_receipt.source_git_sha,
                    "file_mode": "0444",
                },
                "source_plan": {**refinement_plan_record, "schema_version": 2},
                "source_corpus_binding": {
                    "path": binding.relative_path,
                    "binding_id": binding.binding_id,
                    "sha256": plan.binding_sha256,
                    "selected_case_ids": list(binding.selected_case_ids),
                },
                "thresholds": _threshold_record(plan.thresholds),
                "selected_subset": {
                    "cell_ids": ["b020-m065", "b020-m075"],
                    "cardinality": 2,
                    "contains_balanced": True,
                },
                "acceptance": {
                    "analysis_complete": True,
                    "balanced_included": True,
                    "selected_chain_valid": True,
                    "source_plan_verified": True,
                    "source_receipt_verified": True,
                    "thresholds_unchanged": True,
                    "selected_step_count": 2,
                    "ladder_mapping_selected": False,
                },
            }
            collapse_sha = self._write_frozen_document(collapse_path, collapse_document)
            policy = replace(
                policy,
                collapse_receipt=replace(policy.collapse_receipt, sha256=collapse_sha),
            )

            _verify_bitrate_search_source_receipts(
                replace(plan, bitrate_search=policy),
                binding,
                refinement_path,
                collapse_path,
            )

    def test_bitrate_search_rejects_duplicate_keys_in_frozen_receipt(self) -> None:
        plan, binding, _, _ = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        assert plan.bitrate_search is not None
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = Path(temporary_directory) / "receipt.json"
            data = b'{"schema_version":1,"schema_version":1}\n'
            receipt.write_bytes(data)
            receipt.chmod(0o444)
            policy = replace(
                plan.bitrate_search,
                merge_refinement_receipt=replace(
                    plan.bitrate_search.merge_refinement_receipt,
                    sha256=hashlib.sha256(data).hexdigest(),
                ),
            )

            with self.assertRaisesRegex(QualificationFailure, "duplicate key"):
                _verify_bitrate_search_source_receipts(
                    replace(plan, bitrate_search=policy),
                    binding,
                    receipt,
                    None,
                )

    def test_binding_rejects_changed_source_manifest_identity(self) -> None:
        document = json.loads(
            DEFAULT_EXPERIMENT_PLAN.with_name("generated-mv-hevc-corpus-v1.json").read_text(encoding="utf-8")
        )
        document["source_manifest"]["sha256"] = "0" * 64
        build_directory = DEFAULT_EXPERIMENT_PLAN.parents[2] / "build"
        build_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_directory) as temporary_directory:
            binding = Path(temporary_directory) / "binding.json"
            binding.write_text(json.dumps(document), encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "pinned SHA-256"):
                load_corpus_binding(binding)

    def test_private_source_paths_are_rejected(self) -> None:
        private_path = Path("/Volumes/private/source-movie.mkv")

        with self.assertRaisesRegex(QualificationFailure, "leaked private source"):
            _assert_private_values_absent({"source": private_path.as_posix()}, [private_path])

    def test_completed_receipt_is_frozen_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = Path(temporary_directory) / "receipt.json"
            receipt.write_text("{}\n", encoding="utf-8")

            _freeze_receipt(receipt)

            self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o444)

    def test_nonowned_nonempty_work_directory_is_rejected(self) -> None:
        plan, _, plan_sha256, _ = load_experiment_plan(DEFAULT_EXPERIMENT_PLAN)
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory) / "work"
            work_directory.mkdir()
            (work_directory / "unrelated.txt").write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "no ownership marker"):
                _prepare_owned_work_directory(work_directory, plan, plan_sha256)

    def test_case_work_directory_rejects_symlink(self) -> None:
        plan, _, plan_sha256, _ = load_experiment_plan(DEFAULT_EXPERIMENT_PLAN)
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = _prepare_owned_work_directory(Path(temporary_directory) / "work", plan, plan_sha256)
            sibling = work_directory / "sibling"
            sibling.mkdir()
            sentinel = sibling / "preserve.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            (work_directory / "case-a").symlink_to(sibling, target_is_directory=True)

            with self.assertRaisesRegex(QualificationFailure, "must not be a symlink"):
                _reset_case_directory(work_directory, "case-a")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_lock_rejects_concurrent_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            work_directory = Path(temporary_directory) / "work"
            with calibration_lock(output, work_directory):
                with self.assertRaisesRegex(QualificationFailure, "already running"):
                    with calibration_lock(output, work_directory):
                        self.fail("concurrent lock unexpectedly succeeded")

    def test_lock_binds_output_and_work_directory_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "receipt.json"
            work_directory = root / "work"
            with calibration_lock(output, work_directory):
                with self.assertRaisesRegex(QualificationFailure, "already running"):
                    with calibration_lock(output, root / "other-work"):
                        self.fail("same-output lock unexpectedly succeeded")
                with self.assertRaisesRegex(QualificationFailure, "already running"):
                    with calibration_lock(root / "other-receipt.json", work_directory):
                        self.fail("same-work lock unexpectedly succeeded")

    def test_resume_rejects_changed_toolchain_identity(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_experiment_plan(DEFAULT_EXPERIMENT_PLAN)
        environment = {"git_head": "d" * 40, "ffmpeg_sha256": "e" * 64}
        case = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        evidence = _new_evidence(plan, binding, plan_sha256, binding_sha256, environment, [case])
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text(json.dumps(evidence), encoding="utf-8")
            changed = dict(environment)
            changed["ffmpeg_sha256"] = "f" * 64

            with self.assertRaisesRegex(QualificationFailure, "environment"):
                _load_resume_evidence(
                    output,
                    plan=plan,
                    binding=binding,
                    plan_sha256=plan_sha256,
                    binding_sha256=binding_sha256,
                    environment=changed,
                    selected_cases=[case],
                    private_paths=(),
                )

    def test_resume_rejects_duplicate_json_keys(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_experiment_plan(DEFAULT_BITRATE_SEARCH_PLAN)
        environment = {"git_head": "d" * 40, "ffmpeg_sha256": "e" * 64}
        case = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "duplicate key"):
                _load_resume_evidence(
                    output,
                    plan=plan,
                    binding=binding,
                    plan_sha256=plan_sha256,
                    binding_sha256=binding_sha256,
                    environment=environment,
                    selected_cases=[case],
                    private_paths=(),
                )

    def test_refinement_resume_rejects_top_level_threshold_tampering(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_experiment_plan(DEFAULT_REFINEMENT_PLAN)
        environment = {"git_head": "d" * 40, "ffmpeg_sha256": "e" * 64}
        case = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        evidence = _new_evidence(plan, binding, plan_sha256, binding_sha256, environment, [case])
        evidence["pre_registered_thresholds"]["repeat_ssim_spread_limit"] = 0.001
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "pre_registered_thresholds"):
                _load_resume_evidence(
                    output,
                    plan=plan,
                    binding=binding,
                    plan_sha256=plan_sha256,
                    binding_sha256=binding_sha256,
                    environment=environment,
                    selected_cases=[case],
                    private_paths=(),
                )

    def test_resume_accepts_matching_partial_evidence(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_experiment_plan(DEFAULT_EXPERIMENT_PLAN)
        environment = {"git_head": "d" * 40, "ffmpeg_sha256": "e" * 64}
        case = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        evidence = _new_evidence(plan, binding, plan_sha256, binding_sha256, environment, [case])
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "receipt.json"
            output.write_text(json.dumps(evidence), encoding="utf-8")

            loaded = _load_resume_evidence(
                output,
                plan=plan,
                binding=binding,
                plan_sha256=plan_sha256,
                binding_sha256=binding_sha256,
                environment=environment,
                selected_cases=[case],
                private_paths=(),
            )

        self.assertEqual(loaded, evidence)

    def test_completed_resume_rejects_contradictory_summary(self) -> None:
        plan = GeneratedCalibrationSummaryTests._plan()
        binding = GeneratedCalibrationSummaryTests._binding("case-a")
        definition = GeneratedCalibrationSummaryTests._definition()
        evidence: dict[str, object] = {
            "cases": [GeneratedCalibrationSummaryTests._case_record(plan)],
        }
        _refresh_summaries(evidence, plan, binding, {"case-a": definition})
        original = copy.deepcopy(evidence)

        self.assertTrue(
            _completed_resume_is_consistent(
                evidence,
                plan,
                binding,
                {"case-a": definition},
            )
        )
        self.assertEqual(evidence, original)

        evidence["cell_summaries"][0]["median_quality_delta"] = 99
        with self.assertRaisesRegex(QualificationFailure, "summaries contradict"):
            _completed_resume_is_consistent(
                evidence,
                plan,
                binding,
                {"case-a": definition},
            )


class GeneratedCalibrationProductionParityTests(unittest.TestCase):
    def test_measurement_can_preserve_output_until_evidence_is_persisted(self) -> None:
        definition = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            output = root / "output.mov"
            output.write_bytes(b"encoded")
            left = root / "left.mov"
            right = root / "right.mov"
            left.write_bytes(b"left")
            right.write_bytes(b"right")
            split_directory = root / "split"
            split_directory.mkdir()
            prepared = PreparedCase(
                definition=definition,
                source_path=root / "source.mkv",
                reference_left=root / "reference-left.mkv",
                reference_right=root / "reference-right.mkv",
                duration_seconds=4.0,
                frame_count=96,
                source_evidence={"kind": "synthetic"},
            )

            with (
                patch("scripts.qualify_mv_hevc_corpus.split_mv_hevc", return_value=(left, right)),
                patch("scripts.qualify_mv_hevc_corpus.ssim", side_effect=(0.95, 0.96, 0.75, 0.76)),
            ):
                _measure_output(
                    "ffmpeg",
                    prepared,
                    output,
                    split_directory,
                    target_bitrate_mbps=20,
                    delete_output=False,
                )

            self.assertTrue(output.is_file())
            self.assertFalse(split_directory.exists())

    def test_generated_encode_matches_production_rate_control_and_merge_contract(self) -> None:
        definition = CorpusCase(
            case_id="synthetic-animation",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=640,
            eye_height=360,
            frame_rate="24",
        )
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory)
            prepared = PreparedCase(
                definition=definition,
                source_path=work_directory / "source.mkv",
                reference_left=work_directory / "left-reference.mkv",
                reference_right=work_directory / "right-reference.mkv",
                duration_seconds=4.0,
                frame_count=96,
                source_evidence={"kind": "synthetic"},
            )
            with patch("scripts.qualify_mv_hevc_corpus.run") as run_mock:
                _encode_generated(
                    "ffmpeg",
                    prepared,
                    work_directory / "output.mov",
                    work_directory,
                    eye_bitrate_mbps=20,
                    merge_quality=75,
                )

        encode_command = run_mock.call_args_list[0].args[0]
        merge_command = run_mock.call_args_list[1].args[0]
        self.assertEqual(encode_command.count("-bufsize"), 2)
        self.assertEqual(encode_command.count("40M"), 2)
        self.assertEqual(encode_command.count("-profile:v"), 2)
        self.assertEqual(encode_command.count("main"), 2)
        self.assertEqual(encode_command.count("-r"), 2)
        self.assertEqual(encode_command.count("24"), 2)
        self.assertEqual(merge_command[merge_command.index("--color-depth") + 1], "8")

    def test_frame_quality_summary_records_low_percentile_and_temporal_drop(self) -> None:
        summary = summarize_frame_quality(
            [0.99, 0.98, 0.60, 0.97],
            [0.98, 0.97, 0.61, 0.96],
        )

        self.assertEqual(summary["frame_quality_sample_count"], 4)
        self.assertEqual(summary["minimum_frame_same_eye_ssim"], 0.60)
        self.assertEqual(summary["p05_frame_same_eye_ssim"], 0.60)
        self.assertAlmostEqual(summary["maximum_adjacent_frame_ssim_drop"], 0.37)


if __name__ == "__main__":
    unittest.main()
