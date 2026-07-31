import copy
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest

from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import CURRENT_REQUIRED_BOX_TYPES, QualificationFailure
from scripts.qualify_file_upscale_quality_sweep import (
    DEFAULT_SWEEP_PLAN,
    EXPECTED_CASE_IDS,
    EXPECTED_COVERAGE,
    _candidate_order,
    _completed_resume_is_consistent,
    _configured_private_paths,
    _format_duration_seconds,
    _load_resume_evidence,
    _new_evidence,
    _prepare_owned_work_directory,
    _refresh_summaries,
    _run_fx_upscale,
    _validate_candidate_against_base,
    _validate_candidate_record,
    _validate_resume_cases,
    exit_code_for_evidence,
    load_sweep_plan,
    main,
    parse_corpus_binding,
    parse_sweep_plan,
    quality_factor_string,
)


HEX = "0123456789abcdef" * 4


def _duration_seconds(case) -> float:
    return 96 / float(Fraction(case.output_frame_rate))


def _bitrate_mbps(byte_count: int, duration_seconds: float) -> float:
    return round((byte_count * 8) / duration_seconds / 1_000_000, 6)


def _case_record(case, binding) -> dict[str, object]:
    return {
        "id": case.case_id,
        "tags": list(case.tags),
        "quality_gate": case.quality_gate,
        "source": dict(binding.expected_case_sources[case.case_id]),
        "prepared": {
            "duration_seconds": 4.0,
            "frame_count": 96,
            "eye_width": case.output_eye_width,
            "eye_height": case.output_eye_height,
            "frame_rate": case.output_frame_rate,
            "source_sha256": "f" * 64,
        },
        "repeats": [],
    }


def _base_record(case, repeat_index: int) -> dict[str, object]:
    duration_seconds = _duration_seconds(case)
    return {
        "bytes": 1000,
        "codec_name": "hevc",
        "codec_tag_string": "hvc1",
        "duration_seconds": duration_seconds,
        "duration_tolerance_frames": 1,
        "effective_bitrate_mbps": _bitrate_mbps(1000, duration_seconds),
        "elapsed_seconds": 10.0,
        "frame_count": 96,
        "frame_rate": case.output_frame_rate,
        "r_frame_rate": case.output_frame_rate,
        "generated_eye_bitrate_mbps": 20,
        "generated_merge_quality": 75,
        "geometry_scale": 1,
        "height": case.output_eye_height,
        "observed_box_types": sorted(CURRENT_REQUIRED_BOX_TYPES),
        "repeat_index": repeat_index,
        "sha256": "a" * 64,
        "source_sha256": "f" * 64,
        "system_cpu_seconds": 0.1,
        "target_total_eye_bitrate_mbps": 40,
        "user_cpu_seconds": 1.0,
        "width": case.output_eye_width,
    }


def _candidate_record(
    case,
    quality: int,
    repeat_index: int,
    execution_ordinal: int,
    *,
    final_bytes: int,
    quality_score: float,
) -> dict[str, object]:
    duration_seconds = _duration_seconds(case)
    left_cross = 0.40
    right_cross = 0.41
    left_match = quality_score
    right_match = quality_score + 0.001
    final_sha = f"{quality:064x}"[-64:]
    return {
        "base_bytes": 1000,
        "base_effective_bitrate_mbps": _bitrate_mbps(1000, duration_seconds),
        "base_sha256": "a" * 64,
        "bitrate_scaling_factor": quality_factor_string(quality),
        "codec_name": "hevc",
        "codec_tag_string": "hvc1",
        "duration_seconds": duration_seconds,
        "duration_tolerance_frames": 1,
        "effective_bitrate_mbps": _bitrate_mbps(final_bytes, duration_seconds),
        "execution_ordinal": execution_ordinal,
        "final_bytes": final_bytes,
        "final_sha256": final_sha,
        "final_to_base_size_ratio": final_bytes / 1000.0,
        "frame_count": 96,
        "frame_quality_sample_count": 96,
        "frame_rate": case.output_frame_rate,
        "r_frame_rate": case.output_frame_rate,
        "frame_ssim_standard_deviation": 0.0001,
        "geometry_scale": 2,
        "height": case.output_eye_height * 2,
        "id": f"q{quality:03d}",
        "input_copy_sha256": "a" * 64,
        "left_cross_ssim": left_cross,
        "left_match_ssim": left_match,
        "maximum_adjacent_frame_ssim_drop": 0.0002,
        "median_frame_same_eye_ssim": quality_score,
        "min_eye_order_margin": min(left_match - left_cross, right_match - right_cross),
        "min_same_eye_ssim": min(left_match, right_match),
        "minimum_frame_same_eye_ssim": quality_score - 0.001,
        "observed_box_types": sorted(CURRENT_REQUIRED_BOX_TYPES),
        "p05_frame_same_eye_ssim": quality_score - 0.0005,
        "paired_delta_to_q075": None,
        "projected_full_route_elapsed_seconds": 11.0 + quality / 1000.0,
        "quality": quality,
        "quality_factor": f"{quality}/100",
        "repeat_index": repeat_index,
        "right_cross_ssim": right_cross,
        "right_match_ssim": right_match,
        "source_sha256": "f" * 64,
        "upscale_elapsed_seconds": 1.0 + quality / 1000.0,
        "upscale_system_cpu_seconds": 0.1,
        "upscale_user_cpu_seconds": 0.8,
        "width": case.output_eye_width * 2,
    }


def _planned_cases(binding):
    manifest = json.loads(binding.source_manifest_path.read_text(encoding="utf-8"))
    by_id = {case["id"]: case for case in manifest["cases"]}
    return by_id


class FileUpscaleQualityPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan_document = json.loads(DEFAULT_SWEEP_PLAN.read_text(encoding="utf-8"))
        self.binding_document = json.loads(
            DEFAULT_SWEEP_PLAN.with_name("file-upscale-quality-corpus-v1.json").read_text(encoding="utf-8")
        )

    def test_committed_binding_and_plan_are_valid_and_exploratory(self) -> None:
        plan, binding, plan_digest, binding_digest = load_sweep_plan(DEFAULT_SWEEP_PLAN)

        self.assertEqual(binding.selected_case_ids, EXPECTED_CASE_IDS)
        self.assertEqual(binding.required_coverage, EXPECTED_COVERAGE)
        self.assertEqual(binding_digest, self.plan_document["corpus_binding"]["sha256"])
        self.assertEqual(plan.target_id, "upscale_quality")
        self.assertEqual(plan.purpose, "independent_response_characterization_not_ladder_mappings")
        self.assertEqual(plan.balanced_quality, 75)
        self.assertEqual(plan.base_eye_bitrate_mbps, 20)
        self.assertEqual(plan.base_merge_quality, 75)
        self.assertEqual(plan.frame_rate_contract, "exact_rational_match_v1")
        self.assertEqual(plan.duration_tolerance_frames, 1)
        self.assertEqual([candidate.quality for candidate in plan.candidates], [65, 75, 85])
        self.assertEqual(plan.orders, ((65, 75, 85), (75, 85, 65), (85, 65, 75)))
        self.assertFalse(self.plan_document["decision_policy"]["ladder_mapping_selected"])
        self.assertFalse(self.plan_document["decision_policy"]["thresholds_selected"])
        self.assertEqual(len(plan_digest), 64)

    def test_parser_rejects_changed_subset_or_decision_flags(self) -> None:
        binding_document = copy.deepcopy(self.binding_document)
        binding_document["selected_case_ids"].append("production-motion")
        with self.assertRaisesRegex(QualificationFailure, "checked five-case subset"):
            parse_corpus_binding(binding_document)

        plan_document = copy.deepcopy(self.plan_document)
        plan_document["decision_policy"]["ladder_mapping_selected"] = True
        with self.assertRaisesRegex(QualificationFailure, "ladder_mapping_selected"):
            parse_sweep_plan(plan_document)

    def test_plan_rejects_balanced_or_order_drift(self) -> None:
        changed_balanced = copy.deepcopy(self.plan_document)
        changed_balanced["balanced"]["quality"] = 76
        with self.assertRaisesRegex(QualificationFailure, "DEFAULT_UPSCALE_QUALITY"):
            parse_sweep_plan(changed_balanced)

        changed_order = copy.deepcopy(self.plan_document)
        changed_order["orders"][0] = [65, 85, 75]
        with self.assertRaisesRegex(QualificationFailure, "checked cyclic"):
            parse_sweep_plan(changed_order)

        changed_timing = copy.deepcopy(self.plan_document)
        changed_timing["toolchain"]["timing_contract"]["duration_tolerance_frames"] = 2
        with self.assertRaisesRegex(QualificationFailure, "between 1 and 1"):
            parse_sweep_plan(changed_timing)

    def test_load_rejects_binding_hash_mismatch(self) -> None:
        plan, binding, _, _ = load_sweep_plan(DEFAULT_SWEEP_PLAN)
        with patch(
            "scripts.qualify_file_upscale_quality_sweep.load_corpus_binding",
            return_value=(binding, "0" * 64),
        ):
            with self.assertRaisesRegex(QualificationFailure, "pinned SHA-256"):
                load_sweep_plan(DEFAULT_SWEEP_PLAN)
        self.assertEqual(plan.balanced_quality, 75)

    def test_schedule_and_quality_factor_are_exact(self) -> None:
        plan, _, _, _ = load_sweep_plan(DEFAULT_SWEEP_PLAN)

        self.assertEqual(
            [[candidate.quality for candidate in _candidate_order(plan, index)] for index in range(3)],
            [[65, 75, 85], [75, 85, 65], [85, 65, 75]],
        )
        self.assertEqual(quality_factor_string(65), "0.65")
        self.assertEqual(quality_factor_string(75), "0.75")
        with self.assertRaisesRegex(QualificationFailure, "integer"):
            quality_factor_string(75.0)  # type: ignore[arg-type]


class FileUpscaleQualityEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan, self.binding, self.plan_sha, self.binding_sha = load_sweep_plan(DEFAULT_SWEEP_PLAN)
        manifest = __import__("scripts.qualify_mv_hevc_corpus", fromlist=["load_manifest"]).load_manifest(
            self.binding.source_manifest_path
        )
        self.cases_by_id = {case.case_id: case for case in manifest.cases}
        self.full_cases = tuple(self.cases_by_id[case_id] for case_id in self.binding.selected_case_ids)
        self.environment = {"git_head": "1" * 40, "tool": "test"}

    def _evidence(self, selected_cases, *, bytes_by_quality=None, scores_by_quality=None, complete=True):
        bytes_by_quality = bytes_by_quality or {65: 650, 75: 750, 85: 850}
        scores_by_quality = scores_by_quality or {65: 0.961, 75: 0.962, 85: 0.963}
        evidence = _new_evidence(
            self.plan,
            self.binding,
            self.plan_sha,
            self.binding_sha,
            self.environment,
            selected_cases,
        )
        for case in selected_cases:
            case_record = _case_record(case, self.binding)
            max_repeats = self.plan.runs_per_candidate if complete else 1
            for repeat_index in range(max_repeats):
                scheduled_candidates = _candidate_order(self.plan, repeat_index)
                repeat = {
                    "repeat_index": repeat_index,
                    "order": list(self.plan.orders[repeat_index]),
                    "base": _base_record(case, repeat_index),
                    "candidates": [
                        _candidate_record(
                            case,
                            candidate.quality,
                            repeat_index,
                            execution_ordinal,
                            final_bytes=bytes_by_quality[candidate.quality],
                            quality_score=scores_by_quality[candidate.quality],
                        )
                        for execution_ordinal, candidate in enumerate(scheduled_candidates)
                    ],
                }
                case_record["repeats"].append(repeat)
            evidence["cases"].append(case_record)
        _refresh_summaries(evidence, self.plan, self.binding, {case.case_id: case for case in selected_cases})
        return evidence

    def test_complete_full_subset_is_decision_ready_without_selecting_mappings(self) -> None:
        evidence = self._evidence(self.full_cases)

        self.assertEqual(exit_code_for_evidence(evidence), 0)
        self.assertTrue(evidence["acceptance"]["decision_ready"])
        self.assertFalse(evidence["acceptance"]["ladder_mapping_selected"])
        self.assertFalse(evidence["acceptance"]["thresholds_selected"])
        self.assertEqual(evidence["monotonicity_findings"], [])

    def test_subset_and_partial_evidence_exit_three(self) -> None:
        subset = self._evidence(self.full_cases[:1])
        self.assertTrue(subset["acceptance"]["complete"])
        self.assertFalse(subset["acceptance"]["planned_full_stress_subset"])
        self.assertEqual(exit_code_for_evidence(subset), 3)

        partial = self._evidence(self.full_cases[:1], complete=False)
        self.assertFalse(partial["acceptance"]["complete"])
        self.assertEqual(exit_code_for_evidence(partial), 3)

    def test_storage_reversal_or_ambiguity_exit_one(self) -> None:
        reversal = self._evidence(self.full_cases, bytes_by_quality={65: 650, 75: 750, 85: 740})
        self.assertEqual(exit_code_for_evidence(reversal), 1)
        self.assertIn("storage_reversal", {finding["code"] for finding in reversal["monotonicity_findings"]})

        tie = self._evidence(self.full_cases, bytes_by_quality={65: 650, 75: 750, 85: 750})
        self.assertEqual(exit_code_for_evidence(tie), 1)
        self.assertTrue(tie["acceptance"]["response_ambiguous"])

    def test_refresh_adds_paired_deltas_to_same_repeat_balanced(self) -> None:
        evidence = self._evidence(self.full_cases[:1])
        first_repeat = evidence["cases"][0]["repeats"][0]
        low = next(candidate for candidate in first_repeat["candidates"] if candidate["id"] == "q065")
        balanced = next(candidate for candidate in first_repeat["candidates"] if candidate["id"] == "q075")

        self.assertEqual(low["paired_delta_to_q075"]["final_bytes"], -100)
        self.assertEqual(balanced["paired_delta_to_q075"]["final_bytes"], 0)

    def test_candidate_validation_requires_exact_base_copy(self) -> None:
        case = self.full_cases[0]
        record = _candidate_record(case, 75, 0, 1, final_bytes=750, quality_score=0.962)
        record["input_copy_sha256"] = "b" * 64

        with self.assertRaisesRegex(QualificationFailure, "exact base copy"):
            _validate_candidate_record(record, self.plan.candidates[1], 0, 1)

    def test_candidate_validation_binds_record_to_repeat_base(self) -> None:
        case = self.full_cases[0]
        base = _base_record(case, 0)
        record = _candidate_record(case, 65, 0, 0, final_bytes=650, quality_score=0.961)
        record["base_sha256"] = "b" * 64
        record["input_copy_sha256"] = "b" * 64

        _validate_candidate_record(record, self.plan.candidates[0], 0, 0)
        with self.assertRaisesRegex(QualificationFailure, "recorded generated base"):
            _validate_candidate_against_base(record, base, self.plan.candidates[0])

    def test_resume_rejects_non_prefix_candidate_order(self) -> None:
        evidence = self._evidence(self.full_cases[:1], complete=False)
        repeat = evidence["cases"][0]["repeats"][0]
        repeat["candidates"] = [repeat["candidates"][2]]

        with self.assertRaisesRegex(QualificationFailure, "execution prefix"):
            _validate_resume_cases(
                evidence,
                self.plan,
                self.binding,
                {self.full_cases[0].case_id: self.full_cases[0]},
            )

    def test_ssim_direction_is_descriptive_without_a_threshold(self) -> None:
        evidence = self._evidence(
            self.full_cases,
            scores_by_quality={65: 0.963, 75: 0.962, 85: 0.961},
        )

        self.assertEqual(exit_code_for_evidence(evidence), 0)
        self.assertNotIn(
            "quality_reversal_observed",
            {finding["code"] for finding in evidence["monotonicity_findings"]},
        )

    def test_resume_validation_and_completed_consistency(self) -> None:
        evidence = self._evidence(self.full_cases[:1])
        case_definitions = {case.case_id: case for case in self.full_cases[:1]}
        self.assertTrue(_completed_resume_is_consistent(evidence, self.plan, self.binding, case_definitions))

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            loaded = _load_resume_evidence(
                output,
                plan=self.plan,
                binding=self.binding,
                plan_sha256=self.plan_sha,
                binding_sha256=self.binding_sha,
                environment=self.environment,
                selected_cases=self.full_cases[:1],
                private_paths=(),
            )
            self.assertEqual(loaded["experiment_id"], "file-upscale-quality-sweep-v1")

            tampered = copy.deepcopy(evidence)
            tampered["cases"][0]["repeats"][0]["order"] = [65, 85, 75]
            output.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(QualificationFailure, "repeat order"):
                _load_resume_evidence(
                    output,
                    plan=self.plan,
                    binding=self.binding,
                    plan_sha256=self.plan_sha,
                    binding_sha256=self.binding_sha,
                    environment=self.environment,
                    selected_cases=self.full_cases[:1],
                    private_paths=(),
                )

    def test_complete_writable_checkpoint_can_resume_to_finalization(self) -> None:
        evidence = self._evidence(self.full_cases)
        self.assertFalse(evidence["acceptance"]["finalized"])

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            loaded = _load_resume_evidence(
                output,
                plan=self.plan,
                binding=self.binding,
                plan_sha256=self.plan_sha,
                binding_sha256=self.binding_sha,
                environment=self.environment,
                selected_cases=self.full_cases,
                private_paths=(),
            )

            self.assertTrue(loaded["acceptance"]["complete"])
            self.assertFalse(loaded["acceptance"]["finalized"])

    def test_timeout_diagnostic_redacts_configured_private_source(self) -> None:
        private_source = "/private/very-sensitive-release-source.mkv"
        stderr = io.StringIO()
        timeout = subprocess.TimeoutExpired(["tool", private_source], 30)

        with (
            patch.dict(os.environ, {"BD_TO_AVP_RELEASE_MVC_SOURCE": private_source}),
            patch("scripts.qualify_file_upscale_quality_sweep.run_quality_sweep", side_effect=timeout),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertIn(Path(private_source), _configured_private_paths())
            self.assertEqual(main([]), 2)

        self.assertNotIn(private_source, stderr.getvalue())
        self.assertIn("<private-source>", stderr.getvalue())

    def test_owned_work_directory_marker_is_atomic_and_identity_checked(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            work = Path(temp_dir) / "work"
            prepared = _prepare_owned_work_directory(work, self.plan, self.plan_sha)
            marker = prepared / ".bd-to-avp-file-upscale-quality-sweep.json"

            self.assertTrue(marker.is_file())
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8"))["experiment_id"], self.plan.experiment_id)
            with self.assertRaisesRegex(QualificationFailure, "different experiment"):
                _prepare_owned_work_directory(work, self.plan, "0" * 64)

    def test_run_fx_upscale_uses_shared_command_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "base.mov"
            input_path.write_bytes(b"base")
            output_path = input_path.with_name("base Upscaled.mov")

            def fake_run(command):
                self.assertEqual(command, [Path("fx-upscale"), "--bitrate-scaling-factor", "0.75", input_path])
                output_path.write_bytes(b"upscaled")

            with patch("scripts.qualify_file_upscale_quality_sweep.run", side_effect=fake_run):
                self.assertEqual(_run_fx_upscale(Path("fx-upscale"), input_path, 75), output_path)

    def test_ffprobe_duration_accepts_numeric_string(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='{"format":{"duration":"4.000000"}}')

        with patch("scripts.qualify_file_upscale_quality_sweep.run", return_value=completed):
            self.assertEqual(_format_duration_seconds("ffprobe", Path("movie.mov")), 4.0)


if __name__ == "__main__":
    unittest.main()
