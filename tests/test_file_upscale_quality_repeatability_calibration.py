import contextlib
import copy
import hashlib
import io
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_repeatability_calibration import (
    DEFAULT_CALIBRATION_PLAN,
    EVIDENCE_SCHEMA_VERSION,
    EXPECTED_CASE_IDS,
    EXPECTED_CORPUS_SHA256,
    EXPECTED_PREDECESSOR_PLAN_SHA256,
    EXPECTED_PREDECESSOR_RECEIPT_SHA256,
    EXPECTED_PREDECESSOR_SOURCE_GIT_SHA,
    EXPECTED_PREVIOUS_LIMITS,
    EXPECTED_RETAINED_CASE_IDS,
    REPEATABILITY_FIELDS,
    WORK_DIRECTORY_MARKER,
    _cleanup_completed_work_directory,
    _load_resume_evidence,
    _new_evidence,
    _parser,
    _refresh_calibration,
    _retained_artifact_entry,
    _validate_clean_work_directory,
    derive_calibrated_limit,
    derive_repeatability_limits,
    exit_code_for_evidence,
    load_repeatability_calibration_plan,
    main,
    materialized_case_orders,
    parse_repeatability_calibration_plan,
)
from scripts.qualify_mv_hevc_corpus import load_manifest
from scripts.qualify_mv_hevc_quality_match import sha256_file
from tests.test_file_upscale_quality_sweep import (
    _base_record as sweep_base_record,
    _candidate_record as sweep_candidate_record,
    _case_record as sweep_case_record,
)


def _definitions(binding):
    manifest = load_manifest(binding.source_manifest_path)
    by_id = {case.case_id: case for case in manifest.cases}
    return {case_id: by_id[case_id] for case_id in EXPECTED_CASE_IDS}


def _complete_raw_evidence():
    plan, binding, plan_sha256, binding_sha256 = load_repeatability_calibration_plan(DEFAULT_CALIBRATION_PLAN)
    definitions = _definitions(binding)
    predecessor = {"verified": True, "records_used_for_calibration": False}
    environment = {"git_head": "f" * 40}
    evidence = cast(
        dict[str, Any],
        _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            predecessor,
            environment,
        ),
    )
    for case_index, case_id in enumerate(EXPECTED_CASE_IDS):
        definition = definitions[case_id]
        case = sweep_case_record(definition, binding)
        for repeat_index in range(plan.runs_per_candidate):
            quality_score = 0.90 + case_index * 0.001 + repeat_index * 0.000001
            cast(list[object], case["repeats"]).append(
                {
                    "repeat_index": repeat_index,
                    "order": [75],
                    "base": sweep_base_record(definition, repeat_index),
                    "candidates": [
                        sweep_candidate_record(
                            definition,
                            75,
                            repeat_index,
                            0,
                            final_bytes=1800 + repeat_index,
                            quality_score=quality_score,
                        )
                    ],
                }
            )
        cast(list[object], evidence["cases"]).append(case)
    return plan, binding, plan_sha256, binding_sha256, definitions, predecessor, environment, evidence


def _candidate(evidence: dict[str, Any], case_index: int, repeat_index: int) -> dict[str, Any]:
    return cast(dict[str, Any], evidence["cases"][case_index]["repeats"][repeat_index]["candidates"][0])


def _materialize_retained_artifacts(
    root: Path,
    plan,
    binding,
    definitions,
    evidence: dict[str, Any],
) -> Path:
    artifact_directory = root / "artifacts"
    artifact_directory.mkdir()
    entries: list[dict[str, object]] = []
    for case_id in EXPECTED_RETAINED_CASE_IDS:
        case = next(case for case in evidence["cases"] if case["id"] == case_id)
        repeat = case["repeats"][0]
        base = repeat["base"]
        candidate = repeat["candidates"][0]

        base_seed = f"{case_id}-base".encode()
        base_data = (base_seed * (1000 // len(base_seed) + 1))[:1000]
        base_sha256 = hashlib.sha256(base_data).hexdigest()
        base["sha256"] = base_sha256
        candidate["base_sha256"] = base_sha256
        candidate["input_copy_sha256"] = base_sha256
        base_source = root / f"{case_id}-base.mov"
        base_source.write_bytes(base_data)
        entries.append(
            _retained_artifact_entry(
                artifact_directory=artifact_directory,
                source_path=base_source,
                case_id=case_id,
                repeat_index=0,
                kind="generated_base",
                candidate_id=None,
                move=False,
            )
        )

        candidate_seed = f"{case_id}-q075".encode()
        candidate_data = (candidate_seed * (1800 // len(candidate_seed) + 1))[:1800]
        candidate["final_sha256"] = hashlib.sha256(candidate_data).hexdigest()
        candidate_source = root / f"{case_id}-q075.mov"
        candidate_source.write_bytes(candidate_data)
        entries.append(
            _retained_artifact_entry(
                artifact_directory=artifact_directory,
                source_path=candidate_source,
                case_id=case_id,
                repeat_index=0,
                kind="candidate_output",
                candidate_id="q075",
                move=False,
            )
        )
    evidence["retained_artifacts"] = sorted(entries, key=lambda entry: str(entry["artifact_id"]))
    _refresh_calibration(evidence, plan, binding, definitions)
    return artifact_directory


class FileUpscaleRepeatabilityPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_CALIBRATION_PLAN.read_text(encoding="utf-8"))

    def test_plan_binds_exact_corpus_predecessor_limits_tools_and_public_contracts(self) -> None:
        plan, binding, _, binding_sha256 = load_repeatability_calibration_plan(DEFAULT_CALIBRATION_PLAN)

        self.assertEqual(binding_sha256, EXPECTED_CORPUS_SHA256)
        self.assertEqual(binding.selected_case_ids, EXPECTED_CASE_IDS)
        self.assertEqual(plan.predecessor_receipt.schema_version, 3)
        self.assertEqual(plan.predecessor_receipt.sha256, EXPECTED_PREDECESSOR_RECEIPT_SHA256)
        self.assertEqual(plan.predecessor_receipt.source_git_sha, EXPECTED_PREDECESSOR_SOURCE_GIT_SHA)
        self.assertEqual(plan.predecessor_receipt.required_file_mode, 0o444)
        self.assertEqual(sha256_file(plan.predecessor_plan.path), EXPECTED_PREDECESSOR_PLAN_SHA256)
        self.assertEqual(
            {limit.record_field: (limit.quantum, limit.previous_limit) for limit in plan.previous_limits},
            EXPECTED_PREVIOUS_LIMITS,
        )
        self.assertEqual(
            plan.ladder_manifest.sha256, "04620e59e5380c88d3d5152f78712402675f31db6f1253c1d93224af585111dc"
        )
        self.assertEqual(
            plan.video_quality_swift.sha256,
            "6f204564261d859590086ca41e9a27ac9f69bc0feb225137cf0abc4a98082dfa",
        )

    def test_schedule_is_exact_seven_by_five_by_one_q075_shape(self) -> None:
        plan, binding, _, _ = load_repeatability_calibration_plan(DEFAULT_CALIBRATION_PLAN)

        self.assertEqual(plan.case_schedules, materialized_case_orders())
        self.assertEqual(tuple(schedule.case_id for schedule in plan.case_schedules), binding.selected_case_ids)
        self.assertEqual(len(plan.case_schedules), 7)
        self.assertTrue(all(len(schedule.orders) == 5 for schedule in plan.case_schedules))
        self.assertTrue(all(order == (75,) for schedule in plan.case_schedules for order in schedule.orders))
        self.assertEqual([(candidate.candidate_id, candidate.quality) for candidate in plan.candidates], [("q075", 75)])

    def test_plan_rejects_binding_schedule_limit_or_scope_drift(self) -> None:
        changed = copy.deepcopy(self.document)
        changed["corpus_binding"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(QualificationFailure, "corpus-v2"):
            parse_repeatability_calibration_plan(changed)

        changed = copy.deepcopy(self.document)
        changed["execution_order"]["case_orders"][0]["orders"][4] = [85]
        with self.assertRaisesRegex(QualificationFailure, "quality|q075"):
            parse_repeatability_calibration_plan(changed)

        changed = copy.deepcopy(self.document)
        changed["predecessor"]["previous_repeatability"]["metrics"]["min_same_eye_ssim"]["previous_limit"] = 0.0003
        with self.assertRaisesRegex(QualificationFailure, "previous quantum or limit"):
            parse_repeatability_calibration_plan(changed)

        changed = copy.deepcopy(self.document)
        changed["scope"]["selection_forbidden"] = False
        with self.assertRaisesRegex(QualificationFailure, "scope"):
            parse_repeatability_calibration_plan(changed)

    def test_predecessor_receipt_must_be_supplied_by_required_cli_path(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parser().parse_args([])


class FileUpscaleRepeatabilityDerivationTests(unittest.TestCase):
    def test_formula_keeps_previous_floor_and_allows_derived_value_to_win(self) -> None:
        self.assertEqual(derive_calibrated_limit(0.0016, 0.0001, 0.0001), 0.0016)
        self.assertEqual(derive_calibrated_limit(0.0002, 0.00011, 0.0001), 0.0003)

    def test_limits_derive_only_from_raw_q075_records(self) -> None:
        plan, _, _, _, _, _, _, evidence = _complete_raw_evidence()
        target = _candidate(evidence, 3, 4)
        target["final_to_base_size_ratio"] += 0.015

        baseline = derive_repeatability_limits(evidence, plan)
        evidence["repeatability_calibration"] = {
            "metrics": {field: {"observed_maximum": 999.0, "derived_limit": 999.0} for field in REPEATABILITY_FIELDS}
        }
        evidence["case_summaries"] = [{"repeat_ranges": {field: 999.0 for field in REPEATABILITY_FIELDS}}]
        recomputed = derive_repeatability_limits(evidence, plan)

        self.assertEqual(recomputed, baseline)
        size = cast(dict[str, Any], baseline["metrics"])["final_to_base_size_ratio"]
        self.assertEqual(size["source"], {"case_id": "production-motion", "candidate_id": "q075"})
        self.assertAlmostEqual(size["observed_maximum"], 0.019, places=12)
        self.assertEqual(size["previous_limit"], 0.02)
        self.assertEqual(size["multiplier"], 2)
        self.assertEqual(size["quantum"], 0.01)
        self.assertEqual(size["derived_limit"], 0.04)

    def test_receipt_has_no_selection_boundary_or_provisional_output_fields(self) -> None:
        plan, binding, plan_sha256, binding_sha256 = load_repeatability_calibration_plan(DEFAULT_CALIBRATION_PLAN)
        evidence = _new_evidence(
            plan,
            binding,
            plan_sha256,
            binding_sha256,
            {"verified": True},
            {"git_head": "f" * 40},
        )

        forbidden = {
            "boundary_evaluations",
            "valid_subsets",
            "selected_subset",
            "provisional_mappings",
            "candidate_summaries",
        }
        self.assertTrue(forbidden.isdisjoint(evidence))
        self.assertNotIn("boundary_policy", self.document_keys(plan))

    @staticmethod
    def document_keys(plan) -> set[str]:
        return set(json.loads(DEFAULT_CALIBRATION_PLAN.read_text(encoding="utf-8")))


class FileUpscaleRepeatabilityResumeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        (
            self.plan,
            self.binding,
            self.plan_sha256,
            self.binding_sha256,
            self.definitions,
            self.predecessor,
            self.environment,
            self.evidence,
        ) = _complete_raw_evidence()

    def _write_complete(self, root: Path, *, finalized: bool = True):
        evidence = copy.deepcopy(self.evidence)
        artifact_directory = _materialize_retained_artifacts(
            root,
            self.plan,
            self.binding,
            self.definitions,
            evidence,
        )
        evidence["acceptance"]["finalized"] = finalized
        output = root / "receipt.json"
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.chmod(0o644)
        return output, artifact_directory, evidence

    def _load(self, output: Path, artifact_directory: Path):
        return _load_resume_evidence(
            output,
            plan=self.plan,
            binding=self.binding,
            plan_sha256=self.plan_sha256,
            binding_sha256=self.binding_sha256,
            predecessor=self.predecessor,
            environment=self.environment,
            definitions=self.definitions,
            private_paths=(),
            artifact_directory=artifact_directory,
        )

    def test_exact_eight_hash_bound_relative_movs_and_writable_freeze_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, artifact_directory, evidence = self._write_complete(Path(temporary))

            loaded = self._load(output, artifact_directory)

            self.assertEqual(loaded, evidence)
            self.assertEqual(output.stat().st_mode & 0o777, 0o444)
            self.assertEqual(len(evidence["retained_artifacts"]), 8)
            self.assertTrue(all(not Path(entry["path"]).is_absolute() for entry in evidence["retained_artifacts"]))
            self.assertEqual(
                {entry["path"] for entry in evidence["retained_artifacts"]},
                {
                    path
                    for case_id in EXPECTED_RETAINED_CASE_IDS
                    for path in (
                        f"{case_id}/repeat-1/generated-base.mov",
                        f"{case_id}/repeat-1/q075-upscaled.mov",
                    )
                },
            )

    def test_completed_unfinalized_checkpoint_is_resumable_for_final_freeze(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output, artifact_directory, evidence = self._write_complete(Path(temporary), finalized=False)

            loaded = self._load(output, artifact_directory)

            self.assertEqual(loaded, evidence)
            self.assertEqual(output.stat().st_mode & 0o777, 0o644)

    def test_resume_rejects_identity_drift_noncanonical_json_and_private_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, artifact_directory, evidence = self._write_complete(root)
            changed = copy.deepcopy(evidence)
            changed["method"]["runs_per_candidate"] = 4
            output.write_text(json.dumps(changed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(QualificationFailure, "method changed"):
                self._load(output, artifact_directory)

            output.write_text(json.dumps(evidence), encoding="utf-8")
            with self.assertRaisesRegex(QualificationFailure, "canonical JSON"):
                self._load(output, artifact_directory)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = cast(
                dict[str, Any],
                _new_evidence(
                    self.plan,
                    self.binding,
                    self.plan_sha256,
                    self.binding_sha256,
                    self.predecessor,
                    self.environment,
                ),
            )
            evidence["repeatability_calibration"] = {"debug": "/Users/private/source.mov"}
            output = root / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output.chmod(0o644)
            artifact_directory = root / "artifacts"
            artifact_directory.mkdir()
            with self.assertRaisesRegex(QualificationFailure, "private source information"):
                self._load(output, artifact_directory)

    def test_resume_rejects_missing_or_orphan_media(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, artifact_directory, evidence = self._write_complete(root)
            missing = artifact_directory / evidence["retained_artifacts"][0]["path"]
            missing.unlink()
            with self.assertRaisesRegex(QualificationFailure, "missing"):
                self._load(output, artifact_directory)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output, artifact_directory, _ = self._write_complete(root)
            (artifact_directory / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "unrecorded or missing media"):
                self._load(output, artifact_directory)

    def test_incomplete_resume_discards_only_expected_unrecorded_crash_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = cast(
                dict[str, Any],
                _new_evidence(
                    self.plan,
                    self.binding,
                    self.plan_sha256,
                    self.binding_sha256,
                    self.predecessor,
                    self.environment,
                ),
            )
            output = root / "receipt.json"
            output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            output.chmod(0o644)
            artifact_directory = root / "artifacts"
            expected = artifact_directory / "production-dark/repeat-1/generated-base.mov"
            expected.parent.mkdir(parents=True)
            expected.write_bytes(b"crash-window")

            loaded = self._load(output, artifact_directory)

            self.assertEqual(loaded, evidence)
            self.assertFalse(expected.exists())
            (artifact_directory / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "unrecorded or missing media"):
                self._load(output, artifact_directory)

    def test_incomplete_resume_rejects_expected_symlink_without_deleting_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = cast(
                dict[str, Any],
                _new_evidence(
                    self.plan,
                    self.binding,
                    self.plan_sha256,
                    self.binding_sha256,
                    self.predecessor,
                    self.environment,
                ),
            )
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
                self._load(output, artifact_directory)

            self.assertEqual(target.read_bytes(), b"must-survive")
            self.assertTrue(expected.is_symlink())

    def test_completed_work_cleanup_removes_cases_and_rejects_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / WORK_DIRECTORY_MARKER
            marker.write_text("{}", encoding="utf-8")
            case_directory = root / "production-dark"
            case_directory.mkdir()
            (case_directory / "checkpoint.mov").write_bytes(b"checkpoint")

            _cleanup_completed_work_directory(root, ("production-dark",))

            self.assertFalse(case_directory.exists())
            (root / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "orphaned"):
                _validate_clean_work_directory(root)


class FileUpscaleRepeatabilityExitTests(unittest.TestCase):
    def test_exit_zero_complete_three_incomplete_and_two_fatal(self) -> None:
        complete = {
            "acceptance": {
                "complete": True,
                "finalized": True,
                "calibration_receipt_valid": True,
                "derived_limits_complete": True,
            }
        }
        incomplete = {"acceptance": {"complete": False, "finalized": False}}
        self.assertEqual(exit_code_for_evidence(complete), 0)
        self.assertEqual(exit_code_for_evidence(incomplete), 3)
        with self.assertRaises(QualificationFailure):
            exit_code_for_evidence(
                {
                    "acceptance": {
                        "complete": True,
                        "finalized": False,
                        "calibration_receipt_valid": True,
                        "derived_limits_complete": True,
                    }
                }
            )

        with (
            patch(
                "scripts.qualify_file_upscale_quality_repeatability_calibration.run_repeatability_calibration",
                side_effect=QualificationFailure("fatal"),
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--mapping-selection-receipt", "predecessor.json"]), 2)

        with (
            patch(
                "scripts.qualify_file_upscale_quality_repeatability_calibration.run_repeatability_calibration",
                side_effect=KeyboardInterrupt,
            ),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--mapping-selection-receipt", "predecessor.json"]), 3)

    def test_evidence_schema_advances_after_mapping_selection(self) -> None:
        self.assertEqual(EVIDENCE_SCHEMA_VERSION, 4)


if __name__ == "__main__":
    unittest.main()
