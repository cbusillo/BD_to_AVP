import copy
import hashlib
import json
import unittest

from pathlib import Path
from unittest.mock import patch

from scripts.qualify_direct_mv_hevc import QualificationFailure
from scripts.qualify_direct_mv_hevc_metalfx_mapping_confirmation import (
    DEFAULT_CONFIRMATION_PLAN,
    EXPECTED_CANDIDATES,
    EXPECTED_CASE_IDS,
    _verify_balanced_quality_receipt,
    _verify_balanced_routes_receipt,
    _verify_fresh_production_sources,
    evaluate_confirmation,
    main,
    parse_confirmation_plan,
    verify_worker_fallback_contract,
)
from scripts.qualify_direct_mv_hevc_quality_sweep import SweepCandidate, SweepPlan, _refresh_summaries
from scripts.qualify_mv_hevc_corpus import CorpusCase


def _plan():
    return parse_confirmation_plan(json.loads(DEFAULT_CONFIRMATION_PLAN.read_text(encoding="utf-8")))


def _run(run_index: int, quality: float, size: int) -> dict[str, object]:
    return {
        "run_index": run_index,
        "min_same_eye_ssim": quality,
        "final_bytes": size,
        "elapsed_seconds": 1.0,
        "min_eye_order_margin": 0.2,
        "sha256": hashlib.sha256(f"{run_index}-{quality}-{size}".encode()).hexdigest(),
    }


def _complete_evidence():
    plan = _plan()
    sweep_plan = SweepPlan(
        sweep_id=plan.raw_sweep_id,
        corpus_path=plan.corpus_path,
        corpus_id=plan.corpus_id,
        corpus_sha256=plan.corpus_sha256,
        balanced_quality=0.6,
        runs_per_candidate=3,
        candidates=tuple(SweepCandidate(candidate.candidate_id, candidate.quality) for candidate in plan.candidates),
        relative_path="docs/qualification/direct-mv-hevc-metalfx-quality-sweep-confirmation-v1.json",
        target_id="direct_mv_hevc_metalfx_2x",
        upscale_mode="metalfx",
        comparison_scale=(1920, 1080),
    )
    definitions: dict[str, CorpusCase] = {}
    cases: list[dict[str, object]] = []
    production_sources: dict[str, object] = {}
    for case_index, case_id in enumerate(EXPECTED_CASE_IDS):
        tags = ("synthetic_control",) if case_id.startswith("synthetic-") else ("real_mvc",)
        if case_id in {"production-grain-rain", "production-snow-detail"}:
            tags = (*tags, "grain")
        source = {"kind": "synthetic", "filter_sha256": str(case_index) * 64}
        if not case_id.startswith("synthetic-"):
            source = {"kind": "mvc_container", "segment_sha256": str(case_index + 1) * 64}
            production_sources[case_id] = dict(source)
        definitions[case_id] = CorpusCase(
            case_id=case_id,
            tags=tags,
            source={"kind": "synthetic"},
            eye_width=1920,
            eye_height=1080,
            frame_rate="24",
            minimum_eye_order_margin=0.01,
        )
        candidate_records = []
        for candidate_index, candidate in enumerate(plan.candidates):
            runs = [
                _run(
                    run_index,
                    0.92 + case_index * 0.00001 + candidate_index * 0.0002,
                    1000 + candidate_index * 100 + run_index,
                )
                for run_index in range(3)
            ]
            candidate_records.append(
                {"id": candidate.candidate_id, "quality": candidate.quality, "runs": runs, "summary": None}
            )
        cases.append(
            {
                "id": case_id,
                "tags": list(tags),
                "quality_gate": True,
                "source": source,
                "prepared": {
                    "duration_seconds": 4.0,
                    "frame_count": 96,
                    "eye_width": 1920,
                    "eye_height": 1080,
                    "frame_rate": "24",
                },
                "candidates": candidate_records,
            }
        )
    evidence: dict[str, object] = {
        "schema_version": 1,
        "sweep_id": plan.raw_sweep_id,
        "updated_at": "2026-08-01T00:00:00+00:00",
        "source_git_sha": "a" * 40,
        "source_tree_dirty": False,
        "sweep_plan": {
            "path": "docs/qualification/direct-mv-hevc-metalfx-quality-sweep-confirmation-v1.json",
            "sha256": plan.raw_sweep_sha256,
        },
        "manifest": {"path": plan.corpus_path.name, "corpus_id": plan.corpus_id, "sha256": plan.corpus_sha256},
        "selected_case_ids": list(EXPECTED_CASE_IDS),
        "candidates": [{"id": candidate.candidate_id, "quality": candidate.quality} for candidate in plan.candidates],
        "cases": cases,
        "candidate_summaries": [],
        "monotonicity_warnings": [],
    }
    _refresh_summaries(evidence, sweep_plan, definitions, all_gated_case_ids=set(definitions))
    sources = {
        "ordinary_direct_confirmation": {"production_sources": production_sources},
        "balanced_real_mvc_quality": {"quality_passed": True, "eye_order_passed": True},
        "balanced_packaged_routes": {"full_preview_parity": True, "pre_input_fallback": True},
        "worker_fallback_contract": {"aliases_forbidden": True, "file_upscale_quality": 75},
    }
    return plan, evidence, sources


class MetalFXConfirmationPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(DEFAULT_CONFIRMATION_PLAN.read_text(encoding="utf-8"))

    def test_committed_plan_has_fixed_candidates_and_balanced_only_fallback(self) -> None:
        plan = parse_confirmation_plan(self.document)

        self.assertEqual(
            tuple((candidate.step_id, candidate.candidate_id, candidate.quality) for candidate in plan.candidates),
            EXPECTED_CANDIDATES,
        )
        self.assertEqual(plan.fallback_policy["supported_ladder_step_ids"], ["balanced"])
        self.assertEqual(plan.fallback_policy["unsupported_step_action"], "unavailable_not_balanced_alias")

    def test_plan_rejects_non_balanced_fallback_alias(self) -> None:
        document = copy.deepcopy(self.document)
        document["fallback_policy"]["supported_ladder_step_ids"].append("detailed")

        with self.assertRaisesRegex(QualificationFailure, "fallback policy changed"):
            parse_confirmation_plan(document)

    def test_worker_contract_accepts_balanced_and_rejects_every_other_step(self) -> None:
        result = verify_worker_fallback_contract(_plan())

        self.assertEqual(result["supported_ladder_step_ids"], ["balanced"])
        self.assertEqual(result["file_upscale_quality"], 75)
        self.assertEqual(
            result["rejected_step_ids"],
            ["space_saver", "compact", "efficient", "detailed", "high_detail", "maximum_detail"],
        )
        self.assertTrue(result["aliases_forbidden"])


class MetalFXConfirmationEvidenceTests(unittest.TestCase):
    def test_complete_mapping_and_balanced_fallback_are_decision_ready(self) -> None:
        plan, evidence, sources = _complete_evidence()

        confirmation = evaluate_confirmation(plan, evidence, sources)

        self.assertTrue(confirmation["acceptance"]["mapping_objective_decision_ready"])
        self.assertTrue(confirmation["acceptance"]["fallback_policy_decision_ready"])
        self.assertTrue(confirmation["acceptance"]["objective_decision_ready"])
        self.assertEqual(
            confirmation["selected_subset"]["candidate_ids"],
            [candidate[1] for candidate in EXPECTED_CANDIDATES],
        )
        self.assertEqual(
            confirmation["fallback_policy_confirmation"]["unsupported_step_action"],
            "unavailable_not_balanced_alias",
        )

    def test_failed_fallback_evidence_blocks_combined_decision(self) -> None:
        plan, evidence, sources = _complete_evidence()
        sources["balanced_real_mvc_quality"]["quality_passed"] = False

        confirmation = evaluate_confirmation(plan, evidence, sources)

        self.assertTrue(confirmation["acceptance"]["mapping_objective_decision_ready"])
        self.assertFalse(confirmation["acceptance"]["fallback_policy_decision_ready"])
        self.assertFalse(confirmation["acceptance"]["objective_decision_ready"])

    def test_fresh_production_sources_must_match_ordinary_direct_receipt(self) -> None:
        _, evidence, sources = _complete_evidence()
        evidence["cases"][0]["source"]["segment_sha256"] = "f" * 64

        with self.assertRaisesRegex(QualificationFailure, "production-dark"):
            _verify_fresh_production_sources(evidence, sources)

    def test_balanced_quality_receipt_recomputes_matched_source_loss(self) -> None:
        plan = _plan()
        document = {
            "schema_version": 1,
            "source": {"sha256": plan.fallback_policy["matched_source_sha256"]},
            "package": {"app_tree_sha256": "7294f205d9d95d53f72d7fa30d977b2551c6d6ab8c0bdeddc444c354060fc801"},
            "acceptance": {"passed": True, "repeated_full_routes": True},
            "thresholds": {"runs_per_route": 3, "quality_tolerance": 0.002, "minimum_eye_order_margin": 0.001},
            "direct_runs": [
                {"min_same_eye_ssim": 0.9525, "min_eye_order_margin": 0.03, "final_bytes": 100, "elapsed_seconds": 50}
                for _ in range(3)
            ],
            "file_based_runs": [
                {"min_same_eye_ssim": 0.9509, "min_eye_order_margin": 0.03, "final_bytes": 270, "elapsed_seconds": 85}
                for _ in range(3)
            ],
        }
        with patch(
            "scripts.qualify_direct_mv_hevc_metalfx_mapping_confirmation._read_frozen_json",
            return_value=document,
        ):
            result = _verify_balanced_quality_receipt(plan, Path("quality.json"))

        self.assertTrue(result["quality_passed"])
        self.assertAlmostEqual(result["generated_quality_loss_vs_direct"], 0.0016)
        self.assertAlmostEqual(result["generated_to_direct_size_ratio"], 2.7)

    def test_balanced_routes_receipt_requires_exact_pre_input_generated_values(self) -> None:
        plan = _plan()
        direct_route = {
            "intent": "automatic",
            "quality": 0.6,
            "rate_control": "quality",
            "reason": "direct_upscale_eligible",
            "selected": "direct_mv_hevc",
            "upscale_mode": "metalfx",
        }
        fallback_route = {
            "eye_bitrate_mbps": 20,
            "fallback_reason": "metalfx_2x_mv_hevc_unavailable",
            "fallback_timing": "pre_input",
            "intent": "automatic",
            "merge_quality": 75,
            "reason": "direct_capability_unavailable",
            "selected": "generated_mv_hevc",
        }
        document = {
            "schema_version": 3,
            "source": {
                "sha256": plan.fallback_policy["matched_source_sha256"],
                "media": {"duration_seconds": 65.649},
            },
            "package": {"app_tree_sha256": "7294f205d9d95d53f72d7fa30d977b2551c6d6ab8c0bdeddc444c354060fc801"},
            "acceptance": {
                "finalized_artifacts_valid": True,
                "metalfx_4k_direct_full_preview_parity": True,
                "metalfx_4k_fallback_full_preview_parity": True,
                "metalfx_4k_fallback_pre_input": True,
                "metalfx_4k_stage_contracts": True,
                "metalfx_4k_video_dimensions": True,
                "passed": True,
            },
            "metalfx_4k": {
                "direct": {"full": {"route": direct_route}, "preview": {"route": direct_route}},
                "fallback": {"full": {"route": fallback_route}, "preview": {"route": fallback_route}},
            },
        }
        with patch(
            "scripts.qualify_direct_mv_hevc_metalfx_mapping_confirmation._read_frozen_json",
            return_value=document,
        ):
            result = _verify_balanced_routes_receipt(plan, Path("routes.json"))

        self.assertTrue(result["pre_input_fallback"])
        self.assertEqual(result["generated_values"], {"eye_bitrate_mbps": 20, "merge_quality": 75})

    def test_keyboard_interrupt_exits_three(self) -> None:
        arguments = [
            "--ordinary-direct-receipt",
            "/tmp/direct.json",
            "--balanced-quality-receipt",
            "/tmp/quality.json",
            "--balanced-routes-receipt",
            "/tmp/routes.json",
            "--output",
            "/tmp/output.json",
            "--work-directory",
            "/tmp/work",
        ]
        with patch(
            "scripts.qualify_direct_mv_hevc_metalfx_mapping_confirmation.run_confirmation",
            side_effect=KeyboardInterrupt,
        ):
            self.assertEqual(main(arguments), 3)


if __name__ == "__main__":
    unittest.main()
