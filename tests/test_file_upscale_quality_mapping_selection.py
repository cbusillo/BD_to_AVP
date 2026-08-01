import copy
import hashlib
import json
import tempfile
import unittest

from pathlib import Path
from typing import Any, cast

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_file_upscale_quality_mapping_selection import (
    DEFAULT_SELECTION_PLAN,
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
    _read_frozen_source_receipt,
    _refresh_summaries,
    _retained_artifact_entry,
    _validate_clean_work_directory,
    assign_provisional_mappings,
    exit_code_for_evidence,
    load_mapping_selection_plan,
    materialized_case_orders,
    parse_mapping_corpus_binding,
    parse_mapping_selection_plan,
    recompute_source_noise_maxima,
    select_provisional_subset,
)
from scripts.qualify_mv_hevc_corpus import load_manifest
from scripts.qualify_mv_hevc_quality_match import sha256_file
from tests.test_file_upscale_quality_sweep import (
    _base_record as sweep_base_record,
    _candidate_record as sweep_candidate_record,
    _case_record as sweep_case_record,
)


PUBLIC_LADDER_SHA256 = "04620e59e5380c88d3d5152f78712402675f31db6f1253c1d93224af585111dc"
VIDEO_QUALITY_SWIFT_SHA256 = "6f204564261d859590086ca41e9a27ac9f69bc0feb225137cf0abc4a98082dfa"


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


def _definitions(plan: MappingSelectionPlan):
    _, binding, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
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
    collapse_q100: bool = False,
    oversize_q045: bool = False,
    storage_tie_q045_q055: bool = False,
) -> tuple[MappingSelectionPlan, Any, Any, dict[str, Any]]:
    plan, binding, plan_sha256, binding_sha256 = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
    _, definitions = _definitions(plan)
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

    def test_public_manifest_and_swift_bindings_remain_unchanged(self) -> None:
        plan, _, _, _ = load_mapping_selection_plan(DEFAULT_SELECTION_PLAN)
        self.assertEqual(sha256_file(plan.ladder_manifest.path), PUBLIC_LADDER_SHA256)
        self.assertEqual(sha256_file(plan.video_quality_swift.path), VIDEO_QUALITY_SWIFT_SHA256)


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

    def test_completed_writable_receipt_is_recovered_to_frozen_mode(self) -> None:
        plan, binding, definitions, evidence = _complete_evidence()
        with tempfile.TemporaryDirectory() as temporary:
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
                plan_sha256=self.plan_sha256,
                binding_sha256=self.binding_sha256,
                source_response=self.source_response,
                environment=self.environment,
                definitions=definitions,
                private_paths=(),
                artifact_directory=artifact_directory,
            )

            self.assertEqual(output.stat().st_mode & 0o777, 0o444)
            (artifact_directory / "orphan.mov").write_bytes(b"orphan")
            with self.assertRaisesRegex(QualificationFailure, "unrecorded or missing media"):
                _load_resume_evidence(
                    output,
                    plan=plan,
                    binding=binding,
                    plan_sha256=self.plan_sha256,
                    binding_sha256=self.binding_sha256,
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
