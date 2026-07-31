import copy
import json
import tempfile
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_generated_mv_hevc_collapse import (
    DEFAULT_ANALYSIS_PLAN,
    _load_source_corpus_binding,
    _load_source_plan,
    _load_source_receipt,
    _loads_json,
    _validate_source_receipt,
    build_analysis_receipt,
    evaluate_boundaries,
    load_analysis_plan,
    main,
    parse_analysis_plan,
    select_collapsed_subset,
)


class GeneratedCollapsePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_ANALYSIS_PLAN.read_text(encoding="utf-8"))

    def test_committed_plan_is_valid_and_fail_closed(self) -> None:
        plan, digest = load_analysis_plan(DEFAULT_ANALYSIS_PLAN)

        self.assertEqual(plan.analysis_id, "generated-mv-hevc-collapse-analysis-v1")
        self.assertEqual(plan.balanced.cell_id, "b020-m075")
        self.assertEqual(plan.target_named_step_count, 7)
        self.assertEqual(
            plan.source_receipt.sha256,
            "e6b4d5a8908352f1667c092e199853f3cfc25256f22d803e56f94b30783dcbdf",
        )
        self.assertEqual(len(digest), 64)

    def test_rejects_changed_tie_break_order(self) -> None:
        document = copy.deepcopy(self.document)
        document["selection_policy"]["tie_breaks"].reverse()

        with self.assertRaisesRegex(QualificationFailure, "tie_breaks"):
            parse_analysis_plan(document)

    def test_rejects_lower_target_step_count(self) -> None:
        document = copy.deepcopy(self.document)
        document["selection_policy"]["target_named_step_count"] = 2

        with self.assertRaisesRegex(QualificationFailure, "must remain 7"):
            parse_analysis_plan(document)

    def test_strict_json_rejects_duplicate_keys(self) -> None:
        with self.assertRaisesRegex(QualificationFailure, "duplicate key"):
            _loads_json(b'{"schema_version": 1, "schema_version": 2}', "test document")


class GeneratedCollapseAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, self.plan_sha256 = load_analysis_plan(DEFAULT_ANALYSIS_PLAN)
        self.source_plan = _load_source_plan(self.plan)
        self.source_binding = _load_source_corpus_binding(self.source_plan)
        self.receipt = self._receipt()

    def _receipt(self) -> dict[str, object]:
        thresholds = copy.deepcopy(self.source_plan["pre_registered_thresholds"])
        merges = list(self.source_plan["axes"]["merge_quality"])
        cells = [{"id": f"b020-m{merge:03d}", "eye_bitrate_mbps": 20, "merge_quality": merge} for merge in merges]
        eligible_ids = {"b020-m065", "b020-m068", "b020-m071", "b020-m075"}
        evaluations = [
            {
                "cell_id": cell["id"],
                "eye_bitrate_mbps": 20,
                "merge_quality": cell["merge_quality"],
                "complete": True,
                "case_evaluations": [],
                "candidate_constraints_passed": cell["id"] in eligible_ids,
            }
            for cell in cells
        ]
        quality_profiles = (
            {65: 0.9000, 68: 0.9005, 71: 0.9008, 75: 0.9010, 79: 0.9012, 82: 0.9013, 85: 0.9014},
            {65: 0.9000, 68: 0.9010, 71: 0.9017, 75: 0.9020, 79: 0.9022, 82: 0.9023, 85: 0.9024},
        )
        quality_by_case = {
            case_id: quality_profiles[index % len(quality_profiles)]
            for index, case_id in enumerate(self.source_binding.selected_case_ids)
        }
        bytes_by_merge = {65: 100, 68: 130, 71: 150, 75: 200, 79: 250, 82: 280, 85: 320}
        cases = []
        for case_id, quality_by_merge in quality_by_case.items():
            cases.append(
                {
                    "id": case_id,
                    "cells": [
                        {
                            **cell,
                            "summary": {
                                "median_min_same_eye_ssim": quality_by_merge[int(cell["merge_quality"])],
                                "median_final_bytes": bytes_by_merge[int(cell["merge_quality"])],
                            },
                        }
                        for cell in cells
                    ],
                }
            )
        return {
            "schema_version": 1,
            "experiment_id": self.plan.source_receipt.experiment_id,
            "source_git_sha": self.plan.source_receipt.source_git_sha,
            "source_tree_dirty": False,
            "experiment_plan": {
                "path": self.plan.source_plan.path.relative_to(DEFAULT_ANALYSIS_PLAN.parents[2]).as_posix(),
                "sha256": self.plan.source_plan.sha256,
            },
            "corpus_binding": {
                "path": self.source_binding.path.relative_to(DEFAULT_ANALYSIS_PLAN.parents[2]).as_posix(),
                "binding_id": self.source_binding.binding_id,
                "sha256": self.source_binding.sha256,
            },
            "method": {
                "decision_stage": "merge_response_refinement_only",
                "balanced": {"eye_bitrate_mbps": 20, "merge_quality": 75},
                "pre_registered_thresholds": thresholds,
            },
            "pre_registered_thresholds": thresholds,
            "acceptance": {
                "complete": True,
                "experiment_complete": True,
                "execution_passed": True,
                "objective_validation_passed": True,
                "eye_order_passed": True,
                "planned_stress_corpus": True,
                "thresholds_pre_registered": True,
                "thresholds_evaluated": True,
                "refinement_evidence_ready": True,
                "refinement_decision_ready": False,
                "ladder_evidence_ready": False,
                "ladder_mapping_selected": False,
                "ambiguous_adjacent_count": 6,
                "technically_eligible_cell_count": 4,
            },
            "cells": cells,
            "refinement_cell_evaluations": evaluations,
            "selected_case_ids": list(self.source_binding.selected_case_ids),
            "cases": cases,
        }

    def _validated(self):
        return _validate_source_receipt(
            self.plan,
            self.receipt,
            self.source_plan,
            self.source_binding,
        )

    def test_rejects_threshold_drift(self) -> None:
        self.receipt["pre_registered_thresholds"]["aggregate_quality_distinguishability"] = 0.001

        with self.assertRaisesRegex(QualificationFailure, "thresholds differ"):
            self._validated()

    def test_rejects_missing_eligible_balanced(self) -> None:
        balanced = next(
            evaluation
            for evaluation in self.receipt["refinement_cell_evaluations"]
            if evaluation["cell_id"] == "b020-m075"
        )
        balanced["candidate_constraints_passed"] = False
        self.receipt["acceptance"]["technically_eligible_cell_count"] = 3

        with self.assertRaisesRegex(QualificationFailure, "Balanced is not technically eligible"):
            self._validated()

    def test_rejects_source_receipt_that_selected_mapping(self) -> None:
        self.receipt["acceptance"]["ladder_mapping_selected"] = True

        with self.assertRaisesRegex(QualificationFailure, "ladder_mapping_selected must remain false"):
            self._validated()

    def test_rejects_case_set_that_differs_from_checked_binding(self) -> None:
        self.receipt["selected_case_ids"][0] = "different-case"
        self.receipt["cases"][0]["id"] = "different-case"

        with self.assertRaisesRegex(QualificationFailure, "checked corpus binding"):
            self._validated()

    def test_selects_non_adjacent_65_to_75_collapse(self) -> None:
        cells, eligible_ids, cases, thresholds = self._validated()

        boundaries = evaluate_boundaries(cells, eligible_ids, cases, thresholds)
        selected, valid_subsets = select_collapsed_subset(
            cells,
            eligible_ids,
            boundaries,
            self.plan.balanced.cell_id,
        )

        self.assertEqual(selected["cell_ids"], ["b020-m065", "b020-m075"])
        self.assertEqual(selected["cardinality"], 2)
        boundary_by_pair = {
            (boundary["lower_cell_id"], boundary["higher_cell_id"]): boundary for boundary in boundaries
        }
        self.assertTrue(boundary_by_pair[("b020-m065", "b020-m075")]["response_separable"])
        self.assertFalse(boundary_by_pair[("b020-m065", "b020-m068")]["response_separable"])
        self.assertTrue(any(subset["cell_ids"] == ["b020-m075"] for subset in valid_subsets))

    def test_evaluation_order_does_not_change_selected_subset(self) -> None:
        self.receipt["refinement_cell_evaluations"].reverse()
        cells, eligible_ids, cases, thresholds = self._validated()

        boundaries = evaluate_boundaries(cells, eligible_ids, cases, thresholds)
        selected, _ = select_collapsed_subset(cells, eligible_ids, boundaries, self.plan.balanced.cell_id)

        self.assertEqual(eligible_ids, ["b020-m065", "b020-m068", "b020-m071", "b020-m075"])
        self.assertEqual(selected["cell_ids"], ["b020-m065", "b020-m075"])

    def test_receipt_requires_product_decision_below_target(self) -> None:
        cells, eligible_ids, cases, thresholds = self._validated()
        boundaries = evaluate_boundaries(cells, eligible_ids, cases, thresholds)
        selected, valid_subsets = select_collapsed_subset(
            cells,
            eligible_ids,
            boundaries,
            self.plan.balanced.cell_id,
        )

        evidence = build_analysis_receipt(
            self.plan,
            self.plan_sha256,
            "d" * 40,
            self.source_binding,
            cells,
            eligible_ids,
            thresholds,
            boundaries,
            selected,
            valid_subsets,
        )

        self.assertTrue(evidence["acceptance"]["analysis_complete"])
        self.assertFalse(evidence["acceptance"]["target_step_count_met"])
        self.assertFalse(evidence["acceptance"]["bitrate_search_ready"])
        self.assertTrue(evidence["acceptance"]["product_decision_required"])
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])

    def test_source_file_must_be_frozen_before_hash_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            receipt = Path(temporary_directory) / "receipt.json"
            receipt.write_text(json.dumps(self.receipt), encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "frozen read-only"):
                _load_source_receipt(
                    self.plan,
                    receipt,
                    self.source_plan,
                    self.source_binding,
                )


class GeneratedCollapseCliTests(unittest.TestCase):
    @staticmethod
    def _args() -> list[str]:
        return ["--source-receipt", "source.json", "--output", "output.json"]

    def test_complete_collapse_below_target_returns_one(self) -> None:
        evidence = {"acceptance": {"analysis_complete": True, "target_step_count_met": False}}

        with patch("scripts.qualify_generated_mv_hevc_collapse.run_analysis", return_value=evidence):
            self.assertEqual(main(self._args()), 1)

    def test_target_count_met_returns_zero(self) -> None:
        evidence = {"acceptance": {"analysis_complete": True, "target_step_count_met": True}}

        with patch("scripts.qualify_generated_mv_hevc_collapse.run_analysis", return_value=evidence):
            self.assertEqual(main(self._args()), 0)

    def test_invalid_source_returns_two(self) -> None:
        with patch(
            "scripts.qualify_generated_mv_hevc_collapse.run_analysis",
            side_effect=QualificationFailure("invalid"),
        ):
            self.assertEqual(main(self._args()), 2)


if __name__ == "__main__":
    unittest.main()
