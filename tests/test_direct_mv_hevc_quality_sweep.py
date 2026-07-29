import copy
import json
import tempfile
import unittest

from pathlib import Path

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_quality_sweep import (
    DEFAULT_SWEEP_PLAN,
    SweepCandidate,
    SweepPlan,
    _candidate_order,
    _case_complete,
    _load_resume_evidence,
    _new_evidence,
    _prepare_owned_work_directory,
    _refresh_summaries,
    _require_head_tracked_file,
    _validate_resume_cases,
    load_sweep_plan,
    parse_sweep_plan,
    summarize_runs,
)
from scripts.qualify_mv_hevc_corpus import CorpusCase


class DirectQualitySweepPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_SWEEP_PLAN.read_text(encoding="utf-8"))

    def test_committed_plan_is_valid_and_exploratory(self) -> None:
        plan, digest = load_sweep_plan(DEFAULT_SWEEP_PLAN)

        self.assertEqual(plan.balanced_quality, 0.7)
        self.assertEqual(plan.runs_per_candidate, 3)
        self.assertEqual([candidate.quality for candidate in plan.candidates], [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
        self.assertEqual(len(digest), 64)

    def test_rejects_plan_without_balanced_candidate(self) -> None:
        document = copy.deepcopy(self.document)
        document["candidates"] = [candidate for candidate in document["candidates"] if candidate["quality"] != 0.7]

        with self.assertRaisesRegex(QualificationFailure, "Balanced production quality"):
            parse_sweep_plan(document)

    def test_rejects_ladder_step_assignment_purpose(self) -> None:
        document = copy.deepcopy(self.document)
        document["purpose"] = "assign_ladder_steps"

        with self.assertRaisesRegex(QualificationFailure, "exploratory"):
            parse_sweep_plan(document)

    def test_rejects_unsafe_candidate_id(self) -> None:
        document = copy.deepcopy(self.document)
        document["candidates"][0]["id"] = "../escape"

        with self.assertRaisesRegex(QualificationFailure, "qNNN"):
            parse_sweep_plan(document)

    def test_rejects_external_sweep_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "plan.json"
            path.write_text(json.dumps(self.document), encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "inside the repository"):
                load_sweep_plan(path)

    def test_rejects_ignored_untracked_plan_as_source_bound_evidence(self) -> None:
        build_directory = DEFAULT_SWEEP_PLAN.parents[2] / "build"
        build_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=build_directory) as temporary_directory:
            path = Path(temporary_directory) / "ignored-plan.json"
            path.write_text(json.dumps(self.document), encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "tracked in the recorded source commit"):
                _require_head_tracked_file(path, "Sweep plan")


class DirectQualitySweepSummaryTests(unittest.TestCase):
    @staticmethod
    def _plan() -> SweepPlan:
        return SweepPlan(
            sweep_id="test",
            corpus_path=Path("manifest.json"),
            corpus_id="corpus",
            corpus_sha256="a" * 64,
            balanced_quality=0.7,
            runs_per_candidate=2,
            candidates=(
                SweepCandidate("q060", 0.6),
                SweepCandidate("q070", 0.7),
                SweepCandidate("q080", 0.8),
            ),
            relative_path="docs/test-plan.json",
        )

    @staticmethod
    def _run(index: int, quality: float, size: int, elapsed: float) -> dict[str, object]:
        return {
            "run_index": index,
            "min_same_eye_ssim": quality,
            "final_bytes": size,
            "elapsed_seconds": elapsed,
            "min_eye_order_margin": 0.1,
        }

    def test_summarize_runs_uses_medians_and_reports_repeat_spread(self) -> None:
        summary = summarize_runs([self._run(0, 0.98, 100, 2), self._run(1, 0.96, 120, 4)])

        self.assertEqual(summary["median_min_same_eye_ssim"], 0.97)
        self.assertEqual(summary["median_final_bytes"], 110)
        self.assertEqual(summary["median_elapsed_seconds"], 3)
        self.assertAlmostEqual(summary["repeat_ssim_spread"], 0.02)
        self.assertAlmostEqual(summary["repeat_size_ratio_spread"], 20 / 110)

    def test_refresh_summaries_anchors_deltas_to_balanced_and_warns_on_reversal(self) -> None:
        plan = self._plan()
        evidence: dict[str, object] = {
            "source_git_sha": "b" * 40,
            "manifest": {"sha256": "a" * 64},
            "cases": [
                {
                    "id": "case-a",
                    "candidates": [
                        {
                            "id": "q060",
                            "quality": 0.6,
                            "runs": [self._run(0, 0.95, 90, 2), self._run(1, 0.95, 90, 2)],
                        },
                        {
                            "id": "q070",
                            "quality": 0.7,
                            "runs": [self._run(0, 0.96, 100, 2), self._run(1, 0.96, 100, 2)],
                        },
                        {
                            "id": "q080",
                            "quality": 0.8,
                            "runs": [self._run(0, 0.955, 95, 2), self._run(1, 0.955, 95, 2)],
                        },
                    ],
                }
            ],
            "candidate_summaries": [],
            "monotonicity_warnings": [],
        }
        case = CorpusCase(
            case_id="case-a",
            tags=("real_mvc",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )

        _refresh_summaries(evidence, plan, {"case-a": case}, all_gated_case_ids={"case-a"})

        summaries = evidence["candidate_summaries"]
        self.assertEqual(summaries[1]["quality_delta"], 0.0)
        self.assertEqual(summaries[1]["output_size_ratio"], 1.0)
        self.assertAlmostEqual(summaries[0]["quality_delta"], -0.01)
        self.assertAlmostEqual(summaries[0]["output_size_ratio"], 0.9)
        self.assertTrue(any(warning["code"] == "quality_reversal" for warning in evidence["monotonicity_warnings"]))
        self.assertTrue(all(warning["case_id"] == "case-a" for warning in evidence["monotonicity_warnings"]))

    def test_subset_execution_passes_without_claiming_full_corpus(self) -> None:
        plan = self._plan()
        evidence: dict[str, object] = {
            "source_git_sha": "b" * 40,
            "manifest": {"sha256": "a" * 64},
            "cases": [
                {
                    "id": "case-a",
                    "candidates": [
                        {
                            "id": "q060",
                            "quality": 0.6,
                            "runs": [self._run(0, 0.94, 80, 2), self._run(1, 0.94, 80, 2)],
                        },
                        {
                            "id": "q070",
                            "quality": 0.7,
                            "runs": [self._run(0, 0.96, 100, 2), self._run(1, 0.96, 100, 2)],
                        },
                        {
                            "id": "q080",
                            "quality": 0.8,
                            "runs": [self._run(0, 0.98, 120, 2), self._run(1, 0.98, 120, 2)],
                        },
                    ],
                }
            ],
            "candidate_summaries": [],
            "monotonicity_warnings": [],
        }
        case = CorpusCase(
            case_id="case-a",
            tags=("real_mvc",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )

        _refresh_summaries(evidence, plan, {"case-a": case}, all_gated_case_ids={"case-a", "case-b"})

        self.assertTrue(evidence["acceptance"]["execution_passed"])
        self.assertFalse(evidence["acceptance"]["full_quality_gated_corpus"])
        self.assertFalse(evidence["acceptance"]["passed"])

    def test_candidate_order_alternates_to_reduce_order_bias(self) -> None:
        plan = self._plan()

        self.assertEqual([candidate.candidate_id for candidate in _candidate_order(plan, 0)], ["q060", "q070", "q080"])
        self.assertEqual([candidate.candidate_id for candidate in _candidate_order(plan, 1)], ["q080", "q070", "q060"])


class DirectQualitySweepResumeTests(unittest.TestCase):
    @staticmethod
    def _complete_run(candidate: SweepCandidate, run_index: int) -> dict[str, object]:
        return {
            "effective_bitrate_mbps": 1.0,
            "final_bytes": 100,
            "left_cross_ssim": 0.5,
            "left_match_ssim": 0.9,
            "min_eye_order_margin": 0.4,
            "min_same_eye_ssim": 0.9,
            "right_cross_ssim": 0.5,
            "right_match_ssim": 0.9,
            "sha256": "a" * 64,
            "run_index": run_index,
            "target_quality": candidate.quality,
            "elapsed_seconds": 1.0,
            "user_cpu_seconds": 0.5,
            "system_cpu_seconds": 0.1,
        }

    @staticmethod
    def _resume_case(plan: SweepPlan) -> dict[str, object]:
        return {
            "id": "case-a",
            "source": {"segment_sha256": "b" * 64},
            "prepared": {"frame_count": 1},
            "candidates": [
                {
                    "id": candidate.candidate_id,
                    "quality": candidate.quality,
                    "runs": [
                        DirectQualitySweepResumeTests._complete_run(candidate, run_index)
                        for run_index in range(plan.runs_per_candidate)
                    ],
                    "summary": None,
                }
                for candidate in plan.candidates
            ],
        }

    def test_complete_case_requires_valid_runs_for_every_candidate(self) -> None:
        plan = DirectQualitySweepSummaryTests._plan()
        case = self._resume_case(plan)

        self.assertTrue(_case_complete(case, plan))
        case["candidates"][0]["runs"].pop()
        self.assertFalse(_case_complete(case, plan))

    def test_resume_rejects_duplicate_run_indices(self) -> None:
        plan = DirectQualitySweepSummaryTests._plan()
        case = self._resume_case(plan)
        case["candidates"][0]["runs"][1]["run_index"] = 0

        with self.assertRaisesRegex(QualificationFailure, "duplicate run indices"):
            _validate_resume_cases({"cases": [case]}, plan, ["case-a"])

    def test_nonowned_nonempty_work_directory_is_rejected(self) -> None:
        plan, plan_sha256 = load_sweep_plan(DEFAULT_SWEEP_PLAN)
        with tempfile.TemporaryDirectory() as temporary_directory:
            work_directory = Path(temporary_directory) / "work"
            work_directory.mkdir()
            (work_directory / "unrelated.txt").write_text("preserve", encoding="utf-8")

            with self.assertRaisesRegex(QualificationFailure, "no ownership marker"):
                _prepare_owned_work_directory(work_directory, plan, plan_sha256)

    def test_resume_rejects_changed_toolchain(self) -> None:
        plan, plan_sha256 = load_sweep_plan(DEFAULT_SWEEP_PLAN)
        environment = {"git_head": "b" * 40, "encoder_sha256": "c" * 64}
        case = CorpusCase(
            case_id="case-a",
            tags=("animation",),
            source={"kind": "synthetic"},
            eye_width=2,
            eye_height=2,
            frame_rate="24",
        )
        evidence = _new_evidence(plan, plan_sha256, environment, [case])
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "evidence.json"
            output.write_text(json.dumps(evidence), encoding="utf-8")
            changed = dict(environment)
            changed["encoder_sha256"] = "d" * 64

            with self.assertRaisesRegex(QualificationFailure, "toolchain environment"):
                _load_resume_evidence(
                    output,
                    plan=plan,
                    plan_sha256=plan_sha256,
                    environment=changed,
                    selected_case_ids=["case-a"],
                )


if __name__ == "__main__":
    unittest.main()
